"""tests/test_live_hand.py

オーナー 2026-09-30: 「ハンド進行中に真のアクションを入力したいので、フロップがディールされるタイミングで UI に
表示するようにしてください」→ 2026-10-06: 「ハンド情報を、UI にプリフロップのディールが行われたタイミングで
表示できますか？」。

- ロガー（`IntegrationThread(live_hand=True)`）は手札が配られた時点から、そのハンドのここまでの記録を
  `logs/<sid>.live_hand.json` に書き、ハンドが終わったら消す（ハンドの記録 `logs/<sid>.json` には入れない）。
  RFID の自動開始は配った瞬間、「ハンド開始」/ n で先に始めたハンドは席のリーダーがあれば最初の手札が届いた時点、
  席のリーダーが無い構成では始めた時点から。
- 真のアクション入力の画面は、それを一覧のいちばん上に「進行中」で出し、ふつうのハンドと同じように開いて保存できる。
  進行中は「記録どおり」にできず、途中で保存してもブラインドのまま。ハンドが終わると同じハンドに保存した内容が付き、
  記録と照らし合わせる。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from audio.recognizer import parse_actions
from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import PlayerState
from core.ground_truth_repository import GroundTruthRepository
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from tools.ground_truth_ui import hand_detail, list_hands, list_sessions, save_ground_truth

pytest.importorskip("pokerkit")
from core.poker_engine import PokerkitGameState  # noqa: E402

SID = "2026-09-30_190000_session1"


class _Logger:
    """ボタン 席6 / SB 席4 / BB 席5（最初の手番は席6）。"""

    def __init__(self, tmp_path: Path, live: bool = True):
        self.dir = tmp_path
        self.gs = PokerkitGameState([PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
                                    sb=100, bb=200)
        self.t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=self.gs, json_writer=JsonWriter(tmp_path, SID),
            stop_event=threading.Event(), live_hand=live,
        )

    def event(self, action: str, text: str, seat=None) -> None:
        self.t._handle_audio_event(AudioEvent(action, 0, time.time(), text, seat=seat))   # noqa: SLF001
        self.t._publish_live_hand()                                                         # noqa: SLF001 — run() の 1 周

    def say(self, text: str) -> None:
        for event in parse_actions(text, confidence=0.9):
            self.t._handle_audio_event(event)    # noqa: SLF001
            self.t._publish_live_hand()          # noqa: SLF001

    def live(self):
        path = self.dir / f"{SID}.live_hand.json"
        return json.loads(path.read_text(encoding="utf-8"))["hand"] if path.is_file() else None

    def recorded(self) -> list[dict]:
        return json.loads((self.dir / f"{SID}.json").read_text(encoding="utf-8"))["hands"]


class TestLoggerWritesTheHandInProgress:
    def test_from_the_start_until_the_hand_ends(self, tmp_path):
        """席のリーダーが無い構成: 「ハンド開始」の時点から（プリフロップのうちから入れられる）。"""
        lg = _Logger(tmp_path)
        lg.t._json_writer.ensure_created()          # noqa: SLF001 — ロガーは起動時に作る
        lg.t._publish_live_hand()                   # noqa: SLF001
        assert lg.live() is None                     # ハンドの外は出さない
        lg.event("new_hand", "ハンド開始")
        live = lg.live()
        assert (live["hand_id"], live["street"], live["in_progress"], live["actions"]) == (1, "preflop", True, [])
        assert [p["seat"] for p in live["players"]] == [4, 5, 6]
        assert live["players"][0]["stack_start"] == 10000 and live["button_seat"] == 6
        lg.say("コール")
        lg.say("コール")
        assert [(a["seat"], a["action"]) for a in lg.live()["actions"]] == [(6, "call"), (4, "call")]
        lg.say("チェック")                            # プリフロップが閉じる = フロップ
        live = lg.live()
        assert (live["street"], len(live["actions"])) == ("flop", 3)
        assert lg.recorded() == []                   # ハンドの記録には入れない
        lg.say("チェック")
        assert len(lg.live()["actions"]) == 4        # 進むたびに書き直す
        lg.event("winner", "シート4 ウィナー", seat=4)
        assert lg.live() is None                     # 終わったら消す
        assert [h["hand_id"] for h in lg.recorded()] == [1]

    def test_replays_and_tests_do_not_write_it(self, tmp_path):
        lg = _Logger(tmp_path, live=False)
        lg.event("new_hand", "ハンド開始")
        for text in ("コール", "コール", "チェック"):
            lg.say(text)
        assert lg.gs.street == "flop" and lg.live() is None


def _live_of(log_dir: Path, sid: str) -> dict | None:
    path = log_dir / f"{sid}.live_hand.json"
    return json.loads(path.read_text(encoding="utf-8"))["hand"] if path.is_file() else None


class TestWithSeatReaders:
    """席のリーダーがある卓（店舗）: 手札が配られた時点から。"""

    def test_the_automatic_start_shows_it_when_the_cards_are_dealt(self, tmp_path):
        from tests.test_rfid_folds import HOLES, _Table

        tb = _Table(tmp_path)
        tb.t._live_hand = True                      # noqa: SLF001 — ライブのロガー
        tb.t._publish_live_hand()                   # noqa: SLF001
        assert _live_of(tmp_path, "folds") is None   # 配る前
        tb.deal()
        tb.t._publish_live_hand()                   # noqa: SLF001 — run() の 1 周
        live = _live_of(tmp_path, "folds")
        assert (live["hand_id"], live["street"], live["actions"]) == (1, "preflop", [])
        assert {p["seat"]: p["hole_cards"] for p in live["players"]} == HOLES

    def test_a_hand_started_by_n_waits_for_the_first_hole_card(self, tmp_path):
        from tests.test_hand_before_deal import _ManualTable

        tb = _ManualTable(tmp_path)
        tb.t._live_hand = True                      # noqa: SLF001
        tb.type_n()
        tb.t._publish_live_hand()                   # noqa: SLF001
        assert _live_of(tmp_path, "folds") is None   # n のあと、まだ配っていない
        tb.tick(2.0)
        tb.seat_card(4, "Jd")
        tb.t._publish_live_hand()                   # noqa: SLF001
        live = _live_of(tmp_path, "folds")
        assert (live["street"], live["actions"]) == ("preflop", [])
        assert next(p for p in live["players"] if p["seat"] == 4)["hole_cards"] == ["Jd"]


def _completed_hand() -> dict:
    return {
        "hand_id": 1, "session_id": SID, "started_at": "2026-09-30T19:00:00", "ended_at": "2026-09-30T19:01:00",
        "blinds": {"sb": 100, "bb": 200}, "board": ["As", "Kd", "7h"], "button_seat": 6,
        "players": [{"seat": s, "name": f"P{s}", "hole_cards": None, "stack_start": 10000} for s in (4, 5, 6)],
        "winner_seat": 5, "actions": [
            {"street": "preflop", "seat": 6, "action": "fold", "amount": 0},
            {"street": "preflop", "seat": 4, "action": "fold", "amount": 0},
        ], "review_required": False,
    }


def _live_hand(hand_id: int = 2) -> dict:
    return {
        "hand_id": hand_id, "session_id": SID, "started_at": "2026-09-30T19:02:00", "ended_at": None,
        "blinds": {"sb": 100, "bb": 200}, "board": ["2c", "9d", "Jh"], "button_seat": 4,
        "players": [{"seat": s, "name": f"P{s}", "hole_cards": None, "stack_start": 10000} for s in (4, 5, 6)],
        "winner_seat": None, "actions": [
            {"street": "preflop", "seat": 4, "action": "call", "amount": 200},
            {"street": "preflop", "seat": 5, "action": "call", "amount": 100},
            {"street": "preflop", "seat": 6, "action": "check", "amount": 0},
        ], "review_required": False, "street": "flop", "in_progress": True,
    }


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    (d / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [_completed_hand()]}), encoding="utf-8")
    (d / f"{SID}.live_hand.json").write_text(json.dumps({"session_id": SID, "hand": _live_hand()}), encoding="utf-8")
    return d


# 真のアクション（プリフロップまで入れた）
_PREFLOP = {
    "board": [], "players": [], "winner_seat": None, "notes": "", "blind": True,
    "actions": [
        {"seat": 4, "action": "call", "amount": 200, "street": "preflop"},
        {"seat": 5, "action": "call", "amount": 100, "street": "preflop"},
        {"seat": 6, "action": "check", "amount": 0, "street": "preflop"},
    ],
}


class TestPageShowsTheHandInProgress:
    def test_the_list_shows_it_on_top(self, log_dir):
        gt = GroundTruthRepository(log_dir)
        d = list_hands(log_dir, SID, gt)
        assert [(h["hand_id"], h.get("in_progress", False)) for h in d["hands"]] == [(2, True), (1, False)]
        assert d["hands"][0]["street"] == "flop" and d["hands"][0]["board"] == ["2c", "9d", "Jh"]
        assert d["summary"]["hands"] == 1                         # 集計は終わったハンドだけ
        assert [s["session_id"] for s in list_sessions(log_dir, gt)] == [SID]   # 進行中のファイルはセッションではない

    def test_it_opens_blind_and_cannot_be_passed_through(self, log_dir):
        gt = GroundTruthRepository(log_dir)
        detail = hand_detail(log_dir, SID, 2, gt, blind_every=1)
        assert detail["in_progress"] is True and detail["blind"] is True
        assert detail["legal"]["actions"] == []                  # ブラインド = 記録のアクションは出さない
        status, body = save_ground_truth(log_dir, SID, 2, {"source": "captured-passthrough"}, gt)
        assert status == 400 and "進行中" in body["message"]

    def test_a_partial_entry_is_kept_and_joins_the_finished_hand(self, log_dir):
        gt = GroundTruthRepository(log_dir)
        status, body = save_ground_truth(log_dir, SID, 2, {"source": "manual-edit", "hand": _PREFLOP}, gt)
        assert status == 200 and body["in_progress"] is True and body["accuracy"] is None
        again = hand_detail(log_dir, SID, 2, gt, blind_every=1)
        assert again["blind"] is True                             # 途中で保存してもブラインドのまま
        assert [a["seat"] for a in again["legal"]["actions"]] == [4, 5, 6]   # 入れたところから続ける
        # ハンドが終わった: 記録に入り、進行中のファイルは消える
        finished = dict(_live_hand(), ended_at="2026-09-30T19:03:00", winner_seat=4)
        finished.pop("in_progress")
        (log_dir / f"{SID}.json").write_text(
            json.dumps({"session_id": SID, "hands": [_completed_hand(), finished]}), encoding="utf-8")
        (log_dir / f"{SID}.live_hand.json").unlink()
        done = hand_detail(log_dir, SID, 2, gt, blind_every=1)
        assert done["in_progress"] is False and done["blind"] is False        # 記録と照らし合わせる
        assert done["ground_truth"]["blind"] is True and not done["ground_truth"].get("reconciled")
        rows = list_hands(log_dir, SID, gt)["hands"]
        assert [h["hand_id"] for h in rows] == [2, 1] and not rows[0].get("in_progress")

    def test_a_preflop_hand_shows_before_the_flop(self, log_dir):
        """プリフロップ（ボード空・アクション 0 件）の進行中のハンドも一覧に出て、開いて途中まで保存できる。"""
        preflop = dict(_live_hand(), board=[], actions=[], street="preflop")
        (log_dir / f"{SID}.live_hand.json").write_text(json.dumps({"session_id": SID, "hand": preflop}),
                                                       encoding="utf-8")
        gt = GroundTruthRepository(log_dir)
        row = list_hands(log_dir, SID, gt)["hands"][0]
        assert (row["hand_id"], row["in_progress"], row["street"], row["board"]) == (2, True, "preflop", [])
        detail = hand_detail(log_dir, SID, 2, gt, blind_every=1)
        assert detail["in_progress"] is True and detail["blind"] is True
        first = dict(_PREFLOP, actions=_PREFLOP["actions"][:1])
        status, body = save_ground_truth(log_dir, SID, 2, {"source": "manual-edit", "hand": first}, gt)
        assert status == 200 and body["in_progress"] is True

    def test_a_stale_file_for_a_finished_hand_is_ignored(self, log_dir):
        (log_dir / f"{SID}.live_hand.json").write_text(
            json.dumps({"session_id": SID, "hand": _live_hand(hand_id=1)}), encoding="utf-8")
        rows = list_hands(log_dir, SID, GroundTruthRepository(log_dir))["hands"]
        assert [(h["hand_id"], h.get("in_progress", False)) for h in rows] == [(1, False)]

    def test_page_marks_the_row_and_skips_the_record_while_in_progress(self):
        from tools.ground_truth_ui import _PAGE

        assert "進行中（" in _PAGE and "t-live" in _PAGE
        assert "d.in_progress" in _PAGE and "!S.hand.in_progress" in _PAGE
