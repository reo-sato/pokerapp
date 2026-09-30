"""tests/test_corpus_0930_readings.py

店舗の読み上げ集（2026-09-30, 2 人 × 163 句, 音楽を流した店内）で読めていなかった書き起こし。
"""
from __future__ import annotations

import pytest

from audio.recognizer import is_announcement, parse_actions
from core.positions import parse_position
from tools.audio_check import RDP_AUDIO_HINT, InputDevice, WDM_KS_OPEN_HINT, only_wdm_ks, open_error_hint


def _read(text: str) -> list[tuple]:
    return [(e.action, e.amount, e.position, e.hand_name) for e in parse_actions(text)]


class TestWords:
    def test_check_raise_with_a_long_vowel(self):
        """「チェックレーズ 2千」を「チェック」+ 額と読んでいた。"""
        assert _read("チェックレーズ 2千") == [("raise", 2000, None, None)]

    @pytest.mark.parametrize("text, name", [("プルハウス", "Full house"), ("二ペア", "Two pair"),
                                            ("三カード", "Three of a kind"), ("四カード", "Four of a kind")])
    def test_hand_names(self, text, name):
        assert _read(text) == [("end_hand", 0, None, name)]

    @pytest.mark.parametrize("text, position", [
        ("ビックブラインド レイズ1200", "BB"), ("カット オフ コール", "CO"), ("ATG フォールド", "UTG"),
        ("UDG フォールド", "UTG"),
    ])
    def test_position_spellings(self, text, position):
        assert parse_actions(text)[0].position == position

    def test_ascii_position_spellings_need_a_boundary(self):
        assert parse_position("ATGC") is None and parse_position("budget") is None


class TestAnnouncements:
    @pytest.mark.parametrize("text", ["ブラインド200、オールイン400です。", "ブラインド 200 400 です", "ブラインド二百、四百"])
    def test_a_blind_announcement_is_not_an_action(self, text):
        assert parse_actions(text) == [] and is_announcement(text)

    @pytest.mark.parametrize("text", ["ポット1万2000です。", "ポット 3000"])
    def test_a_pot_announcement(self, text):
        assert parse_actions(text) == [] and is_announcement(text)

    @pytest.mark.parametrize("text", ["ビッグブラインド レイズ 1200", "スモールブラインド コール", "レイズ 600", "ご視聴ありがとうございました。"])
    def test_not_an_announcement(self, text):
        assert not is_announcement(text)


def _amounts(text: str) -> list[tuple]:
    return [(e.action, e.amount, e.parse_flags) for e in parse_actions(text)]


class TestAmountNextToAWord:
    """Whisper は「、」の代わりに空白で書くことがある（「コール 3000」= コールのあとに次の人の 3000）。
    額だけの運用では 3000 を失い、以降の手番がずれていた。"""

    @pytest.mark.parametrize("text, expected", [
        ("コール 3000", [("call", 0, ()), ("bet", 3000, ("amount_only",))]),
        ("コール　3000", [("call", 0, ()), ("bet", 3000, ("amount_only",))]),        # 全角の空白
        ("3000 コール", [("bet", 3000, ("amount_only",)), ("call", 0, ())]),
        ("チェック 800", [("check", 0, ()), ("bet", 800, ("amount_only",))]),
        ("600点 コール", [("bet", 600, ("amount_only",)), ("call", 0, ())]),
        ("コール 3000 コール", [("call", 0, ()), ("bet", 3000, ("amount_only",)), ("call", 0, ())]),
    ])
    def test_a_space_next_to_the_word_separates(self, text, expected):
        assert _amounts(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("600点コールです", [("call", 600, ())]),          # 区切らずに言った額はコールの額のまま
        ("コールです 3000", [("call", 3000, ())]),          # 語の隣ではない
        ("1万 2000 コール", [("call", 10000, ())]),          # 額の中の空白は変えない（前と同じ）
    ])
    def test_otherwise_unchanged(self, text, expected):
        assert _amounts(text) == expected

    def test_seats_and_positions_stay_with_the_call(self):
        (call, amount) = parse_actions("シート3 コール 3000")
        assert (call.action, call.seat, amount.amount) == ("call", 3, 3000)
        (call, amount) = parse_actions("BTN コール 3000")
        assert (call.position, amount.amount) == ("BTN", 3000)

    @pytest.mark.parametrize("text, amounts", [
        ("500、1500", [500, 1500]),                  # ベットのあとにレイズ（どちらも読まずに捨てていた）
        ("600点、1800点", [600, 1800]),
        ("500、1500、1500です", [500, 1500]),         # 同じ額の言い直しは 1 つ
        ("1500、500", []),                           # 額が下がる = 賭けの並びではない
        ("5、6、7", []),                              # ブラインドより小さい
        ("ポット、2千点", []),
    ])
    def test_amounts_in_turn(self, text, amounts):
        events = parse_actions(text)
        assert [e.amount for e in events] == amounts
        assert all(e.parse_flags == ("amount_only",) for e in events)

    @pytest.mark.parametrize("text", ["1500、トータル 1500", "トータル 1500"])
    def test_total(self, text):
        """「トータル」はレイズの額がトータルだと言い添えたもの（額だけの発話のまま）。"""
        assert _amounts(text) == [("bet", 1500, ("amount_only",))]


class TestAmountNextToAWordAtTheTable:
    """ボタン 席6（最初の手番）/ SB 席4 / BB 席5。持ち点はどの席も 10000。"""

    def _say(self, tmp_path, *said: str):
        from tests.test_rfid_folds import _Table
        from tests.test_store_2026_09_27 import STORE_HOLES

        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        for text in said:
            tb.say(text)
            tb.tick(tb.now + 2.0)
        return tb

    def test_the_next_raise_after_a_call(self, tmp_path):
        tb = self._say(tmp_path, "600", "コール 3000", "フォールド", "コール")
        assert [(a.seat, a.action, a.amount) for a in tb.t._current_actions] == [   # noqa: SLF001
            (6, "raise", 600), (4, "call", 500), (5, "raise", 3000), (6, "fold", 0), (4, "call", 2400)]

    def test_the_called_amount_is_not_a_raise(self, tmp_path):
        tb = self._say(tmp_path, "600", "コール 600", "コール")
        assert [(a.seat, a.action, a.amount) for a in tb.t._current_actions] == [   # noqa: SLF001
            (6, "raise", 600), (4, "call", 500), (5, "call", 400)]

    def test_the_called_amount_after_the_round_closes_is_not_the_next_bet(self, tmp_path):
        """「コール 2千5百」（2500 へのコールの額の言い直し）でコールがラウンドを閉じると、額がフロップのベットに
        なっていた（店舗 2026-09-29 fded6f75 の 4 ハンド目）。次のストリートは札を配ってからなので、同じ発話の額は
        次のストリートのベットではない。"""
        tb = self._say(tmp_path, "600", "2500", "フォールド", "コール 2千5百")
        assert [(a.street, a.seat, a.action, a.amount) for a in tb.t._current_actions] == [   # noqa: SLF001
            ("preflop", 6, "raise", 600), ("preflop", 4, "raise", 2500), ("preflop", 5, "fold", 0),
            ("preflop", 6, "call", 1900)]
        assert any("同じ発話の額" in n for n in tb.notices)

    def test_a_bet_in_the_next_utterance_is_kept(self, tmp_path):
        tb = self._say(tmp_path, "600", "2500", "フォールド", "コール", "2500")
        assert [(a.street, a.seat, a.action, a.amount) for a in tb.t._current_actions][-1] == (
            "flop", 4, "bet", 2500)


class TestPlayersLeft:
    """「スリープレイヤーズ」= 残り 3 人（ディーラーがストリートの移りに言う。任意, オーナー 2026-09-30）。
    数を額と読んでいた（「コール 3プレイヤーズ」= コール 3）。"""

    @pytest.mark.parametrize("text, expected", [
        ("スリープレイヤーズ", [("players_left", 3)]),
        ("3プレイヤーズ", [("players_left", 3)]),
        ("フォー プレーヤーズ", [("players_left", 4)]),
        ("三プレイヤーズ", [("players_left", 3)]),
        ("3 players", [("players_left", 3)]),
        ("スリーウェイ", [("players_left", 3)]),
        ("ツープレイヤーズ", [("heads_up", 0)]),
        ("コール 3プレイヤーズ", [("call", 0), ("players_left", 3)]),
        ("コール、スリープレイヤーズ", [("call", 0), ("players_left", 3)]),
        ("3プレイヤーズ、チェック", [("players_left", 3), ("check", 0)]),
        ("スリープレイヤーズ、ターンカード", [("players_left", 3)]),
        ("フォールド", [("fold", 0)]),
    ])
    def test_reading(self, text, expected):
        assert [(e.action, e.amount) for e in parse_actions(text)] == expected

    def _table(self, tmp_path, *said: str):
        from tests.test_rfid_folds import _Table
        from tests.test_store_2026_09_27 import STORE_HOLES

        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        for text in said:
            tb.say(text)
            tb.tick(tb.now + 2.0)
        return tb

    def test_the_unheard_call_is_filled_in(self, tmp_path):
        """ボタン 席6 / SB 席4 / BB 席5。席5 のコールが聞き取れず、ディーラーが「スリープレイヤーズ」。"""
        tb = self._table(tmp_path, "600", "コール", "スリープレイヤーズ")
        acts = tb.t._current_actions   # noqa: SLF001
        assert [(a.street, a.seat, a.action, a.amount) for a in acts] == [
            ("preflop", 6, "raise", 600), ("preflop", 4, "call", 500), ("preflop", 5, "call", 400)]
        assert (acts[-1].actor_source, acts[-1].needs_review, acts[-1].reason) == (
            "implied", False, "players_left_call")
        assert tb.gs.street == "flop"

    def test_two_unheard_calls(self, tmp_path):
        tb = self._table(tmp_path, "600", "スリープレイヤーズ")
        assert [(a.seat, a.action, a.amount) for a in tb.t._current_actions] == [   # noqa: SLF001
            (6, "raise", 600), (4, "call", 500), (5, "call", 400)]

    def test_more_players_than_said(self, tmp_path):
        tb = self._table(tmp_path, "600", "コール", "ヘッズアップ")
        assert len(tb.t._current_actions) == 2                     # noqa: SLF001
        assert any("まだ 3 人残っています" in n for n in tb.notices)

    def test_fewer_players_than_said(self, tmp_path):
        tb = self._table(tmp_path, "600", "フォールド", "スリープレイヤーズ")
        assert [(a.seat, a.action) for a in tb.t._current_actions] == [(6, "raise"), (4, "fold")]   # noqa: SLF001
        assert any("記録では 2 人です" in n for n in tb.notices)
        assert tb.t._hand_needs_review                              # noqa: SLF001


class TestRdpHint:
    """RDP の音声が接続元に回っていると、マイクは WDM-KS 方式にしか出ず、Bluetooth のマイクを開けない
    （店舗 2026-09-30: [Errno -9999] Unanticipated host error）。"""

    def test_only_wdm_ks(self):
        wdm = [InputDevice(11, "Headset (…DJI Mic Mini 2-ED8C97)", "Windows WDM-KS", True, False),
               InputDevice(6, "Microphone (Realtek HD Audio Mic input)", "Windows WDM-KS", False, False)]
        assert only_wdm_ks(wdm) and only_wdm_ks([{"api": "Windows WDM-KS"}])
        assert not only_wdm_ks(wdm + [InputDevice(1, "Headset (DJI Mic Mini 2-EBA00C", "MME", True, True)])
        assert not only_wdm_ks([])
        assert "リモート PC で再生" in RDP_AUDIO_HINT

    def test_open_error_hint(self):
        assert open_error_hint({"api": "Windows WDM-KS"}) == WDM_KS_OPEN_HINT
        assert open_error_hint({"api": "MME"}) == ""
