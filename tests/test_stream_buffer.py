# tests/test_stream_buffer.py
"""spec.md FR-15〜18 AudioStreamBuffer および認識ヘルパーのテスト。"""
from __future__ import annotations

import pytest

from audio.recognizer import extract_amount, extract_seat, extract_position, extract_action
from audio.stream_buffer import AudioStreamBuffer, BufferState
from core.rule_engine import ActionType


# ──────────────────────────────────────────────────────────────────────────────
# AudioStreamBuffer 状態機械
# ──────────────────────────────────────────────────────────────────────────────

class TestStreamBufferConfirmatory:
    def test_call_confirmatory(self):
        """「コール」→ 即 ACTION_CONFIRMED({action: 'call'})（FR-15）。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("コール")
        assert len(events) == 1
        assert events[0].action == "call"
        assert buf.state == BufferState.IDLE

    def test_bet_with_amount(self):
        """「ベット 1000」→ ACTION_CONFIRMED({action: 'bet', amount: 1000})。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("ベット 1000")
        assert len(events) == 1
        assert events[0].action == "bet"
        assert events[0].amount == 1000
        assert buf.state == BufferState.IDLE

    def test_fold_confirmatory(self):
        """「フォールド」→ ACTION_CONFIRMED({action: 'fold'})。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("フォールド")
        assert len(events) == 1
        assert events[0].action == "fold"


class TestStreamBufferPending:
    def test_pending_then_confirm(self):
        """「コールですか？」→ PENDING_CONFIRM。続く「コール」→ ACTION_CONFIRMED（FR-17）。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("コールですか？")
        assert events == []
        assert buf.state == BufferState.PENDING_CONFIRM

        events = buf.process_utterance("コール")
        assert len(events) == 1
        assert events[0].action == "call"
        assert buf.state == BufferState.IDLE

    def test_pending_desuka_without_question_mark(self):
        """「コールですか」（？なし）→ PENDING_CONFIRM。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("コールですか")
        assert events == []
        assert buf.state == BufferState.PENDING_CONFIRM

    def test_pending_then_declaratory_discards_pending(self):
        """「コールですか？」→ PENDING。続く「ハンド開始」→ PHASE_EVENT、pending は破棄（FR-17）。"""
        buf = AudioStreamBuffer()
        buf.process_utterance("コールですか？")
        assert buf.state == BufferState.PENDING_CONFIRM

        events = buf.process_utterance("ハンド開始")
        assert len(events) == 1
        assert events[0].action == "new_hand"
        assert buf.state == BufferState.IDLE

    def test_pending_overwrite(self):
        """PENDING_CONFIRM 中に別の確認型発話 → pending を上書きしてイベントなし。"""
        buf = AudioStreamBuffer()
        buf.process_utterance("コールですか？")
        assert buf.state == BufferState.PENDING_CONFIRM

        events = buf.process_utterance("レイズですよね")
        assert events == []
        assert buf.state == BufferState.PENDING_CONFIRM
        assert buf._pending is not None
        assert buf._pending.action == "raise"

    def test_desu_yone_is_pending(self):
        """「ですよね」パターンで PENDING に遷移する。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("ベット500ですよね")
        assert events == []
        assert buf.state == BufferState.PENDING_CONFIRM


class TestStreamBufferDeclaratory:
    def test_new_hand_declaratory(self):
        """「ハンド開始」→ 即 PHASE_EVENT({action: 'new_hand'})（FR-15）。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("ハンド開始")
        assert len(events) == 1
        assert events[0].action == "new_hand"
        assert buf.state == BufferState.IDLE

    def test_winner_declaratory(self):
        """「ウィナー」→ PHASE_EVENT({action: 'winner'})。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("ウィナー")
        assert len(events) == 1
        assert events[0].action == "winner"

    def test_showdown_declaratory(self):
        """「ショーダウン」→ PHASE_EVENT({action: 'showdown'})。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("ショーダウン")
        assert len(events) == 1
        assert events[0].action == "showdown"


class TestStreamBufferSeatAndPosition:
    def test_seat_mention_and_amount(self):
        """「シート3 レイズ 2400」→ ACTION_CONFIRMED({action: 'raise', amount: 2400, mentioned_seat: 3})（FR-26）。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("シート3 レイズ 2400")
        assert len(events) == 1
        ev = events[0]
        assert ev.action == "raise"
        assert ev.amount == 2400
        assert ev.mentioned_seat == 3

    def test_position_mention(self):
        """「BTN、コール」→ ACTION_CONFIRMED({mentioned_position: 'BTN'})（FR-26）。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("BTN、コール")
        assert len(events) == 1
        assert events[0].action == "call"
        assert events[0].mentioned_position == "BTN"

    def test_sb_position(self):
        """「SBベット」→ mentioned_position = 'SB'。"""
        buf = AudioStreamBuffer()
        events = buf.process_utterance("SBベット")
        assert len(events) == 1
        assert events[0].mentioned_position == "SB"


class TestStreamBufferReset:
    def test_reset_clears_pending(self):
        """reset() で PENDING_CONFIRM → IDLE に戻る。"""
        buf = AudioStreamBuffer()
        buf.process_utterance("コールですか？")
        assert buf.state == BufferState.PENDING_CONFIRM

        buf.reset()
        assert buf.state == BufferState.IDLE
        assert buf._pending is None


# ──────────────────────────────────────────────────────────────────────────────
# extract_amount（漢数字・K 表記）
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractAmount:
    def test_kanji_2500(self):
        """「二千五百」→ 2500。"""
        assert extract_amount("二千五百") == 2500

    def test_k_notation_decimal(self):
        """「2.5K」→ 2500。"""
        assert extract_amount("2.5K") == 2500

    def test_k_notation_integer(self):
        """「5K」→ 5000。"""
        assert extract_amount("5K") == 5000

    def test_plain_digit(self):
        """「800」→ 800。"""
        assert extract_amount("800") == 800

    def test_man_notation(self):
        """「3万」→ 30000。"""
        assert extract_amount("3万") == 30000

    def test_no_amount_returns_none(self):
        """金額なし → None。"""
        assert extract_amount("チェック") is None

    def test_seat_not_counted_as_amount(self):
        """「シート3 レイズ 800」→ 席番号3ではなく 800 を返す。"""
        assert extract_amount("シート3 レイズ 800") == 800


# ──────────────────────────────────────────────────────────────────────────────
# extract_seat
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractSeat:
    def test_seat_kanji_digit(self):
        assert extract_seat("シート3 レイズ") == 3

    def test_seat_fullwidth(self):
        assert extract_seat("シート２ コール") == 2

    def test_seat_english(self):
        assert extract_seat("seat 5 fold") == 5

    def test_ban_pattern(self):
        assert extract_seat("3番 ベット") == 3

    def test_no_seat_returns_none(self):
        assert extract_seat("コール 500") is None


# ──────────────────────────────────────────────────────────────────────────────
# extract_position
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractPosition:
    def test_btn(self):
        assert extract_position("BTN コール") == "BTN"

    def test_utg(self):
        assert extract_position("UTGレイズ") == "UTG"

    def test_japanese_sb(self):
        assert extract_position("スモールブラインド ベット") == "SB"

    def test_no_position_returns_none(self):
        assert extract_position("コール 500") is None


# ──────────────────────────────────────────────────────────────────────────────
# extract_action
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractAction:
    def test_call(self):
        assert extract_action("コール") == ActionType.CALL

    def test_raise(self):
        assert extract_action("レイズ 2400") == ActionType.RAISE

    def test_fold(self):
        assert extract_action("フォールド") == ActionType.FOLD

    def test_phase_event_returns_none(self):
        """winner / new_hand は ActionType に含まれないため None を返す。"""
        assert extract_action("ウィナー") is None
        assert extract_action("ハンド開始") is None

    def test_no_action_returns_none(self):
        assert extract_action("こんにちは") is None
