"""tests/test_llm_candidates.py

LLM の提案を推定器の候補に足す実験（`tools/llm_hand_prompt.py` / `tools/llm_candidates.py` / `tools/bench_llm.py`,
推定器の `HandWindow.seeds`）。API は呼ばない（偽のクライアント）。`anthropic` が入っていなくても通る。
"""
from __future__ import annotations

import dataclasses
import json
import logging
from types import SimpleNamespace

import pytest

from audio.recognizer import parse_actions
from tools.llm_hand_prompt import canonical_json, input_hash, prompt_hash, spoken_text


class TestSpokenText:
    def test_reads_back_with_the_current_reader(self):
        text = spoken_text([{"action": "raise", "amount": 8000, "seat": 5}, {"action": "fold", "seat": 6},
                            {"action": "call", "amount": 8000, "seat": None}])
        got = [(e.action, e.amount, e.seat) for e in parse_actions(text, confidence=1.0, utterance_start_ts=0.0)]
        assert got == [("raise", 8000, 5), ("fold", 0, 6), ("call", 0, None)]

    def test_without_seat(self):
        assert spoken_text([{"action": "bet", "amount": 1500, "seat": 3}], with_seat=False) == "ベット 1500"

    def test_unknown_action_is_skipped(self):
        assert spoken_text([{"action": "showdown"}]) == ""


def test_hashes_are_stable():
    payload = {"b": 1, "a": [1, 2]}
    assert canonical_json(payload) == canonical_json({"a": [1, 2], "b": 1})
    assert input_hash(payload) == input_hash(dict(payload))
    assert len(prompt_hash()) == 12


# ───────────────────── 店舗の 1 ハンド（09-27 027e4b15: 4 人・推定器が点で負けるハンド） ─────────────────────


@pytest.fixture(scope="module")
def hand():
    logging.disable(logging.CRITICAL)
    from tools.llm_candidates import make_estimator, scored_hands, select_sessions

    pairs = select_sessions(None, None, ["027e4b15"])
    assert pairs, "fixture 2026-09-27-027e4b15 が無い"
    inp, presence = pairs[0]
    est = make_estimator(inp, presence)
    hands = scored_hands(inp, est)
    assert len(hands) == 1
    yield est, hands[0]
    logging.disable(logging.NOTSET)


def _u(i, kind, *actions):
    return {"index": i, "kind": kind, "actions": [{"action": a, "amount": m, "seat": s} for a, m, s in actions]}


def _perfect(truth: dict) -> dict:
    """真のアクションどおりの解釈（手で作った = LLM が全部当てた場合）。"""
    return {"interpretations": [{
        "utterances": [_u(0, "action", ("raise", 600, 6)), _u(1, "action", ("call", None, 4), ("call", None, 5)),
                       _u(2, "noise"), _u(3, "action", ("check", None, 4)), _u(4, "action", ("bet", 1000, 5)),
                       _u(5, "action", ("call", None, 6)), _u(7, "action", ("check", None, None)),
                       _u(8, "action", ("bet", 1400, None)), _u(9, "action", ("call", None, None)),
                       _u(11, "action", ("check", None, None)), _u(12, "action", ("bet", 6500, None)),
                       _u(13, "action", ("call", None, None)), _u(14, "noise"), _u(15, "muck")],
        "silent_actions": [{"after_index": 5, "action": "call"}, {"after_index": 9, "action": "call"},
                           {"after_index": 13, "action": "call"}],
        "actions": [dict(a) for a in truth["actions"]], "button_seat": None,
        "winner_seat": truth.get("winner_seat"), "confidence": 0.9, "note": "",
    }]}


class TestHandInput:
    def test_deterministic_and_sends_no_names_or_truth(self, hand):
        from tools.llm_hand_prompt import hand_input

        est, h = hand
        a, b = hand_input(est, h.window), hand_input(est, h.window)
        assert canonical_json(a) == canonical_json(b)
        text = canonical_json(a)
        for name in {p.get("name") for p in est.setup.get("players") or []} - {None, ""}:
            assert f'"{name}"' not in text or name.isdigit()     # 名前は席番号と同じ値のときだけ（fixture は A/B/C）
        assert "notes" not in text and "truth" not in text
        assert a["table"]["preflop_order"] and a["table"]["postflop_order"]
        assert [u["i"] for u in a["utterances"]] == list(range(len(a["utterances"])))
        assert all(u["t"] >= -1.0 for u in a["utterances"])     # ハンドの始まりからの秒


class TestSeeds:
    def test_reads_only_where_the_answer_differs(self, hand):
        from tools.llm_candidates import interpretation_edits

        est, h = hand
        (edits,) = interpretation_edits(_perfect(h.truth), est, h.window)
        reads = [e for e in edits if e.kind == "read"]
        inserts = [e for e in edits if e.kind == "insert"]
        # 「600」→ レイズ・「フォールド、コール」→ コール 2 つ。今の読みと同じアクションの発話（席を足しただけ・
        # 第 2 の耳で既にベット 1000 と読む「シング」）、「ヘッズアップ」だけの発話、マックの「フォールド」は直さない
        assert [e.value for e in reads] == ["シート6 レイズ 600", "シート4 コール、シート5 コール"]
        assert [e.value for e in inserts] == ["call", "call", "call"]
        assert len({e.at for e in inserts}) == 3

    def test_no_answer_no_seeds(self, hand):
        from tools.llm_candidates import seeds_for

        est, h = hand
        assert seeds_for(None, est, h.window) == ()
        assert seeds_for({"interpretations": []}, est, h.window) == ()

    def test_same_as_current_reading_is_not_a_seed(self, hand):
        from tools.llm_candidates import interpretation_edits, seeds_for

        est, h = hand
        same = {"interpretations": [{"utterances": [], "silent_actions": [], "actions": [], "button_seat": None,
                                     "winner_seat": None, "confidence": 0.5, "note": ""}]}
        assert interpretation_edits(same, est, h.window) == [()]
        assert seeds_for(same, est, h.window) == ()

    def test_perfect_answer_replays_to_the_truth(self, hand):
        from integration.estimator import _ordered
        from tools.llm_candidates import interpretation_edits
        from tools.measure_capture_accuracy import hand_fully_correct

        est, h = hand
        (edits,) = interpretation_edits(_perfect(h.truth), est, h.window)
        assert hand_fully_correct(h.truth, est.evaluate_edits(h.window, _ordered(edits)).hand)

    def test_direct_hands_use_the_rfid_board(self, hand):
        from tools.llm_candidates import direct_hands
        from tools.measure_capture_accuracy import hand_fully_correct

        est, h = hand
        (d,) = direct_hands(_perfect(h.truth), h.window)
        assert d["board"] == list(h.window.base.get("board") or [])
        assert hand_fully_correct(h.truth, d)


@pytest.mark.slow       # 推定器の探索を 4 回（このハンドで合わせて数十秒）
def test_seeds_join_the_candidates_and_empty_seeds_change_nothing(hand):
    from integration.estimator import hand_from_key
    from tools.llm_candidates import seeds_for
    from tools.measure_capture_accuracy import hand_fully_correct

    est, h = hand
    plain = est.estimate_hand(h.window)
    empty = est.estimate_hand(dataclasses.replace(h.window, seeds=()))
    assert plain.record_keys == empty.record_keys and plain.best.score == empty.best.score
    seeded = est.estimate_hand(dataclasses.replace(h.window, seeds=seeds_for(_perfect(h.truth), est, h.window)))
    assert any(hand_fully_correct(h.truth, hand_from_key(k)) for k in seeded.record_keys)
    # 違法・壊れた直しの組（範囲外の発話の読み）は候補を壊さない
    from integration.estimator import Edit

    broken = est.estimate_hand(dataclasses.replace(h.window, seeds=((Edit("read", h.window.end + 999, "コール"),),)))
    assert broken.best.hand


# ───────────────────── 呼び出し（偽のクライアント）とキャッシュ ─────────────────────


class _FakeMessages:
    def __init__(self, stop_reason: str, parsed=None):
        self.stop_reason = stop_reason
        self.parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.parsed, usage=usage,
                               model=kwargs["model"])


def _client(stop_reason: str, parsed=None):
    messages = _FakeMessages(stop_reason, parsed)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


class TestAsk:
    def test_answer_and_cache_roundtrip(self, tmp_path):
        pytest.importorskip("pydantic")
        from tools.llm_candidates import answer_model, ask, cache_path, cost_usd, load_cached

        model = answer_model()
        parsed = model.model_validate({"interpretations": []})
        client, messages = _client("end_turn", parsed)
        payload = {"hand_id": 1, "utterances": []}
        record = ask(client, "claude-opus-5-5", payload)
        assert record["answer"] == {"interpretations": []}
        sent = messages.calls[0]
        assert sent["fallbacks"] == "default" and sent["thinking"] == {"type": "adaptive"}
        assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert json.loads(sent["messages"][0]["content"].split("\n", 1)[1]) == payload
        path = cache_path(tmp_path, "claude-opus-5-5", "s1", 1)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        assert load_cached(path, payload)["answer"] == {"interpretations": []}
        assert load_cached(path, {"hand_id": 2}) is None          # 入力が違えば使わない
        assert cost_usd("claude-opus-5-5", record["usage"]) == pytest.approx((1000 * 4 + 500 * 20 + 2000 * 0.2) / 1e6)

    def test_haiku_has_no_server_side_fallback(self):
        pytest.importorskip("pydantic")
        from tools.llm_candidates import answer_model, ask

        client, messages = _client("end_turn", answer_model().model_validate({"interpretations": []}))
        ask(client, "claude-haiku-5-5", {"hand_id": 1})
        assert "fallbacks" not in messages.calls[0] and "betas" not in messages.calls[0]

    @pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
    def test_refusal_and_truncation_are_no_answer(self, stop):
        pytest.importorskip("pydantic")
        from tools.llm_candidates import ask

        client, _ = _client(stop, parsed=SimpleNamespace(model_dump=lambda: {"interpretations": ["x"]}))
        record = ask(client, "claude-opus-5-5", {"hand_id": 1})
        assert record["answer"] is None and record["stop_reason"] == stop
