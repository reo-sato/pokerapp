"""tests/test_reconstruction_hardening.py

ADR-A/B/C/D — アクション履歴復元アルゴリズムの正当性修正バッチのテスト。

- S1: 金額パース（N千/N百/小数万/K/曖昧 N万M）
- S2: 席番号の strip / 抽出の統一（空白・全角・漢数字）
- S3: raise の to/by 曖昧性 flag
- S4: 金額 snap review の同次元比較（gap >= bb）
- V1: 語彙拡張 + 複数キーワード flag
- V4: bb 倍数への round 寄せ
- G1: 制御語ガード（低信頼保留 / mid-hand new_hand の異常確定）
- G3: fold 済み席の RFID を actor 証拠にしない
- G4: 高信頼 ASR × 射影で action 変化 → review
- B2/B4/B5: ハンド外イベントの unresolved 記録 / winner fallback
- S7: split pot（チョップ）
- T1/T2/T3: 発話区間ベース照合窓 / event 時刻由来 timestamp
- T4: recorder のキャプチャ/推論分離・有音ゲート（初の recorder テスト）
- envelope: parse_flags / utterance_start_ts の round-trip + 旧形式後方互換
"""
from __future__ import annotations

import queue
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from audio.recognizer import (
    HIGH_CONF_ASR,
    _extract_all_seat_nos,
    _extract_seat_no,
    apply_corrections,
    parse_action,
    parse_amount,
    parse_amount_ex,
)
from core.engine_types import LegalContext
from core.events import AudioEvent, RFIDEvent
from core.game_state import PlayerState
from core.hand_log import ActionRecord
from integration.engine import IntegrationThread


# ――― helpers ―――

def _players(n: int, stack: int = 10000) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=stack) for i in range(n)]


def _pk(n: int = 3, new_hand: bool = True):
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState
    gs = PokerkitGameState(_players(n), sb=100, bb=200)
    if new_hand:
        gs.new_hand()
    return gs


class _NullWriter:
    _session_id = "test-session"

    def __init__(self):
        self.hands: list[dict] = []

    def append_hand_summary(self, summary):
        self.hands.append(summary.to_dict())

    @property
    def path(self):  # pragma: no cover
        return Path("/dev/null")


def _thread(gs, **kwargs) -> tuple[IntegrationThread, list[ActionRecord], _NullWriter]:
    captured: list[ActionRecord] = []
    writer = _NullWriter()
    t = IntegrationThread(
        audio_queue=queue.Queue(),
        game_state=gs,
        json_writer=writer,
        on_action=captured.append,
        stop_event=threading.Event(),
        **kwargs,
    )
    return t, captured, writer


def _audio(action, amount=0, ts=None, text="", seat=None, conf=0.9, **kw) -> AudioEvent:
    return AudioEvent(
        action=action, amount=amount, timestamp=ts if ts is not None else time.time(),
        raw_text=text, seat=seat, confidence=conf, **kw,
    )


def _rfid_seat(seat: int, ts: float) -> RFIDEvent:
    return RFIDEvent(
        tag_id=f"T{seat}", card="", reader_id=f"seat_{seat}", role="seat",
        seat=seat, timestamp=ts, raw_tag_id=f"T{seat}",
    )


# ――― S1: 金額パース ―――

class TestParseAmountS1:
    @pytest.mark.parametrize("text,expected", [
        ("2千", 2000),          # 従来は 2 と誤読 → min-raise へ静かに clamp
        ("5百", 500),
        ("2千5百", 2500),
        ("1.5万", 15000),
        ("1.5K", 1500),
        ("1.5k", 1500),
        ("1万2千", 12000),
        ("3万", 30000),
        ("5K", 5000),
        ("1,200", 1200),
        ("800", 800),
        ("二千五百", 2500),
        ("レイズ ２千", 2000),   # 全角は parse_action で NFKC されるが単体でも通す
    ])
    def test_values(self, text, expected):
        # parse_action 経由と等価にするため NFKC 相当の入力で検証
        import unicodedata
        assert parse_amount(unicodedata.normalize("NFKC", text)) == expected

    def test_man_trailing_digit_is_ambiguous(self):
        r = parse_amount_ex("4万2")
        assert r.value == 42000 and r.ambiguous is True

    def test_kanji_man_trailing_digit_is_ambiguous(self):
        r = parse_amount_ex("四万二")
        assert r.value == 42000 and r.ambiguous is True

    def test_man_sen_not_ambiguous(self):
        r = parse_amount_ex("1万2千")
        assert r.value == 12000 and r.ambiguous is False

    def test_parse_action_flags_ambiguous_amount(self):
        ev = parse_action("シート3 レイズ 4万2", confidence=0.9)
        assert ev is not None
        assert ev.amount == 42000
        assert "ambiguous_amount" in ev.parse_flags


# ――― S2: 席番号の strip / 抽出統一 ―――

class TestSeatExtractionS2:
    @pytest.mark.parametrize("text,seat,amount", [
        ("シート1 レイズ 800", 1, 800),
        ("シート 3 レイズ 800", 3, 800),     # 空白入りでも席番号が金額に流入しない
        ("seat ３ レイズ 800", 3, 800),      # 全角数字
        ("シート三 レイズ 800", 3, 800),     # 漢数字席
    ])
    def test_seat_and_amount(self, text, seat, amount):
        ev = parse_action(text, confidence=0.9)
        assert ev is not None
        assert ev.seat == seat
        assert ev.amount == amount

    def test_extract_all_seats(self):
        assert _extract_all_seat_nos("シート3 シート5 チョップ") == [3, 5]
        assert _extract_all_seat_nos("シート1 ウィナー") == [1]
        assert _extract_seat_no("シート 7 コール") == 7


# ――― V1: 語彙 + 複数キーワード flag ―――

class TestVocabularyV1:
    @pytest.mark.parametrize("text,action", [
        ("フォルド", "fold"),
        ("マック", "fold"),
        ("降ります", "fold"),
        ("リレイズ 600", "raise"),
        ("スリーベット 600", "raise"),
        ("シート3 シート5 チョップ", "winner"),
        ("スプリット", "winner"),
    ])
    def test_synonyms(self, text, action):
        ev = parse_action(text, confidence=0.9)
        assert ev is not None and ev.action == action

    def test_multi_keyword_flag(self):
        ev = parse_action("チェックレイズ", confidence=0.9)
        assert ev is not None
        assert ev.action == "check"                       # 先頭のみ採用
        assert "multi_action_keywords" in ev.parse_flags  # + 要レビュー

    def test_containment_not_flagged(self):
        # スリーベット ⊃ ベット は包含マッチ → flag しない
        ev = parse_action("スリーベット 600", confidence=0.9)
        assert ev.parse_flags == ()

    def test_utterance_start_ts_passthrough(self):
        ev = parse_action("コール", confidence=0.9, utterance_start_ts=123.5)
        assert ev.utterance_start_ts == 123.5


# ――― S3/S4/V4/G4: apply_corrections ―――

def _facing_bet(bb: int = 0, committed: int = 0) -> LegalContext:
    return LegalContext(
        actor_seat=1,
        legal_actions=frozenset({"fold", "call", "raise", "allin"}),
        amount_to_call=300,
        min_raise=600,
        max_raise=10000,
        bb=bb,
        committed=committed,
    )


class TestCorrectionsHardening:
    def test_s4_snap_gap_over_bb_flags(self):
        # 従来 pin（gap > m でしか flag しない）は「2千→2 誤読 → min clamp」を無警告で通した。
        # bb が分かるなら gap >= bb で review（同次元比較, ADR-A S4）。
        r = apply_corrections("raise", 50, _facing_bet(bb=200))
        assert r.amount == 600
        assert r.needs_review is True
        assert "amount_snapped" in r.reason

    def test_s4_small_snap_no_review(self):
        # gap(100) < bb(200) の微修正は snap のみ（reason は残すが review しない）。
        r = apply_corrections("raise", 700, _facing_bet(bb=200))
        # 700 は bb 倍数でない → 600 or 800 へ round。round(700/200)=4 → 800（レンジ内）。
        assert r.amount == 800
        assert "rounded_to_bb" in r.reason
        assert r.needs_review is False

    def test_v4_round_to_bb(self):
        r = apply_corrections("raise", 850, _facing_bet(bb=200))
        assert r.amount == 800
        assert "rounded_to_bb" in r.reason
        assert r.needs_review is False

    def test_v4_no_round_when_multiple(self):
        r = apply_corrections("raise", 800, _facing_bet(bb=200))
        assert r.amount == 800
        assert r.reason == ""

    def test_s3_raise_to_vs_by_ambiguous(self):
        # heard 400 は to 解釈では min(600) 未満 = 非合法。だが「追加額」解釈なら
        # committed(0)+call(300)+400=700 で合法 → by 読み上げの可能性を flag。
        r = apply_corrections("raise", 400, _facing_bet(bb=200))
        assert r.amount == 600                     # 採用は従来どおり to 解釈 + snap
        assert r.needs_review is True
        assert "raise_to_vs_by_ambiguous" in r.reason

    def test_s3_not_flagged_when_both_legal(self):
        # 両解釈とも合法な通常レイズは慣例（to 読み上げ）を信頼して flag しない。
        r = apply_corrections("raise", 800, _facing_bet(bb=200, committed=0))
        assert r.needs_review is False

    def test_g4_high_conf_projection_flags(self):
        r = apply_corrections("bet", 800, _facing_bet(bb=200), whisper_conf=HIGH_CONF_ASR)
        assert r.action == "raise"
        assert r.corrected_from == "bet"
        assert r.needs_review is True
        assert "high_conf_asr_projection" in r.reason

    def test_g4_low_conf_projection_not_flagged(self):
        r = apply_corrections("bet", 800, _facing_bet(bb=200), whisper_conf=0.7)
        assert r.action == "raise"
        assert r.needs_review is False


# ――― B2/B5: ハンド外イベント / winner fallback ―――

class TestUnresolvedAndWinnerFallback:
    def test_b2_betting_without_hand_emits_unresolved(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("bet", 500, text="ベット500"))
        assert len(cap) == 1
        rec = cap[-1]
        assert rec.actor_source == "unresolved"
        assert rec.apply_ok is False
        assert rec.reason == "no_active_hand"
        assert rec.needs_review is True
        assert writer.hands == []          # summary は書かれない

    def test_b5_winner_without_anything_is_held(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("winner", text="ウィナー"))
        assert writer.hands == []          # 空 junk summary を書かない
        assert cap[-1].reason == "no_active_hand"

    def test_b5_winner_no_seat_falls_back_to_last_aggressor(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", text="ハンド開始"))
        t._handle_audio_event(_audio("raise", 600, text="レイズ 600"))  # actor seat3
        t._handle_audio_event(_audio("winner", text="ウィナー"))        # 席なし
        assert len(writer.hands) == 1
        hand = writer.hands[0]
        assert hand["winner_seat"] == 3            # 最後のアグレッサー
        assert hand["review_required"] is True     # 推定なので review

    def test_b4_handler_error_becomes_unresolved(self):
        gs = _pk(3)
        t, cap, writer = _thread(gs)

        def boom(*a, **k):
            raise RuntimeError("boom")
        gs.advance_street = boom  # showdown 経路で内部例外を強制
        t._handle_audio_event(_audio("showdown", text="ショーダウン"))
        assert cap[-1].reason == "handler_error"
        assert cap[-1].apply_ok is False


# ――― G1: 制御語ガード ―――

class TestControlWordGuardG1:
    def test_low_conf_control_held(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs, control_conf_threshold=0.6)
        t._handle_audio_event(_audio("new_hand", conf=0.3, text="ハンド開始"))
        assert gs.hand_id == 0                       # 状態は動かない
        assert cap[-1].reason == "low_conf_control_held"

    def test_high_conf_control_passes(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs, control_conf_threshold=0.6)
        t._handle_audio_event(_audio("new_hand", conf=0.9, text="ハンド開始"))
        assert gs.hand_id == 1

    def test_none_conf_control_passes(self):
        # 制御 UI（GUI ボタン/CLI）由来のイベントは confidence を持たない → ガード対象外。
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs, control_conf_threshold=0.6)
        t._handle_audio_event(_audio("new_hand", conf=None, text=""))
        assert gs.hand_id == 1

    def test_default_threshold_zero_disables_guard(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", conf=0.01, text="ハンド開始"))
        assert gs.hand_id == 1

    def test_midhand_new_hand_finalizes_abnormally(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", text="ハンド開始"))
        t._handle_audio_event(_audio("raise", 600, text="レイズ 600"))
        t._handle_audio_event(_audio("new_hand", text="ハンド開始"))  # 勝者未宣言のまま
        assert len(writer.hands) == 1                # 記録は捨てられず異常確定される
        assert writer.hands[0]["review_required"] is True
        assert gs.hand_id == 2                       # 新ハンドは開始される（1→2）


# ――― G3: fold 済み席の RFID を actor 証拠にしない ―――

class TestRFIDEvidenceSanityG3:
    def test_folded_seat_rfid_not_used_as_actor(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", ts=1000.0, text="ハンド開始"))
        t._handle_audio_event(_audio("fold", ts=1001.0, text="フォールド"))  # seat3 fold
        # fold 済み seat3 のカードが読まれる（チップ整理等）
        t._process_rfid_event(_rfid_seat(3, 1001.5))
        t._handle_audio_event(_audio("call", ts=1002.0, text="コール"))
        rec = cap[-1]
        assert rec.action == "call"
        assert rec.seat == 1                       # prior（seat1）のまま、seat3 に飛ばない
        assert rec.actor_source == "engine_prior"
        assert not any(r.action == "fold" and r.reason == "synth_silent_fold" for r in cap[2:])


# ――― S7: split pot（チョップ） ―――

class TestSplitPotS7:
    def test_chop_splits_pot_and_records_awards(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", text="ハンド開始"))
        t._handle_audio_event(_audio("winner", text="シート1 シート2 チョップ"))
        assert len(writer.hands) == 1
        hand = writer.hands[0]
        assert hand["winner_seat"] == 1            # 読み上げ先頭 = 従来互換
        assert hand["pot_awards"] == [
            {"seat": 1, "amount": 150},
            {"seat": 2, "amount": 150},
        ]                                          # SB100+BB200=300 を等分
        assert hand["pot_total"] == 300
        assert hand["review_required"] is True     # chop は必ず review
        by_seat = {p["seat"]: p for p in hand["players"]}
        assert by_seat[1]["result"] == 50          # -100 + 150
        assert by_seat[2]["result"] == -50         # -200 + 150
        assert by_seat[3]["result"] == 0

    def test_single_winner_has_no_pot_awards(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", text="ハンド開始"))
        t._handle_audio_event(_audio("winner", text="シート1 ウィナー"))
        assert "pot_awards" not in writer.hands[0]  # additive: 従来ハンドは absent


# ――― T1/T2/T3: 照合窓 / timestamp ―――

class TestTimingT:
    def test_t2_utterance_window_matches_delayed_asr(self):
        # RFID は発話時に読まれるが、ASR デコード遅延で audio event.timestamp は 4.5 秒後。
        # utterance_start_ts があれば窓 [start-2, ts+2] に入る。
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", ts=1000.0, text="ハンド開始"))
        t._process_rfid_event(_rfid_seat(3, 1000.5))
        t._handle_audio_event(_audio(
            "call", ts=1005.0, text="コール", utterance_start_ts=1000.4,
        ))
        rec = cap[-1]
        assert rec.source["rfid"] is True
        assert rec.actor_source == "rfid"

    def test_t2_without_start_ts_falls_back_to_old_window(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        t._handle_audio_event(_audio("new_hand", ts=1000.0, text="ハンド開始"))
        t._process_rfid_event(_rfid_seat(3, 1000.5))
        t._handle_audio_event(_audio("call", ts=1005.0, text="コール"))
        rec = cap[-1]
        assert rec.source["rfid"] is False         # 従来窓 ±2s では届かない（後方互換）

    def test_t3_record_timestamp_from_event(self):
        gs = _pk(3, new_hand=False)
        t, cap, writer = _thread(gs)
        ts = 1_750_000_000.25
        t._handle_audio_event(_audio("new_hand", ts=ts, text="ハンド開始"))
        t._handle_audio_event(_audio("call", ts=ts + 1, text="コール"))
        expected = datetime.fromtimestamp(ts + 1).isoformat(timespec="milliseconds")
        assert cap[-1].timestamp == expected


# ――― envelope round-trip（ADR-A/B additive） ―――

class TestEnvelopeCompat:
    def test_round_trip_with_new_fields(self):
        from integration.replay import event_from_envelope
        from output.event_recorder import event_to_envelope
        ev = AudioEvent(
            action="raise", amount=42000, timestamp=10.0, raw_text="レイズ 4万2",
            seat=3, confidence=0.8, parse_flags=("ambiguous_amount",),
            utterance_start_ts=8.5,
        )
        d = event_to_envelope(ev)
        back = event_from_envelope(d)
        assert back == ev

    def test_default_fields_omitted_in_envelope(self):
        from output.event_recorder import event_to_envelope
        ev = AudioEvent(action="call", amount=0, timestamp=10.0, raw_text="コール")
        d = event_to_envelope(ev)
        assert "parse_flags" not in d and "utterance_start_ts" not in d

    def test_old_envelope_loads_with_defaults(self):
        from integration.replay import event_from_envelope
        d = {"type": "audio", "timestamp": 3001.0, "action": "raise", "amount": 600,
             "raw_text": "シート1 レイズ 600", "seat": 1, "confidence": 0.85}
        ev = event_from_envelope(d)
        assert ev.parse_flags == ()
        assert ev.utterance_start_ts is None


# ――― T4: recorder（キャプチャ/推論分離・有音ゲート） ―――

class _FakeTranscriber:
    def __init__(self, text="シート1 コール", conf=0.9):
        self.text = text
        self.conf = conf
        self.calls: list[bytes] = []

    def transcribe_with_confidence(self, audio_bytes):
        self.calls.append(audio_bytes)
        return self.text, self.conf


def _voiced_chunk(n=1024, amp=5000) -> bytes:
    return struct.pack(f"{n}h", *([amp] * n))


def _silent_chunk(n=1024) -> bytes:
    return b"\x00" * (n * 2)


def _make_audio_thread(transcriber=None):
    from audio.recorder import AudioThread
    q: queue.Queue = queue.Queue()
    t = AudioThread(
        audio_queue=q, stop_event=threading.Event(),
        transcriber=transcriber or _FakeTranscriber(),
    )
    return t, q


def _run_capture(t, chunks: list[bytes]) -> None:
    it = iter(chunks + [b""])  # b"" = fake stream 終端
    t._capture_loop(lambda: next(it), 1024)


class TestRecorderT4:
    def test_voiced_utterance_enqueued_with_start_ts(self):
        t, _ = _make_audio_thread()
        _run_capture(t, [_silent_chunk()] * 2 + [_voiced_chunk()] * 6 + [_silent_chunk()] * 9)
        item = t._chunk_queue.get_nowait()
        assert item is not None
        audio_bytes, start_ts = item
        assert isinstance(start_ts, float)
        assert len(audio_bytes) >= 6 * 1024 * 2     # 有音 6 チャンク（+pre-roll）以上

    def test_silence_only_is_gated(self):
        # 有音ゲート: 無音だけのバッファは推論に送らない（プロンプトのオウム返し対策）。
        t, _ = _make_audio_thread()
        _run_capture(t, [_silent_chunk()] * 20)
        assert t._chunk_queue.empty()

    def test_short_blip_dropped(self):
        # _MIN_BUFFER_SECONDS(0.3s = 約5チャンク)未満の短音は捨てる。
        t, _ = _make_audio_thread()
        _run_capture(t, [_voiced_chunk()] * 2 + [_silent_chunk()] * 9)
        assert t._chunk_queue.empty()

    def test_process_chunk_emits_event_with_utterance_start(self):
        fake = _FakeTranscriber(text="シート1 コール", conf=0.9)
        t, q = _make_audio_thread(fake)
        t._process_chunk(b"\x00\x00", utterance_start_ts=123.0)
        ev = q.get_nowait()
        assert ev.action == "call"
        assert ev.seat == 1
        assert ev.confidence == 0.9
        assert ev.utterance_start_ts == 123.0

    def test_inference_queue_drops_oldest_when_full(self):
        t, _ = _make_audio_thread()
        for i in range(10):
            t._enqueue_utterance(bytes([i]) * 2, float(i))
        items = []
        while not t._chunk_queue.empty():
            items.append(t._chunk_queue.get_nowait())
        assert len(items) == 8                      # _INFERENCE_QUEUE_MAX
        assert items[-1][1] == 9.0                  # 新しい発話が残る
