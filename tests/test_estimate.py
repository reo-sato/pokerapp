"""tests/test_estimate.py — 推定器 v0（tools/estimate.py）とシミュレーション（tools/simulate.py）。"""
from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("pokerkit")

from tools.estimate import (  # noqa: E402
    PARAMS,
    estimate_dict,
    estimate_session,
    inputs_from_fixtures,
    params_hash,
    structure_penalties,
    utterance_options,
)
from tools.simulate import Noise, ear_form, observe, run, simulate_session  # noqa: E402

@pytest.fixture(autouse=True)
def _quiet():
    """推定の候補の再生で出るエンジンのログを止める（このファイルのテストのあいだだけ。モジュールの頭で止めると
    ほかのファイルのログの検査まで止まる = 監査 1 D14）。"""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _ear(text: str, cands: list[tuple[str, float]], logp: float = -0.5) -> dict:
    return {"text": text, "logp": logp, "candidates": [{"text": t, "logp": v} for t, v in cands]}


class TestOptions:
    def test_a_clean_whisper_reading_is_the_default_and_hard_to_drop(self):
        opts = utterance_options({"utterance_start_ts": 1.0, "text": "コール", "no_speech": False})
        assert [o.source for o in opts] == ["whisper", "drop"]
        assert opts[0].keys == (("call", 0, None, None),) and opts[0].logp == 0.0
        assert opts[1].logp == PARAMS["drop_short"]          # アクションの言葉だけの短い発話は捨てにくい

    def test_a_stock_hallucination_with_an_ear_candidate(self):
        """店舗 7b897671 ハンド 3: オールインへのコールが「ご覧いただきありがとうございます。」になった。"""
        row = {"utterance_start_ts": 1.0, "text": "ご覧いただきありがとうございます。", "no_speech": False,
               "ear": _ear("る空", [("コール", -5.3), ("千", -12.0)], logp=-0.5)}
        opts = utterance_options(row)
        assert opts[0].source == "drop" and opts[0].logp == PARAMS["drop_heard"]   # 音はあった
        ear = [o for o in opts if o.source == "ear"]
        assert [o.keys for o in ear] == [(("call", 0, None, None),)]              # -12 の候補は選択肢にしない
        assert ear[0].logp > opts[0].logp                                          # 構造が同じなら候補を採る

    def test_a_canned_phrase_has_two_states_when_enabled(self):
        """`canned_action` > 0（推定器 v1）: 決まり文句になった発話は「雑談だった log(1 − r) / アクションを言った
        log r + log q」。q はフォールド・チェック・コールに一様 + 第 2 の耳の候補の重み exp(差 / 温度)（自由に聞いた文も
        重み 1 で分母に入れる）。v0（0）はこれまでどおり。"""
        import math

        params = dict(PARAMS, canned_action=0.2, canned_ear_temp=2.0)
        row = {"utterance_start_ts": 1.0, "text": "ご覧いただきありがとうございます。", "no_speech": False,
               "ear": _ear("る空", [("コール", -5.3), ("千", -12.0)], logp=-0.5)}
        opts = utterance_options(row, params)
        assert opts[0].source == "drop" and opts[0].logp == pytest.approx(math.log(0.8))
        q = {o.text: math.exp(o.logp) / 0.2 for o in opts[1:]}
        weight = math.exp((-5.3 + 0.5) / 2.0)          # -12 の候補は差 -11.5 < −8 で入れない
        assert q["コール"] == pytest.approx(weight / (1 + weight) + 1 / (1 + weight) / 3)
        assert q["フォールド"] == pytest.approx(q["チェック"]) == pytest.approx(1 / (1 + weight) / 3)
        assert set(q) == {"コール", "フォールド", "チェック"} and sum(q.values()) == pytest.approx(1.0)
        # 第 2 の耳がはっきり額を聞いた: 賭けの読みが入る（上から ear_top 個まで）
        amount = {"utterance_start_ts": 2.0, "text": "ご覧いただきありがとうございます。", "no_speech": False,
                  "ear": _ear("強くて", [("九百点", -1.0), ("百点", -3.4), ("二百点", -4.3), ("千百", -5.4),
                                       ("千", -6.0)], logp=-0.7)}
        texts = [o.text for o in utterance_options(amount, params)[1:]]
        assert texts[0] == "九百点" and len([t for t in texts if t not in ("コール", "フォールド", "チェック")]) == 3
        # ライブの規則で読めた決まり文句（第 2 の耳の救い出し）はこれまでどおり
        rescued = {"utterance_start_ts": 3.0, "text": "ご視聴ありがとうございました。", "no_speech": False,
                   "ear": _ear("六百", [("六百", -0.6), ("二百", -9.0)])}
        assert utterance_options(rescued, params)[0].source == "rescue"

    def test_the_live_rule_is_the_default_when_the_ears_agree(self):
        row = {"utterance_start_ts": 1.0, "text": "のっぴょく", "no_speech": False,
               "ear": _ear("六百", [("六百", -0.6), ("二百", -9.0)])}
        opts = utterance_options(row)
        assert opts[0].source == "rescue" and opts[0].keys == (("bet", 600, None, None),)
        assert any(o.source == "drop" for o in opts)

    def test_control_words_are_not_choices(self):
        """ハンドの区切り・勝者の宣言は選ばない（ハンドどうしを独立に保つ）。"""
        opts = utterance_options({"utterance_start_ts": 1.0, "text": "シート1 ウィナー", "no_speech": False})
        assert len(opts) == 1

    def test_chatter_candidates_far_from_the_audio_are_not_choices(self):
        row = {"utterance_start_ts": 1.0, "text": "まあでもそういうのもあるよね。", "no_speech": False,
               "ear": _ear("まあでもそういうのもあるよね", [("二千", -17.8), ("百", -18.0)], logp=-0.4)}
        assert len(utterance_options(row)) == 1


class TestPenalties:
    def test_structure_penalties(self):
        hand = {"actions": [
            {"street": "preflop", "seat": 1, "action": "call", "actor_source": "implied", "reason": "implied_before_flop"},
            {"street": "preflop", "seat": 2, "action": "raise", "amount": 500, "reason": "amount_snapped"},
            {"street": "preflop", "seat": 3, "action": "call", "reason": "check_facing_bet"},
            {"street": "flop", "seat": 3, "action": "fold"},
        ], "board": ["As", "Kd", "7h", "2c", "9s"], "winner_seat": 2, "winner_source": "fold"}
        names = [n for n, _ in structure_penalties(hand, [{"reason": "betting_over"}])]
        assert names == ["補ったアクション", "額を寄せた", "読み替えた", "反映できない発話（betting_over）",
                         "ボードより前のストリートで終わった"]

    def test_missing_board_cards_are_not_evidence(self):
        """札が無いのは読めていないだけのことがある（店舗 fded6f75 ハンド 5 で「フロップ、チェック、チェック」を捨てていた）。"""
        hand = {"actions": [{"street": "flop", "seat": 1, "action": "check"}], "board": [], "winner_source": "cards"}
        assert structure_penalties(hand, []) == []


class TestSimulation:
    def test_ear_form(self):
        assert ear_form("レイズ 600") == "レイズ 六百"
        assert ear_form("シート3 コール") == "コール"
        assert ear_form("BTN フォールド") == "フォールド"
        assert ear_form("フォールド、コール") == "フォールド、コール"
        assert ear_form("シート1 ウィナー") is None and ear_form("フォールド、フォールド、コール") is None
        assert ear_form("600点") == "六百" and ear_form("1万2千") == "一万二千"
        assert ear_form("スリープレイヤーズ") is None and ear_form("ツーペア") is None

    def test_observation_kinds(self):
        import random

        rng = random.Random(1)
        row = observe("レイズ 600", 10.0, rng, Noise.only("misread", 1.0))
        assert row["text"] == "レイズ 200" and "六百" in json.dumps(row["ear"], ensure_ascii=False)
        assert observe("コール", 10.0, rng, Noise.only("missing", 1.0)) is None
        assert "ご" in observe("コール", 10.0, rng, Noise.only("hallucinate", 1.0))["text"]

    def test_without_noise_the_replay_is_the_script(self):
        noise = Noise(misread=0, hallucinate=0, garble=0, missing=0, chatter=0)
        result = run(noise, sessions=1, hands=6, seats=6)
        assert result.default_exact == result.estimate_exact == result.hands == 6

    @pytest.mark.slow                    # シミュレーションの 1 セッション（約 10 秒）
    def test_the_estimator_rescues_hallucinated_actions(self):
        """幻聴になった発話を、第 2 の耳の候補とハンドの筋から戻す（シミュレーションの 1 セッション）。"""
        noise = Noise(misread=0, hallucinate=0.2, garble=0, missing=0, chatter=0, ear_agree=0.3)
        result = run(noise, sessions=1, hands=10, seats=6)
        assert result.estimate_rows[0] >= result.default_rows[0]
        # 10 発話中 6 つが幻聴のハンドは、読み直しも推定もほとんど合わない（どちらが多く合うかは偶然 = 1 つまで許す）
        assert result.estimate_exact > result.default_exact and result.better > 3 * result.worse and result.worse <= 1


@pytest.fixture(scope="module")
def results():
    out = {}
    for inp in inputs_from_fixtures():
        if inp.session_id[:8] not in ("7b897671", "a6ee12e4"):
            continue
        estimates, _ = estimate_session(inp)
        out[inp.session_id[:8]] = (inp, estimates)
    return out


@pytest.mark.slow                        # 店舗の 2 セッションを推定する fixture（約 7 秒）を共有する
class TestStoreFixtures:
    """店舗の真のアクションのあるハンドで、推定が読み直し（既定）より悪くならない（ADR-0056 追記 1 の S3 の条件）。"""

    def test_not_worse_than_the_reparse(self, results):
        from tools.estimate import compare_with_truth

        for inp, estimates in results.values():
            for r in compare_with_truth(estimates, inp.truth):
                assert r.estimate[0] >= r.default[0], (inp.session_id, r)

    def test_the_hallucinated_call_is_recovered(self, results):
        """7b897671 ハンド 3: リバーのオールインへのコールが幻聴になり、札の離脱でフォールドと記録していた。"""
        from tools.estimate import compare_with_truth

        inp, estimates = results["7b897671"]
        r = next(r for r in compare_with_truth(estimates, inp.truth) if r.hand_id == 3)
        assert r.default == (10, 11) and r.estimate == (11, 11) and r.in_nbest

    def test_estimate_file(self, results):
        inp, estimates = results["a6ee12e4"]
        data = json.loads(json.dumps(estimate_dict(inp.session_id, estimates), ensure_ascii=False))
        assert data["estimator_version"] and data["params_hash"] == params_hash() and data["tier"] == "session"
        hand = data["hands"][0]
        assert hand["best"]["actions"] and hand["nbest"][0]["score"] == hand["best"]["score"]
        assert all("options" in c for c in hand["choices"])


def test_simulated_session_shape():
    inp = simulate_session(2, hands=3)
    assert len(inp.truth["hands"]) == 3 and inp.events and inp.transcripts
    assert set(inp.setup["hand_stacks"]) == {"1", "2", "3"}
