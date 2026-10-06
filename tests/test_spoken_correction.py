"""tests/test_spoken_correction.py

口頭の訂正「失礼しました」（オーナー 2026-10-06）。ハンドのアクションを最初からやり直すことはない。ハンドの進行を
優先して、ストリートの最初からやり直すか、直前のアクションを訂正する。「失礼しました」を合図に、そのあとのアクション
との辻褄も含めてより自然な方を選ぶ（会話の「失礼しました」なら何も変えない）。

- 合図の読み: 発話のどこにあってもよい（前は直す前、後ろは直したあとのアクション）。「失礼失礼」「失礼します」は合図に
  しない。店舗: 台本の「600点」→「失礼しました。800点。」、7b897671 ハンド 3 の「失礼しました。これでお願いします。」。
- engine: 解釈（直前の訂正 / ストリートの言い直し / 訂正なし）ごとにハンドを組み直し、合図のあとの記録の不自然さ
  （使えない額・補ったアクション・札が配られる前の語が次のストリートに入った…）+ 解釈の重みで選ぶ。合図のあとの
  発話ごとに選び直し、次のストリートの札・ハンドの終わりで決める。replay も同じ。そのハンドは要確認。
"""
from __future__ import annotations

import queue
import threading

import pytest

from audio.recognizer import CORRECTION, parse_actions
from core.events import AudioEvent, RFIDEvent
from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from core.poker_engine import PokerkitGameState  # noqa: E402
from integration.engine import IntegrationThread  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402


def _actions(text: str) -> list[tuple]:
    return [(e.action, e.amount) for e in parse_actions(text)]


def _board(tb: _Table, index: int, card: str) -> None:
    ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                   timestamp=tb.now, raw_tag_id=card, board_index=index)
    tb.recorder.record(ev)
    tb.t._process_rfid_event(ev)   # noqa: SLF001


def _flop(tb: _Table) -> None:
    for i, card in enumerate(["2c", "7d", "9s"], start=1):
        _board(tb, i, card)
    tb.tick(tb.now + 3)


def _choice(tb: _Table) -> str:
    return tb.t._corrections[-1]["choice"]   # noqa: SLF001


class TestReadingTheCue:
    @pytest.mark.parametrize("text", [
        "失礼しました", "失礼しました。", "失礼いたしました", "大変失礼しました", "しつれいしました",
        "あ、失礼しました。", "失礼しました。これでお願いします。",   # 7b897671 ハンド 3
    ])
    def test_the_cue(self, text):
        assert _actions(text) == [(CORRECTION, 0)]

    @pytest.mark.parametrize("text", [
        "アクションじゃない? プロプのアクションもあります。 ごめんごめん。失礼失礼。",   # fded6f75
        "失礼します", "失礼", "ごめんなさい", "すみません", "失礼しました?",
    ])
    def test_conversation_is_not_the_cue(self, text):
        assert CORRECTION not in [a for a, _ in _actions(text)]

    def test_actions_before_and_after_the_cue(self):
        assert _actions("失礼しました。800点。") == [(CORRECTION, 0), ("bet", 800)]   # 台本 10/01 ハンド 23
        assert _actions("コール、失礼しました、レイズ 1800") == [("call", 0), (CORRECTION, 0), ("raise", 1800)]


class TestPreviousAction:
    def test_the_amount_of_the_previous_action(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("600")                               # 席6（ボタン）
        tb.tick(tb.now + 2)
        tb.say("失礼しました。800点。")
        assert tb.played() == [("preflop", 6, "raise", 800)]
        assert any("直前のアクション（席6 の raise 600「600」）を取り消しました" in n for n in tb.notices)
        tb.say("コール")
        tb.say("コール")
        assert tb.played() == [("preflop", 6, "raise", 800), ("preflop", 4, "call", 700),
                               ("preflop", 5, "call", 600)]
        record = tb.t._current_actions[0]           # noqa: SLF001
        assert "correction_prev" in record.reason and record.needs_review
        assert tb.t._hand_needs_review              # noqa: SLF001

    def test_the_previous_players_action(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                        # 席6
        tb.say("コール")                            # 席4（本当はレイズ）
        tb.say("失礼しました")
        assert tb.played() == [("preflop", 6, "raise", 600)]
        tb.say("レイズ 1800")
        tb.say("コール")
        tb.say("コール")
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "raise", 1800),
                               ("preflop", 5, "call", 1600), ("preflop", 6, "call", 1200)]
        assert _choice(tb) == "prev"               # どちらの解釈でも辻褄が合う → 直前の訂正


class TestStreetRestated:
    def test_words_spoken_before_the_next_card_choose_the_street(self, tmp_path):
        """フロップの「チェック」「ベット 500」を言い直す（席5 のチェックを言い落とした）。直前の訂正だと言い直しが
        ターンの札の前にターンのアクションになる → ターンの札でストリートの言い直しに組み直す。"""
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール")
        tb.say("コール")
        tb.say("チェック")
        tb.tick(tb.now + 2)
        _flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2)
        tb.say("ベット 500")
        tb.tick(tb.now + 2)
        tb.say("失礼しました")
        tb.tick(tb.now + 1)
        tb.say("チェック、チェック、ベット 500")
        tb.tick(tb.now + 2)
        tb.say("コール")
        tb.tick(tb.now + 1)
        tb.say("フォールド")
        tb.lift(5)
        tb.tick(tb.now + 4)
        _board(tb, 4, "Kh")
        tb.tick(tb.now + 2)
        tb.say("チェック")
        flop = [p for p in tb.played() if p[0] == "flop"]
        assert flop == [("flop", 4, "check", 0), ("flop", 5, "check", 0), ("flop", 6, "bet", 500),
                        ("flop", 4, "call", 500), ("flop", 5, "fold", 0)]
        assert tb.played()[-1] == ("turn", 4, "check", 0)
        corr = tb.t._corrections[-1]                # noqa: SLF001
        assert corr["choice"] == "street" and corr["frozen"]
        assert any("フロップの最初からの言い直しとして組み直しました" in n for n in tb.notices)

    def test_a_seat_that_left_before_the_cue_folds_on_its_turn(self, tmp_path):
        """プリフロップを言い直す。合図の前に札が離れた席4 は、言い直しの手番でフォールド。言い直した「フォールド」は
        その離脱と組にして、次の人に付けない。"""
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                        # 席6
        tb.tick(tb.now + 1)
        tb.lift(4)
        tb.tick(tb.now + 3.5)                       # 席4 フォールド（札の離脱）
        tb.say("コール")                            # 席5
        tb.tick(tb.now + 2)
        tb.say("失礼しました")
        tb.tick(tb.now + 1)
        tb.say("レイズ 600")
        tb.say("フォールド")
        tb.say("コール")
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0),
                               ("preflop", 5, "call", 400)]
        assert _choice(tb) == "street"
        assert any("札が離れた席4 のフォールドの言い直し" in n for n in tb.notices)


class TestConversation:
    def test_the_next_players_action_keeps_the_record(self, tmp_path):
        """会話の「失礼しました」のあとに次の人のコール。直前の訂正だと席5 のアクションが聞こえていないことになる
        （フロップの札で補う）→ 訂正なし。"""
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.say("コール")
        tb.say("失礼しました")
        tb.say("コール")
        tb.tick(tb.now + 3)
        _flop(tb)
        tb.say("チェック")
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "call", 500),
                               ("preflop", 5, "call", 400), ("flop", 4, "check", 0)]
        assert _choice(tb) == "none"


class TestNothingToCorrect:
    def test_outside_a_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.say("失礼しました")
        assert any("訂正する進行中のハンドがありません" in n for n in tb.notices)

    def test_at_the_start_of_a_new_street(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール")
        tb.say("コール")
        tb.say("チェック")
        tb.tick(tb.now + 2)
        _flop(tb)
        before = tb.played()
        tb.say("失礼しました")
        assert tb.played() == before
        assert any("このストリートに訂正する声のアクションがありません" in n for n in tb.notices)


class TestReplay:
    def test_the_replay_gives_the_same_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.tick(tb.now + 1)
        tb.lift(4)
        tb.tick(tb.now + 3.5)
        tb.say("コール")
        tb.tick(tb.now + 2)
        tb.say("失礼しました")
        tb.tick(tb.now + 1)
        tb.say("レイズ 600")
        tb.say("フォールド")
        tb.say("コール")
        tb.tick(tb.now + 2)
        _flop(tb)
        tb.say("ベット 1000")                       # 席5
        tb.lift(6)
        tb.tick(tb.now + 15)                        # 席6 フォールド → 席5 の勝ち
        assert tb.hands, "ハンドが確定していない"
        live = [(a.street, a.seat, a.action, a.amount) for h in tb.hands for a in h.actions]
        assert live[:3] == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400)]
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert [(a.street, a.seat, a.action, a.amount) for h in replayed for a in h.actions] == live
        assert replayed[-1].review_required


class TestVoiceOnly:
    """札を読まない卓（台本・声だけ）: フォールドは声で決まり、全員フォールドのハンドの確定は解釈を決めてから。"""

    def _thread(self):
        hands, notices = [], []
        gs = PokerkitGameState([PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)], sb=100, bb=200)

        class _Writer:
            _session_id = "voice"

            def append_hand_summary(self, summary):
                pass

        t = IntegrationThread(audio_queue=queue.Queue(), game_state=gs, json_writer=_Writer(),
                              on_hand=hands.append, on_notice=notices.append, stop_event=threading.Event(),
                              auto_winner=True)
        clock = [1000.0]

        def say(text):
            clock[0] += 2.0
            for event in parse_actions(text, confidence=0.9, utterance_start_ts=clock[0]):
                event.timestamp = clock[0] + 0.5
                t._handle_audio_event(event)   # noqa: SLF001
        say("ハンド開始")
        return t, say, hands

    def test_the_hand_ends_after_the_interpretation(self):
        t, say, hands = self._thread()
        say("レイズ 600")                           # 席6
        say("コール")                               # 席4（本当はフォールド）
        say("失礼しました")
        say("フォールド")                           # 席4
        say("フォールド")                           # 席5 → 席6 の勝ち
        assert hands, "ハンドが確定していない"
        assert [(a.seat, a.action, a.amount) for a in hands[-1].actions] == [
            (6, "raise", 600), (4, "fold", 0), (5, "fold", 0)]
        assert hands[-1].winner_seat == 6 and hands[-1].review_required

    def test_control_words_are_not_dropped(self):
        t, say, hands = self._thread()
        say("レイズ 600")
        say("失礼しました、レイズ 800")
        t._handle_audio_event(AudioEvent(           # noqa: SLF001 — `w 6`（勝者を打った）
            action="winner", amount=0, timestamp=2000.0, raw_text="w 6", seat=6))
        assert hands and hands[-1].winner_seat == 6
        assert [(a.seat, a.action, a.amount) for a in hands[-1].actions][0] == (6, "raise", 800)


class TestRepeatedCue:
    def test_a_repeated_cue_retracts_only_one_action(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.say("コール")
        tb.say("失礼しました、失礼しました")
        assert tb.played() == [("preflop", 6, "raise", 600)]
        tb.say("レイズ 1800")
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "raise", 1800)]
        assert len(tb.t._corrections) == 1          # noqa: SLF001

    def test_a_second_correction_after_more_actions(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("600")
        tb.say("失礼しました。800点。")             # 席6 の額の訂正
        tb.say("コール")                            # 席4（本当は 1600 のレイズ）
        tb.say("失礼しました。1600")                # 席4 の訂正
        tb.say("コール")
        tb.say("コール")
        assert tb.played() == [("preflop", 6, "raise", 800), ("preflop", 4, "raise", 1600),
                               ("preflop", 5, "call", 1400), ("preflop", 6, "call", 800)]
        assert [c["choice"] for c in tb.t._corrections] == ["prev", "prev"]   # noqa: SLF001
