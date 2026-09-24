"""tests/test_rfid_table_flow.py

ADR-0058: 卓の流れに合わせた RFID の解釈（本番の起動経路 = main.py で有効）。

店舗の実卓で分かった 2 つの前提を、入力なしで扱う:

- **フォールドした手札は卓の中央へ押し出され、ボードのリーダーの上を通る** → そのハンドで席に
  記録した札はボードの札にしない（その席の「マック」として記録する）。記録していない札も、
  ボードは載り続けてから確定するので一瞬の通過では位置を取らない。
- **ミスディールの配り直しで、ディーラーに入力を求められない** → 前の札が `release_sec` 以上
  見えず、新しい札が載り続けたら差し替える。一瞬の読み落ち（ISSUE-0026）では差し替わらない。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.table_state import build_table_state
from integration.engine import IntegrationThread
from integration.replay import event_from_envelope
from output.event_recorder import event_to_envelope
from output.json_writer import JsonWriter
from rfid.card_master import CardMaster
from rfid.reader_thread import RFIDThread

ROOT = Path(__file__).parent.parent

CARDS = {
    # 席 1 / 2 / 3 の手札
    "04:A1": "As", "04:A2": "Kd", "04:B1": "Qh", "04:B2": "Jc", "04:C1": "9s", "04:C2": "8s",
    # ボード
    "04:F1": "5d", "04:F2": "Tc", "04:F3": "2h", "04:D1": "7c",
    # 配り直しの新しい札（UID は実機と同じく 16 進）
    "04:E1": "Ah", "04:E2": "Ad", "04:E3": "Ac",
}


class _Bridge:
    def __init__(self) -> None:
        self.uids: list[str] = []

    def connect(self) -> bool:
        return True

    def read_uids(self) -> list[str]:
        return list(self.uids)

    def close(self) -> None:
        pass


class Table:
    """席 3 つ + ボード 2 台（左 BL / 右 BR）の卓を、時計を手で進めながら回す決定的ドライバ。"""

    CONFIGS = [
        {"name": "S1", "role": "seat", "seat": 1},
        {"name": "S2", "role": "seat", "seat": 2},
        {"name": "S3", "role": "seat", "seat": 3},
        {"name": "BL", "role": "board"},
        {"name": "BR", "role": "board"},
    ]

    def __init__(self, tmp_path: Path, *, commit: float = 2.0, gap: float = 1.5,
                 release: float | None = 6.0) -> None:
        cm = CardMaster(tmp_path / "cards.json")
        for uid, card in CARDS.items():
            cm.register(uid, card)
        self.bridges = {cfg["name"]: _Bridge() for cfg in self.CONFIGS}
        self.now = 1000.0
        self.queue = make_rfid_queue()
        self.thread = RFIDThread(
            rfid_queue=self.queue, card_master=cm, reader_configs=self.CONFIGS,
            stop_event=threading.Event(),
            bridge_factory=lambda name, reader=0: self.bridges[name],
            clock=lambda: self.now,
            commit_sec=commit, gap_sec=gap, release_sec=release,
        )

    def put(self, reader: str, *uids: str) -> None:
        self.bridges[reader].uids = list(uids)

    def run(self, seconds: float, step: float = 0.3) -> list[RFIDEvent]:
        """`seconds` 秒ぶん全リーダーを poll し、その間に出た event を返す。"""
        events: list[RFIDEvent] = []
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = round(self.now + step, 6)
            for i, cfg in enumerate(self.CONFIGS):
                self.thread._poll_reader(self.bridges[cfg["name"]], cfg, f"reader_{i}")  # noqa: SLF001
            while not self.queue.empty():
                events.append(self.queue.get_nowait())
        return events

    def mucked_at(self, seat: int):
        return self.thread.presence_snapshot()[seat]["mucked_at"]


def _board(events: list[RFIDEvent]) -> list[tuple]:
    return [(e.card, e.board_index, e.replaces) for e in events if e.role == "board"]


class TestFoldedHandsOverTheBoard:
    def test_folded_hand_passing_over_the_board_is_not_a_board_card(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.put("S2", "04:B1", "04:B2")
        dealt = t.run(0.3)
        assert [(e.seat, e.card) for e in dealt] == [(1, "As"), (1, "Kd"), (2, "Qh"), (2, "Jc")]

        # 席 1 がフォールド: 手札が席から消え、左のボードのリーダーの上を 0.6 秒で通過する
        t.put("S1")
        t.put("BL", "04:A1", "04:A2")
        assert t.run(0.6) == []
        t.put("BL")
        assert t.run(5.0) == []
        assert t.mucked_at(1) is not None          # 卓モニタに「マック」と出る
        assert t.mucked_at(2) is None

        # flop は 1..3、turn は 4（通過した手札が位置を取っていない）
        t.put("BL", "04:F1")
        t.put("BR", "04:F2", "04:F3")
        assert _board(t.run(2.4)) == [("5d", 1, None), ("Tc", 2, None), ("2h", 3, None)]
        t.put("BR", "04:F2", "04:F3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]

    def test_hole_card_resting_on_a_board_reader_never_becomes_a_board_card(self, tmp_path: Path):
        """押し出された手札がボードのリーダーの上で止まっても、ボードの札にはしない。"""
        t = Table(tmp_path)
        t.put("S3", "04:C1", "04:C2")
        t.run(0.3)
        t.put("S3")
        t.put("BL", "04:C1", "04:C2")
        assert t.run(10.0) == []
        assert t.mucked_at(3) is not None

    def test_unknown_card_passing_quickly_does_not_take_a_position(self, tmp_path: Path):
        """席で読めなかった札（手に持っていた等）も、通過しただけでは位置を取らない。"""
        t = Table(tmp_path)
        t.put("BR", "04:A1")               # 席では読んでいない札が 0.6 秒通過
        assert t.run(0.6) == []
        t.put("BR")
        assert t.run(5.0) == []
        t.put("BL", "04:F1")
        assert _board(t.run(2.4)) == [("5d", 1, None)]

    def test_hole_card_filter_also_applies_in_the_immediate_mode(self, tmp_path: Path):
        """従来の解釈（最初に見えた瞬間に確定）でも、手札はボードの札にしない。"""
        t = Table(tmp_path, commit=0.0, gap=0.0, release=None)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("BL", "04:A1")
        assert t.run(0.3) == []
        assert t.mucked_at(1) is not None
        t.put("BL", "04:A1", "04:F1")
        assert _board(t.run(0.3)) == [("5d", 1, None)]


class TestBoardDwell:
    def test_board_card_counts_after_it_stays_and_keeps_its_first_seen_time(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1")
        assert t.run(1.5) == []                   # まだ確定しない
        events = t.run(1.0)
        assert _board(events) == [("5d", 1, None)]
        assert events[0].timestamp == pytest.approx(1000.3)   # 最初に見えた時刻（ADR-0055）

    def test_intermittent_reads_commit_once(self, tmp_path: Path):
        """結合の弱い台で 0.9 秒読み落ちても（gap 1.5 秒以内）、1 回だけ確定し再発火しない。"""
        t = Table(tmp_path)
        events = []
        for present, seconds in ((True, 0.6), (False, 0.9), (True, 0.9), (False, 0.6), (True, 1.2)):
            t.put("BL", *(["04:F1"] if present else []))
            events += t.run(seconds)
        assert _board(events) == [("5d", 1, None)]

    def test_flop_card_dropout_does_not_give_its_slot_to_the_turn(self, tmp_path: Path):
        """flop の 1 枚が 7 秒読めなくても、残りが載っているので turn は 4 枚目のまま。"""
        t = Table(tmp_path)
        t.put("BL", "04:F1", "04:F2")
        t.put("BR", "04:F3")
        t.run(2.4)
        t.put("BL", "04:F1")                      # 2 枚目が読めなくなる
        t.run(7.0)
        t.put("BR", "04:F3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]
        t.put("BL", "04:F1", "04:F2")             # 読めるようになれば元の 2 枚目で再発火
        assert _board(t.run(0.3)) == [("Tc", 2, None)]


class TestRedealWithoutInput:
    def test_flop_redeal_replaces_the_positions(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1", "04:F2")
        t.put("BR", "04:F3")
        t.run(2.4)
        t.put("BL")                               # 早すぎた flop を回収
        t.put("BR")
        t.run(7.0)
        t.put("BL", "04:E1", "04:E2")
        t.put("BR", "04:E3")
        assert _board(t.run(2.4)) == [("Ah", 1, "5d"), ("Ad", 2, "Tc"), ("Ac", 3, "2h")]
        t.put("BR", "04:E3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]

    def test_hole_card_redeal_replaces_both_cards(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")                               # ミスディールで回収
        t.run(7.0)
        t.put("S1", "04:E1", "04:E2")
        assert t.run(1.5) == []                   # 新しい札が載り続けるまで待つ
        events = t.run(1.0)
        assert {e.card for e in events} == {"Ah", "Ad"}
        assert {e.replaces for e in events} == {"As", "Kd"}
        assert all(e.seat == 1 for e in events)

    def test_peeking_at_cards_does_not_replace_them(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")                               # 持ち上げて見ている
        t.run(10.0)
        t.put("S1", "04:A1", "04:A2")
        events = t.run(2.4)
        assert sorted(e.card for e in events) == ["As", "Kd"]   # 同じ札の再発火だけ
        assert all(e.replaces is None for e in events)

    def test_third_card_waits_until_an_old_card_is_gone(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1", "04:A1", "04:A2", "04:E1")
        with caplog.at_level(logging.WARNING, logger="rfid.reader_thread"):
            assert t.run(10.0) == []              # 前の 2 枚が載っている間は記録しない
        assert sum("3 枚目" in r.getMessage() for r in caplog.records) == 1
        t.put("S1", "04:A1", "04:E1")             # Kd が回収された
        events = t.run(7.0)
        assert [(e.card, e.replaces) for e in events] == [("Ah", "Kd")]

    def test_redeal_can_move_a_card_to_another_seat(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.put("S2", "04:B1", "04:B2")
        t.run(0.3)
        t.put("S1")                               # 全員の札を回収して配り直す
        t.put("S2")
        t.run(7.0)
        t.put("S1", "04:E2", "04:E3")
        t.put("S2", "04:A1", "04:E1")             # 前は席 1 だった As が席 2 に来る
        events = t.run(2.4)
        by_seat = {seat: {e.card for e in events if e.seat == seat} for seat in (1, 2)}
        assert by_seat == {1: {"Ad", "Ac"}, 2: {"As", "Ah"}}
        assert {e.replaces for e in events if e.seat == 2} == {"Qh", "Jc"}

    def test_board_card_on_a_seat_reader_is_not_a_hole_card(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1")
        t.run(2.4)
        t.put("BL")
        t.put("S2", "04:F1")
        assert t.run(10.0) == []

    def test_release_none_keeps_explicit_corrections_only(self, tmp_path: Path):
        """`release_sec=None` では自動の差し替えをしない（明示の cs / cb だけ）。"""
        t = Table(tmp_path, release=None)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")
        t.run(10.0)
        t.put("S1", "04:E1", "04:E2")
        assert t.run(5.0) == []


class TestManualCorrectionsAndNewHand:
    def test_explicit_board_correction_frees_the_slot(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1", "04:F2", "04:F3")
        t.run(2.4)
        t.put("BL", "04:F1", "04:F3")
        t.thread.forget_board_position(2)
        t.put("BL", "04:F1", "04:E1", "04:F3")
        assert _board(t.run(2.4)) == [("Ah", 2, None)]

    def test_explicit_seat_correction_rereads_at_once(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.thread.forget_seat_cards(1)
        t.put("S1", "04:E1", "04:E2")
        events = t.run(0.3)
        assert sorted(e.card for e in events) == ["Ad", "Ah"]
        assert all(e.replaces is None for e in events)

    def test_new_hand_forgets_owners_and_muck(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")
        t.put("BL", "04:A1")
        t.run(0.6)
        t.put("BL")
        t.run(2.0)
        assert t.mucked_at(1) is not None
        t.thread.reset_for_new_hand()
        assert t.mucked_at(1) is None
        t.put("BL", "04:A1")                      # 前のハンドの手札が次のハンドのボードに来てもよい
        assert _board(t.run(2.4)) == [("As", 1, None)]


# ――― engine 側（差し替え / 席の移動 / ボード位置の差し替え）―――


def _engine(tmp_path: Path) -> IntegrationThread:
    gs = GameStateManager(
        [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=100) for i in range(3)], sb=1, bb=2
    )
    return IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, "flow"), stop_event=threading.Event(),
    )


def _seat(t: IntegrationThread, seat: int, card: str, replaces: str | None = None,
          ts: float = 100.0) -> None:
    t._process_rfid_event(RFIDEvent(  # noqa: SLF001
        tag_id=card, card=card, reader_id=f"seat_{seat}", role="seat", seat=seat,
        timestamp=ts, raw_tag_id=card, replaces=replaces,
    ))


def _board_ev(t: IntegrationThread, index: int, card: str, ts: float) -> None:
    t._process_rfid_event(RFIDEvent(  # noqa: SLF001
        tag_id=card, card=card, reader_id="board_0", role="board", seat=None,
        timestamp=ts, raw_tag_id=card, board_index=index,
    ))


class TestEngine:
    def test_replacement_swaps_the_hole_card_and_marks_review(self, tmp_path: Path):
        t = _engine(tmp_path)
        _seat(t, 1, "As")
        _seat(t, 1, "Kd")
        assert t._hand_needs_review is False                  # noqa: SLF001
        _seat(t, 1, "Ah", replaces="Kd")
        assert t._hole_cards[1] == ["As", "Ah"]               # noqa: SLF001
        assert t._hand_needs_review is True                   # noqa: SLF001

    def test_moved_card_leaves_its_old_seat(self, tmp_path: Path):
        t = _engine(tmp_path)
        for seat, card in ((1, "As"), (1, "Kd"), (2, "Qh"), (2, "Jc")):
            _seat(t, seat, card)
        _seat(t, 2, "As", replaces="Jc")
        assert t._hole_cards[1] == ["Kd"]                     # noqa: SLF001
        assert t._hole_cards[2] == ["Qh", "As"]               # noqa: SLF001
        assert t._hand_needs_review is True                   # noqa: SLF001

    def test_board_replacement_takes_the_new_cards_time(self, tmp_path: Path):
        t = _engine(tmp_path)
        _board_ev(t, 1, "5d", ts=100.0)
        _board_ev(t, 1, "5d", ts=150.0)                       # 同じ札の再発火は時刻を変えない
        assert t._board_dealt_at[1] == 100.0                  # noqa: SLF001
        assert t._hand_needs_review is False                  # noqa: SLF001
        _board_ev(t, 1, "Ah", ts=200.0)                       # 配り直しで別の札
        assert t._board_cards == ["Ah"]                       # noqa: SLF001
        assert t._board_dealt_at[1] == 200.0                  # noqa: SLF001
        assert t._hand_needs_review is True                   # noqa: SLF001


# ――― 記録（events.jsonl）と卓状態 ―――


class TestRecordingAndTableState:
    def test_replaces_round_trips_and_is_omitted_when_absent(self):
        ev = RFIDEvent(tag_id="04:E1", card="Ah", reader_id="reader_0", role="seat", seat=1,
                       timestamp=1.0, raw_tag_id="04:E1", replaces="Kd")
        env = event_to_envelope(ev)
        assert env["replaces"] == "Kd"
        assert event_from_envelope(env).replaces == "Kd"
        plain = event_to_envelope(RFIDEvent(
            tag_id="04:A1", card="As", reader_id="reader_0", role="seat", seat=1,
            timestamp=1.0, raw_tag_id="04:A1",
        ))
        assert "replaces" not in plain                        # 旧 replay・golden と同じ形

    def test_envelope_with_replaces_matches_the_schema(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads(
            (ROOT / "docs/contracts/schemas/reconstruction_event.schema.json").read_text(encoding="utf-8")
        )
        assert schema["version"] == "0.4"
        env = event_to_envelope(RFIDEvent(
            tag_id="04:E1", card="Ah", reader_id="reader_3", role="board", seat=None,
            timestamp=1.0, raw_tag_id="04:E1", board_index=1, replaces="5d",
        ))
        jsonschema.validate(env, schema)

    def test_table_state_shows_mucked_seats(self):
        state = build_table_state(
            session_id="s", hand_id=1, now=100.0, updated_at="t", seats=[1, 2],
            hole_cards={1: ["As", "Kd"], 2: ["Qh", "Jc"]},
            presence={
                1: {"present": False, "absent_since": 90.0, "mucked_at": 95.0},
                2: {"present": True, "absent_since": None, "mucked_at": 80.0},
            },
            board=[], board_timeline=[],
        )
        s1, s2 = state.seats
        assert s1.mucked is True
        assert s2.mucked is False                             # いま載っている席はマック表示にしない
        assert state.to_dict()["mucked_seats"] == [1]

    def test_monitor_text_says_muck(self):
        from tools.table_monitor import _format_text
        text = _format_text({
            "session_id": "s", "hand_id": 1, "updated_at": "t", "age_sec": 0.1,
            "rfid_street": "preflop", "engine_street": "preflop", "button_seat": None, "board": [],
            "seats": [{"seat": 1, "present": False, "mucked": True, "likely_folded": False,
                       "dealt_in": True, "away_sec": 3.0, "cards": ["As", "Kd"], "position": ""}],
        })
        assert "マック" in text
