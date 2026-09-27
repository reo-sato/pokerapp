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
