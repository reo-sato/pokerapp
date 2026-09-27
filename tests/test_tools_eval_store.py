"""tests/test_tools_eval_store.py

店舗のログをいまのコードで再生して評価するツール（ADR-0056 追記 1 の S0）と、それを支える記録:

- 在否で決めた配布を信号（rfid kind `deal` + `cards`）として記録し、ハンドを始めた時点（`hand_start`）も記録する。
  replay はそれに従う = 起動時に卓に残っていた札で存在しないハンドを始めない（店舗 15dd2034 の再生で起きた）。
- 記録から卓の設定（席・持ち点・ブラインド・1 ハンド目のボタン）を取り、再生して記録と突き合わせる。
- 真のアクションとの一致率、要確認の精度と再現率、行の出所、タイムライン、回帰テスト用の書き出し。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from output.event_recorder import event_to_envelope  # noqa: E402
from tests.test_rfid_folds import FOLDOUT_CONFIRM_SEC, _Table  # noqa: E402
from tests.test_silent_runs import _board  # noqa: E402
from tools import eval_store  # noqa: E402

HOLES_1 = {4: ["2h", "5d"], 5: ["3c", "Td"], 6: ["4c", "Tc"]}
HOLES_2 = {4: ["As", "Ad"], 5: ["Ks", "Kd"], 6: ["Qs", "Qh"]}
SID = "folds"                                    # _Table の JsonWriter の session_id


def _write_events(tb: _Table, folder: Path) -> None:
    lines = [json.dumps(event_to_envelope(e), ensure_ascii=False) for e in tb.recorder.events]
    (folder / f"{SID}.events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _two_hands(tmp_path: Path) -> _Table:
    """1 ハンド目: レイズに 2 人が降りる。2 ハンド目: フロップまでチェックで進み、ショーダウン。"""
    tb = _Table(tmp_path)
    tb.deal(HOLES_1)
    tb.say("レイズ 600")
    tb.tick(tb.now + 1.0)
    for seat in (4, 5):
        tb.lift(seat)
        tb.tick(tb.now + 4.0)
    tb.tick(tb.now + FOLDOUT_CONFIRM_SEC + 1.0)
    tb.cards = {}
    tb.absent_since = {}
    tb.tick(tb.now + 3.0)
    tb.deal(HOLES_2)
    for text in ("コール", "コール", "チェック"):
        tb.say(text)
        tb.tick(tb.now + 1.0)
    _board(tb, ["7d", "6h", "Qc"])
    for text in ("チェック", "チェック", "チェック"):
        tb.say(text)
        tb.tick(tb.now + 1.0)
    _board(tb, ["2c"])
    tb.say("チェックアラウンド")
    tb.tick(tb.now + 1.0)
    _board(tb, ["9s"])
    tb.say("チェックアラウンド")
    tb.tick(tb.now + 1.0)
    tb.say("ハンド終了")
    tb.tick(tb.now + 1.0)
    assert len(tb.hands) == 2
    _write_events(tb, tmp_path)
    return tb


def _files(tmp_path: Path) -> eval_store.SessionFiles:
    (session,) = eval_store.find_sessions(tmp_path)
    return session


class TestRecordedDeal:
    def test_the_deal_and_the_hand_start_are_recorded(self, tmp_path):
        tb = _two_hands(tmp_path)
        signals = [(e.kind, e.cards) for e in tb.recorder.events if getattr(e, "kind", "card") in ("deal", "hand_start")]
        assert [k for k, _ in signals] == ["deal", "hand_start", "deal", "hand_start"]
        assert sorted(signals[0][1]) == sorted(f"{s}:{c}" for s, cards in HOLES_1.items() for c in cards)

    def test_replay_follows_the_recorded_deal_not_leftover_cards(self, tmp_path):
        # 起動したとき卓に前の札が残っている（席の手札 + ボード）。live はボードに札がある間は配布としない
        tb = _Table(tmp_path)
        tb.t._board_presence = lambda: {"present_count": 5}                       # noqa: SLF001
        for seat, cards in HOLES_1.items():
            tb.put(seat, cards)
        from core.events import RFIDEvent
        for seat, cards in HOLES_1.items():
            for card in cards:
                ev = RFIDEvent(tag_id=card, card=card, reader_id=f"r{seat}", role="seat", seat=seat,
                               timestamp=tb.now, raw_tag_id=card)
                tb.recorder.record(ev)
                tb.t._process_rfid_event(ev)                                     # noqa: SLF001
        for i, card in enumerate(["9h", "Qc", "Qs", "9c", "Th"], start=1):
            ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                           timestamp=tb.now, raw_tag_id=card, board_index=i)
            tb.recorder.record(ev)
            tb.t._process_rfid_event(ev)                                         # noqa: SLF001
        tb.tick(tb.now + 3.0)
        assert not tb.t._hand_open                                               # noqa: SLF001
        tb.t._board_presence = lambda: {"present_count": 0}                       # noqa: SLF001 — 片付けた
        tb.cards = {}
        tb.tick(tb.now + 3.0)
        tb.deal(HOLES_2)
        tb.say("レイズ 600")
        tb.tick(tb.now + 1.0)
        for seat in (4, 5):
            tb.lift(seat)
            tb.tick(tb.now + 4.0)
        tb.tick(tb.now + FOLDOUT_CONFIRM_SEC + 1.0)
        (live,) = tb.hands
        _write_events(tb, tmp_path)
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        assert report.differences == [] and len(report.replayed) == 1
        assert report.replayed[0]["players"][0]["hole_cards"] == HOLES_2[4]
        # 配布の信号を外した古い形の記録では、残っていた札で存在しないハンドを始めてしまう（修正前の再生）
        events = (tmp_path / f"{SID}.events.jsonl").read_text(encoding="utf-8").splitlines()
        old = [line for line in events if '"deal"' not in line and '"hand_start"' not in line]
        (tmp_path / f"{SID}.events.jsonl").write_text("\n".join(old) + "\n", encoding="utf-8")
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        assert report.differences and "配布の信号" in report.note


class TestEvaluate:
    def test_setup_comes_from_the_record(self, tmp_path):
        _two_hands(tmp_path)
        record = json.loads((tmp_path / f"{SID}.json").read_text(encoding="utf-8"))
        setup = eval_store.session_setup(record)
        assert [p["seat"] for p in setup["players"]] == [4, 5, 6]
        assert all(p["stack"] == 10000 for p in setup["players"])
        assert (setup["sb"], setup["bb"], setup["button_prior"]) == (100, 200, 5)   # 1 ハンド目のボタンは席6

    def test_replay_matches_the_record(self, tmp_path):
        _two_hands(tmp_path)
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        assert report.hands == 2 and report.differences == []
        assert report.flags == {"auto_new_hand": True, "auto_winner": True, "rfid_folds": True}
        assert report.sources == {"音声": 13, "RFID": 2}               # 2 人のフォールドは札の離脱

    def test_truth_accuracy_and_review_precision(self, tmp_path):
        tb = _two_hands(tmp_path)
        truth = []
        for hand in tb.hands:
            actions = [{"street": a.street, "seat": a.seat, "action": a.action, "amount": a.amount}
                       for a in hand.actions]
            truth.append({"hand_id": hand.hand_id, "actions": actions, "board": list(hand.board),
                          "winner_seat": hand.winner_seat, "annotator": "owner", "source": "manual-edit"})
        truth[0]["actions"][0]["amount"] = 800                                # 本当は 800 のレイズだった
        truth[0].update(blind=True, entry_sec=30.0)                            # 記録を見ずに入れた
        truth[1]["actions"][0]["unsure"] = True
        truth[1]["entry_sec"] = 50.0
        (tmp_path / f"{SID}.ground_truth.json").write_text(json.dumps({"hands": truth}), encoding="utf-8")
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        rec = report.truth["record"]
        assert rec["hands"] == 2 and rec["wrong_rows"] == 1 and rec["missed_rows"] == 0
        assert rec["action_accuracy"] < 1.0
        assert rec["flagged_rows"] == sum(a.needs_review for h in tb.hands for a in h.actions)
        assert rec["review_recall"] in (0.0, 1.0)
        assert report.truth["replay"]["wrong_rows"] == 1
        assert (rec["blind_hands"], rec["unsure_rows"], rec["entry_sec_avg"]) == (1, 1, 40.0)
        assert rec["blind_accuracy"] == 2 / 3 and rec["seen_accuracy"] == 1.0

    def test_timeline_and_rows(self, tmp_path):
        _two_hands(tmp_path)
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        windows = eval_store.hand_windows(report.replayed)
        lines = eval_store.timeline(_files(tmp_path), windows[2])
        assert any("札 7d → ボード 1 枚目" in line for line in lines)
        assert any("入力 check" in line or "「チェック」" in line for line in lines)
        rows = eval_store.compare_rows(None, report.live[1], report.replayed[1])
        assert "preflop 席4 call 200" in rows[1]                  # 2 ハンド目のボタンは席4（最初の手番）
        assert not any(row.lstrip().startswith("≠") for row in rows)


def _transcripts(tb: _Table, folder: Path, *, heard: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> list:
    """記録した音声のアクションから聞き取りの記録（`<sid>.transcripts.jsonl`）を作る。

    heard: 発話の文 → 書き起こし（聞き違い）。drop: 記録したときに読めなかった発話（events.jsonl から外す）。
    """
    from core.events import AudioEvent

    rows: dict[float, dict] = {}
    for e in tb.recorder.events:
        if isinstance(e, AudioEvent) and e.utterance_start_ts is not None:
            text = (heard or {}).get(e.raw_text, e.raw_text)
            rows.setdefault(e.utterance_start_ts, {
                "utterance_start_ts": e.utterance_start_ts, "heard_at": e.timestamp, "text": text,
                "confidence": 0.9, "audio_sec": 1.0, "audio_file": f"{int(e.utterance_start_ts * 1000)}.wav",
            })
    (folder / f"{SID}.transcripts.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows.values()), encoding="utf-8")
    if drop:
        kept = [e for e in tb.recorder.events if not (isinstance(e, AudioEvent) and e.raw_text in drop)]
        lines = [json.dumps(event_to_envelope(e), ensure_ascii=False) for e in kept]
        (folder / f"{SID}.events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(rows.values())


class TestListening:
    """書き起こしをいまの読み取りで読み直した再生と、音声を採点し直した文での再生（S2）。"""

    def test_reading_again_is_the_same_when_nothing_changed(self, tmp_path):
        _transcripts(_two_hands(tmp_path), tmp_path)
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        assert report.reparsed and eval_store.diff_record(report.replayed, report.reparsed) == []

    def test_reading_again_uses_the_current_parser(self, tmp_path):
        # 記録したときは「レイス 600」を読めず、レイズが記録に無かった（いまは音の近さで読める）
        tb = _two_hands(tmp_path)
        _transcripts(tb, tmp_path, heard={"レイズ 600": "レイス 600"}, drop=("レイズ 600",))
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        replay_rows = [eval_store._row(a) for a in report.replayed[0]["actions"]]   # noqa: SLF001
        assert ("preflop", 6, "raise", 600) not in replay_rows
        live_rows = [eval_store._row(a) for a in report.live[0]["actions"]]         # noqa: SLF001
        rows = report.reparsed[0]["actions"]
        assert [eval_store._row(a) for a in rows] == live_rows                        # noqa: SLF001
        (raise_row,) = [a for a in rows if a["action"] == "raise"]
        assert raise_row["needs_review"] and "fuzzy_keyword" in raise_row["reason"]
        assert raise_row["raw_text"] == "レイス 600"

    def test_rescored_best_texts(self, tmp_path, capsys):
        tb = _two_hands(tmp_path)
        rows = _transcripts(tb, tmp_path, heard={"レイズ 600": "れいぞう 600"}, drop=("レイズ 600",))
        rescored = []
        for r in rows:
            best = "レイズ 600" if r["text"] == "れいぞう 600" else r["text"]
            rescored.append({"audio_file": r["audio_file"], "text": r["text"], "best": best,
                             "scored": [{"text": best, "logprob": -1.0}, {"text": r["text"], "logprob": -3.5}]})
        rescored.append({"audio_file": "missing.wav", "text": "", "error": "音声のファイルがありません"})
        (tmp_path / f"{SID}.rescored.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rescored), encoding="utf-8")
        truth = [{"hand_id": h.hand_id, "board": list(h.board), "winner_seat": h.winner_seat,
                  "actions": [{"street": a.street, "seat": a.seat, "action": a.action, "amount": a.amount}
                              for a in h.actions]} for h in tb.hands]
        (tmp_path / f"{SID}.ground_truth.json").write_text(json.dumps({"hands": truth}), encoding="utf-8")
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        lis = report.listening
        assert (lis["rescored"], lis["errors"], lis["changed_text"], lis["changed_actions"]) == (len(rows), 1, 1, 1)
        (change,) = lis["changed"]
        assert (change["heard"], change["best"], change["margin"]) == ("れいぞう 600", "レイズ 600", 2.5)
        # 書き起こし（読めない）より、採点し直した文（レイズ 600）の方が真のアクションに合う
        assert report.truth["rescored"]["action_accuracy"] == 1.0
        assert report.truth["reparse"]["action_accuracy"] < 1.0
        eval_store.print_report([report], True, 1, None, {SID: _files(tmp_path)})
        out = capsys.readouterr().out
        assert "音声の採点: " in out and "「れいぞう 600」" in out and "採点し直した文: 一致率 100%" in out
        assert "採点: 「レイズ 600」→ raise 600" in out                         # タイムライン


class TestFixtures:
    def _with_truth(self, tmp_path: Path) -> _Table:
        tb = _two_hands(tmp_path)
        hand = tb.hands[1]
        truth = {"hand_id": hand.hand_id, "board": list(hand.board), "winner_seat": hand.winner_seat,
                 "actions": [{"street": a.street, "seat": a.seat, "action": a.action, "amount": a.amount}
                             for a in hand.actions]}
        (tmp_path / f"{SID}.ground_truth.json").write_text(json.dumps({"hands": [truth]}), encoding="utf-8")
        return tb

    def test_export_and_check(self, tmp_path):
        self._with_truth(tmp_path)
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        out = tmp_path / "fixtures"
        folder = eval_store.export_fixture(_files(tmp_path), report, out)
        expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        (hand,) = expected["hands"]
        assert hand["baseline"]["action_correct"] == hand["baseline"]["action_total"] > 0
        assert eval_store.check_fixture(folder) == []
        # 悪くなったら（ここでは真のアクションとの一致が baseline を下回るように書き換え）知らせる
        hand["baseline"]["action_correct"] += 1
        (folder / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
        assert eval_store.check_fixture(folder)

    def test_export_keeps_a_higher_baseline(self, tmp_path):
        self._with_truth(tmp_path)
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        out = tmp_path / "fixtures"
        folder = eval_store.export_fixture(_files(tmp_path), report, out)
        expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        expected["hands"][0]["baseline"]["action_correct"] = 99
        (folder / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
        eval_store.export_fixture(_files(tmp_path), report, out)
        again = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        assert again["hands"][0]["baseline"]["action_correct"] == 99       # 黙って下げない

    def test_an_old_style_fixture_is_not_overwritten(self, tmp_path):
        self._with_truth(tmp_path)
        report = eval_store.evaluate_session(_files(tmp_path), {}, None)
        out = tmp_path / "fixtures"
        date = report.live[0]["started_at"][:10]
        old = out / f"{date}-{SID}"
        old.mkdir(parents=True)
        (old / "expected.json").write_text(json.dumps({"actions": []}), encoding="utf-8")
        with pytest.raises(SystemExit):
            eval_store.export_fixture(_files(tmp_path), report, out)


class TestCommandLine:
    def test_a_zip_from_pack_logs(self, tmp_path, capsys):
        folder = tmp_path / "logs"
        folder.mkdir()
        _two_hands(folder)
        archive = tmp_path / "pokerlogs.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for p in folder.iterdir():
                if p.is_file():
                    zf.write(p, f"{SID}/{p.name}")
            zf.writestr("manifest.json", json.dumps({"code_fingerprint": {"main.py": "0"}}))
        assert eval_store.main([str(archive), "--timeline", "--hand", "2"]) == 0
        out = capsys.readouterr().out
        assert "セッション folds（2 ハンド）" in out and "記録したときと違う（main.py）" in out
        assert "再生の結果は記録と同じ" in out and "--- ハンド 2" in out and "--- ハンド 1" not in out

    def test_json_output(self, tmp_path, capsys):
        _two_hands(tmp_path)
        assert eval_store.main([str(tmp_path), "--json"]) == 0
        (session,) = json.loads(capsys.readouterr().out)
        assert session["session_id"] == SID and session["differences"] == []

    def test_no_sessions(self, tmp_path, capsys):
        assert eval_store.main([str(tmp_path)]) == 1
