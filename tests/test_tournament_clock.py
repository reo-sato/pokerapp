"""トーナメントの時計をロガー（IntegrationThread）につないだ動き（ADR-0071）: レベルの時間で次のハンドからブラインド・
アンテが上がる・時計の操作・記録の先頭の卓の設定・replay で同じ記録になる・卓モニタの表示。"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from audio.recognizer import parse_actions  # noqa: E402
from core.blind_structure import TournamentClock, parse_structure  # noqa: E402
from core.event_queue import make_audio_queue  # noqa: E402
from core.events import AudioEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from core.poker_engine import create_game_state  # noqa: E402
from integration.engine import IntegrationThread  # noqa: E402
from integration.replay import load_events, load_session_setup, replay_events  # noqa: E402
from output.event_recorder import EventRecorder  # noqa: E402
from output.json_writer import JsonWriter  # noqa: E402

STRUCTURE = parse_structure({"name": "t", "levels": [
    {"sb": 100, "bb": 200, "ante": 0, "minutes": 1},
    {"sb": 200, "bb": 400, "ante": 300, "minutes": 1},
    {"break": True, "minutes": 1},
    {"sb": 300, "bb": 600, "ante": 600, "minutes": 1},
]})
T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


def _players():
    return [PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (1, 2, 3)]


class Live:
    """ライブのループの代わり: 時刻を進めて、周期の時計の確認と入力を流す。"""

    def __init__(self, tmp_path: Path, *, tournament: bool = True) -> None:
        self.now = T0
        self.notes: list[str] = []
        self.saved: list[int] = []
        self.recorder = EventRecorder(tmp_path / "s.events.jsonl")
        self.recorder.record_setup({"table_mode": "tournament", "sb": 100, "bb": 200, "ante": 0}, T0)
        self.clock = TournamentClock.start(STRUCTURE, T0) if tournament else None
        self.thread = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=create_game_state("pokerkit", _players(), 100, 200),
            json_writer=JsonWriter(tmp_path, "s"), clock=lambda: self.now, stop_event=threading.Event(),
            event_recorder=self.recorder, auto_winner=True, on_notice=self.notes.append,
            table_mode="tournament", tournament=self.clock,
            on_clock_change=lambda c: self.saved.append(c.index))
        if self.clock is not None:
            self.thread._emit_level(self.now)

    def at(self, t: float) -> None:
        self.now = T0 + t
        self.thread._check_tournament_clock()

    def send(self, event: AudioEvent) -> None:
        self.at(event.timestamp - T0)
        self.thread._record(event)
        self.thread._handle_audio_event(event)

    def say(self, t: float, text: str) -> None:
        for ev in parse_actions(text, confidence=1.0):
            ev.timestamp = ev.utterance_start_ts = T0 + t
            self.send(ev)

    def control(self, t: float, action: str, raw: str = "") -> None:
        self.send(AudioEvent(action=action, amount=0, timestamp=T0 + t, raw_text=raw))

    def hands(self, tmp_path: Path) -> list[dict]:
        return json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["hands"]


def _fold_hand(live: Live, t: float) -> None:
    live.control(t, "new_hand")
    live.say(t + 1, "フォールド フォールド")


class TestLevels:
    def test_level_up_mid_hand_waits_for_the_next_hand(self, tmp_path):
        live = Live(tmp_path)
        live.control(10, "new_hand")
        live.at(65)                                    # レベル 2 の時間（ハンドの途中）
        live.say(66, "フォールド フォールド")
        live.say(68, "ブラインド上がります。SB200、BB400です")
        _fold_hand(live, 70)
        first, second = live.hands(tmp_path)
        assert first["blinds"] == {"sb": 100, "bb": 200, "level": 1} and first["table_mode"] == "tournament"
        assert second["blinds"] == {"sb": 200, "bb": 400, "bb_ante": 300, "level": 2}
        assert second["pot_total"] == 200 + 400 + 300
        assert any("レベル 2" in n and "時間です" in n for n in live.notes)
        assert live.saved == [1]

    def test_break_keeps_blinds_then_next_level(self, tmp_path):
        live = Live(tmp_path)
        live.at(125)                                   # ブレイク
        assert any("ブレイク" in n for n in live.notes)
        live.say(126, "ブラインド上がります")
        _fold_hand(live, 130)                          # ブレイク中のハンドはレベル 2 のまま
        live.at(185)
        live.say(186, "ブラインド上がります")
        _fold_hand(live, 190)
        a, b = live.hands(tmp_path)
        assert a["blinds"]["level"] == 2 and b["blinds"] == {"sb": 300, "bb": 600, "bb_ante": 600, "level": 3}

    def test_clock_commands(self, tmp_path):
        live = Live(tmp_path)
        live.control(5, "clock", "pause")
        live.at(500)
        assert live.clock.index == 0                   # 止まっているあいだは上がらない
        live.control(501, "clock", "next")
        assert live.clock.index == 1 and live.clock.paused
        live.control(502, "clock", "resume")
        live.control(503, "clock", "set 3 0.5")
        assert live.clock.current.level == 3 and live.clock.remaining(live.now) == 30
        live.control(504, "clock", "")
        assert "t レベル 3: 300/600（アンテ 600）・残り 0:29・最後のレベル" == live.notes[-1]
        _fold_hand(live, 510)
        (hand,) = live.hands(tmp_path)
        assert hand["blinds"]["level"] == 3

    def test_clock_without_tournament_only_notices(self, tmp_path):
        live = Live(tmp_path, tournament=False)
        live.control(1, "clock", "next")
        assert "時計はありません" in live.notes[-1]

    def test_ante_amount_command(self, tmp_path):
        live = Live(tmp_path, tournament=False)
        live.control(1, "set_bb_ante", "500")
        _fold_hand(live, 2)
        (hand,) = live.hands(tmp_path)
        assert hand["blinds"]["bb_ante"] == 500 and hand["pot_total"] == 100 + 200 + 500


class TestAnnouncement:
    """時計がレベルを越えたあと、最初に始まるハンドの最初のアクションまでにブラインド上昇の宣言があればそのハンドから、
    無ければその次のハンドから上げる（2026-10-28 オーナー。10/26 は時計がロガーのほうが 1〜2 分早かった）。"""

    def _levels(self, live, tmp_path):
        return [h["blinds"]["level"] for h in live.hands(tmp_path)]

    def test_without_an_announcement_it_waits_one_more_hand(self, tmp_path):
        live = Live(tmp_path)
        live.at(65)
        _fold_hand(live, 70)                            # 遷移のあと最初のハンド: 宣言なし → まだ前のレベル
        _fold_hand(live, 80)                            # その次のハンドから上げる
        assert self._levels(live, tmp_path) == [1, 2]

    def test_announced_before_the_deal(self, tmp_path):
        live = Live(tmp_path)
        live.at(65)
        live.say(66, "次のハンドからブラインドが上がります")
        _fold_hand(live, 70)
        assert self._levels(live, tmp_path) == [2]

    def test_announced_after_the_deal_before_the_first_action(self, tmp_path):
        live = Live(tmp_path)
        live.at(65)
        live.control(70, "new_hand")
        live.say(71, "ブラインド上がってます。SB200、BB400")   # ハンドを新しいブラインドで始め直す
        live.say(72, "フォールド フォールド")
        (hand,) = live.hands(tmp_path)
        assert hand["blinds"]["level"] == 2 and hand["pot_total"] == 200 + 400 + 300
        assert any("このハンドからレベル 2" in n for n in live.notes)

    def test_announced_after_the_first_action_is_for_the_next_hand(self, tmp_path):
        live = Live(tmp_path)
        live.at(65)
        live.control(70, "new_hand")
        live.say(71, "フォールド")
        live.say(72, "ブラインド上がります")
        live.say(73, "フォールド")
        _fold_hand(live, 80)
        assert self._levels(live, tmp_path) == [1, 2]

    def test_the_replay_decides_the_same(self, tmp_path):
        live = Live(tmp_path)
        live.at(65)
        live.control(70, "new_hand")
        live.say(71, "ブラインド上がってます")
        live.say(72, "フォールド フォールド")
        _fold_hand(live, 80)
        recorded = live.hands(tmp_path)
        hands = replay_events(load_events(tmp_path / "s.events.jsonl"), backend="pokerkit", players=_players(),
                              sb=100, bb=200, session_id="s", out_dir=tmp_path / "replay", auto_winner=True,
                              table_mode="tournament")
        assert [h.to_dict() for h in hands] == recorded
        assert [h["blinds"]["level"] for h in recorded] == [2, 2]


class TestReplay:
    def test_recorded_levels_replay_to_the_same_hands(self, tmp_path):
        live = Live(tmp_path)
        _fold_hand(live, 10)
        live.at(65)
        live.say(66, "ブラインド上がります")
        _fold_hand(live, 70)
        live.control(80, "clock", "set 3")
        live.at(81)
        _fold_hand(live, 90)
        recorded = live.hands(tmp_path)
        events_path = tmp_path / "s.events.jsonl"
        assert load_session_setup(events_path)["table_mode"] == "tournament"
        out = tmp_path / "replay"
        hands = replay_events(load_events(events_path), backend="pokerkit", players=_players(), sb=100, bb=200,
                              session_id="s", out_dir=out, auto_winner=True, table_mode="tournament")
        assert [h.to_dict() for h in hands] == recorded
        assert [h["blinds"].get("level") for h in recorded] == [1, 2, 3]


class TestTableInfo:
    def test_info_for_the_monitor(self, tmp_path):
        live = Live(tmp_path)
        live.at(30)
        info = live.thread.table_info()
        assert info["mode"] == "tournament" and info["clock"]["ends_at"] == T0 + 60
        assert info["clock"]["next"]["bb"] == 400 and info["clock"]["next_break_at"] == T0 + 120


def test_records_match_the_schemas(tmp_path):
    """記録の先頭の卓の設定・set_level / clock の入力・ハンドの記録が契約の schema に通る（ADR-0071）。"""
    jsonschema = pytest.importorskip("jsonschema")
    root = Path(__file__).resolve().parent.parent / "docs/contracts/schemas"
    event_schema = json.loads((root / "reconstruction_event.schema.json").read_text(encoding="utf-8"))
    hand_schema = json.loads((root / "hand.schema.json").read_text(encoding="utf-8"))
    live = Live(tmp_path)
    live.recorder.record_setup({"table_mode": "tournament", "sb": 100, "bb": 200, "ante": 0,
                                "structure": STRUCTURE.to_dict(), "level": 1, "elapsed_min": 0.0,
                                "seats": [{"seat": 1, "name": "P1", "stack": 10000}], "first_button": 3}, T0)
    _fold_hand(live, 10)
    live.control(20, "clock", "next")
    _fold_hand(live, 30)
    for line in (tmp_path / "s.events.jsonl").read_text(encoding="utf-8").splitlines():
        jsonschema.validate(json.loads(line), event_schema)
    for hand in live.hands(tmp_path):
        jsonschema.validate(hand, hand_schema)


def test_cli_text_setup_starts_a_tournament(tmp_path, monkeypatch, capsys):
    """端末の入力（t）でトーナメントを選ぶと、記録の先頭に卓の設定・始めのレベルのアンテ・時計の保存が残る。"""
    import main
    from core import blind_structure

    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"structures": [STRUCTURE.to_dict() | {"starting_stack": 5000}]}), encoding="utf-8")
    monkeypatch.setattr(blind_structure, "_DEFAULT_FILE", tmp_path / "none.json")
    monkeypatch.setattr(blind_structure, "_SAMPLE_FILE", sample)
    logs = tmp_path / "logs"
    cfg = {"audio": {"enabled": False}, "rfid": {"enabled": False}, "engine": {"backend": "pokerkit"},
           "table_state": {"enabled": False}, "recording": {"enabled": True}}
    # モード t・ストラクチャー 1・レベル 2・経過 0.5 分・3 席（持ち点は空 = 5000）・ボタン空・ログ
    answers = iter(["t", "1", "2", "0.5", "3", "", "", "", "", "", "", "", str(logs),
                    "n", "フォールド フォールド", "clock", "q"])
    monkeypatch.setattr("core.config.load_config", lambda path=None: cfg)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    main.run_cli()
    out = capsys.readouterr().out
    assert "トーナメント: t レベル 2 200/400（アンテ 300）・残り 0:" in out
    [record] = [p for p in logs.glob("*.json") if p.name.count(".") == 1 and "tournament" not in p.name
                and not p.name.startswith(("last_", "tournament_"))]
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["table"]["table_mode"] == "tournament" and data["table"]["level"] == 2
    assert [p["stack"] for p in data["table"]["seats"]] == [5000, 5000, 5000]
    (hand,) = data["hands"]
    assert hand["blinds"] == {"sb": 200, "bb": 400, "bb_ante": 300, "level": 2} and hand["table_mode"] == "tournament"
    setup = load_session_setup(logs / f"{record.stem}.events.jsonl")
    assert setup["structure"]["name"] == "t"
    assert json.loads((logs / "tournament_clock.json").read_text(encoding="utf-8"))["index"] == 1
