"""integration/engine.py

audio / camera / RFID (ESP32 HTTP) の 3 ソースを統合し、confidence スコアを算出する。

ソース優先度: RFID > audio > camera

Confidence 行列 (legacy backend):
  RFID + audio + camera : 1.00
  RFID + audio          : 0.95
  RFID + camera         : 0.85
  RFID のみ             : 0.70
  audio + camera        : 0.80
  audio のみ            : 0.50
  camera のみ           : 0.30
  なし                  : 0.00

カード情報 (ESP32 RFID):
  role="board" かつ board_index 付きイベント
      → _board_positions[board_index] に格納、ボード枚数でストリート自動推移
  role="seat" かつ card 付きイベント
      → _hole_cards[seat] に最大 2 枚蓄積、手終了時に HandSummary に反映

アクション照合 (role="seat"):
  発話区間 [utterance_start_ts, timestamp] ± MATCH_WINDOW 秒以内の同席イベントと照合して
  confidence 向上（ADR-0048 T1/T2: ASR デコード遅延が照合窓を食い潰さないよう、窓は発話区間
  ベースの両側窓。utterance_start_ts 欠損時は従来どおり timestamp ± MATCH_WINDOW）。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from audio.recognizer import _extract_all_seat_nos, _extract_seat_no, apply_corrections
from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameStateManager, Street
from core.hand_log import ActionRecord, HandSummary
from core.table_state import build_table_state
from output.event_recorder import EventRecorder
from output.json_writer import JsonWriter

if TYPE_CHECKING:
    from core.engine_types import LegalContext
    from core.session_repository import SessionRepository
    from output.table_state_writer import TableStateWriter

logger = logging.getLogger(__name__)

MATCH_WINDOW = 2.0
# 卓状態を定期 publish する間隔（秒）。カードが外れても RFIDEvent は出ないので、
# 有効席（= 札が載っている席）の変化はこの間隔で UI に届く。
TABLE_STATE_INTERVAL = 1.0
# センサーイベントの保持期間。照合窓（MATCH_WINDOW）とは独立で、ASR デコード遅延
# （チャンク 5 秒 + 推論）で audio イベントが遅れて届いても RFID/camera 証拠が
# expire で消えないだけの余裕を持たせる（ADR-0048 T1）。
CAMERA_BUFFER_TTL = 12.0

# silent-fold 合成で許す最大席数（ISSUE-0009）。超過は合成せず prior 維持 + needs_review。
SILENT_FOLD_CAP = 2
# 合成した silent-fold の confidence（sensor 観測なしの推定。常に needs_review）。
# REVIEW_THRESHOLD 未満であることを較正で固定（ADR-0033 P8, tools/calibrate_confidence.py）。
SYNTH_FOLD_CONFIDENCE = 0.3
# Whisper 信頼度が欠測（None）のときの保守的既定（ADR-0033 追記 / B3）。
# 従来は 1.0（満点）補完で「情報が無いほど confidence が上がる」逆転があった。
# 0.5 は audio-only では REVIEW_THRESHOLD を下回る = 欠測 audio 単独は要レビュー側に倒す。
MISSING_WHISPER_CONF = 0.5

# ――― Confidence スコア定数 ―――
_CONF_RFID_AUDIO_CAMERA = 1.00
_CONF_RFID_AUDIO        = 0.95
_CONF_RFID_CAMERA       = 0.85
_CONF_RFID_ONLY         = 0.70
_CONF_AUDIO_CAMERA      = 0.80
_CONF_AUDIO_ONLY        = 0.50
_CONF_CAMERA_ONLY       = 0.30

# board_index → street 推移しきい値 (1-indexed, ≥N 枚でその street)
_BOARD_STREET_THRESHOLDS = {3: "flop", 4: "turn", 5: "river"}

# G1（ADR-0049）: 状態を大きく動かす制御語。config の閾値 > 0 のとき低信頼 ASR を保留する。
_CONTROL_ACTIONS = frozenset({"new_hand", "winner", "showdown", "end_hand"})

# ――― 手札が配られたら新しいハンド / 勝者の自動判定（ADR-0062）―――
# 次のハンドの配布とみなす時間窓（秒）。この間に 2 席以上へ札が配られたら配布と判断する。
# 片付けの途中で 1 枚だけ別の席のリーダーに触れた札は、窓を過ぎると捨てる。
DEAL_WINDOW_SEC = 15.0
# 配布を検出してから、それより前に話された発話の認識を待つ上限（秒）。認識が遅れていても、
# 前のハンドのアクション（最後のコールやマック）を新しいハンドに入れないため。
DEAL_SPEECH_WAIT_SEC = 60.0
# 配った直後（アクション前）のハンドに届いた `w` / 「ウィナー」を、前のハンドへの宣言とみなす時間（秒）。
LATE_WINNER_SEC = 60.0
# RFID の在否で配布を検出する（ADR-0063）: 2 席以上に手札 2 枚ずつがこの秒数載り続け、ボードの
# リーダーが空なら配布。シャッフル・ウォッシュで一瞬リーダーを通った札では始めない。
DEAL_STABLE_SEC = 1.5
# 載り続けているとみなす途切れの上限（読み落ち 1〜2 周ぶん）
DEAL_GAP_SEC = 0.6
# ハンドの途中でも、卓（席とボードのリーダー）に札が 1 枚も無い状態がこの秒数続いたらプレーは
# 終わったとみなし、音声を聞き流す（片付け・シャッフル中の会話を認識に回さない, ADR-0063）。
TABLE_CLEAR_SEC = 15.0
# いまのストリートの最初の札が見える前にこの秒数以上早く話し始めた「コール」は、前のストリートで
# 言われたもの（言い直し）とみなす（店舗の実測: 「コール」のあとの「600点コールです」がターンの札の 3 秒前）。
STALE_CALL_MARGIN_SEC = 1.0

# review の理由にしない parse flag（読み方の情報。「数字だけ」「チェックアラウンド」は運用どおりの言い方）
_INFO_PARSE_FLAGS = frozenset({"amount_only", "check_around"})

_SUIT_MARKS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}


def _card_text(card: str) -> str:
    """"Td" → "10♦"（CLI の表示用）。形の違う札（未登録の UID 等）はそのまま。"""
    if len(card) == 2 and card[1].lower() in _SUIT_MARKS:
        rank = "10" if card[0] in "Tt" else card[0].upper()
        return rank + _SUIT_MARKS[card[1].lower()]
    return card


def _cards_text(cards) -> str:
    return " ".join(_card_text(c) for c in cards)


def calc_confidence(has_rfid: bool, has_audio: bool, has_camera: bool) -> float:
    """センサー組み合わせから confidence スコアを返す。"""
    if has_rfid and has_audio and has_camera:
        return _CONF_RFID_AUDIO_CAMERA
    if has_rfid and has_audio:
        return _CONF_RFID_AUDIO
    if has_rfid and has_camera:
        return _CONF_RFID_CAMERA
    if has_rfid:
        return _CONF_RFID_ONLY
    if has_audio and has_camera:
        return _CONF_AUDIO_CAMERA
    if has_audio:
        return _CONF_AUDIO_ONLY
    if has_camera:
        return _CONF_CAMERA_ONLY
    return 0.0


# ――― 派生 confidence (3 因子, ADR-0009 §6, D3) ―――
# rules-aware 経路専用。legacy は上の固定 8 行 calc_confidence のまま（挙動不変）。
# 重みは **較正済み**（ADR-0033）。golden fixtures の archetype + 境界グリッドに対し較正プロパティ
# P1〜P9（順序単調性・閾値分離・合法性ゲート・欠測既定等）を満たすことを
# `tools/calibrate_confidence.py` / `tests/test_confidence_calibration.py` で回帰ロックする。
# 変更時は同ハーネスで再検証すること。
_CONF_W_A = 0.15            # 合意度 A の重み
_CONF_W_Q = 0.85           # ソース品質 Q の重み（w_A + w_Q = 1）
_CONF_L_PENALTY = 0.25     # pokerkit が action を受理しなかったときの合法性ゲート L
_CONF_BASE = {"rfid": 0.78, "audio": 0.50, "camera": 0.28}  # ソース base 信頼度（RFID>audio>camera）
# confidence がこの閾値未満なら needs_review（ADR-0009 §6 条件⑤）。将来 config 化。
# 音声優先運用（v1 は audio のみが必須経路）のため、良好な audio-only(whisper>=0.6) は閾値超え＝自動
# review しない。低 whisper / 合成 fold / 欠測 whisper は閾値未満＝review（較正 P7/P8/P9, ADR-0033）。
REVIEW_THRESHOLD = 0.40


def derive_confidence(
    *,
    apply_ok: bool,
    whisper_conf: float,
    audio_agree: bool,
    rfid_present: bool,
    rfid_agree: bool,
    camera_present: bool,
    camera_agree: bool,
) -> float:
    """3 因子（L 合法性 / A 合意度 / Q ソース品質）から confidence を導出する（ADR-0009 §6, D3）。

    - L = 1.0（pokerkit 受理）/ `_CONF_L_PENALTY`（非受理）。最重要の合法性ゲート。
    - A = 一致した存在ソース数 / 存在ソース数（audio は当該アクションにつき常に存在）。
    - Q = 一致した存在ソースの base 信頼度の noisy-OR（audio は whisper_conf でスケール）。
    confidence = clamp(L · (w_A·A + w_Q·Q), 0, 1)。
    """
    present = {"rfid": rfid_present, "audio": True, "camera": camera_present}
    agree = {"rfid": rfid_agree, "audio": audio_agree, "camera": camera_agree}

    L = 1.0 if apply_ok else _CONF_L_PENALTY
    n_present = sum(present.values())
    n_agree = sum(1 for s in present if present[s] and agree[s])
    A = (n_agree / n_present) if n_present else 0.0

    prod = 1.0
    for s in present:
        if present[s] and agree[s]:
            q = _CONF_BASE[s] * (whisper_conf if s == "audio" else 1.0)
            prod *= (1.0 - q)
    Q = 1.0 - prod

    return max(0.0, min(1.0, L * (_CONF_W_A * A + _CONF_W_Q * Q)))


class IntegrationThread(threading.Thread):
    """audio / camera / RFID の 3 キューを消費してゲーム状態を更新する。"""

    def __init__(
        self,
        audio_queue: EventQueue,
        game_state: GameStateManager,
        json_writer: JsonWriter,
        camera_queue: Optional[EventQueue] = None,
        rfid_queue: Optional[EventQueue] = None,
        on_action: Optional[Callable[[ActionRecord], None]] = None,
        on_rfid_card: Optional[Callable[[RFIDEvent], None]] = None,
        stop_event: Optional[threading.Event] = None,
        event_recorder: Optional[EventRecorder] = None,
        on_hand: Optional[Callable[["HandSummary"], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        session_repo: "Optional[SessionRepository]" = None,
        seat_player_map: Optional[dict[int, str]] = None,
        on_new_hand: Optional[Callable[[], None]] = None,
        on_card_correction: Optional[Callable[[str, int], None]] = None,
        seat_cards_absent_since: Optional[Callable[[int], Optional[float]]] = None,
        seat_presence: Optional[Callable[[], dict]] = None,
        table_state_writer: "Optional[TableStateWriter]" = None,
        control_conf_threshold: float = 0.0,
        board_presence: Optional[Callable[[], dict]] = None,
        auto_new_hand: bool = False,
        auto_winner: bool = False,
        speech_backlog: Optional[Callable[[], int]] = None,
        on_notice: Optional[Callable[[str], None]] = None,
        listen_gate: Optional[threading.Event] = None,
        on_cards: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Args:
            on_rfid_card: カード検出時のコールバック (GUI スレッドには渡さず
                          _update_queue 経由で処理すること)。スレッド安全に設計すること。
            event_recorder: 生イベントを sidecar に記録する recorder (R1, ADR-0010)。
                            None なら記録しない (= 挙動不変)。解釈前に呼ばれる。
            on_hand: ハンド確定時に HandSummary を渡すコールバック (replay/テスト用、additive)。
                     on_rfid_card 同様 integration スレッドで発火するためスレッド安全に扱うこと。
            clock: epoch 秒を返す時計 (既定 time.time)。決定的 replay 用に注入する (F1)。
                   buffer 期限はこの時計に従う（ActionRecord/HandSummary の時刻は
                   event.timestamp 由来 = ADR-0048 T3 で live/replay 同義）。
            session_repo: S2.x session レイヤ (ADR-0008 Pattern A)。`seat_player_map` と共に与えると
                          hand 開始時に assign_seat（write-through）し、HandSummary.players に player_id を
                          additive 埋め込む。None なら従来動作（session 未接続・挙動不変, rollback path）。
            seat_player_map: seat_no → player_id（registry の UUID hex）。session_repo と対で有効。
            on_new_hand: 新ハンド開始時に呼ぶフック（additive, 既定 None = 従来動作）。
                         RFID の board 位置を **engine と同じタイミングでリセット**するために使う
                         （`RFIDThread.reset_board_positions`, ISSUE-0026）。integration スレッドで
                         発火するのでスレッド安全に実装すること。
            on_card_correction: ミスディール訂正フック（additive, 既定 None = 訂正は engine 内のみ）。
                         `("board", 位置)` / `("seat", 席)` で呼ぶので、RFID 側の割り当て・
                         デバウンスも同じタイミングで落とす（`RFIDThread.forget_board_position` /
                         `forget_seat_cards`, ADR-0054）。integration スレッドで発火する。
            seat_cards_absent_since: 席のカードが消えたまま戻っていない時刻（epoch）を返す関数
                         （additive, 既定 None = 従来どおり処理時刻を使う）。**fold の判定には
                         使わない**（プレイヤーはカードを持ち上げるので不在は fold を意味しない）。
                         合成 silent-fold に **実際に札が席から離れた時刻**を与えるためだけに読む
                         （音声の時系列と突き合わせて履歴を再生するため, ADR-0055）。
            seat_presence: 席ごとのカード在否を返す関数（`RFIDThread.presence_snapshot`）。
                         `table_state_writer` と対で、**RFID だけから導く卓状態**（カード / 有効席 /
                         ストリート）を publish するのに使う。None なら卓状態は在否なしで作られる。
            table_state_writer: 卓状態スナップショットの書き出し先（`output/table_state_writer.py`）。
                         None なら publish しない（= 挙動不変）。
            control_conf_threshold: 制御語（new_hand/winner/showdown）を受理する Whisper 信頼度の
                          下限（ADR-0049 G1）。0.0（既定）で無効 = 従来挙動。閾値未満の制御語は
                          状態を動かさず保留レコード（needs_review）として on_action にのみ流す。
            board_presence: ボードの読み取り状況（外れた札 / 数える前の札）を返す関数
                         （`RFIDThread.board_presence`, ADR-0058）。卓状態の「外れた」「確認中」表示にだけ
                         使い、ボードの記録は変えない。None なら表示しない。
            auto_new_hand: 手札が配られたら新しいハンドを始める（ADR-0062, 既定 False = 従来どおり
                         `n` / 「ハンド開始」だけ）。前のハンドが終わっていなければ先に確定する。
            auto_winner: 勝者を自動で決める（ADR-0062, 既定 False）。rules-aware backend のみ。
                         ほかが全員フォールドしたら残った人。ベッティングが終わったあとの「フォールド」は
                         ショーダウンでのマック（一番アウトオブポジションから順）。マックが無ければ
                         「ハンド終了」か次の配布のときに手札とボードで判定する。
            speech_backlog: 認識待ちの発話の数を返す関数（`AudioThread.backlog`）。配布を検出しても、
                         それより前に話された発話を先に反映してから新しいハンドを始めるために使う。
                         None なら待たない（音声なし・replay）。
            on_notice: 操作する人へのお知らせ（ハンドの開始・勝者・判定待ち）を受け取る関数。
                         integration スレッドで呼ばれる。None なら logger だけ。
            listen_gate: プレー中だけ set される Event（ADR-0063）。`AudioThread` が見て、プレー中で
                         ない間の発話を認識に回さない。None なら常に聞く。
            on_cards: RFID で読んだ札（手札・フロップ / ターン / リバー・確定時のまとめ）を 1 行ずつ
                         受け取る関数（CLI の表示用）。integration スレッドで呼ばれる。None なら logger だけ。
        """
        super().__init__(daemon=True, name="IntegrationThread")
        self._audio_queue = audio_queue
        self._camera_queue = camera_queue
        self._rfid_queue = rfid_queue
        self._game_state = game_state
        self._json_writer = json_writer
        self._on_action = on_action
        self._on_rfid_card = on_rfid_card
        self._stop_event = stop_event or threading.Event()
        self._event_recorder = event_recorder
        self._on_hand = on_hand
        self._clock: Callable[[], float] = clock or time.time
        self._control_conf_threshold = control_conf_threshold
        # S2.x session 統合（ADR-0008 Pattern A, write-through）。両方揃ったときのみ有効。
        self._session_repo = session_repo
        self._seat_player_map: dict[int, str] = dict(seat_player_map or {})
        self._session_layer_active = session_repo is not None and bool(self._seat_player_map)
        self._on_new_hand = on_new_hand
        self._on_card_correction = on_card_correction
        self._seat_cards_absent_since = seat_cards_absent_since
        self._seat_presence = seat_presence
        self._board_presence = board_presence
        self._table_state_writer = table_state_writer
        # 定期 publish の最終時刻。カードが**外れた**ときは RFIDEvent が出ないので
        # （デバウンスは増えた UID にしか反応しない）、fold を卓状態に映すには定期更新が要る。
        self._table_state_published_at: float = 0.0

        # センサーイベントのバッファ
        self._camera_buffer: list[CameraEvent] = []
        self._rfid_seat_buffer: list[RFIDEvent] = []

        # ハンド内の一時バッファ
        self._current_actions: list[ActionRecord] = []
        self._hand_started_at: str = self._now_iso()
        # ハンド開始の epoch。マック観測をハンド内にクランプするのに使う（ADR-0055）。
        self._hand_started_epoch: Optional[float] = None
        self._stack_start: dict[int, int] = {}
        # new_hand 済みで勝者未確定のハンドが進行中か（G1 の状態妥当性チェック /
        # ハンド外イベントの unresolved 記録に使う）。
        self._hand_open: bool = False
        # ハンドの途中に届いた席替えの名前（席 → 名前）。次のハンドの開始時に反映する（ADR-0059）。
        self._pending_renames: dict[int, str] = {}

        # RFID カード情報
        self._board_cards: list[str] = []          # 順序付きボードカード（表示用）
        self._board_positions: dict[int, str] = {} # board_index → card
        # board_index → **最初に検出した時刻**（epoch）。ターン/リバーの配布時刻はベッティング
        # ラウンドの区切りとしてアクションの時刻に対応するため記録する（ADR-0055）。
        # 再検出（結合の弱いリーダーの間欠読み）では上書きしない = 配布の瞬間を保つ。
        self._board_dealt_at: dict[int, float] = {}
        self._board_source: str = ""
        self._hole_cards: dict[int, list[str]] = {}  # seat → [card1, card2]

        # RFID カードがマスター未解決のままハンドが進んだ場合、ハンド全体を
        # 要レビューにする（個々の ActionRecord では捕捉できないため）。
        self._hand_needs_review: bool = False

        # ――― 手札が配られたら新しいハンド / 勝者の自動判定（ADR-0062）―――
        self._auto_new_hand = auto_new_hand
        self._auto_winner = auto_winner
        self._speech_backlog = speech_backlog
        self._on_notice = on_notice
        self._rules_aware = bool(getattr(game_state, "rules_aware", False))
        # 次のハンドとして読んだ札 (席, 札, 時刻)。在否が無いとき（replay 等）はこれで配布を判断する。
        self._deal_events: list[tuple[int, str, float]] = []
        # 配布と判断したハンドの手札（席 → 札）。ハンドを始めるときに手札として入れる。
        self._deal_hands: dict[int, list[str]] = {}
        # 在否による配布の検出: 席 → (手札 2 枚が載り始めた時刻, 最後に載っていた時刻)（ADR-0063）
        self._deal_since: dict[int, tuple[float, float]] = {}
        self._deal_at: Optional[float] = None          # 最初の札が見えた時刻
        self._deal_detected_at: Optional[float] = None  # 検出した時刻（engine の時計）
        # 配布の検出で RFID をリセット済み（ハンド開始時に二重にリセットしない）
        self._rfid_reset_for_deal = False
        # プレー中か（配布〜確定、または卓が空になるまで）。音声の聞き取りとボードの受付に使う。
        self._listen_gate = listen_gate
        self._in_play = False
        if listen_gate is not None:
            listen_gate.clear()
        self._table_empty_since: Optional[float] = None
        # いまのハンドを配布の検出で始めたか（直後の「ハンド開始」/ `n` を二重に数えない）。
        self._hand_auto_started = False
        # ショーダウンでマックした席（見せずに降りた = ポットを受け取れない）。
        self._showdown_mucks: list[int] = []
        self._showdown_notice_shown = False
        # 直前に確定したハンド（自動で決めた勝者のあとに届いた `w` / 「ウィナー」を扱う）。
        self._last_result: Optional[dict] = None
        # CLI に出した札（同じ札を何度も出さない）: 席 → 手札、ボードは出した枚数
        self._on_cards = on_cards
        self._shown_holes: dict[int, tuple[str, ...]] = {}
        self._shown_board = 0

    def stop(self) -> None:
        self._stop_event.set()

    def set_seat_player_map(self, seat_player_map: Optional[dict[int, str]]) -> None:
        """seat→player_id を更新し session レイヤの有効/無効を再評価する(E3, ADR-0008)。

        GUI(座席設定ダイアログ)が構築後に seating を確定/変更するための setter。
        ハンド境界(新ハンドを put する前)に GUI スレッドから呼ぶこと。`session_repo` 未注入なら
        map を持っても接続を有効化しない(rollback path 維持)。
        """
        self._seat_player_map = dict(seat_player_map or {})
        self._session_layer_active = (
            self._session_repo is not None and bool(self._seat_player_map)
        )

    def _record(self, event: AudioEvent | CameraEvent | RFIDEvent) -> None:
        """生イベントを sidecar に記録する (recorder 未設定なら no-op = 挙動不変)。解釈前に呼ぶ。"""
        if self._event_recorder is not None:
            self._event_recorder.record(event)

    def run(self) -> None:
        logger.info("IntegrationThread started")
        while not self._stop_event.is_set():
            self._drain_camera_queue()
            self._drain_rfid_queue()

            self._publish_table_state_if_due()
            self._check_deal_presence()     # 手札の配布（ADR-0063）
            self._check_table_cleared()     # 片付け（プレーの終わり）

            try:
                event = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                # 配布を検出していて、それより前の発話がもう無ければ新しいハンドを始める（ADR-0062）
                self._start_dealt_hand_if_ready()
                self._expire_buffers()
                continue

            self._record(event)
            self._handle_audio_event(event)
            self._expire_buffers()

        logger.info("IntegrationThread stopped")

    # ――― バッファ管理 ―――

    def _drain_camera_queue(self) -> None:
        if self._camera_queue is None:
            return
        while True:
            try:
                ev = self._camera_queue.get_nowait()
            except queue.Empty:
                break
            self._record(ev)
            self._camera_buffer.append(ev)

    def _drain_rfid_queue(self) -> None:
        if self._rfid_queue is None:
            return
        while True:
            try:
                ev: RFIDEvent = self._rfid_queue.get_nowait()
            except queue.Empty:
                break
            self._record(ev)
            self._process_rfid_event(ev)

    def _process_rfid_event(self, ev: RFIDEvent) -> None:
        """受信した RFIDEvent を役割に応じて振り分ける。"""
        self._dispatch_rfid_event(ev)
        # 卓状態はカードが動くたびに publish する（UI への反映経路, ADR-0056 D5）。
        self._publish_table_state(observed_at=ev.timestamp)

    def _dispatch_rfid_event(self, ev: RFIDEvent) -> None:
        if ev.role == "board":
            self._handle_board_rfid(ev)
        else:
            self._handle_seat_rfid(ev)

    def _warn_duplicate_board_cards(self) -> None:
        """ボードに同じカードが 2 枚以上ある = 物理的にあり得ない（1 組のデッキ）。

        起こり得る原因は **`rfid_cards.json` の重複登録**（同じカード名に 2 つの UID）か
        誤読み。どちらもハンドログが壊れるので WARN + needs_review を立てる（ISSUE-0026）。
        枚数判定によるストリート自動遷移も水増しされるため、黙って進めない。
        """
        dupes = sorted({c for c in self._board_cards if self._board_cards.count(c) > 1})
        if not dupes:
            return
        logger.warning(
            "ボードに同じカードが複数あります: %s（board=%s）— 1 組のデッキではあり得ません。"
            "rfid_cards.json の重複登録を疑ってください（`python tools/register_cards.py list`）。"
            "needs_review を立てます",
            ", ".join(dupes), self._board_cards,
        )
        self._hand_needs_review = True

    def _handle_board_rfid(self, ev: RFIDEvent) -> None:
        """ボードカードの RFID イベントを処理する。"""
        if self._deal_at is not None and ev.timestamp >= self._deal_at:
            # 配ったあとのボードの札は新しいハンドのもの（ADR-0062）
            self._start_dealt_hand_if_ready(force=True)
        elif self._auto_new_hand and not (self._hand_open and self._in_play):
            # 手札が配られる前・プレーが終わったあと（シャッフル・片付け）のボードは読まない（ADR-0063）
            logger.debug("プレー中でないボードの札を無視しました: %s (tag=%s)", ev.card, ev.tag_id)
            return
        if self._auto_new_hand and self._betting_over() and (
            ev.replaces or ev.board_index not in self._board_positions
            and len(self._board_positions) >= 5
        ):
            # ショーダウン待ちのボードは変えない（次のハンドのウォッシュで差し替わるのを防ぐ, ADR-0063）
            logger.info("ショーダウン待ちのボードの差し替えを無視しました: %s", ev.card)
            return
        if not ev.card:
            logger.warning(
                "Board RFID event has no card (tag=%s reader=%s) — needs_review",
                ev.tag_id, ev.reader_id,
            )
            self._hand_needs_review = True
            return

        if ev.board_index is not None:
            # 位置指定あり: board_positions に格納して順序保証
            previous = self._board_positions.get(ev.board_index)
            unchanged = previous == ev.card
            self._board_positions[ev.board_index] = ev.card
            if previous is not None and not unchanged:
                # 配り直しで同じ位置の札が替わった（ADR-0058）。配布時刻も新しい札のものにする。
                self._board_dealt_at[ev.board_index] = ev.timestamp
                self._hand_needs_review = True
                logger.info(
                    "配り直し: ボード %d 枚目を差し替えました（%s → %s）",
                    ev.board_index, previous, ev.card,
                )
            else:
                # 配布時刻は **最初の検出**を採る（再発火で上書きしない, ADR-0055）。
                self._board_dealt_at.setdefault(ev.board_index, ev.timestamp)
            self._board_cards = [
                self._board_positions[i]
                for i in sorted(self._board_positions)
            ]
            # tag を出すのは、同じカード名が別 UID で 2 枚登録されている（= rfid_cards.json の
            # 重複登録）ケースを名前だけのログから切り分けられないため（ISSUE-0026）。
            # 同じ位置に同じ札の再検出は新しい情報ではない。結合の弱いリーダーは載っている札を
            # 何度も読み直すので、INFO のままだと端末が埋まって CLI の入力が壊れる（ISSUE-0034）。
            logger.log(
                logging.DEBUG if unchanged else logging.INFO,
                "Board card [pos=%d]: %s (tag=%s) — board so far: %s",
                ev.board_index, ev.card, ev.tag_id, self._board_cards,
            )
            self._warn_duplicate_board_cards()
            self._try_advance_street_from_rfid()
            self._show_board(
                replaced=(ev.board_index, previous, ev.card)
                if previous is not None and not unchanged else None
            )
        else:
            # board_index なし: 末尾に追記
            self._board_cards.append(ev.card)
            logger.info(
                "Board card (no index): %s (tag=%s) — board so far: %s",
                ev.card, ev.tag_id, self._board_cards,
            )
            self._warn_duplicate_board_cards()
            self._show_board()

        if not self._board_source:
            self._board_source = "rfid"

        if self._on_rfid_card:
            self._on_rfid_card(ev)

    def _handle_seat_rfid(self, ev: RFIDEvent) -> None:
        """座席カードの RFID イベントを処理する (ホールカード蓄積 + アクション照合用)。"""
        if self._auto_new_hand and ev.card and ev.seat is not None and (
            (self._deal_at is not None and ev.timestamp >= self._deal_at) or self._is_next_deal(ev)
        ):
            self._collect_deal(ev)   # 次のハンドの札（ADR-0062）。いまのハンドには入れない
            return
        # ホールカード蓄積 (カード情報がある場合のみ)
        if ev.card and ev.seat is not None:
            seat_cards = self._hole_cards.setdefault(ev.seat, [])
            # デッキ整合: 同じ札を別の席に記録していたら外す（配り直しで席が変わった, ADR-0058）。
            moved = False
            for other_seat, other_cards in self._hole_cards.items():
                if other_seat != ev.seat and ev.card in other_cards:
                    other_cards.remove(ev.card)
                    moved = True
                    logger.info(
                        "配り直し: %s を席 %d から席 %d に移しました", ev.card, other_seat, ev.seat,
                    )
            if ev.replaces and ev.replaces in seat_cards and ev.card not in seat_cards:
                # 配り直しで前の札が消え、新しい札が載り続けた（RFIDThread が確かめてから送る）。
                seat_cards[seat_cards.index(ev.replaces)] = ev.card
                self._hand_needs_review = True
                logger.info(
                    "配り直し: 席 %d の %s を %s に差し替えました（cards: %s）",
                    ev.seat, ev.replaces, ev.card, seat_cards,
                )
                if self._on_rfid_card:
                    self._on_rfid_card(ev)
            elif ev.card not in seat_cards and len(seat_cards) < 2:
                seat_cards.append(ev.card)
                logger.info(
                    "Hole card detected: seat=%d card=%s (cards so far: %s)",
                    ev.seat, ev.card, seat_cards,
                )
                if self._on_rfid_card:
                    self._on_rfid_card(ev)
            if moved:
                self._hand_needs_review = True
            self._show_hole_cards()
        elif not ev.card:
            logger.warning(
                "Seat RFID event has no card (tag=%s reader=%s seat=%s) — needs_review",
                ev.tag_id, ev.reader_id, ev.seat,
            )
            self._hand_needs_review = True

        # アクション照合バッファに追加
        self._rfid_seat_buffer.append(ev)

    def _try_advance_street_from_rfid(self) -> None:
        """ボードカード枚数に応じてストリートを自動推移する (RFID 優先証拠)。"""
        n = len(self._board_positions)
        gs = self._game_state
        target_street = _BOARD_STREET_THRESHOLDS.get(n)
        if target_street is None:
            return
        street_enum = {
            "flop":  Street.FLOP,
            "turn":  Street.TURN,
            "river": Street.RIVER,
        }.get(target_street)
        if street_enum is None:
            return
        if gs.street == street_enum.value:
            return  # 既にそのストリート
        try:
            gs.advance_street(street_enum)
            if gs.street == street_enum.value:
                logger.info(
                    "Street auto-advanced to %s by RFID board cards (%d cards detected)",
                    target_street, n,
                )
            else:
                # rules-aware backend（pokerkit）はベッティング完了で進むので、ここは no-op に
                # なる（契約どおり）。毎回 INFO を出すと端末が埋まるので DEBUG に落とす。
                logger.debug(
                    "RFID street hint %s (%d cards) — backend keeps %s",
                    target_street, n, gs.street,
                )
        except ValueError:
            # 後退遷移など無効な場合は無視
            logger.debug(
                "RFID street advance to %s skipped (current=%s)",
                target_street, gs.street,
            )

    def _now_iso(self) -> str:
        """注入された時計 (既定 time.time) を ISO 文字列に（buffer 期限・fallback 用, F1）。"""
        return self._iso(self._clock())

    @staticmethod
    def _iso(ts: float) -> str:
        """epoch 秒を ISO 文字列に。ActionRecord/HandSummary の時刻は event.timestamp 由来で
        統一する（ADR-0048 T3: live と replay で同じ envelope から同じ時刻が出る）。"""
        return datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")

    def _synth_fold_timestamp(self, seat: int, event: AudioEvent) -> tuple[str, bool]:
        """合成 silent-fold の時刻（ADR-0055）。

        既定は推定を引き起こした後続アクション（`event`）の時刻だが、RFID が
        「その席のカードが消えたまま戻っていない時刻」を持っていればそれを使う。
        ディーラーが fold を宣言しない席の**実時刻**は他に手掛かりがないため、
        音声の時系列と突き合わせて履歴を再生するにはこれが唯一の情報源になる。

        **不在そのものを fold の判定に使わない**のが要点（プレイヤーはカードを持ち上げて
        見るので、不在 ≠ fold）。fold の判定は従来どおり合法手・actor 推定が行い、
        ここは時刻だけを差し替える。時刻はハンド開始〜現在にクランプする
        （前ハンドの観測や未来時刻が混ざらないように）。
        """
        fallback = self._iso(event.timestamp)
        now = self._clock()
        if self._seat_cards_absent_since is None:
            return fallback, False
        try:
            absent = self._seat_cards_absent_since(seat)
        except Exception:  # noqa: BLE001 — 観測が取れなくても記録は続ける
            logger.exception("seat_cards_absent_since failed (seat=%s)", seat)
            return fallback, False
        if absent is None or self._hand_started_epoch is None:
            return fallback, False
        if not self._hand_started_epoch <= absent <= max(now, event.timestamp):
            logger.debug(
                "muck 観測 %.3f がハンド範囲外（開始 %.3f, 現在 %.3f）— イベント時刻を使います",
                absent, self._hand_started_epoch, now,
            )
            return fallback, False
        return self._iso(absent), True

    def _expire_buffers(self) -> None:
        cutoff = self._clock() - CAMERA_BUFFER_TTL
        self._camera_buffer = [e for e in self._camera_buffer if e.timestamp >= cutoff]
        self._rfid_seat_buffer = [e for e in self._rfid_seat_buffer if e.timestamp >= cutoff]

    # ――― 照合窓（ADR-0048 T1/T2: 発話区間ベースの両側窓）―――

    @staticmethod
    def _event_window(event: AudioEvent) -> tuple[float, float]:
        """audio イベントの照合窓 [lo, hi] を返す。

        発話開始（utterance_start_ts）から ASR 確定（timestamp）までの発話区間の両側に
        MATCH_WINDOW を張る。utterance_start_ts 欠損（旧記録/直接構築）時は従来どおり
        timestamp ± MATCH_WINDOW に退化する（後方互換）。
        """
        start = event.utterance_start_ts if event.utterance_start_ts is not None else event.timestamp
        if start > event.timestamp:
            start = event.timestamp
        return start - MATCH_WINDOW, event.timestamp + MATCH_WINDOW

    @staticmethod
    def _window_distance(ts: float, lo: float, hi: float) -> float:
        """窓 [lo, hi] からの距離（窓内は 0）。最近傍選択に使う。"""
        if ts < lo:
            return lo - ts
        if ts > hi:
            return ts - hi
        return 0.0

    def _pop_matching_camera_event(self, seat: int, event: AudioEvent) -> Optional[CameraEvent]:
        lo, hi = self._event_window(event)
        candidates = [e for e in self._camera_buffer if e.seat == seat and lo <= e.timestamp <= hi]
        if not candidates:
            return None
        best = min(candidates, key=lambda e: self._window_distance(e.timestamp, lo, hi) or abs(e.timestamp - event.timestamp))
        self._camera_buffer.remove(best)
        return best

    def _pop_matching_rfid_event(self, seat: int, event: AudioEvent) -> Optional[RFIDEvent]:
        """同席・照合窓内の RFID seat イベントを返し除去する。"""
        lo, hi = self._event_window(event)
        candidates = [
            e for e in self._rfid_seat_buffer
            if e.seat == seat and lo <= e.timestamp <= hi
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda e: abs(e.timestamp - event.timestamp))
        self._rfid_seat_buffer.remove(best)
        return best

    # ――― イベントハンドラ ―――

    def _handle_audio_event(self, event: AudioEvent) -> None:
        """audio イベントの入口。例外はここで捕捉して unresolved レコードに変換する
        （ADR-0047 B4: live の run() 捕捉と replay の直接呼び出しで同一セマンティクス。
        イベントの無音消失を全廃する = B2）。"""
        if self._deal_at is not None and _spoken_at(event) >= self._deal_at:
            # 配ったあとに話されたアクションは新しいハンドのもの。前に話されたもの（前のハンドの
            # 最後のコールやマック）は、認識が遅れて届いても前のハンドに入れる（ADR-0062）。
            self._start_dealt_hand_if_ready(force=True)
        try:
            self._dispatch_audio_event(event)
        except Exception:
            logger.exception("Error handling audio event: %s", event)
            self._emit_unresolved(event, reason="handler_error")

    def _dispatch_audio_event(self, event: AudioEvent) -> None:
        action = event.action
        gs = self._game_state

        # G1（ADR-0049）: 低信頼 ASR の制御語は状態を動かさず保留（閾値 0 = 無効が既定）。
        if (
            action in _CONTROL_ACTIONS
            and self._control_conf_threshold > 0.0
            and event.confidence is not None
            and event.confidence < self._control_conf_threshold
        ):
            self._emit_unresolved(event, reason="low_conf_control_held")
            return

        if action == "new_hand":
            if self._hand_auto_started and self._hand_open and not self._current_actions:
                # 手札を配った時点で始めている（ADR-0062）。二重に始めるとボタンが 2 つ進む。
                self._notice(f"ハンド {gs.hand_id} は手札が配られた時点で始めています（「ハンド開始」は不要です）")
                return
            # G1: 勝者未宣言のまま新ハンドが宣言された → 進行中の記録を捨てず異常確定する。
            if self._hand_open and self._current_actions and not self._finish_by_rules(event):
                logger.warning(
                    "new_hand mid-hand (hand_id=%d): 勝者未宣言のため異常確定してから開始する",
                    gs.hand_id,
                )
                self._hand_needs_review = True
                self._finalize_hand(self._fallback_winner_seat(), event)
            self._start_new_hand(event)
            return

        if action == "end_hand":
            self._handle_end_hand(event)
            return

        if action == "showdown":
            gs.advance_street(Street.SHOWDOWN)
            return

        if action == "winner":
            self._handle_winner(event)
            return

        if action == "rebuy":
            self._handle_rebuy(event)
            return

        if action == "correct_board":
            self._handle_correct_board(event)
            return

        if action == "correct_seat":
            self._handle_correct_seat(event)
            return

        if action == "rename_seat":
            self._handle_rename_seat(event)
            return

        # ベッティングアクション。rules-aware backend（pokerkit）は境界で actor 推定 + 合法手
        # 射影、legacy（空 legal_context）は従来経路で挙動不変（ADR-0009 §1）。
        legal_ctx = gs.legal_context()
        if "amount_only" in event.parse_flags:
            # 数字だけの発話 = ベットかレイズ。使えない額なら記録しない（「7」「いまのベットと同じ額」）
            problem = self._amount_only_problem(event, legal_ctx)
            if problem is not None:
                self._notice(f"数字だけの「{event.raw_text}」は記録しませんでした（{problem}）")
                return
            event = replace(event, action="raise" if "raise" in legal_ctx.legal_actions else "bet")
        if legal_ctx.legal_actions:
            if event.action == "call" and legal_ctx.amount_to_call == 0 and self._said_before_street(event):
                # チェックとして次の人の手番を食わない（言い直しの「600点コールです」, 店舗の実測）
                self._notice(f"「{event.raw_text}」は前のストリートのコール（言い直し）とみなして記録しませんでした")
                return
            if "check_around" in event.parse_flags:
                if self._said_before_street(event):
                    self._notice(f"「{event.raw_text}」は前のストリートのチェックアラウンドとみなして記録しませんでした")
                    return
                self._handle_check_around(event, legal_ctx)
                return
            self._handle_rules_aware_action(event, legal_ctx)
        elif self._betting_over():
            # ベッティングが終わってショーダウンを待っている。ここでの「フォールド」はマック（ADR-0062）
            if action == "fold" and self._auto_winner:
                self._apply_showdown_muck(event)
            else:
                self._emit_unresolved(event, reason="betting_over")
        else:
            self._handle_legacy_action(event)

    def _amount_only_problem(self, event: AudioEvent, ctx: LegalContext) -> Optional[str]:
        """数字だけの発話をベット・レイズにできない理由（できるなら None）。

        レイズはいまのベットより大きい額、ベットは最小ベット以上の額だけ（ショーダウンで手を読み上げた
        「7」や、コールの額を言い直した「600点」をアクションにしない）。
        """
        legal = ctx.legal_actions
        if not legal or ctx.actor_seat is None:
            return "ベッティングは終わっています" if self._betting_over() else "ベッティング中ではありません"
        if "raise" in legal:
            current = ctx.committed + ctx.amount_to_call
            if event.amount <= current:
                return f"いまのベット {current} 以下の額です"
        elif "bet" in legal:
            if event.amount < ctx.min_raise:
                return f"最小ベット {ctx.min_raise} より少ない額です"
        else:
            return "ベット・レイズできる手番ではありません"
        return None

    def _street_started_at(self) -> Optional[float]:
        """いまのストリートの最初の札が見えた時刻（フロップは 3 枚のうち最初）。読めていなければ None。"""
        indices = {"flop": (1, 2, 3), "turn": (4,), "river": (5,)}.get(self._game_state.street, ())
        times = [self._board_dealt_at[i] for i in indices if i in self._board_dealt_at]
        return min(times) if times else None

    def _said_before_street(self, event: AudioEvent) -> bool:
        """発話がいまのストリートの札より前に始まった（= 前のストリートのラウンドの発話）か。

        ボードを RFID で読んでいるハンドのターン・リバーで、その札がまだ見えていなければ前（配る前）。
        フロップは札がまだ 1 枚も読めていないと RFID の有無が分からないので、見えたときだけ比べる。
        """
        street = self._game_state.street
        if street not in ("flop", "turn", "river"):
            return False
        started = self._street_started_at()
        if started is None:
            return street != "flop" and self._board_source == "rfid"
        return _spoken_at(event) < started - STALE_CALL_MARGIN_SEC

    def _handle_check_around(self, event: AudioEvent, legal_ctx: LegalContext) -> None:
        """「チェックアラウンド」= このラウンドでまだ動いていない全員がチェックした（オーナーの説明）。

        手番の人から順に、ラウンドが閉じる（ストリートが進む / ベッティングが終わる）までチェックを入れる。
        最初の人がチェックできない（ベットがある）ときは、1 つのチェックとして読む（合法手へ射影して要確認）。
        """
        gs = self._game_state
        if "check" not in legal_ctx.legal_actions:
            self._handle_rules_aware_action(event, legal_ctx)
            return
        street = gs.street
        each = replace(event, seat=None, position=None)
        ctx = legal_ctx
        for _ in range(max(len(self._game_seats()), 1)):
            self._handle_rules_aware_action(each, ctx)
            ctx = gs.legal_context()
            if gs.street != street or ctx.actor_seat is None or "check" not in ctx.legal_actions:
                return

    def _handle_winner(self, event: AudioEvent) -> None:
        """winner 宣言の処理。複数席の読み上げは split pot（ADR-0050 S7）、席が読めない場合は
        fallback 連鎖（ADR-0047 B5: 単独 active 席 → 最後のアグレッサー + review → 手番席）。"""
        seats = _extract_all_seat_nos(event.raw_text)

        if (
            self._hand_auto_started and self._hand_open and not self._current_actions
            and self._last_result
            and _spoken_at(event) - (self._hand_started_epoch or 0.0) <= LATE_WINNER_SEC
        ):
            # 配ったばかりのハンドに勝者は無い = 前のハンドへの宣言が遅れて届いた（ADR-0062）
            self._late_winner(event, seats)
            return

        # 進行中のハンドが無い（新ハンド前 / 確定済み）winner は保留する（ISSUE-0028）。
        # pokerkit は end_hand 後に actor を持たず、確定済みハンドへの再宣言を通すとポットの
        # 二重加算か（end_hand が例外になり）空 summary の量産になる。legacy は
        # is_hand_active が常に True なので従来どおり下の判定に進む。
        if not self._game_state.is_hand_active():
            if self._auto_winner and self._last_result:
                self._late_winner(event, seats)
                return
            self._emit_unresolved(event, reason="no_active_hand")
            return

        # 確定対象が何も無い winner（進行中ハンドなし・アクションもカードも review 状態も無い）
        # はハルシネーション疑い → 保留（空 summary を書かない, ADR-0047 B5）。
        # 何かしら記録があれば従来どおり確定する（明示 new_hand なしの運用も従来サポート）。
        if (
            not self._hand_open
            and not self._current_actions
            and not self._board_cards
            and not self._hole_cards
            and not self._hand_needs_review
        ):
            self._emit_unresolved(event, reason="no_active_hand")
            return

        if len(seats) > 1:
            self._finalize_hand(seats[0], event, winner_seats=seats)
            return

        winner_seat = seats[0] if seats else None
        if winner_seat is None:
            # 席を言わない「ウィナー」: ショーダウンなら残った全員が見せたとして手札で決める（ADR-0062）
            if self._finish_by_rules(event):
                return
            winner_seat = self._fallback_winner_seat()
            if winner_seat is None:
                self._emit_unresolved(event, reason="winner_seat_unresolved")
                return
            logger.warning(
                "Could not extract winner seat from %r, using inferred seat=%d",
                event.raw_text, winner_seat,
            )
        self._finalize_hand(winner_seat, event)

    def _fallback_winner_seat(self) -> Optional[int]:
        """勝者席が読み上げから取れないときの推定連鎖（ADR-0047 B5）。

        1. active 席が 1 つ → その席（全員 fold の決定的ケース、review 不要）。
        2. 最後のアグレッサー（bet/raise/allin）→ 推定なので hand を review に。
        3. engine の手番席（従来 fallback）→ 同じく review。
        4. どれも取れなければ None（呼び出し側が保留レコード化）。
        """
        gs = self._game_state
        active = self._remaining_seats()   # ショーダウンでマックした席は除く（ADR-0062）
        if len(active) == 1:
            return active[0]
        for rec in reversed(self._current_actions):
            if rec.action in ("bet", "raise", "allin") and (not active or rec.seat in active):
                self._hand_needs_review = True
                return rec.seat
        try:
            seat = gs.get_current_player()
        except (RuntimeError, ValueError):
            return None
        self._hand_needs_review = True
        return seat

    # ――― 手札が配られたら新しいハンド（ADR-0062）―――

    def _notice(self, message: str) -> None:
        """操作する人へのお知らせ（CLI に出す）。logger にも残す。"""
        logger.info("%s", message)
        if self._on_notice is not None:
            try:
                self._on_notice(message)
            except Exception:  # noqa: BLE001 — 表示の失敗で記録を止めない
                logger.exception("on_notice failed")

    # ――― 読んだ札の表示（CLI）―――

    def _card_info(self, message: str) -> None:
        """RFID で読んだ札を 1 行で知らせる（CLI の [カード] 行）。logger にも残す。"""
        logger.info("カード: %s", message)
        if self._on_cards is not None:
            try:
                self._on_cards(message)
            except Exception:  # noqa: BLE001 — 表示の失敗で記録を止めない
                logger.exception("on_cards failed")

    def _show_hole_cards(self) -> None:
        """2 枚そろった席の手札を出す（まだ出していない席・配り直しで変わった席だけ）。"""
        if not self._hand_open:
            return
        shown = []
        for seat in sorted(self._hole_cards):
            cards = tuple(self._hole_cards[seat])
            if len(cards) != 2 or self._shown_holes.get(seat) == cards:
                continue
            redeal = seat in self._shown_holes
            self._shown_holes[seat] = cards
            shown.append(f"席{seat} {_cards_text(cards)}" + ("（配り直し）" if redeal else ""))
        if shown:
            self._card_info("手札 " + " ／ ".join(shown))

    def _show_board(self, replaced: Optional[tuple[int, str, str]] = None) -> None:
        """フロップ（3 枚）・ターン・リバーがそろったとき、配り直しで替わったときにボードを出す。"""
        if not self._hand_open:
            return
        board = self._board_cards
        if replaced is not None:
            index, old, new = replaced
            self._card_info(
                f"ボード {index} 枚目を差し替え {_card_text(old)} → {_card_text(new)}"
                f"（{_cards_text(board)}）"
            )
        elif len(board) > self._shown_board and len(board) >= 3:
            if len(board) == 3:
                self._card_info(f"フロップ {_cards_text(board)}")
            else:
                name = "ターン" if len(board) == 4 else "リバー"
                self._card_info(f"{name} {_card_text(board[-1])}（ボード {_cards_text(board)}）")
        self._shown_board = len(board)

    def _describe_cards(self, summary: HandSummary) -> str:
        """確定したハンドの札をまとめて 1 行にする（ボード / 席ごとの手札と、見せた手の役）。"""
        from core.showdown import HAND_NAMES_JA

        hands = {h["seat"]: HAND_NAMES_JA.get(h["hand"], h["hand"]) for h in summary.showdown or []}
        parts = [f"ボード {_cards_text(summary.board)}"] if summary.board else []
        for player in summary.players:
            if player.get("hole_cards"):
                text = f"席{player['seat']} {_cards_text(player['hole_cards'])}"
                if player["seat"] in hands:
                    text += f"（{hands[player['seat']]}）"
                parts.append(text)
        return " ／ ".join(parts)

    def _hand_in_play(self) -> bool:
        """配り終えてプレーが始まっているか（アクション・ボードがある / ベッティングが終わった）。

        まだ何も起きていないハンドに届いた札は、同じハンドの配り直しとして扱う（ADR-0058）。
        """
        return bool(self._current_actions or self._board_positions or self._betting_over())

    def _is_next_deal(self, ev: RFIDEvent) -> bool:
        """この席の札が **次のハンドの配布**か（ADR-0062）。

        - ハンドが無い（前のハンドは確定済み / まだ始めていない）ときに卓の席へ置かれた札。
        - プレー中のハンドで、手札 2 枚がそろっている席に来た別の札・差し替え・別の席の札。
          手札がそろっていない席に来た新しい札は、読み遅れた手札としていまのハンドに入れる。
        """
        try:
            seats = self._game_state.get_stacks()
        except Exception:  # noqa: BLE001
            return False
        if ev.seat not in seats:
            return False                     # 卓で使っていない席のリーダー
        if not self._hand_open:
            return True
        cards = self._hole_cards.get(ev.seat, [])
        if ev.card in cards or not self._hand_in_play():
            return False                     # 同じ札の読み直し / まだ配っている（配り直し）
        return (
            ev.replaces is not None
            or len(cards) >= 2
            or ev.card in self._board_cards
            or any(ev.card in other for seat, other in self._hole_cards.items() if seat != ev.seat)
        )

    def _collect_deal(self, ev: RFIDEvent) -> None:
        """次のハンドの札を集める。

        配布と判断したあと（`_deal_at` あり）の札は、そのハンドの手札にする。判断の前は、RFID の在否が
        あれば在否で判断し（`_check_deal_presence`, シャッフルで一瞬通った札では始めない）、無ければ
        （replay・HTTP 受信）2 席以上に 2 枚ずつ読めたら配布とする。
        """
        if self._deal_at is not None:
            self._add_deal_card(ev.seat, ev.card)
            return
        # 片付けの途中で触れた札などは、窓を過ぎたら捨てる
        self._deal_events = [
            e for e in self._deal_events if ev.timestamp - e[2] <= DEAL_WINDOW_SEC
        ]
        if not any(s == ev.seat and c == ev.card for s, c, _ in self._deal_events):
            self._deal_events.append((ev.seat, ev.card, ev.timestamp))
        if self._seat_presence is not None:
            self._check_deal_presence()
            return
        hands: dict[int, list[str]] = {}
        for seat, card, _ in self._deal_events:
            hands.setdefault(seat, []).append(card)
        dealt = [s for s, cards in hands.items() if len(cards) >= 2]
        if len(dealt) >= 2:
            self._deal_detected(min(ts for _, _, ts in self._deal_events), hands)

    def _add_deal_card(self, seat: int, card: str) -> None:
        cards = self._deal_hands.setdefault(seat, [])
        if len(cards) < 2 and not any(card in c for c in self._deal_hands.values()):
            cards.append(card)

    def _game_seats(self) -> list[int]:
        try:
            return sorted(self._game_state.get_stacks())
        except Exception:  # noqa: BLE001
            return []

    def _check_deal_presence(self) -> None:
        """RFID の在否から次のハンドの配布を検出する（ADR-0063）。

        配布 = 卓の 2 席以上に、前のハンドと違う手札 2 枚が `DEAL_STABLE_SEC` 載り続け、ボードの
        リーダーが空。シャッフル・ウォッシュでリーダーを一瞬通った札や、ショーダウンのあと残っている
        前のハンドの札では始めない。
        """
        if not self._auto_new_hand or self._seat_presence is None or self._deal_at is not None:
            return
        if self._hand_open and not self._hand_in_play():
            self._deal_since = {}          # 始めたばかりのハンド（配り直しは同じハンド）
            return
        now = self._clock()
        try:
            snapshot = self._seat_presence() or {}
            board = (self._board_presence() or {}) if self._board_presence is not None else {}
        except Exception:  # noqa: BLE001 — 在否が取れなければ判断しない
            return
        hands: dict[int, list[str]] = {}
        for seat in self._game_seats():
            cards = list((snapshot.get(seat) or {}).get("cards") or [])
            previous = self._hole_cards.get(seat, [])
            if len(cards) >= 2 and any(c not in previous for c in cards):
                since = self._deal_since.get(seat, (now, now))[0]
                self._deal_since[seat] = (since, now)
                hands[seat] = cards[:2]
            elif seat in self._deal_since and now - self._deal_since[seat][1] > DEAL_GAP_SEC:
                del self._deal_since[seat]
        stable = [
            s for s, (since, last) in self._deal_since.items()
            if now - since >= DEAL_STABLE_SEC and now - last <= DEAL_GAP_SEC and s in hands
        ]
        if len(stable) < 2 or (board.get("present_count") or 0) > 0:
            return
        self._deal_detected(min(self._deal_since[s][0] for s in stable), hands)

    def _deal_detected(self, deal_at: float, hands: dict[int, list[str]]) -> None:
        """配布と判断した。RFID のボード位置をいま捨て（シャッフル中に読んだ札を持ち越さない）、
        プレー中にして、配る前の発話を反映し終えたら新しいハンドを始める。"""
        self._deal_at = deal_at
        self._deal_detected_at = self._clock()
        self._deal_since = {}
        self._deal_hands = {}
        for seat, cards in sorted(hands.items()):
            for card in cards:
                self._add_deal_card(seat, card)
        logger.info("手札の配布を検出しました（%s）", self._deal_hands)
        self._set_in_play(True)
        if self._on_new_hand is not None:
            try:
                self._on_new_hand()
                self._rfid_reset_for_deal = True
            except Exception:  # noqa: BLE001
                logger.exception("on_new_hand hook failed at deal")
        self._start_dealt_hand_if_ready()

    def _set_in_play(self, in_play: bool) -> None:
        """プレー中かを切り替える（音声の聞き取りとボードの受付, ADR-0063）。"""
        if self._in_play == in_play:
            return
        self._in_play = in_play
        self._table_empty_since = None
        if self._listen_gate is not None:
            if in_play:
                self._listen_gate.set()
            else:
                self._listen_gate.clear()
        logger.info("プレー中: %s", "はい（聞き取りを再開）" if in_play else "いいえ（次の配布まで音声を聞き流す）")

    def _check_table_cleared(self) -> None:
        """ハンドが確定していなくても、卓に札が無い状態が続いたらプレーは終わったとみなす。"""
        if not (self._in_play and self._hand_open and self._deal_at is None) or self._seat_presence is None:
            return
        try:
            seats = self._seat_presence() or {}
            board = (self._board_presence() or {}) if self._board_presence is not None else {}
        except Exception:  # noqa: BLE001
            return
        if any(info.get("present") for info in seats.values()) or (board.get("present_count") or 0):
            self._table_empty_since = None
            return
        now = self._clock()
        if self._table_empty_since is None:
            self._table_empty_since = now
        elif now - self._table_empty_since >= TABLE_CLEAR_SEC:
            self._set_in_play(False)

    def _speech_pending_before_deal(self) -> bool:
        """配る前に話された発話が、まだ認識中か（先に前のハンドへ反映する）。"""
        if self._speech_backlog is None:
            return False
        waited = self._clock() - (self._deal_detected_at if self._deal_detected_at is not None
                                  else self._clock())
        if waited > DEAL_SPEECH_WAIT_SEC:
            logger.warning(
                "配布の検出から %.0f 秒たっても認識待ちの発話があります — 新しいハンドを先に始めます",
                waited,
            )
            return False
        try:
            pending = int(self._speech_backlog())
        except Exception:  # noqa: BLE001 — 待てないなら始める
            return False
        return pending > 0 or not self._audio_queue.empty()

    def _start_dealt_hand_if_ready(self, force: bool = False) -> None:
        """配布を検出していれば、そのハンドを始める（前のハンドが終わっていなければ先に確定する）。"""
        if self._deal_at is None:
            return
        if not force and self._speech_pending_before_deal():
            return
        if self._hand_open and self._hand_in_play():
            self._close_hand_for_next_deal()
        if self._hand_open:
            # 配る前に「ハンド開始」/ n で始めていたハンド → 配った札をこのハンドの手札にする
            self._adopt_deal_cards()
            return
        self._start_new_hand(started_at=self._deal_at, auto=True)

    def _adopt_deal_cards(self) -> None:
        """配布の検出で集めた札を、いまのハンドの手札にする（配布と判断していない札は捨てる）。"""
        if self._deal_at is not None:
            for seat, dealt in self._deal_hands.items():
                cards = self._hole_cards.setdefault(seat, [])
                for card in dealt:
                    if len(cards) < 2 and not any(card in c for c in self._hole_cards.values()):
                        cards.append(card)
            logger.info("配られた手札: %s", {s: c for s, c in self._hole_cards.items() if c})
        self._deal_events = []
        self._deal_hands = {}
        self._deal_at = None
        self._deal_detected_at = None

    def _close_hand_for_next_deal(self) -> None:
        """次の手札が配られたのに確定していないハンドを、確定してから次へ進む。"""
        if self._finish_by_rules(ended_ts=self._deal_at):
            return
        gs = self._game_state
        remaining = self._remaining_seats()
        if len(remaining) == 1:   # 全員フォールド（勝者の自動判定が off でも決まっている）
            self._finalize_hand(remaining[0], winner_source="fold", ended_ts=self._deal_at)
            return
        winner = self._fallback_winner_seat()
        if winner is None:
            remaining = self._remaining_seats()
            winner = remaining[0] if remaining else min(gs.get_stacks())
        self._hand_needs_review = True
        self._notice(
            f"ハンド {gs.hand_id} の勝者が決まらないまま次の手札が配られました — "
            f"席{winner} を仮の勝者にします（要確認）"
        )
        self._finalize_hand(winner, winner_source="estimated", ended_ts=self._deal_at)

    # ――― 勝者の自動判定（ADR-0062）―――

    def _remaining_seats(self) -> list[int]:
        """まだポットを争っている席（フォールドもショーダウンでのマックもしていない）。"""
        try:
            active = self._game_state.get_active_seats()
        except Exception:  # noqa: BLE001
            return []
        return [s for s in active if s not in self._showdown_mucks]

    def _betting_over(self) -> bool:
        """ハンドの途中でベッティングが終わっている（手番が無い = ショーダウン待ち）か。"""
        if not (self._rules_aware and self._hand_open):
            return False
        gs = self._game_state
        try:
            return gs.is_hand_active() and gs.legal_context().actor_seat is None
        except Exception:  # noqa: BLE001
            return False

    def _maybe_finish_hand(self, event: Optional[AudioEvent] = None) -> bool:
        """ほかが全員フォールド（マック）したら、残った人の勝ちで確定する。

        2 人以上が残ってベッティングが終わったとき（ショーダウン）は、ここでは決めない。見せずに
        マックした人は手札が強くてもポットを失うので、手札で決めてよいのは誰もマックしなかった
        ときだけ（「ハンド終了」/ 次の配布 = `_finish_showdown`）。
        """
        if not (self._auto_winner and self._rules_aware and self._hand_open):
            return False
        if not self._game_state.is_hand_active():
            return False
        remaining = self._remaining_seats()
        if len(remaining) == 1:
            self._finalize_hand(remaining[0], event, winner_source="fold")
            return True
        if len(remaining) >= 2 and self._betting_over() and not self._showdown_notice_shown:
            self._showdown_notice_shown = True
            self._notice(
                "ショーダウン（" + "・".join(f"席{s}" for s in remaining) + "）— 見せずにマックしたら"
                "「フォールド」、全員見せたら「ハンド終了」（言わなければ次の手札が配られたときに判定）"
            )
        return False

    def _finish_by_rules(
        self, event: Optional[AudioEvent] = None, *, explicit: bool = False,
        ended_ts: Optional[float] = None,
    ) -> bool:
        """ルールで勝者が決まるなら確定する（1 人残り / ショーダウンを手札で）。決まったら True。"""
        if not (self._auto_winner and self._rules_aware and self._hand_open):
            return False
        if not self._game_state.is_hand_active():
            return False
        remaining = self._remaining_seats()
        if len(remaining) == 1:
            self._finalize_hand(remaining[0], event, winner_source="fold", ended_ts=ended_ts)
            return True
        if len(remaining) >= 2 and self._betting_over():
            return self._finish_showdown(event, explicit=explicit, ended_ts=ended_ts)
        return False

    def _finish_showdown(
        self, event: Optional[AudioEvent] = None, *, explicit: bool = False,
        ended_ts: Optional[float] = None,
    ) -> bool:
        """残った全員が手札を見せたとして、RFID の手札とボードで勝者を決める（side pot も）。"""
        from core.showdown import award_pots, evaluate_hands

        gs = self._game_state
        remaining = self._remaining_seats()
        board = list(self._board_cards)
        gaps = [] if len(board) == 5 else [f"ボード {len(board)}/5 枚"]
        for seat in remaining:
            n = len(self._hole_cards.get(seat, []))
            if n < 2:
                gaps.append(f"席{seat} の手札 {n}/2 枚")
        cards = board + [c for s in remaining for c in self._hole_cards.get(s, [])]
        if not gaps and len(set(cards)) != len(cards):
            gaps.append("同じ札が 2 か所にあります")
        if gaps:
            message = f"勝者を手札で判定できません（{'・'.join(gaps)}）— w <席> で入力してください"
            if explicit:
                self._notice(message)
            else:
                logger.info("%s", message)
            return False
        try:
            hands = evaluate_hands({s: self._hole_cards[s] for s in remaining}, board)
            pots = gs.current_pots() or [{"amount": gs.pot, "eligible_seats": remaining}]
            awards, winners_by_pot = award_pots(pots, hands, gs.acting_order())
        except Exception:  # noqa: BLE001 — 判定できなければ人に任せる
            logger.exception("手札での勝者判定に失敗しました")
            if explicit:
                self._notice("勝者を手札で判定できませんでした — w <席> で入力してください")
            return False
        if not winners_by_pot:
            return False
        order = gs.acting_order()
        showdown = [hands[s].to_dict() for s in sorted(remaining, key=lambda s: order.index(s)
                                                       if s in order else s)]
        self._finalize_hand(
            winners_by_pot[0][0], event, awards=awards, winner_source="cards",
            showdown=showdown, ended_ts=ended_ts,
        )
        return True

    def _mucked_stronger_hand(self, seat: int, remaining: list[int]) -> bool:
        """マックした席の手札が、残りの誰よりも強かったか（手札とボードが全部読めているときだけ）。"""
        from core.showdown import evaluate_hands

        board = list(self._board_cards)
        if len(board) != 5 or any(len(self._hole_cards.get(s, [])) != 2 for s in remaining):
            return False
        try:
            hands = evaluate_hands({s: self._hole_cards[s] for s in remaining}, board)
        except Exception:  # noqa: BLE001 — 参考情報
            return False
        others = [hands[s].value for s in remaining if s != seat]
        return bool(others) and hands[seat].value > max(others)

    def _apply_showdown_muck(self, event: AudioEvent) -> None:
        """ベッティングが終わったあとの「フォールド」= ショーダウンでのマック。

        見せずにマックした人は、手札が強くてもポットを受け取れない。席が言われなければ、残っている中で
        一番アウトオブポジション（ボタンの次の席から）の人がマックしたとみなす（店の運用）。
        手札で見るとマックした人の方が強かったときは、勝者はマックしなかった人のまま要確認にする。
        """
        gs = self._game_state
        remaining = self._remaining_seats()
        sensed = self._sensed_seat(event)
        by_order = sensed is None or sensed not in remaining
        if by_order:
            seat = next((s for s in gs.acting_order() if s in remaining), None)
        else:
            seat = sensed
        if seat is None or len(remaining) < 2:
            self._emit_unresolved(event, reason="betting_over")
            return
        stronger = self._mucked_stronger_hand(seat, remaining)
        self._showdown_mucks.append(seat)
        confidence = derive_confidence(
            apply_ok=True,
            whisper_conf=(
                event.confidence if event.confidence is not None else MISSING_WHISPER_CONF
            ),
            audio_agree=True,
            rfid_present=False, rfid_agree=False,
            camera_present=False, camera_agree=False,
        )
        reasons = ["showdown_muck"]
        if by_order and len(remaining) > 2:
            reasons.append("muck_order_assumed")   # 3 人以上: 順番どおりにマックしたとは限らない
        if stronger:
            reasons.append("mucked_stronger_hand")
        reasons.extend(event.parse_flags)
        needs_review = len(reasons) > 1 or confidence < REVIEW_THRESHOLD
        if by_order:
            actor_source = "engine_prior"
        elif event.seat == seat:
            actor_source = "spoken_seat"
        else:
            actor_source = "spoken_position"
        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._iso(event.timestamp),
            street=Street.SHOWDOWN.value,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action="fold",
            amount=0,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source={"camera": False, "audio": True, "rfid": False},
            needs_review=needs_review,
            confidence=confidence,
            position=self._position_of(seat),
            actor_source=actor_source,
            reason="+".join(reasons),
            asr_confidence=event.confidence,
            apply_ok=True,
            raw_text=event.raw_text or None,
        )
        self._current_actions.append(record)
        if self._on_action:
            self._on_action(record)
        if stronger:
            self._notice(f"席{seat} がマック — 手札はこちらの方が強いので確認してください（要確認）")
        self._maybe_finish_hand(event)

    def _handle_end_hand(self, event: AudioEvent) -> None:
        """「ハンド終了」: 残った全員が見せたとして勝者を決める。決められなければ `w <席>` を案内する。"""
        gs = self._game_state
        if not self._hand_open or not gs.is_hand_active():
            logger.info("「ハンド終了」— 進行中のハンドはありません（確定済み）")
            return
        if not (self._auto_winner and self._rules_aware):
            self._notice("勝者を w <席> で入力してください")
            return
        if self._finish_by_rules(event, explicit=True):
            return
        if not self._betting_over():
            actor = gs.legal_context().actor_seat
            self._notice(
                f"ハンド {gs.hand_id} はまだベッティングの途中です（次は席{actor} の番）— "
                "抜けたアクションが無いか確認し、勝者は w <席> で入力してください"
            )

    def _late_winner(self, event: AudioEvent, seats: list[int]) -> None:
        """確定したハンドのあとに届いた `w` / 「ウィナー」（配ったばかりのハンドには勝者が無い）。"""
        last = self._last_result or {}
        winners = last.get("winners", [])
        shown = "・".join(f"席{s}" for s in winners) or "?"
        if not seats or set(seats) <= set(winners):
            self._notice(f"ハンド {last.get('hand_id')} の勝者は {shown} で確定しています")
            return
        self._notice(
            f"ハンド {last.get('hand_id')} は {shown} の勝ちで確定済みです — "
            "違っていればスマホの訂正画面で直してください"
        )
        self._emit_unresolved(event, reason="winner_after_hand_end")

    def _emit_unresolved(self, event: AudioEvent, reason: str) -> None:
        """状態に適用できなかった audio イベントを「適用不能レコード」として必ず可視化する
        （ADR-0047 B2: 無音消失の全廃）。ゲーム状態は変更しないため _current_actions には積まず
        on_action（GUI/監査）にのみ流す。進行中ハンドがあればハンド全体を要レビューにする。"""
        gs = self._game_state
        seat = event.seat if event.seat is not None else 0
        try:
            name = gs.get_player_name(seat) if seat else ""
        except Exception:
            name = ""
        try:
            stack = gs.get_stack(seat) if seat else 0
        except Exception:
            stack = 0
        try:
            pot = gs.pot
        except Exception:
            pot = 0
        try:
            street = gs.street
        except Exception:
            street = Street.PREFLOP.value
        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._iso(event.timestamp),
            street=street,
            seat=seat,
            player_name=name,
            action=event.action,
            amount=event.amount,
            pot_after=pot,
            stack_after=stack,
            source={"camera": False, "audio": True, "rfid": False},
            needs_review=True,
            confidence=0.0,
            position=self._position_of(seat) if seat else "",
            actor_source="unresolved",
            reason=reason,
            asr_confidence=event.confidence,
            apply_ok=False,
            raw_text=event.raw_text or None,
        )
        if self._hand_open:
            self._hand_needs_review = True
        if self._on_action:
            self._on_action(record)
        if reason == "no_active_hand":
            # 運用ミス（`n` の打ち忘れ / ハンド終了後の入力）なので次の操作を案内する（ISSUE-0028）。
            logger.warning(
                "進行中のハンド（手番）が無いため %s（raw=%r）を保留しました — "
                "先に「新ハンド」（CLI の n）を実行してください",
                event.action, event.raw_text,
            )
        elif reason == "betting_over":
            logger.warning(
                "ベッティングは終わっています（ショーダウン待ち）— %s（raw=%r）を保留しました",
                event.action, event.raw_text,
            )
        else:
            logger.warning("Unresolved audio event (%s): %r", reason, event.raw_text)

    def _handle_rebuy(self, event: AudioEvent) -> None:
        """GUI/CLI から queue 経由で届いた rebuy を integration スレッドで適用する。

        GameStateManager / PokerkitGameState はロックを持たないため、状態変更は本スレッドに
        一元化する（規約「スレッド間通信は queue のみ」）。リバイはポーカーアクションではない
        ため HandSummary.actions には積まず、on_action への通知レコードのみ発行する
        （GUI/CLI はこれを受けてスタック表示を更新する）。
        """
        gs = self._game_state
        seat = event.seat if event.seat is not None else _extract_seat_no(event.raw_text)
        if seat is None:
            logger.warning("rebuy event without seat: %r", event.raw_text)
            return
        try:
            gs.rebuy(seat, event.amount)
        except ValueError:
            logger.exception("rebuy failed (seat=%s amount=%s)", seat, event.amount)
            return
        if self._on_action:
            self._on_action(ActionRecord(
                hand_id=gs.hand_id,
                timestamp=self._iso(event.timestamp),
                street=gs.street,
                seat=seat,
                player_name=gs.get_player_name(seat),
                action="rebuy",
                amount=event.amount,
                pot_after=gs.pot,
                stack_after=gs.get_stack(seat),
                source={"camera": False, "audio": False, "rfid": False},
                needs_review=False,
                confidence=1.0,
            ))

    def _handle_rename_seat(self, event: AudioEvent) -> None:
        """席替えで席のプレイヤー名を変える（CLI の `name`, ADR-0059）。名前は `raw_text`。

        ハンドの途中なら次のハンドの開始時に反映する（ハンドの記録は 1 ハンドの中で名前が
        揃うように。席と player_id の対応も次のハンドから切り替わる）。
        """
        seat = event.seat
        name = (event.raw_text or "").strip()
        if seat is None or not name:
            logger.warning("rename_seat without seat or name: seat=%r raw=%r", seat, event.raw_text)
            return
        if self._hand_open:
            self._pending_renames[seat] = name
            logger.info("席 %d の名前を次のハンドから %s にします", seat, name)
            return
        self._apply_rename(seat, name)

    def _apply_rename(self, seat: int, name: str) -> None:
        try:
            self._game_state.set_player_name(seat, name)
        except ValueError:
            logger.warning("rename_seat: 席 %d はありません（%s）", seat, name)
            return
        logger.info("席 %d の名前を %s にしました", seat, name)

    # ――― ミスディール訂正（ADR-0054） ―――

    def _handle_correct_board(self, event: AudioEvent) -> None:
        """ボード `amount` 枚目の記録を取り消す（ミスディールしたカードの載せ替え）。

        ストリートは戻さない。カードを 1 枚差し替えても「フロップはフロップ」であり、
        ルール上の進行は変わらない（差し替え後に枚数が戻れば自動遷移は no-op になる）。
        """
        index = event.amount
        if index not in self._board_positions:
            logger.warning(
                "ボード %s 枚目は記録されていません（board=%s）— 訂正は無効です",
                index, self._board_cards,
            )
            return
        removed = self._board_positions.pop(index)
        self._board_dealt_at.pop(index, None)   # 差し替え後の配布時刻を採り直す（ADR-0055）
        self._board_cards = [self._board_positions[i] for i in sorted(self._board_positions)]
        self._shown_board = len(self._board_cards)   # 置き直した札をもう一度出す
        # 訂正が入ったハンドは人間が記録を確認できるようにする（監査痕）。
        self._hand_needs_review = True
        logger.info(
            "ボード %d 枚目 %s を取り消しました（board=%s）— 正しいカードを置いてください",
            index, removed, self._board_cards,
        )
        self._notify_card_correction("board", index)

    def _handle_correct_seat(self, event: AudioEvent) -> None:
        """席 `seat` のホールカード記録を取り消し、物理的に載っている札を読み直させる。"""
        seat = event.seat
        if seat is None:
            logger.warning("席が指定されていないため訂正できません（raw=%r）", event.raw_text)
            return
        removed = self._hole_cards.pop(seat, [])
        self._shown_holes.pop(seat, None)            # 読み直した札をもう一度出す
        self._hand_needs_review = True
        logger.info(
            "席 %d のホールカード %s を取り消しました — 正しいカードを置き直してください",
            seat, removed or "（記録なし）",
        )
        self._notify_card_correction("seat", seat)

    # ――― 卓状態の publish（RFID 由来のみ。アクション推定に依存しない） ―――

    def _publish_table_state_if_due(self) -> None:
        """一定間隔で卓状態を publish する（カードが外れたことは event にならないため）。"""
        if self._table_state_writer is None:
            return
        now = self._clock()
        if now - self._table_state_published_at < TABLE_STATE_INTERVAL:
            return
        self._publish_table_state()

    def _publish_table_state(self, observed_at: Optional[float] = None) -> None:
        """RFID から導いた卓状態（カード / 有効席 / ストリート）を sidecar へ publish する。

        アクション推定とは独立した経路。実プレイ環境で「カード読み取り・有効席・ストリート遷移が
        プレイ速度で取れるか」「UI に反映されるか」を検証するための観測出力（ADR-0056 D4/D5）。
        writer 未注入なら no-op（= 挙動不変）。
        """
        if self._table_state_writer is None:
            return
        gs = self._game_state
        presence: dict = {}
        if self._seat_presence is not None:
            try:
                presence = self._seat_presence() or {}
            except Exception:  # noqa: BLE001 — 観測が取れなくても記録は続ける
                logger.exception("seat_presence failed — 在否なしで卓状態を出します")
        board_presence: dict = {}
        if self._board_presence is not None:
            try:
                board_presence = self._board_presence() or {}
            except Exception:  # noqa: BLE001 — 表示用。取れなくても卓状態は出す
                logger.exception("board_presence failed — ボードの在否なしで卓状態を出します")
        try:
            seats = sorted(set(gs.get_stacks()) | set(presence))
            state = build_table_state(
                session_id=self._json_writer._session_id,  # noqa: SLF001
                hand_id=gs.hand_id,
                now=self._clock(),
                updated_at=self._now_iso(),
                seats=seats,
                hole_cards=self._hole_cards,
                presence=presence,
                board=self._board_cards,
                board_timeline=self._build_board_timeline(),
                engine_street=gs.street,
                button_seat=getattr(gs, "button_seat", None),
                position_map=self._safe_position_map(),
                board_absent_since=board_presence.get("absent"),
                board_pending=board_presence.get("pending"),
            )
        except Exception:  # noqa: BLE001 — 表示用の派生。失敗でハンドを止めない
            logger.exception("卓状態の組み立てに失敗しました — スキップします")
            return
        self._table_state_published_at = self._clock()
        self._table_state_writer.publish(state, observed_at=observed_at)

    def _safe_position_map(self) -> dict[int, str]:
        try:
            return self._game_state.position_map()
        except Exception:  # noqa: BLE001
            return {}

    def _position_of(self, seat: int) -> str:
        """席のポジション名（BTN/SB/BB/UTG…）。ボタンを持たない backend では空文字。"""
        try:
            return self._game_state.position_map().get(seat, "")
        except Exception:  # noqa: BLE001 — 表示用の付随情報。失敗で記録を止めない
            return ""

    def _sensed_seat(self, event: AudioEvent) -> Optional[int]:
        """発話が明示した席（= 「誰が行動したか」の明示証拠, 仕様 §7 / FR-26）。

        席番号（"シート3"）が最優先。無ければ **ポジション名**（"BTN、コール"）を現ハンドの
        `position_map` で席に解決する。ボタンを持たない backend（legacy）や、そのポジションが
        卓に無い場合は None（= 明示証拠なし = engine の手番を維持）。

        RFID の検出はここに入らない（カードの**存在**は**行動**ではない, ISSUE-0033）。
        """
        if event.seat is not None:
            return event.seat
        if not event.position:
            return None
        for seat, name in self._safe_position_map().items():
            if name == event.position:
                return seat
        logger.debug(
            "ポジション %r は現在の卓に無いため無視します（position_map=%s）",
            event.position, self._safe_position_map(),
        )
        return None

    def _build_board_timeline(self) -> list[dict]:
        """ボード各枚の配布時刻を index 昇順で返す（ADR-0055）。

        ターン（4 枚目）/ リバー（5 枚目）の配布時刻はベッティングラウンドの区切りとして
        アクションの時刻に対応するため記録する。フロップは 3 枚の最小値がラウンドの開始
        （フロップ内の順序自体は意味を持たない）。時刻が取れていない位置は落とす。
        """
        return [
            {
                "index": index,
                "card": self._board_positions[index],
                "dealt_at": self._iso(self._board_dealt_at[index]),
            }
            for index in sorted(self._board_positions)
            if index in self._board_dealt_at
        ]

    def _notify_card_correction(self, kind: str, key: int) -> None:
        """RFID 側（割り当て・デバウンス）も同じタイミングで落とす。失敗してもハンドは止めない。"""
        if self._on_card_correction is None:
            return
        try:
            self._on_card_correction(kind, key)
        except Exception:  # noqa: BLE001
            logger.exception("on_card_correction hook failed — ハンドは続行します")

    def _handle_legacy_action(self, event: AudioEvent) -> None:
        """rules-aware でない backend（legacy）の従来アクション処理（挙動不変）。

        pokerkit backend でも hand 未開始/終了後は legal_context が空でここに落ちるが、
        その場合 get_current_player が例外になるため unresolved レコード化する（B2）。
        """
        action = event.action
        gs = self._game_state

        # pokerkit backend でも「合法手が無い」= 手番なし（新ハンド前 / ハンド終了後 /
        # 全員オールイン後）はここに落ちてくる。actor が無いのは運用ミスであってバグでは
        # ないので、traceback ではなく unresolved レコード + 案内ログにする（ISSUE-0028 / B2）。
        try:
            seat = gs.get_current_player()
        except (RuntimeError, ValueError):
            self._emit_unresolved(event, reason="no_active_hand")
            return

        street_at_action = gs.street   # 適用前のストリートを記録する（ISSUE-0029）

        try:
            gs.apply_action(seat, action, event.amount)
        except ValueError:
            logger.exception("apply_action failed (seat=%d, action=%s)", seat, action)
            needs_review = True
        else:
            needs_review = False

        cam_event  = self._pop_matching_camera_event(seat, event)
        rfid_event = self._pop_matching_rfid_event(seat, event)

        has_camera = cam_event is not None
        has_rfid   = rfid_event is not None

        source = {"camera": has_camera, "audio": True, "rfid": has_rfid}
        confidence = calc_confidence(has_rfid=has_rfid, has_audio=True, has_camera=has_camera)

        if has_rfid:
            logger.debug(
                "RFID corroboration: seat=%d tag=%s card=%r Δ=%.3fs",
                seat, rfid_event.tag_id, rfid_event.card,
                abs(rfid_event.timestamp - event.timestamp),
            )
        if has_camera:
            logger.debug(
                "Camera corroboration: seat=%d Δ=%.3fs",
                seat, abs(cam_event.timestamp - event.timestamp),
            )

        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._iso(event.timestamp),
            street=street_at_action,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action=action,
            amount=event.amount,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source=source,
            needs_review=needs_review,
            confidence=confidence,
            position=self._position_of(seat),
        )
        self._current_actions.append(record)

        if self._on_action:
            self._on_action(record)

        logger.debug("ActionRecord: %s", record)

    def _resolve_actor(
        self, event: AudioEvent, legal_ctx: LegalContext
    ) -> tuple[int, bool, list[int], str]:
        """明示発話席から actor を推定する（ADR-0009 §4 を ISSUE-0033 で改訂）。

        prior = engine の合法手番。**明示発話（席番号 or ポジション名）だけ**を sensed とし
        （`_sensed_seat`）、prior と異なれば silent-fold 合成（`fold_through`,
        cap=SILENT_FOLD_CAP・atomic）で sensed まで手番を進める。合成成功なら actor=sensed、
        cap 超過/到達不可なら prior 維持（合成せず）。いずれの競合（sensed≠prior）も needs_review。

        **RFID の seat 読みは actor の証拠にしない**（ISSUE-0033 / ADR-0056。ADR-0049 G3 の
        「active 席の読みだけ採用」も配布直後は全席 active なので防げず、この点は本方針が
        supersede する）。RFID が観測するのは「その席に**カードがある**」であって「その席が
        **行動した**」ではない。ホールカードの配布は数秒で最大 16 件の検出を生み、持ち上げた札を
        置き直しても 1 件出るため、行動と区別できない。カメラ（chip motion = 行動の観測）を
        廃止した結果、存在検出が「物理証拠」の座に繰り上がっていたのが誤りだった。RFID は
        **同席の裏付け**（`_pop_matching_rfid_event`）としてのみ使う — こちらは actor を別席へ
        動かす力を持たないので無害。

        Returns: (actor, conflict, 合成 fold した席列, 競合理由 = 監査 reason, ADR-0049 G3)
        """
        prior = legal_ctx.actor_seat
        sensed = self._sensed_seat(event)

        if sensed is None or sensed == prior:
            return prior, False, [], ""

        # sensed != prior: 明示発話が別席を指す → silent-fold 合成を試みる（cap 内・atomic）。
        try:
            folded = self._game_state.fold_through(sensed, max_folds=SILENT_FOLD_CAP)
        except (ValueError, NotImplementedError):
            logger.warning(
                "silent-fold 合成不可: prior=%s sensed=%s (cap=%d 超過/到達不可) → prior 維持 + review",
                prior, sensed, SILENT_FOLD_CAP,
            )
            # G3: 破棄した証拠（採用しなかった sensed 席）を監査 reason に残す。
            return prior, True, [], f"actor_conflict_capped(sensed={sensed})"
        logger.info("silent-fold 合成: prior=%s → actor=%s (folded=%s)", prior, sensed, folded)
        return sensed, True, folded, "actor_sensed_over_prior"

    def _append_synth_fold(self, seat: int, event: AudioEvent) -> None:
        """合成した silent-fold を fold アクションとして記録する（推定なので常に needs_review）。

        時刻は RFID のマック観測があればそれを使う（ADR-0055）。`source.rfid` はその時刻が
        物理観測由来であることを示す（confidence は据え置き = 判定材料にはしていない）。
        無ければ推定を引き起こした `event` の時刻（ADR-0048 T3）。
        """
        gs = self._game_state
        timestamp, from_rfid = self._synth_fold_timestamp(seat, event)
        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=timestamp,
            street=gs.street,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action="fold",
            amount=0,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source={"camera": False, "audio": False, "rfid": from_rfid},
            position=self._position_of(seat),
            needs_review=True,
            confidence=SYNTH_FOLD_CONFIDENCE,
            actor_source="engine_prior",
            reason="synth_silent_fold",
            apply_ok=True,
        )
        self._current_actions.append(record)
        if self._on_action:
            self._on_action(record)
        logger.debug("ActionRecord (synth-fold): seat=%d (inferred silent fold)", seat)

    def _handle_rules_aware_action(self, event: AudioEvent, legal_ctx: LegalContext) -> None:
        """rules-aware backend（pokerkit）でのアクション処理（ADR-0009 §4/§5/§6）。

        D2b: 物理/明示証拠から actor を推定し（必要なら silent-fold 合成）、apply_corrections で
        合法手へ射影して適用。合成 fold は fold アクションとして記録。
        D3: 派生 confidence（3 因子）+ needs_review 条件。
        G2（ADR-0047）: actor_source / corrected_from / reason / asr_confidence / apply_ok を
        ActionRecord に配線し、review の理由を逆引き可能にする。
        """
        gs = self._game_state
        actor, conflict, synthesized_seats, conflict_reason = self._resolve_actor(event, legal_ctx)

        # 合成した silent-fold を先に記録（手番順: 中間席の fold → 当該 actor のアクション）。
        for fseat in synthesized_seats:
            self._append_synth_fold(fseat, event)

        # B1（ADR-0047）: fold 合成で盤面（min-raise / to-call / legal set）が変わるため、
        # 射影は必ず合成後の legal_context に対して行う（stale ctx の再利用禁止）。
        if synthesized_seats:
            legal_ctx = gs.legal_context()

        corrected = apply_corrections(event.action, event.amount, legal_ctx, event.confidence)

        # ストリートは「適用前」を記録する。pokerkit はベッティングラウンドが閉じると
        # apply_action の中で次ストリートへ自動進行するため、適用後を読むとラウンドを
        # 閉じたアクション（BB のチェック等）が次ストリートに記録されてしまう（ISSUE-0029）。
        street_at_action = gs.street

        try:
            gs.apply_action(actor, corrected.action, corrected.amount)
            apply_ok = True
        except ValueError:
            logger.exception(
                "rules-aware apply_action failed (seat=%s action=%s amount=%s)",
                actor, corrected.action, corrected.amount,
            )
            apply_ok = False

        cam_event = self._pop_matching_camera_event(actor, event)
        # RFID は **同席の裏付け**としてのみ使う（actor を動かさない, ISSUE-0033）。
        rfid_event = self._pop_matching_rfid_event(actor, event)
        has_rfid = rfid_event is not None
        has_camera = cam_event is not None

        source = {"camera": has_camera, "audio": True, "rfid": has_rfid}
        # D3: 3 因子の派生 confidence（ADR-0009 §6）。audio は当該アクションにつき常に存在。
        # audio が actor と一致するか（明示の席/ポジションが無いか同席なら一致）。
        sensed = self._sensed_seat(event)
        audio_agree = sensed is None or sensed == actor
        confidence = derive_confidence(
            apply_ok=apply_ok,
            whisper_conf=(
                event.confidence if event.confidence is not None else MISSING_WHISPER_CONF
            ),
            audio_agree=audio_agree,
            rfid_present=has_rfid, rfid_agree=has_rfid,
            camera_present=has_camera, camera_agree=has_camera,
        )
        # needs_review 条件（ADR-0009 §6）— ①非合法 ②高信頼 ASR×規則矛盾/④amount snap
        # （apply_corrections.needs_review が②④を内包）③actor 競合（prior↔sensor）
        # ⑤低 confidence ⑥パース曖昧性（ambiguous_amount / multi_action_keywords, ADR-0047 S1/V1）。
        needs_review = (
            (not apply_ok)
            or corrected.needs_review
            or conflict
            or any(flag not in _INFO_PARSE_FLAGS for flag in event.parse_flags)
            or confidence < REVIEW_THRESHOLD
        )

        # G2: actor の根拠 = 採用した actor と一致する最優先の**明示**証拠。RFID は actor を
        # 決めない（裏付けは source.rfid に出る, ISSUE-0033）。ポジション名で決まった場合は
        # "spoken_position"（ISSUE-0032）。
        if event.seat is not None and event.seat == actor:
            actor_source = "spoken_seat"
        elif sensed is not None and sensed == actor:
            actor_source = "spoken_position"
        else:
            actor_source = "engine_prior"

        reasons = [r for r in (corrected.reason, conflict_reason) if r]
        reasons.extend(event.parse_flags)

        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._iso(event.timestamp),
            street=street_at_action,
            seat=actor,
            player_name=gs.get_player_name(actor),
            action=corrected.action,
            amount=corrected.amount,
            pot_after=gs.pot,
            stack_after=gs.get_stack(actor),
            source=source,
            needs_review=needs_review,
            confidence=confidence,
            position=self._position_of(actor),
            actor_source=actor_source,
            corrected_from=corrected.corrected_from,
            reason="+".join(reasons),
            asr_confidence=event.confidence,
            apply_ok=apply_ok,
            raw_text=event.raw_text or None,
        )
        self._current_actions.append(record)

        if self._on_action:
            self._on_action(record)

        logger.debug(
            "ActionRecord (rules-aware): seat=%d action=%s amount=%d synth=%s conflict=%s "
            "corrected_from=%s reason=%s review=%s",
            actor, corrected.action, corrected.amount, synthesized_seats, conflict,
            corrected.corrected_from, record.reason, needs_review,
        )
        # ほかが全員フォールドしたら確定、ベッティングが終わったらショーダウンの案内（ADR-0062）
        self._maybe_finish_hand(event)

    # ――― ハンド開始 / 終了 ―――

    def _start_new_hand(
        self, event: Optional[AudioEvent] = None, *,
        started_at: Optional[float] = None, auto: bool = False,
    ) -> None:
        """新しいハンドを始める。`auto` = 手札の配布を検出して始めた（ADR-0062）。"""
        gs = self._game_state
        for seat, name in self._pending_renames.items():   # ハンドの途中に届いた席替え（ADR-0059）
            self._apply_rename(seat, name)
        self._pending_renames = {}
        # S5（ADR-0047）: stack_start はブラインド post 前に取る。pokerkit backend は new_hand() で
        # ブラインドを自動 post するため、post 後に取ると result がブラインド分ずれる。
        self._stack_start = gs.get_stacks()
        gs.new_hand()
        self._current_actions = []
        if started_at is not None:
            self._hand_started_epoch = started_at
        else:
            self._hand_started_epoch = event.timestamp if event is not None else self._clock()
        self._hand_started_at = self._iso(self._hand_started_epoch)
        self._board_cards = []
        self._board_positions = {}
        self._board_dealt_at = {}
        self._board_source = ""
        self._hole_cards = {}
        self._hand_needs_review = False
        self._hand_open = True
        self._hand_auto_started = auto
        self._showdown_mucks = []
        self._showdown_notice_shown = False
        self._shown_holes = {}
        self._shown_board = 0
        self._set_in_play(True)
        if self._rfid_reset_for_deal:
            self._rfid_reset_for_deal = False   # 配布の検出でリセット済み（ADR-0063）
        elif self._on_new_hand is not None:
            # RFID の board 位置を engine と同じタイミングでリセットする（ISSUE-0026）。
            # engine 側の board は「カードが外れても縮まない」ので、RFID だけが独自に位置を
            # 振り直すと両者がずれて同じ札が 2 か所に出る。ハンドの切れ目を唯一の同期点にする。
            try:
                self._on_new_hand()
            except Exception:  # noqa: BLE001 — フックの失敗でハンドを止めない
                logger.exception("on_new_hand hook failed — ハンドは続行します")
        # 配布の検出で集めた札をこのハンドの手札にする（RFID のリセット後に載っている札も読み直される）
        self._adopt_deal_cards()
        if self._session_layer_active:
            self._assign_seats_for_hand(gs.hand_id)
        logger.info("New hand started: hand_id=%d", gs.hand_id)
        button = getattr(gs, "button_seat", None)
        details = [d for d in (
            "手札が配られました" if auto else "",
            f"ボタン 席{button}" if button is not None else "",
        ) if d]
        self._notice(f"ハンド {gs.hand_id} 開始" + (f"（{' / '.join(details)}）" if details else ""))
        self._show_hole_cards()
        self._publish_table_state()

    def _assign_seats_for_hand(self, hand_id: int) -> None:
        """S2.x: hand 開始時に seat→player を session レイヤへ write-through する（ADR-0008 §4）。

        個々の assign 失敗（seat/player 重複等）は当該ハンドを止めず log に留める（hand logger の
        記録継続性を優先）。session_id は JsonWriter の session_id（session レイヤ採番の UUID4 hex）。
        """
        session_id = self._json_writer._session_id  # noqa: SLF001
        for seat_no, player_id in self._seat_player_map.items():
            try:
                self._session_repo.assign_seat(session_id, hand_id, seat_no, player_id)
            except Exception:
                logger.exception(
                    "assign_seat failed (session=%s hand=%d seat=%d player=%s)",
                    session_id, hand_id, seat_no, player_id,
                )

    def _finalize_hand(
        self,
        winner_seat: int,
        event: Optional[AudioEvent] = None,
        winner_seats: Optional[list[int]] = None,
        *,
        awards: Optional[dict[int, int]] = None,
        winner_source: Optional[str] = None,
        showdown: Optional[list[dict]] = None,
        ended_ts: Optional[float] = None,
    ) -> None:
        """ハンドを確定して HandSummary を書き出す。

        - winner_seats（複数）は split pot（ADR-0050 S7）: `end_hand_split` で pot を等分し
          `pot_awards` を additive に記録する（winner_seat は先頭勝者 = 従来互換）。
        - awards（席 → 額）はショーダウンを手札で判定した結果（ADR-0062）: side pot ごとの勝者どおりに
          `end_hand_awards` で配る。`winner_source` は勝者の決まり方（fold / cards / estimated）、
          `showdown` は見せた手札の役。
        - engine の end_hand が失敗しても記録は捨てず、review 付きで書き出す（ADR-0047 B5:
          actions の持ち越し/消失を全廃）。
        """
        gs = self._game_state
        ended = False
        try:
            if awards is not None:
                gs.end_hand_awards(awards)
            elif winner_seats is not None and len(winner_seats) > 1:
                awards = gs.end_hand_split(winner_seats)
                self._hand_needs_review = True  # chop は必ず人の確認を通す（ADR-0050）
            else:
                gs.end_hand(winner_seat)
            ended = True
        except Exception:
            logger.exception(
                "end_hand failed (winner_seat=%s); 記録は review 付きで確定する", winner_seat
            )
            self._hand_needs_review = True

        # S2.x: 当該 hand の seat→player_id を session レイヤから解決（無効なら空 = 従来動作）。
        seat_player: dict[int, str] = {}
        if self._session_layer_active:
            try:
                seat_player = self._session_repo.resolve_seat_map_for_hand(
                    self._json_writer._session_id, gs.hand_id  # noqa: SLF001
                )
            except Exception:
                logger.exception("resolve_seat_map_for_hand failed; player_id を省略")

        stacks_end = gs.get_stacks()
        players_info = []
        for seat in sorted(stacks_end.keys()):
            hole = self._hole_cards.get(seat, [])
            info = {
                "seat":              seat,
                "name":              gs.get_player_name(seat),
                "hole_cards":        list(hole) if hole else None,
                "hole_cards_source": "rfid" if hole else "",
                "stack_start":       self._stack_start.get(seat, 0),
                "stack_end":         stacks_end[seat],
                "result":            stacks_end[seat] - self._stack_start.get(seat, 0),
            }
            if self._session_layer_active:
                # additive: session 接続時のみ player_id を載せる（未割当 seat は None）。
                info["player_id"] = seat_player.get(seat)
            players_info.append(info)

        if self._hole_cards:
            logger.info(
                "Hand %d: hole_cards from RFID — %s",
                gs.hand_id,
                {s: cards for s, cards in self._hole_cards.items()},
            )

        # S6（ADR-0047）: pot_total は engine の pot スナップショット（実コミット額）を優先する。
        # 従来の「bet/raise/call/allin の amount 加算」は rules-aware 経路では "to" 総額の
        # 多重加算になる。legacy（pots() が空）は従来加算に fallback = 挙動不変。
        pots = gs.pots() if ended else []
        if pots:
            pot_total = sum(p.get("amount", 0) for p in pots)
        else:
            pot_total = sum(
                a.amount for a in self._current_actions
                if a.action in ("bet", "raise", "call", "allin")
            )

        if ended_ts is None and event is not None:
            ended_ts = event.timestamp
        summary = HandSummary(
            hand_id=gs.hand_id,
            session_id=self._json_writer._session_id,
            started_at=self._hand_started_at,
            ended_at=self._iso(ended_ts) if ended_ts is not None else self._now_iso(),
            blinds={"sb": gs._sb, "bb": gs._bb},  # noqa: SLF001
            board=list(self._board_cards),
            board_source=self._board_source,
            board_timeline=self._build_board_timeline(),
            button_seat=getattr(gs, "button_seat", None),
            position_map=dict(self._safe_position_map()),
            players=players_info,
            pot_total=pot_total,
            pots=pots,
            winner_seat=winner_seat,
            pot_awards=(
                [{"seat": s, "amount": amt} for s, amt in sorted(awards.items())]
                if awards is not None and (len(awards) > 1 or winner_seats) else None
            ),
            actions=list(self._current_actions),
            review_required=(
                self._hand_needs_review
                or any(a.needs_review for a in self._current_actions)
            ),
            winner_source=winner_source,
            showdown=showdown,
        )

        self._json_writer.append_hand_summary(summary)
        if self._on_hand:
            self._on_hand(summary)
        logger.info("Hand %d finalized. Winner: seat %s", gs.hand_id, winner_seat)
        self._notice(self._describe_result(summary, awards))
        cards = self._describe_cards(summary)
        if cards:
            self._card_info(f"ハンド {summary.hand_id}: {cards}")
        self._last_result = {
            "hand_id": gs.hand_id,
            "winners": sorted(awards) if awards else [winner_seat],
            "source": winner_source,
        }
        self._publish_table_state()
        self._current_actions = []
        # _current_actions と対称にリセットし、stale フラグが次のサマリーへ
        # 漏れない（new_hand を挟まない再 finalize でも残らない）ようにする。
        self._hand_needs_review = False
        self._hand_open = False
        self._hand_auto_started = False
        self._showdown_mucks = []
        if self._deal_at is None:
            self._set_in_play(False)            # 次の配布まで音声を聞き流す（ADR-0063）

    def _describe_result(self, summary: HandSummary, awards: Optional[dict[int, int]]) -> str:
        """確定したハンドを 1 行で表す（CLI のお知らせ用）。"""
        from core.showdown import HAND_NAMES_JA

        gs = self._game_state

        def who(seat: Optional[int]) -> str:
            try:
                return f"席{seat} {gs.get_player_name(seat)}"
            except Exception:  # noqa: BLE001
                return f"席{seat}"

        if awards and len(awards) > 1:
            text = "分配: " + "・".join(f"{who(s)} {amt}" for s, amt in sorted(awards.items()))
        else:
            text = f"勝ち: {who(summary.winner_seat)}"
        hands = {h["seat"]: h["hand"] for h in (summary.showdown or [])}
        detail = {
            "fold": "ほかは全員フォールド",
            "estimated": "勝者が決まらず仮",
        }.get(summary.winner_source or "", "")
        if summary.winner_source == "cards" and summary.winner_seat in hands:
            detail = HAND_NAMES_JA.get(hands[summary.winner_seat], hands[summary.winner_seat])
        tail = [d for d in (detail, f"ポット {summary.pot_total}",
                            "要確認" if summary.review_required else "") if d]
        return f"ハンド {summary.hand_id} 終了 — {text}（{' / '.join(tail)}）"


# ――― ユーティリティ ―――

def _spoken_at(event: AudioEvent) -> float:
    """発話が始まった時刻（無ければ処理時刻 = 打った入力）。配布の前後を比べるのに使う（ADR-0062）。"""
    return event.utterance_start_ts if event.utterance_start_ts is not None else event.timestamp


def _extract_seat_from_text(text: str) -> Optional[int]:
    """後方互換エイリアス。席抽出の実装は audio.recognizer に単一化（ADR-0047 S2）。"""
    return _extract_seat_no(text)
