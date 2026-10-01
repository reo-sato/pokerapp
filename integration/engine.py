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
import re
import threading
import time
import unicodedata
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from audio.recognizer import _extract_all_seat_nos, _extract_seat_no, apply_corrections
from core.control_queue import parse_script_hand
from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameStateManager, Street
from core.hand_log import UNKNOWN_CARD, ActionRecord, HandSummary, street_totals
from core.positions import DEAL_ORDER_TIE_SEC, button_from_deal
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

# ――― フォールドは札の離脱から（オーナー決定 2026-09-25）―――
# 席の札が離れたまま戻らなければフォールド（札を持ち上げて見るのと区別する秒数）。卓の中央を通過したら待たない。
FOLD_ABSENT_SEC = 3.0
# 最後の 1 人を残すフォールド（札の離脱）を確定するまでの待ち。札が戻る・「ショーダウン」なら取り消す
# （ショーダウンでは札を前に出すのでリーダーから離れる）。音声の「フォールド」・中央の通過・次の配布でも確定。
FOLDOUT_CONFIRM_SEC = 10.0
# ショーダウンで見せずにマックする人は素早くマックし、ディーラーが「フォールド」と言う（オーナー, 2026-09-30）。
# ベッティングが終わって（リバーの札があとならその札から）この秒数マックが無ければ、残った全員が見せたとして
# 手札で勝者を決める（次の配布まで待たない）。
SHOWDOWN_MUCK_SEC = 8.0
# 札の離脱・中央の通過で決めたフォールドの confidence（物理観測。通過の方が確か）
RFID_FOLD_CONFIDENCE = 0.8
RFID_MUCK_CONFIDENCE = 0.95
# 「フォールド」と言われたのに札が席に残っていた席を、次のアクションの前にフォールドにしたとき（音声だけ）
SPOKEN_FOLD_CONFIDENCE = 0.5
# ボードの枚数 → ストリートと、その始まりの札の位置（ボードの札が置かれたら前のラウンドは終わっている）
_BOARD_STREETS = {3: ("flop", (1, 2, 3)), 4: ("turn", (4,)), 5: ("river", (5,))}
_STREET_RANK = {"preflop": 0, "flop": 1, "turn": 2, "river": 3, "showdown": 4}
# プレー中にこの秒数、マイクに声が入らなければ知らせる（ワイヤレスマイクの電池切れ等, 2026-09-25）
SILENT_MIC_SEC = 60.0
# ハンドを始めてからこの秒数たったら、卓で使っていない席に手札が無いかを確かめる（配っている途中の席を
# 「手札が無い」と言わないため）
SEAT_SETUP_CHECK_SEC = 5.0
# 「フォールド」と聞こえたが手番の人の札がまだ席にあるとき、この秒数以内に札が離れたら、その発話の時刻の
# フォールドにする（勝った人が先に札を投げても、降りた人の方が先になる）。
SPOKEN_FOLD_WINDOW_SEC = 15.0
# 札の離脱で組み直す（取り消す）ときに入力として残す音声のアクション
_REPLAYABLE_ACTIONS = frozenset({"fold", "check", "call", "bet", "raise", "allin", "heads_up", "players_left"})
# 誰の応答も聞こえていないオールイン（コールは補っただけ）のあと、ベッティングが終わっているのにベッティングの
# 言葉がこの数だけ聞こえたら、そのオールインを聞き違いとみて外す（店舗 2026-09-27: 自信 0.27 の「オールイン」の
# あとのチェック 5 回と「2700」がすべて保留になり、全員オールインの 59700 のポットになった）
HELD_WORDS_TO_DROP_ALLIN = 2
# ディーラーはオールインを言い直す（店舗 2026-09-29: 「オーリー」→「オールイン、コール」/「オールインフォールド」、
# 「オールイン」→「オールイン、コール、ショーダウン」。間は 2.1〜6.5 秒）。直前の記録がオールインで、その発話から
# この秒数以内に「オールイン」と聞こえたら、同じオールインの言い直しとみて次の人のオールインにしない。同じ発話の
# 「オールイン、オールインです」も言い直し（間を置かずに 2 人が続けてオールインすることはまず無い, オーナー 2026-09-30）。
# 同じ額のベット・レイズ（「ベット 2000」→「ベット 2000です」）も同じ: 次の人は同じ額をベット・レイズできない
# （同じ額ならコール）ので、言い直しを次の人のレイズにしていた。
ALLIN_RESTATE_SEC = 8.0
_BETTING_WORDS = frozenset({"check", "call", "bet", "raise", "allin"})
# 額の候補から選ぶとき（`_choose_amount`）、使える額の 1 番と 2 番の点数の差がこれ未満なら「額があいまい」（要確認）
AMOUNT_CLOSE = 1.0
# 読んだ額がいま使えない（最小ベット・レイズに届かない）とき、第 2 の耳の額ごとの点数から使える額を選び直すのは、
# その額の点数がいちばん確からしい額（使えない額も含む）からこの差以内のときだけ（音がどの使える額にも遠ければ選ばない）
LEGAL_AMOUNT_MAX_GAP = 5.0

# review の理由にしない parse flag（読み方の情報。「数字だけ」「チェックアラウンド」は運用どおりの言い方）
_INFO_PARSE_FLAGS = frozenset({"amount_only", "check_around"})

# 分けた（勝者が 2 人以上）ことを言う語（`core.constants.ACTION_KEYWORDS` の winner のうち）
_SPLIT_WORDS = re.compile(r"チョップ|ちょっぷ|スプリット|split|chop", re.IGNORECASE)

_SUIT_MARKS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}


def _card_text(card: str) -> str:
    """"Td" → "10♦"（CLI の表示用）。読めなかった位置は "?"。形の違う札（未登録の UID 等）はそのまま。"""
    if card == UNKNOWN_CARD:
        return "?"
    if len(card) == 2 and card[1].lower() in _SUIT_MARKS:
        rank = "10" if card[0] in "Tt" else card[0].upper()
        return rank + _SUIT_MARKS[card[1].lower()]
    return card


def _cards_text(cards) -> str:
    return " ".join(_card_text(c) for c in cards)


def _seats_text(seats) -> str:
    return "・".join(str(s) for s in seats)


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
        rfid_folds: bool = False,
        fold_absent_sec: float = FOLD_ABSENT_SEC,
        speech_pending_since: Optional[Callable[[], Optional[float]]] = None,
        voice_heard_at: Optional[Callable[[], Optional[float]]] = None,
        recorded_deals: bool = False,
        before_new_hand: Optional[Callable[[int], None]] = None,
        button_from_deal: bool = True,
        live_hand: bool = False,
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
            before_new_hand: 新しいハンドの持ち点を取る直前に、そのハンドの hand_id で呼ぶフック（additive,
                         既定 None）。評価の replay が各ハンドを記録の持ち点から始めるのに使う
                         （`replay_events(hand_stacks=...)`）。
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
            rfid_folds: フォールドを**席の札の離脱**で決める（オーナー決定 2026-09-25）。音声の「フォールド」を
                         次の手番の人に付けない。rules-aware backend のみ。在否（`seat_presence`）から離脱を
                         観測して leave / muck / return / confirm の RFIDEvent を記録する（replay はそれで再現）。
            fold_absent_sec: 札が離れたまま何秒でフォールドとみなすか（config `engine.fold_absent_sec`）。
            speech_pending_since: まだアクションになっていない発話の一番早い話し始め（`AudioThread.
                         oldest_pending_start`）。札の離脱は、それより前に話された発話を反映してから入れる。
            voice_heard_at: マイクに最後に声が入った時刻（`AudioThread.last_voice_at`）。プレー中に
                         `SILENT_MIC_SEC` 入らなければ、マイクの電池・受信機・音量を確かめるよう知らせる。
            button_from_deal: 手札を最初に読んだ順（配った順）が別の席をボタンとした配り方にだけ合うなら、
                         ディーラーがボタンを動かし忘れたとみて、そのハンドのボタンを直して組み直す（config
                         `engine.button_from_deal`, 既定 True, 2026-09-29）。読んだ時刻は在否（`seat_presence` の
                         `since`）から取り、`deal_order` の信号として記録する（replay も同じ判断）。False でも記録はする。
            live_hand: フロップが配られたら、進行中のハンド（ここまでの記録）を `JsonWriter.write_live_hand` に
                         書き、ハンドが終わったら消す（真のアクション入力の画面がハンドの途中で入力する, オーナー
                         2026-09-30）。ライブのロガーだけ True（既定 False = replay・テストは書かない）。
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
        self._before_new_hand = before_new_hand
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
        # 卓の最初の持ち点（持ち点 0 の席に手札が配られたときに戻す, 店舗 2026-09-27）
        try:
            self._initial_stacks: dict[int, int] = dict(game_state.get_stacks())
        except Exception:  # noqa: BLE001
            self._initial_stacks = {}
        # 持ち点 0 で手札が配られて最初の持ち点に戻した席 → (戻した額, そのハンドの番号)。そのハンドが終わって
        # 次のハンドが始まるまでに届いた買い足し（`r`）は、戻した額の代わりにする（二重にしない）
        self._provisional_stacks: dict[int, tuple[int, int]] = {}
        # ハンドのあとに直す持ち点（席 → 差）。戻した額の代わりの買い足しがハンドの途中に届いたとき
        self._stack_corrections: dict[int, int] = {}
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
        # replay: 記録に配布（deal）とハンド開始（hand_start）の信号がある。札の読み取りから配布を決め直さず、
        # live が在否で決めた配布と、発話を待って始めた時点に従う（ADR-0056 追記 1, S0）。
        self._recorded_deals = recorded_deals
        self._hand_start_due = False
        # replay: 配布を検出してからハンドを始めるまでに届いた、そのハンドの札の信号（始めてから反映する）
        self._signals_before_start: list[RFIDEvent] = []
        # 手札が配られる前のボードの札を読まなかった（最初の手札で RFID のボード位置を捨てる）
        self._board_before_deal = False
        # プレー中か（配布〜確定、または卓が空になるまで）。音声の聞き取りとボードの受付に使う。
        self._listen_gate = listen_gate
        self._in_play = False
        if listen_gate is not None:
            listen_gate.clear()
        self._table_empty_since: Optional[float] = None
        self._voice_heard_at = voice_heard_at
        self._silent_mic_warned = False
        # いまのハンドを配布の検出で始めたか（直後の「ハンド開始」/ `n` を二重に数えない）。
        self._hand_auto_started = False
        # ショーダウンでマックした席（見せずに降りた = ポットを受け取れない）。
        self._showdown_mucks: list[int] = []
        # ショーダウンを手札で判定できなかった理由（次の配布でチップを動かさずに終えるときの表示）
        self._showdown_gaps: list[str] = []
        self._showdown_notice_shown = False
        # ショーダウンの最後の出来事（ベッティングの終わり・役名・マック）を話した時刻。`SHOWDOWN_MUCK_SEC` 何も
        # 無ければ手札で決める
        self._showdown_at: Optional[float] = None
        # ショーダウンで見せた席 → ディーラーが言った役名（見せる順はアウトオブポジションから）
        self._showdown_shown: dict[int, Optional[str]] = {}
        # 直前に確定したハンド（自動で決めた勝者のあとに届いた `w` / 「ウィナー」を扱う）。
        self._last_result: Optional[dict] = None
        # ディーラーがショーダウンで言った勝った役の名前（pokerkit の役名, 2026-09-26）。ハンドごとに捨てる。
        self._announced_hand: Optional[str] = None
        # ハンドを始められなかった理由（同じ理由を繰り返し知らせない。始められたら None）
        self._start_refused: Optional[str] = None
        self._current_input_index: Optional[int] = None
        # 「フォールド」と聞こえたが札がまだ席にある席 → 発話の時刻（札が離れたらこの時刻のフォールドにする）
        self._spoken_folds: dict[int, float] = {}
        self._spoken_fold_raw: dict[int, str] = {}     # その「フォールド」の聞き取った文（記録用）
        # CLI に出した札（同じ札を何度も出さない）: 席 → 手札、ボードは出した枚数
        self._on_cards = on_cards
        self._shown_holes: dict[int, tuple[str, ...]] = {}
        self._shown_board = 0
        self._seat_setup_warned = False   # このハンドで席の設定の食い違いを知らせたか
        # ――― フォールドは札の離脱から ―――
        self._rfid_folds = bool(rfid_folds) and self._rules_aware
        self._fold_absent_sec = float(fold_absent_sec)
        self._speech_pending_since = speech_pending_since
        # 席 → {"t": 離れた時刻, "muck": 中央を通過, "applied": ハンドの記録に入れた}
        self._departures: dict[int, dict] = {}
        # このハンドの入力（組み直しで同じ順に流し直す）: ("audio", AudioEvent) / ("leave", RFIDEvent)
        self._hand_inputs: list[tuple[str, object]] = []
        # 札の離脱を入れる直前の状態（戻ったら取り消して組み直す）。聞こえたベット・レイズ・オールインの
        # 直前の状態も持つ（オールインは記録を "allin" に入れる = 聞き違いなら外して組み直す）
        self._checkpoints: list[dict] = []
        # ベッティングが終わったあとに聞こえたベッティングの言葉の数（聞き違いのオールインを見つける）
        self._held_betting_words = 0
        # 最後に記録した賭け（オールイン・ベット・レイズ）(記録, 話し始めた時刻, 言った額)（言い直しを見分ける）
        self._last_wager: Optional[tuple[ActionRecord, float, int]] = None
        # そのベット・レイズの前の状態と、そのときの合法手（額の言い直しで組み直す, `_restate_wager_amount`）
        self._last_wager_point: Optional[tuple[dict, LegalContext]] = None
        # 最後の 1 人を残すフォールドの確定待ち {"seat", "t"}
        self._foldout_pending: Optional[dict] = None
        self._foldout_winner_left = False
        self._rebuilding = False
        # 直前のアクションの時刻（手番でない席の離脱を「前の人の聞き落とし」とみなしてよいかの判断）
        self._last_action_at: Optional[float] = None
        # ボードの札で分かったストリートの始まり（street → 最初の札が見えた時刻）と、前のラウンドを閉じたか
        self._street_marks: dict[str, float] = {}
        self._streets_synced: set[str] = set()
        # ――― ボタンの置き忘れの救済（2026-09-29）―――
        self._button_from_deal = bool(button_from_deal)
        self._live_hand = bool(live_hand)
        self._live_signature: Optional[tuple] = None     # 最後に書いた進行中のハンド（変わったときだけ書く）
        # このハンドで配った順を見たか（1 ハンドに 1 回）
        self._deal_order_checked = False
        # ハンドを始めた直後の状態（ボタンを直して始め直すとき、ここから入力を流し直す）
        self._hand_origin: Optional[dict] = None

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
            self._publish_live_hand()       # 進行中のハンド（フロップから, 真のアクション入力の画面）
            self._check_deal_presence()     # 手札の配布（ADR-0063）
            self._check_deal_order()        # 配った順（ボタンの置き忘れ）
            self._check_table_cleared()     # 片付け（プレーの終わり）
            self._check_silent_mic()        # マイクに声が入っているか
            self._check_seat_setup()        # 起動時の席と札を置いた席の食い違い
            self._poll_departures()         # 席の札の離脱・戻り（フォールド）

            try:
                event = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                # 配布を検出していて、それより前の発話がもう無ければ新しいハンドを始める（ADR-0062）
                self._start_dealt_hand_if_ready()
                self._apply_idle_observations()
                self._check_foldout_timeout()
                self._check_showdown_timeout()
                self._expire_buffers()
                continue

            self._record(event)
            self._handle_audio_event(event)
            self._expire_buffers()

        self._close_open_hand_at_stop()
        logger.info("IntegrationThread stopped")

    def _close_open_hand_at_stop(self) -> None:
        """終了（`q`）のとき、届いている発話を反映してから確定していないハンドを保存する。

        店舗 2026-09-27: ハンドの途中で終了して 2 ハンドが記録に残らなかった。次の手札が配られたときと
        同じ規則で閉じる（ショーダウンなら手札で判定、決まらなければ仮の勝者 + 要確認）。終了も記録に残す
        （replay で同じハンドが保存される）。
        """
        try:
            self._drain_rfid_queue()
            while True:
                try:
                    event = self._audio_queue.get_nowait()
                except queue.Empty:
                    break
                self._record(event)
                self._handle_audio_event(event)
            if self._hand_open and self._hand_in_play():
                end = AudioEvent(action="session_end", amount=0, timestamp=self._clock(), raw_text="（終了）")
                self._record(end)
                self._handle_audio_event(end)
        except Exception:  # noqa: BLE001 — 終了の途中の失敗で止まらない（記録は events.jsonl に残っている）
            logger.exception("終了時にハンドを保存できませんでした")

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
        if ev.kind != "card":
            self._handle_seat_signal(ev)    # 札の離脱・戻り・確定（フォールド）
            return
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
        dupes = sorted({c for c in self._board_cards
                        if c != UNKNOWN_CARD and self._board_cards.count(c) > 1})
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
            if not self._hand_open:
                return                       # ハンドを始められていない（参加できる席が 2 つ未満）
        elif self._auto_new_hand and not (self._hand_open and self._in_play):
            # 手札が配られる前・プレーが終わったあと（シャッフル・片付け）のボードは読まない（ADR-0063）
            logger.debug("プレー中でないボードの札を無視しました: %s (tag=%s)", ev.card, ev.tag_id)
            return
        elif self._auto_new_hand and self._waiting_for_deal():
            # 「ハンド開始」/ n で先に始めたハンドで、まだ手札が配られていない = 配る前のウォッシュ。
            # RFID が振ったボードの位置は、最初の手札が届いたときに捨てる（本物の flop を 1 枚目から数える）。
            self._board_before_deal = True
            logger.debug("手札が配られる前のボードの札を無視しました: %s (tag=%s)", ev.card, ev.tag_id)
            return
        if self._auto_new_hand and self._betting_over() and (
            ev.replaces or ev.board_index not in self._board_positions
            and self._board_top() >= 5
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
            self._board_cards = self._board_list()
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
            self._note_street_marks()
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
                if self._board_before_deal:
                    self._forget_board_before_deal()
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

    def _board_list(self) -> list[str]:
        """ボードを位置どおりに（読めなかった位置は `??`）。最後に読めた位置まで。

        フロップの 1 枚が読めずにターン・リバーが読めたとき、読めた札だけを詰めるとターンの札がフロップに
        並ぶ（オーナー 2026-09-29: 充当してはいけない）。位置を保てば、枚数 = いまのストリートになる。
        """
        if not self._board_positions:
            return []
        top = max(self._board_positions)
        return [self._board_positions.get(i, UNKNOWN_CARD) for i in range(1, top + 1)]

    def _board_top(self) -> int:
        """ボードの読めた一番後ろの位置（3 = フロップ・4 = ターン・5 = リバー）。途中の位置が読めていなくてもよい。"""
        return max(self._board_positions, default=0)

    def _try_advance_street_from_rfid(self) -> None:
        """ボードカード枚数に応じてストリートを自動推移する (RFID 優先証拠)。"""
        n = self._board_top()
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
            if self._deal_at is None:
                # いま始めた: 始めるのを待っていたあいだに札が離れた席（降りた人）を、この発話より先に見る
                # （店舗 b0a27270 ハンド 2: 配って 5 秒で降りた席6 に、始めるきっかけの「八百」が付いた）
                self._poll_departures()
        if self._rfid_folds and self._hand_open:
            # この発話より前に離れた札を先に反映する（フォールドのあとの人のアクションとして読む）
            self._apply_observations_before(_spoken_at(event), event.timestamp)
        try:
            # ハンドの入力として残す（札が戻ったとき・ボタンを直したときに同じ順に流し直す）
            if self._rules_aware and self._hand_open and event.action in _REPLAYABLE_ACTIONS:
                self._run_input("audio", event)
            else:
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

        if action == "script_hand":
            self._handle_script_hand(event)
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
            if self._foldout_pending is not None and _spoken_at(event) >= self._foldout_pending["t"]:
                # 札の離脱を最後のフォールドとみていたが、ショーダウン（札を前に出した）だった
                self._showdown_after_foldout(_spoken_at(event), "「ショーダウン」と聞こえたので")
                return
            if (self._rules_aware and self._hand_open and not self._betting_over()
                    and len(self._remaining_seats()) >= 2):
                # ディーラーがショーダウンと言った = ベッティングは終わっている。閉じていないラウンドは
                # 聞き取れなかったとみて閉じる（このあと札を前に出してもフォールドにしない, 店舗 2026-09-27）
                self._close_betting(_spoken_at(event))
                self._notice("「ショーダウン」— 閉じていないベッティングは聞き取れなかったとみて閉じました（要確認）")
            gs.advance_street(Street.SHOWDOWN)
            return

        if action == "heads_up":
            self._handle_players_left(event, 2)
            return

        if action == "players_left":
            self._handle_players_left(event, event.amount)
            return

        if action == "session_end":
            # 終了（q）: 確定していないハンドを次の配布と同じ規則で閉じて保存する
            if self._hand_open and self._hand_in_play():
                self._close_hand_for_next_deal(ended_ts=event.timestamp, when="終了しました")
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

        if action == "set_blinds":
            self._handle_set_blinds(event)
            return

        if action in ("sit_out", "sit_in"):
            self._handle_sit(event)
            return

        if action == "set_button":
            self._handle_set_button(event)
            return

        # ベッティングアクション。rules-aware backend（pokerkit）は境界で actor 推定 + 合法手
        # 射影、legacy（空 legal_context）は従来経路で挙動不変（ADR-0009 §1）。
        if action == "fold" and self._rfid_folds and self._hand_open:
            if self._foldout_pending is not None:
                self._confirm_foldout()      # 札の離脱で決めた最後のフォールドを、ディーラーの宣言で確定
                return
            if not self._betting_over():
                self._handle_fold_word(event)   # 次の手番の人には付けない（オーナー決定）
                return
            # ベッティングが終わったあと（ショーダウン）は従来どおり: 見せずにマック（ディーラーが宣言する）
        if action in ("allin", "bet", "raise") and self._rules_aware and self._hand_open and self._is_restated(event):
            return
        if action in ("bet", "raise") and self._rules_aware and self._hand_open and self._restate_wager_amount(event):
            return
        legal_ctx = gs.legal_context()
        if "phonetic_amount" in event.parse_flags:
            # 意味のない単発の語を音で読んだ額: 候補のうち、いま使える額（最小ベット・レイズ以上・持ち点まで）から
            # 音の点数とポットに対する大きさの重みで選ぶ
            picked = self._choose_amount(event, legal_ctx)
            if picked is None:
                options = event.amount_options or tuple(a for a, _ in event.amount_scores) or (event.amount,)
                shown = "・".join(str(a) for a in options[:3])
                self._notice(f"「{event.raw_text}」は額（{shown}）の言い間違いに聞こえますが、いまは使えない額なので"
                             "記録しませんでした")
                if self._betting_over():
                    self._count_held_betting_word()
                return
            event = picked
        elif self._amount_needs_choice(event, legal_ctx):
            # 「レイズ 300」（最小レイズ 1000）のように読んだ額がいま使えない: 第 2 の耳の額ごとの点数から、使える額を
            # 選び直す（オーナー 2026-10-01: 可能な額は最小 1bb〜持ち点、チップの刻みしか無い）
            picked = self._choose_amount(event, legal_ctx, flag="legal_amount", max_gap=LEGAL_AMOUNT_MAX_GAP)
            if picked is not None:
                if not self._rebuilding:
                    self._notice(f"「{event.raw_text}」の {event.amount} はいま使えない額なので、音の近い使える額 "
                                 f"{picked.amount} にしました（要確認）")
                event = picked
        if "amount_only" in event.parse_flags:
            # 数字だけの発話 = ベットかレイズ。使えない額なら記録しない（「7」「いまのベットと同じ額」）
            problem = self._amount_only_problem(event, legal_ctx)
            if problem is None and self._said_with_round_closer(event):
                problem = f"前のラウンドを閉じた「{self._current_actions[-1].raw_text}」と同じ発話の額です"
            if problem is not None:
                self._notice(f"数字だけの「{event.raw_text}」は記録しませんでした（{problem}）")
                if self._betting_over():
                    self._count_held_betting_word()
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
                if action in _BETTING_WORDS:
                    self._count_held_betting_word()
        else:
            self._handle_legacy_action(event)

    def _choose_amount(self, event: AudioEvent, ctx: LegalContext, *, flag: Optional[str] = None,
                       max_gap: Optional[float] = None) -> Optional[AudioEvent]:
        """額の候補（音の点数 `amount_scores`、無ければ `amount_options` の順）のうち、いまの手番でベット・レイズに使える
        額（最小ベット・レイズ〜オールイン）から、音の点数 + ポットに対する大きさの重み（`core.bet_sizing`）が最も高い額。

        使える額が無い（`max_gap` があれば、その額の音の点数がいちばん確からしい額からその差より遠い）なら None。
        使える額の 1 番と 2 番の差が `AMOUNT_CLOSE` 未満なら `ambiguous_amount` も付ける（要確認）。`flag` は付ける印。
        """
        from core.bet_sizing import amount_prior

        if ctx.actor_seat is None or not ({"bet", "raise"} & ctx.legal_actions):
            return None
        scores = dict(event.amount_scores)
        if not scores:
            options = event.amount_options or ((event.amount,) if event.amount else ())
            scores = {a: -float(i) for i, a in enumerate(options)}
        if not scores:
            return None
        top = max(scores.values())
        pot = self._game_state.pot
        ranked = sorted(
            ((s + amount_prior(a, ctx, pot), -i, a) for i, (a, s) in enumerate(scores.items())
             if ctx.min_raise <= a <= ctx.max_raise and (max_gap is None or s >= top - max_gap)),
            reverse=True,
        )
        if not ranked:
            return None
        best_score, _, best = ranked[0]
        flags = list(event.parse_flags)
        close = len(ranked) > 1 and best_score - ranked[1][0] < AMOUNT_CLOSE
        for extra in (flag, "ambiguous_amount" if close else None):
            if extra and extra not in flags:
                flags.append(extra)
        if not self._rebuilding:
            logger.info("「%s」の額を %d にしました（使える額の候補 %s）", event.raw_text, best,
                        [(a, round(sc, 2)) for sc, _, a in ranked[:3]])
        return replace(event, amount=best, parse_flags=tuple(flags))

    def _amount_needs_choice(self, event: AudioEvent, ctx: LegalContext) -> bool:
        """読んだ額がいまの最小ベット・レイズに届かない（使えない）ので、第 2 の耳の額ごとの点数から選び直すか。

        語と一緒に言った額（「レイズ 300」）は最小に届かなければ選び直す。数字だけの発話（店の言い方 = 額だけ）は、
        いまのベットより大きく最小レイズに届かないとき（これまでは最小レイズに寄せていた）だけ。いまのベット以下の
        数字（コールの額の言い直し・ポットの読み上げ・役の「7」など）は従来どおり記録しない（`_amount_only_problem`）。
        額が無い語（「レイズ」だけ）は選ばない（次の発話の額を待つ）。第 2 の耳の点数が無ければ従来どおり。
        """
        if (event.action not in ("bet", "raise") or not event.amount or not event.amount_scores
                or not (self._rules_aware and self._hand_open) or ctx.actor_seat is None
                or not ({"bet", "raise"} & ctx.legal_actions) or event.amount >= ctx.min_raise):
            return False
        if "amount_only" in event.parse_flags:
            return "raise" in ctx.legal_actions and event.amount > ctx.committed + ctx.amount_to_call
        return True

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

    def _said_with_round_closer(self, event: AudioEvent) -> bool:
        """数字だけの発話が、前のラウンドを閉じたアクションと同じ発話か。

        次のストリートは札を配ってからなので、同じ発話の額は次のストリートのベットではない（「コール 2千5百」=
        2500 へのコールの額の言い直し。コールがラウンドを閉じるとフロップのベットにしていた, 店舗 2026-09-29）。
        """
        if not self._current_actions or event.utterance_start_ts is None:
            return False
        last = self._current_actions[-1]
        return last.street != self._game_state.street and self._last_action_at == event.utterance_start_ts

    def _street_started_at(self) -> Optional[float]:
        """いまのストリートの最初の札が見えた時刻（フロップは 3 枚のうち最初）。読めていなければ None。"""
        indices = {"flop": (1, 2, 3), "turn": (4,), "river": (5,)}.get(self._game_state.street, ())
        times = [self._board_dealt_at[i] for i in indices if i in self._board_dealt_at]
        return min(times) if times else None

    def _is_restated(self, event: AudioEvent) -> bool:
        """オールイン・ベット・レイズが、直前に記録した同じ賭けの言い直しか（`ALLIN_RESTATE_SEC`）。

        直前の記録がその賭けで（あいだにほかのアクションが無い）、話し始めがその発話から `ALLIN_RESTATE_SEC` 以内
        （同じ発話の中の 2 つ目も）のとき。オールインはオールインの、ベット・レイズは同じ額（額を言ったとき。数字だけの
        発話は `_amount_only_problem` が扱う）のベット・レイズの言い直し。
        """
        last = self._last_wager
        if last is None or not self._current_actions or self._current_actions[-1] is not last[0]:
            return False
        record, spoken, said_amount = last
        if event.action == "allin":
            if record.action != "allin":
                return False
        elif ("amount_only" in event.parse_flags or not event.amount
              or event.amount not in (record.amount, said_amount)):
            return False      # 記録した額（寄せたあと）か、言った額と同じときだけ
        if not 0.0 <= _spoken_at(event) - spoken <= ALLIN_RESTATE_SEC or self._fold_word_between(spoken, event):
            return False
        self._mark_restated(record, event, "allin_restated" if record.action == "allin" else "restated")
        return True

    def _fold_word_between(self, since: float, event: AudioEvent) -> bool:
        """`since`（賭けの話し始め）からこの発話までに「フォールド」と聞こえて、まだ席に付けていない（札が席に残っている
        = 次のアクションの前に手番の人のフォールドにする）か。あいだのアクションなので、この発話は言い直しではない
        （店舗 7b897671 ハンド 3:「1600」→「フォールド」→「2千3百」の「2千3百」は次の人の額）。"""
        return any(since <= t <= _spoken_at(event) for t in self._spoken_folds.values())

    def _mark_restated(self, record: ActionRecord, event: AudioEvent, reason: str) -> None:
        if reason not in (record.reason or "").split("+"):
            record.reason = "+".join(r for r in (record.reason, reason) if r)
        if not self._rebuilding:
            logger.info("「%s」は席%d の %s（「%s」）の言い直しとみなしました",
                        event.raw_text, record.seat, record.action, record.raw_text)
            self._notice(f"「{event.raw_text}」は、直前の「{record.raw_text}」（席{record.seat}）の言い直しとみなしました")

    def _restate_wager_amount(self, event: AudioEvent) -> bool:
        """直前に記録したベット・レイズの額の言い直し: あとから言った額で組み直す（オーナー 2026-10-01）。

        店舗 42f7b964 ハンド 1: 「せーのっ!」（第 2 の耳が 千八百）→「1700」→「コール 1700点」で、1800 のレイズと
        1400 のコールになった（正しくは 1700 と 1300）。1709932e ハンド 3: プレイヤーの「レイズにします。」とディーラーの
        「レイズ」「レイズ 1 千点」が 3 つのレイズになった。

        直前の記録がそのベット・レイズで（あいだにほかのアクションが無い）、話し始めがその発話から `ALLIN_RESTATE_SEC`
        以内のとき:
        - 額の無いベット・レイズのあとの、同じ語の額の無い発話 = 宣言と復唱（同じ賭け。記録しない）。
        - 額の無いベット・レイズのあとの額 = その賭けの額（ただし額の無い「ベット」のあとの「レイズ N」は次の人のレイズ）。
        - 額を言った賭けのあとの違う額で、次の人のレイズにならない額（いまのベット以下・最小レイズに届かない）=
          言い直し（次の人のレイズになる額なら、別の人のレイズとして従来どおり）。
        言い直した額が前の人に使える額のときだけ、その賭けの前に戻って言い直した額で組み直す（要確認）。言い直しの発話と、
        あいだの記録しなかったベット・レイズの語（復唱・同じ額の言い直し）は、その賭けの入力に取り込む（組み直しで
        流し直すと、額を言った賭けのあとの「レイズ」= 次の人のレイズ、前の額 = 言い直し、と読み違えるため）。
        """
        last, point = self._last_wager, self._last_wager_point
        if (last is None or point is None or not self._current_actions
                or self._current_actions[-1] is not last[0] or "phonetic_amount" in event.parse_flags):
            return False      # 音で読んだ額（意味のない語）で、はっきり聞こえた額を言い直さない
        record, spoken, said = last
        if record.action not in ("bet", "raise") or not 0.0 <= _spoken_at(event) - spoken <= ALLIN_RESTATE_SEC:
            return False
        if self._fold_word_between(spoken, event):
            return False                     # あいだに「フォールド」（席に付ける前）= あいだのアクション
        checkpoint, wager_ctx = point
        index, current = checkpoint["index"], self._current_input_index
        if (current is None or not index < current < len(self._hand_inputs)
                or self._hand_inputs[index][0] != "audio"):
            return False
        original: AudioEvent = self._hand_inputs[index][1]
        word = None if "amount_only" in event.parse_flags else event.action    # 言った語（数字だけなら無し）
        if not event.amount:
            if said or word != original.action:
                return False      # 額を言った賭け・「ベット」のあとの「レイズ」= 次の人のレイズ（額は聞こえなかった）
            self._mark_restated(record, event, "restated")
            return True
        if event.amount in (record.amount, said):
            return False                     # 同じ額は `_is_restated` と `_amount_only_problem` が扱う
        if said:
            ctx = self._game_state.legal_context()
            if "raise" in ctx.legal_actions and ctx.min_raise <= event.amount <= ctx.max_raise:
                return False                 # 次の人のレイズになる額 = 別の人のレイズ
        elif original.action == "bet" and word == "raise":
            return False                     # 額の無い「ベット」のあとの「レイズ N」= 次の人のレイズ
        if not wager_ctx.min_raise <= event.amount <= wager_ctx.max_raise:
            return False                     # 前の人にも使えない額
        between = self._hand_inputs[index + 1:current]
        absorbed = [item for kind, item in between if kind == "audio" and item.action in ("bet", "raise")]
        rest = [(kind, item) for kind, item in between
                if not (kind == "audio" and item.action in ("bet", "raise"))]
        flags = [f for f in original.parse_flags if f != "ambiguous_amount"]
        for flag in (*(f for f in event.parse_flags if f in ("ambiguous_amount", "second_ear")), "amount_restated"):
            if flag not in flags:
                flags.append(flag)
        restated = replace(
            original, amount=event.amount, parse_flags=tuple(flags),
            raw_text=" → ".join(t for t in (original.raw_text, *(a.raw_text for a in absorbed), event.raw_text) if t),
        )
        self._hand_inputs = self._hand_inputs[:index]
        self._restore_checkpoint(checkpoint)
        self._replay_inputs([("audio", restated), *rest])
        if not self._rebuilding:
            logger.info("「%s」は席%d の %s %s（「%s」）の額の言い直しとみて、%s に組み直しました",
                        event.raw_text, record.seat, record.action, record.amount, record.raw_text, event.amount)
            self._notice(
                f"「{event.raw_text}」は、直前の「{record.raw_text}」（席{record.seat} の {record.amount}）の額の"
                f"言い直しとみて、{event.amount} に直しました（要確認）"
            )
            if self._on_action:
                for rebuilt in self._current_actions[checkpoint["actions"]:]:
                    self._on_action(rebuilt)
            if self._foldout_pending is None:
                self._maybe_finish_hand()
        return True

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

    # ――― フォールドは札の離脱から（オーナー決定 2026-09-25）―――
    #
    # - 席の札が `fold_absent_sec` 離れたまま戻らない / 卓の中央を通過 → その席のフォールド。
    # - 離脱は、それより前に話し始めた発話を反映し終えてから入れる（認識は数秒遅れる。ショーダウンで前に出した
    #   札や、アクションのあとの覗き見をフォールドと取り違えないため）。手番の人の離脱は待たずに入れ、手番で
    #   ない人の離脱は、次に聞こえたアクションの前に入れる。
    # - 手番でない席が離れた = その前の人のアクションが聞き取れなかった（オーナー決定）。間の人をチェック /
    #   コール（要確認）で補ってからフォールドを入れる。
    # - 札が戻ったら、少なくともその時刻まではフォールドではない（オーナー決定）→ 入れたフォールドを取り消し、
    #   そのあとの入力を流し直して記録を組み直す。
    # - ベッティングが終わったあと（ショーダウン）は札を前に出すので、離脱はマックにしない。見せずに
    #   マックしたときはディーラーが「フォールド」と言う（従来の扱い）。

    def _seat_signal(self, kind: str, seat: int, timestamp: float,
                     observed_at: Optional[float] = None) -> RFIDEvent:
        return RFIDEvent(
            tag_id="", card="", reader_id="", role="seat", seat=seat, timestamp=timestamp,
            raw_tag_id="", kind=kind, observed_at=observed_at,
        )

    def _emit_seat_signal(self, ev: RFIDEvent) -> None:
        """在否から作った札の離脱・戻り・確定を記録してから反映する（replay が同じ順で再現する）。"""
        if self._rebuilding:
            return
        self._record(ev)
        self._process_rfid_event(ev)

    def _oldest_speech(self) -> Optional[float]:
        if self._speech_pending_since is None:
            return None
        try:
            return self._speech_pending_since()
        except Exception:  # noqa: BLE001 — 分からなければ待たない
            return None

    def _poll_departures(self) -> None:
        """席の在否から、札の離脱（フォールドの候補）と戻りを見つける（live のみ）。"""
        if not (self._rfid_folds and self._hand_open) or self._seat_presence is None or self._rebuilding:
            return
        try:
            snapshot = self._seat_presence() or {}
            active = set(self._game_state.get_active_seats())
        except Exception:  # noqa: BLE001 — 在否が取れなければ判断しない
            return
        now = self._clock()
        for seat in sorted(set(self._hole_cards) | set(self._departures)):
            info = snapshot.get(seat) or {}
            dep = self._departures.get(seat)
            if info.get("present"):
                # このハンドの手札が戻った（次のハンドの札を置いたのは「戻った」ではない）。「フォールド」の
                # 発話で降ろした席の札は元から席にある（戻ったのではない）
                if (dep is not None and not dep.get("spoken")
                        and set(info.get("cards") or ()) & set(self._hole_cards.get(seat, ()))):
                    self._observe_return(seat, now)
                continue
            since, mucked = info.get("absent_since"), info.get("mucked_at")
            if dep is not None:
                pending = self._foldout_pending
                if pending is not None and pending["seat"] == seat and mucked is not None:
                    self._emit_seat_signal(self._seat_signal("confirm", seat, now))   # 中央を通過 = 確定
                continue
            if seat not in active or seat in self._showdown_mucks:
                continue
            if mucked is not None and (since is None or mucked >= since - 1.0):
                t = since if since is not None else mucked
                self._departures[seat] = {"t": self._fold_time(seat, t), "muck": True, "applied": False}
            elif since is not None and now - since >= self._fold_absent_sec:
                self._departures[seat] = {"t": self._fold_time(seat, since), "muck": False, "applied": False}
        pending = self._foldout_pending
        if pending is not None and not self._foldout_winner_left:
            for seat in self._remaining_seats():
                info = snapshot.get(seat) or {}
                since = info.get("absent_since")
                if not info.get("present") and since is not None and now - since >= self._fold_absent_sec:
                    self._foldout_winner_left = True
                    folder = self._departures.get(pending["seat"], {})
                    if folder.get("facing_bet"):
                        # ベットに全員が降りた = 勝った人がポットを取って札を前に出した（オーナー: 素早く投げる）。
                        # リバーでは「コール」を聞き落としたショーダウンの可能性もあるので、役名・「ショーダウン」を
                        # 待ってから確定する（確定までの時間をこの離脱から数え直す）。
                        if self._board_top() >= 5:
                            pending["t"] = max(pending["t"], since)
                            logger.info("席%d の札も離れました（リバー）— 役名か「ショーダウン」が無ければ %.0f 秒後に確定",
                                        seat, FOLDOUT_CONFIRM_SEC)
                        else:
                            logger.info("席%d の札も離れました（勝ってポットを取った）", seat)
                    elif self._board_top() >= 5 and not folder.get("muck"):
                        # リバーでベットが無いのに残った人の札も離れた = ショーダウンで札を前に出した
                        self._emit_seat_signal(self._seat_signal("showdown", pending["seat"], now))
                    else:
                        self._hand_needs_review = True
                        self._notice(f"席{seat} の札も離れました — フォールドかショーダウンか確認してください（要確認）")

    def _observe_return(self, seat: int, now: float) -> None:
        dep = self._departures.get(seat)
        if dep is None:
            return
        if not dep.get("applied") or dep.get("action") == "check":
            del self._departures[seat]           # 記録に入れていない / チェック（リバー）だった離脱は消すだけ
            return
        self._emit_seat_signal(self._seat_signal("return", seat, now))

    def _note_street_marks(self) -> None:
        """ボードの札が置かれた時刻を、そのストリートの始まりとして覚える（前のラウンドはそこで終わっている）。"""
        if not (self._rfid_folds and self._hand_open):
            return
        for count, (street, indices) in _BOARD_STREETS.items():
            if street in self._street_marks:
                continue
            # フロップは 2 枚読めれば始まっている（3 枚目が読めないことがある。ボードに 2 枚だけ載ることはない）
            flop_seen = street == "flop" and sum(i in self._board_positions for i in indices) >= 2
            if self._board_top() < count and not flop_seen:
                continue
            times = [self._board_dealt_at[i] for i in indices if i in self._board_dealt_at]
            if times:
                self._street_marks[street] = min(times)

    def _pending_observations(self, bound: float) -> list[tuple[float, int, object]]:
        """`bound` より前に起きた、まだ記録に入れていない観測（時刻順）: ストリートの始まり / 札の離脱。"""
        items: list[tuple[float, int, object]] = [
            (t, 0, street) for street, t in self._street_marks.items()
            if street not in self._streets_synced
        ]
        items += [(d["t"], 1, seat) for seat, d in self._departures.items() if not d.get("applied")]
        return sorted(i for i in items if i[0] < bound)

    def _observation_pending(self, item: tuple[float, int, object]) -> bool:
        _, kind, key = item
        if kind == 0:
            return key not in self._streets_synced
        dep = self._departures.get(key)
        return dep is not None and not dep.get("applied")

    def _apply_observation(self, item: tuple[float, int, object], timestamp: float) -> None:
        t, kind, key = item
        if kind == 0:
            index = next(n for n, (street, _) in _BOARD_STREETS.items() if street == key)
            self._emit_seat_signal(RFIDEvent(
                tag_id="", card="", reader_id="", role="board", seat=None, timestamp=timestamp,
                raw_tag_id="", board_index=index, kind="street", observed_at=t,
            ))
            self._streets_synced.add(key)            # 反映できなくても同じ観測を何度も試さない
        else:
            dep = self._departures[key]
            self._emit_seat_signal(self._seat_signal(
                "muck" if dep.get("muck") else "leave", key, timestamp, observed_at=t,
            ))
            dep["applied"] = True

    def _apply_observations_before(self, spoken_at: float, timestamp: float) -> None:
        """この発話より前に起きたこと（ストリートの札・札の離脱）を、発話を反映する前に時刻順に入れる。

        ストリートの札は 1 秒の余裕を見る（札を置く直前に話し始めた前のラウンドの最後のアクションを先に入れる）。
        """
        for item in self._pending_observations(spoken_at):
            if item[1] == 0 and item[0] > spoken_at - STALE_CALL_MARGIN_SEC:
                continue
            if self._observation_pending(item):
                self._apply_observation(item, min(self._clock(), timestamp))

    def _apply_idle_observations(self) -> None:
        """発話が途切れているとき: ストリートの札（とその前の離脱）と、手番の人の離脱を入れる。

        手番でない人の離脱は「前の人の聞き落とし」の補完を伴うので、次に聞こえたアクションか次の
        ストリートの札まで待つ。認識中の発話より後に起きたことは、その発話を反映してから入れる。
        """
        if not (self._rfid_folds and self._hand_open) or self._rebuilding:
            return
        oldest = self._oldest_speech()
        bound = float("inf") if oldest is None else oldest
        for _ in range(4 * max(1, len(self._departures) + len(self._street_marks))):
            if not self._hand_open:
                return
            items = self._pending_observations(bound)
            if not items:
                return
            if any(kind == 0 for _, kind, _ in items):
                item = items[0]                       # ストリートの札まで、時刻順に
            else:
                if self._apply_pending_spoken_fold(bound, "spoken_fold_before_departure"):
                    continue          # 「フォールド」と言われた手番の人が降り、別の席の離脱が待っている
                actor = self._game_state.legal_context().actor_seat
                item = next((i for i in items if i[2] == actor), None)
                if item is None:
                    return
            self._apply_observation(item, self._clock())

    def _check_foldout_timeout(self) -> None:
        pending = self._foldout_pending
        if pending is None or self._rebuilding:
            return
        now = self._clock()
        deadline = pending["t"] + FOLDOUT_CONFIRM_SEC
        oldest = self._oldest_speech()
        if now >= deadline and (oldest is None or oldest > deadline):
            self._emit_seat_signal(self._seat_signal("confirm", pending["seat"], now))

    def _handle_seat_signal(self, ev: RFIDEvent) -> None:
        if ev.kind == "deal":
            self._apply_deal_signal(ev)
            return
        if ev.kind == "showdown_end":
            self._end_showdown(ev)
            return
        if ev.kind == "hand_start":
            if self._recorded_deals:
                self._hand_start_due = True      # live がこの時点でハンドを始めた（発話を待ったあと）
                self._start_dealt_hand_if_ready()
            return
        if ev.kind == "deal_order":
            self._apply_deal_order(ev)
            return
        if not (self._rfid_folds and self._hand_open):
            if self._rfid_folds and self._recorded_deals and self._deal_at is not None and (
                    ev.observed_at if ev.observed_at is not None else ev.timestamp) >= self._deal_at:
                # replay: live は配った直後の発話でハンドを始め、その発話の処理の中で出した信号を発話と同じ時刻で
                # 記録した。同じ時刻は札（信号）を発話より先に流すので、ハンドの開始より前に届く → 始めてから
                # 反映する（店舗 2026-09-29 d0f055fb ハンド 1: 配った直後の「フォールド」と、その席の札の離脱）
                self._signals_before_start.append(ev)
            return
        if ev.kind == "street":
            self._run_input("street", ev)
            return
        if ev.seat is None:
            return
        # "reinterpret"（無言の連続の解釈し直し, reconstruction_event 0.7）は、コール・チェックを毎回言う運用に
        # したので出さない（2026-09-29）。前の記録にあっても何もしない。
        if ev.kind in ("leave", "muck"):
            self._run_input("leave", ev)
        elif ev.kind == "spoken_fold":
            self._run_input("spoken_fold", ev)
        elif ev.kind == "return":
            self._retract_departure(ev.seat, f"席{ev.seat} の札が戻ったので")
        elif ev.kind == "confirm":
            self._confirm_foldout()
        elif ev.kind == "showdown" and self._foldout_pending is not None:
            self._showdown_after_foldout(ev.timestamp, "残った人の札も離れた（ショーダウン）ので")

    def _run_input(self, kind: str, item) -> None:
        """ハンドの入力を記録してから反映する（札が戻ったとき、同じ順に流し直して組み直すため）。"""
        index = len(self._hand_inputs)
        self._hand_inputs.append((kind, item))
        previous = self._current_input_index     # 入れ子（発話の処理中に作った信号）でも外側の index を保つ
        self._current_input_index = index
        try:
            if kind == "audio":
                self._dispatch_audio_event(item)
            elif kind == "street":
                self._sync_to_street(item)
            elif kind == "spoken_fold":
                self._register_spoken_fold(item)
            else:
                self._apply_leave(item, index)
        finally:
            self._current_input_index = previous

    def _sync_to_street(self, ev: RFIDEvent) -> None:
        """ボードの札が置かれた = 前のラウンドは終わっている。閉じていなければ、札が離れていた人は
        フォールド、残っている人はチェック / コール（聞き取れなかったとみる, 要確認）で閉じる。"""
        target = _BOARD_STREETS.get(ev.board_index or 0, (None, ()))[0]
        if target is None:
            return
        t = ev.observed_at if ev.observed_at is not None else ev.timestamp
        for street, _ in _BOARD_STREETS.values():
            if _STREET_RANK[street] <= _STREET_RANK[target]:
                self._streets_synced.add(street)
        gs = self._game_state
        for _ in range(8 * max(1, len(self._game_seats()))):
            if not self._hand_open or _STREET_RANK.get(gs.street, 4) >= _STREET_RANK[target]:
                break
            ctx = gs.legal_context()
            actor = ctx.actor_seat
            if actor is None:
                break
            if self._apply_pending_spoken_fold(t, f"spoken_fold+implied_before_{target}"):
                continue                         # 「フォールド」と言われたまま札が残っていた手番の席
            dep = self._departed().get(actor)
            if dep is not None and dep["t"] < t and actor not in self._showdown_mucks:
                self._fold_departed(actor, ctx)
            else:
                self._imply_action(actor, ctx, t, f"implied_before_{target}")
        self._resolve_departures()

    def _apply_leave(self, ev: RFIDEvent, index: int) -> None:
        seat = ev.seat
        t = ev.observed_at if ev.observed_at is not None else ev.timestamp
        dep = self._departures.setdefault(seat, {"t": t, "muck": ev.kind == "muck", "applied": False})
        dep.update(t=t, muck=dep.get("muck") or ev.kind == "muck")
        try:
            active = self._game_state.get_active_seats()
        except Exception:  # noqa: BLE001
            active = []
        if seat not in active or seat in self._showdown_mucks or self._betting_over():
            dep["applied"] = True                # ショーダウンでは札を前に出す（マックはディーラーの宣言で）
            return
        self._checkpoints.append(self._take_checkpoint(index, seat))
        dep["applied"] = True
        self._resolve_departures()

    def _take_checkpoint(self, index: int, seat: Optional[int] = None) -> dict:
        """入力 `index` を反映する前の状態（あとで別の解釈で流し直すため）。"""
        return {
            "index": index, "seat": seat, "gs": self._game_state.snapshot(),
            "actions": len(self._current_actions), "mucks": list(self._showdown_mucks),
            "notice": self._showdown_notice_shown, "last": self._last_action_at,
            "showdown_at": self._showdown_at, "shown": dict(self._showdown_shown),
            "foldout": dict(self._foldout_pending) if self._foldout_pending else None,
            "applied": {s: d.get("applied", False) for s, d in self._departures.items()},
            "review": self._hand_needs_review,
            "synced": set(self._streets_synced),
            "spoken": dict(self._spoken_folds),
            "held": self._held_betting_words,
        }

    def _checkpoint_at(self, index: int) -> Optional[dict]:
        return next((c for c in self._checkpoints if c["index"] == index), None)

    def _restore_checkpoint(self, checkpoint: dict) -> None:
        """`_take_checkpoint` の時点に戻す（それ以降の記録・仮説・チェックポイントは捨てる）。"""
        index = checkpoint["index"]
        self._checkpoints = [c for c in self._checkpoints if c["index"] < index]
        self._game_state.restore(checkpoint["gs"])
        del self._current_actions[checkpoint["actions"]:]
        self._showdown_mucks = list(checkpoint["mucks"])
        self._showdown_notice_shown = checkpoint["notice"]
        self._showdown_at = checkpoint.get("showdown_at")
        self._showdown_shown = dict(checkpoint.get("shown", {}))
        self._last_action_at = checkpoint["last"]
        self._foldout_pending = checkpoint["foldout"]
        self._spoken_folds = dict(checkpoint.get("spoken", {}))
        # 「フォールド」の発話から作った離脱はチェックポイントより後なら捨てる（流し直しで作り直す）
        self._departures = {s: d for s, d in self._departures.items()
                            if not (d.get("spoken") and s not in checkpoint["applied"])}
        for other, d in self._departures.items():
            d["applied"] = checkpoint["applied"].get(other, False)
        self._streets_synced = set(checkpoint["synced"])
        self._held_betting_words = checkpoint.get("held", 0)

    def _replay_inputs(self, rest: list, reassign_spoken_folds: bool = False) -> None:
        """記録した入力を同じ順に流し直す（お知らせ・画面は止めて、終わってから記録だけ流す）。

        `reassign_spoken_folds`: 「フォールド」と言われた席（spoken_fold）を、流し直した時点の手番の席にする。
        記録の席は前の手番の順で決めた席なので、ボタンを直して流し直すときに使う。
        """
        callbacks = (self._on_action, self._on_notice, self._on_cards)
        self._on_action = self._on_notice = self._on_cards = None
        rebuilding = self._rebuilding         # 組み直しの中の組み直し（額の言い直し）でも外側の流し直しを続ける
        self._rebuilding = True
        try:
            for kind, item in rest:
                if reassign_spoken_folds and kind == "spoken_fold":
                    actor = self._game_state.legal_context().actor_seat
                    if actor is None:
                        continue
                    item = replace(item, seat=actor)
                try:
                    self._run_input(kind, item)
                except Exception:  # noqa: BLE001 — 組み直しの 1 件の失敗で残りを止めない
                    logger.exception("組み直しで入力を反映できませんでした: %s", item)
        finally:
            self._rebuilding = rebuilding
            self._on_action, self._on_notice, self._on_cards = callbacks

    def _departed(self) -> dict[int, dict]:
        return {s: d for s, d in self._departures.items() if d.get("applied")}

    def _resolve_departures(self) -> None:
        """札が離れた席をフォールドにする（手番が来た席）。手番より先の席が離れていたら、間の人の
        アクションが聞き取れなかったとみてチェック / コールで補う。"""
        if self._rebuilding and not self._hand_open:
            return
        gs = self._game_state
        for _ in range(4 * max(1, len(self._game_seats()))):
            if not self._hand_open:
                return
            ctx = gs.legal_context()
            actor = ctx.actor_seat
            if actor is None:
                break
            departed = self._departed()
            if actor in departed and actor not in self._showdown_mucks:
                self._fold_departed(actor, ctx)
                continue
            since = max(self._last_action_at or 0.0, self._street_started_at() or 0.0)
            later = [s for s in gs.seats_to_act()
                     if s != actor and s in departed and s not in self._showdown_mucks
                     and departed[s]["t"] >= since]
            if not later:
                break
            self._imply_action(actor, ctx, departed[later[0]]["t"],
                               f"implied_before_fold(seat={later[0]})")
        remaining = self._remaining_seats()
        if len(remaining) == 1 and self._foldout_pending is None and self._hand_open:
            folder = max(self._departed().items(), key=lambda item: item[1]["t"], default=(None, {}))
            self._foldout_pending = {"seat": folder[0], "t": folder[1].get("t", self._clock())}
            self._foldout_winner_left = False
            self._notice(
                f"席{folder[0]} の札が離れたので、席{remaining[0]} の勝ちとみます — "
                f"{FOLDOUT_CONFIRM_SEC:.0f} 秒後に確定（札が戻る・「ショーダウン」なら取り消し）"
            )
        elif len(remaining) >= 2 and not self._rebuilding:
            self._maybe_finish_hand()

    def street_total(self, record: ActionRecord) -> Optional[int]:
        """表示用: そのアクションのあと、その人がこのストリートで出した額の合計（CLI がコールをトータルで見せる,
        オーナー 2026-09-29）。いまのハンドの記録に無い行（未適用など）は None。integration スレッドから呼ぶ。"""
        index = next((i for i, r in enumerate(self._current_actions) if r is record), None)
        if index is None:
            return None
        return street_totals(self._current_actions[:index + 1], self._stack_start or {})[index]

    def _append_rfid_record(self, record: ActionRecord) -> None:
        self._current_actions.append(record)
        if self._on_action:
            self._on_action(record)

    def _fold_departed(self, seat: int, ctx: LegalContext) -> None:
        """札が離れた手番の人のフォールド。チェックできる（ベットが無い）ときはチェックにして、以降は
        勝敗の対象から外す（pokerkit はベットが無いときのフォールドを受け付けない）。"""
        gs = self._game_state
        dep = self._departures[seat]
        muck = bool(dep.get("muck"))
        street = gs.street
        can_fold = "fold" in ctx.legal_actions
        # ベットが無いときに札が離れた: リバーならショーダウンに向けて札を前に出した（チェック）。それより前は
        # 降りた（フォールド。pokerkit はチェックできるときのフォールドを受け付けないので force_fold）。
        action = "fold" if can_fold or street != "river" else "check"
        try:
            if can_fold or action == "check":
                gs.apply_action(seat, action, 0)
            else:
                gs.force_fold(seat)
        except ValueError:
            logger.exception("札の離脱によるフォールドを反映できませんでした（席 %s）", seat)
            self._showdown_mucks.append(seat)
            return
        dep["action"] = action
        dep["facing_bet"] = can_fold
        reasons = ["rfid_muck" if muck else "rfid_departure"]
        if not can_fold:
            reasons.append("river_check" if action == "check" else "no_bet")
        self._append_rfid_record(ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._iso(dep["t"]),
            street=street,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action=action,
            amount=0,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source={"camera": False, "audio": False, "rfid": True},
            needs_review=not can_fold,
            confidence=RFID_MUCK_CONFIDENCE if muck else RFID_FOLD_CONFIDENCE,
            position=self._position_of(seat),
            actor_source="rfid_muck" if muck else "rfid_departure",
            reason="+".join(reasons),
            apply_ok=True,
        ))
        self._last_action_at = dep["t"]

    def _handle_players_left(self, event: AudioEvent, count: int) -> None:
        """「ヘッズアップ」/「スリープレイヤーズ」= 残りが `count` 人（ディーラーが次のストリートへ進むときに言う。
        言わないこともある, 店舗 2026-09-27 / オーナー 2026-09-30）。

        その人数が残っていて、ベットに向き合っている人がいれば、その人は降りずに次へ進んだ = コールした（レイズなら
        額が言われる）。言われなかった「コール」をこれで補い、要確認にしない。次のストリートの札で先に閉じた
        ラウンドの補ったコールも裏付ける。多く残っていればフォールドの聞き落とし、少なければ聞き違いを知らせる。
        """
        if not (self._rules_aware and self._hand_open) or count < 2:
            return
        gs = self._game_state
        spoken = _spoken_at(event)
        self._apply_pending_spoken_fold(spoken, "spoken_fold_before_heads_up")
        remaining = self._remaining_seats()
        if len(remaining) > count:
            self._hand_needs_review = True
            self._notice(f"「{event.raw_text}」— まだ {len(remaining)} 人残っています（フォールドの聞き落とし？ 要確認）")
            return
        if len(remaining) < 2:
            return
        if len(remaining) < count:
            self._hand_needs_review = True
            self._notice(f"「{event.raw_text}」— 記録では {len(remaining)} 人です（フォールドの聞き違い？ 要確認）")
            return
        reason = "heads_up_call" if count == 2 else "players_left_call"
        street = gs.street
        implied = False
        for _ in range(len(remaining)):
            ctx = gs.legal_context()
            if gs.street != street or ctx.actor_seat is None or ctx.amount_to_call <= 0:
                break
            self._imply_action(ctx.actor_seat, ctx, spoken, reason, confirmed=True)
            implied = True
        if implied:
            return
        last = self._current_actions[-1] if self._current_actions else None
        if (last is not None and last.street != gs.street and last.actor_source == "implied"
                and last.action == "call" and last.needs_review):
            last.needs_review = False
            last.reason = "+".join(r for r in (last.reason, "heads_up" if count == 2 else "players_left") if r)

    def _imply_action(self, seat: int, ctx: LegalContext, t: float, reason: str,
                      confirmed: bool = False) -> None:
        """言われなかったアクション（チェック / コール）を入れる。

        コール・チェックは毎回言う運用なので（オーナー, 2026-09-29。それまでの「同じアクションの 2 回目以降は
        言わない」運用は廃止）、言われなかった = 聞き取れなかったとみて要確認。`confirmed` はディーラーの別の
        言葉で裏付けられた場合（「ヘッズアップ」）。
        """
        gs = self._game_state
        action = "check" if ctx.amount_to_call == 0 else "call"
        amount = 0 if action == "check" else ctx.amount_to_call
        street = gs.street
        gs.apply_action(seat, action, amount)
        self._append_rfid_record(ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._iso(t),
            street=street,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action=action,
            amount=amount,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source={"camera": False, "audio": False, "rfid": False},
            needs_review=not confirmed,
            confidence=SYNTH_FOLD_CONFIDENCE,
            position=self._position_of(seat),
            actor_source="implied",
            reason=reason,
            apply_ok=True,
        ))
        self._last_action_at = t

    # ――― 聞き違いのオールイン（店舗 2026-09-27）―――
    #
    # 「オールイン」に誰も応えていない（コールは次のストリートの札で補っただけ）のに、全員オールインで
    # ベッティングが終わったあとも「チェック」「コール」や額が聞こえ続けるなら、オールインは聞き違い。
    # 外して、そのあとの入力を流し直して記録を組み直す。

    def _count_held_betting_word(self) -> None:
        """ベッティングが終わっていて保留にしたベッティングの言葉を数える（`HELD_WORDS_TO_DROP_ALLIN` で判断）。"""
        self._held_betting_words += 1
        if self._held_betting_words >= HELD_WORDS_TO_DROP_ALLIN and not self._rebuilding:
            self._drop_unanswered_allin()

    def _unanswered_allin(self) -> Optional[dict]:
        """いちばん新しい聞こえたオールインのチェックポイント。誰の応答も聞こえていない（そのあとの記録が、
        補ったコール・チェックと札の離脱のフォールドだけで、補ったコールがある）ときだけ。"""
        checkpoint = next((c for c in reversed(self._checkpoints) if c.get("allin") is not None), None)
        if checkpoint is None:
            return None
        record = checkpoint["allin"]
        at = next((i for i, r in enumerate(self._current_actions) if r is record), None)
        if at is None:
            return None
        later = self._current_actions[at + 1:]
        if not any(r.actor_source == "implied" and r.action == "call" for r in later):
            return None
        if any(r.actor_source not in ("implied", "rfid_departure", "rfid_muck") for r in later):
            return None                       # 誰かのコール・フォールドが聞こえている = オールインの裏付け
        return checkpoint

    def _drop_unanswered_allin(self) -> bool:
        """誰も応えていないオールインを聞き違いとみて外し、そのあとの入力から記録を組み直す。外したら True。"""
        checkpoint = self._unanswered_allin()
        if checkpoint is None:
            return False
        record = checkpoint["allin"]
        index = checkpoint["index"]
        rest = self._hand_inputs[index + 1:]
        self._hand_inputs = self._hand_inputs[:index]
        self._restore_checkpoint(checkpoint)
        self._replay_inputs(rest)
        self._hand_needs_review = True
        logger.warning(
            "席%d の「%s」（オールイン）に誰も応えていないのに、ベッティングの言葉が続きました — "
            "聞き違いとみて外し、記録を組み直しました", record.seat, record.raw_text,
        )
        self._notice(
            f"「{record.raw_text}」（席{record.seat} のオールイン）に誰も応えていないのに「チェック」などが続くので、"
            "聞き違いとみて外し、記録を組み直しました（要確認）"
        )
        if self._on_action:
            for rebuilt in self._current_actions[checkpoint["actions"]:]:
                self._on_action(rebuilt)
        if self._foldout_pending is None:
            self._maybe_finish_hand()
        return True

    def _handle_fold_word(self, event: AudioEvent) -> None:
        """ベッティング中の「フォールド」: 手番の人にすぐには付けない。札が離れかけている席があれば、その席の
        フォールドをいま入れる（3 秒待たない）。札が残っていれば手番の席を覚えておき、札が離れたとき
        （その発話の時刻のフォールド）か、同じ人の番のまま次のアクションが聞こえたとき・次のストリートの
        札が置かれたときに、その席のフォールドにする（「フォールド、コール」と続けて言った = 別の人。
        店舗 2026-09-27: 札が席に残ったままの「フォールド、コール」で「コール」が降りた人に付いていた）。"""
        if self._rebuilding:
            return                               # 記録した信号（leave / spoken_fold）の流し直しで再現する
        spoken_at = _spoken_at(event)
        if any(d.get("applied") and d.get("action") == "fold" and not d.get("spoken")
               and 0 <= spoken_at - d["t"] <= SPOKEN_FOLD_WINDOW_SEC
               for d in self._departures.values()):
            return                               # 札の離脱で降ろしたばかりの人のこと（言うのが遅れた）
        seat, since = self._absent_seat_for_fold_word()
        if seat is None:
            actor = self._game_state.legal_context().actor_seat
            if actor is not None and self._seat_presence is not None:
                # 記録して反映する（replay も同じ順で再現する）。札が離れたらその席のフォールドをこの発話の
                # 時刻にする（オーナー: 勝った人が先に札を投げても、降りた人の方が先になる）
                self._spoken_fold_raw[actor] = event.raw_text or ""
                self._emit_seat_signal(self._seat_signal(
                    "spoken_fold", actor, event.timestamp, observed_at=_spoken_at(event),
                ))
            self._notice(f"「{event.raw_text}」— 手番の人の札が席に残っているのでまだフォールドにしません"
                         "（札が離れるか、次のアクションで入れます）")
            return
        dep = self._departures.setdefault(seat, {"t": since, "muck": False, "applied": False})
        if dep.get("applied"):
            return
        self._emit_seat_signal(self._seat_signal(
            "muck" if dep.get("muck") else "leave", seat, event.timestamp, observed_at=dep["t"],
        ))

    def _fold_time(self, seat: int, t: float) -> float:
        """離脱の時刻。「フォールド」がその少し前に聞こえていれば、発話の時刻にする。"""
        spoken = self._spoken_folds.pop(seat, None)
        self._spoken_fold_raw.pop(seat, None)
        if spoken is not None and 0 <= t - spoken <= SPOKEN_FOLD_WINDOW_SEC:
            return spoken
        return t

    def _register_spoken_fold(self, ev: RFIDEvent) -> None:
        """「フォールド」と言われたのに札が席に残っていた手番の席を覚える（記録した信号から。replay も同じ）。"""
        if ev.seat is not None:
            self._spoken_folds[ev.seat] = ev.observed_at if ev.observed_at is not None else ev.timestamp

    def _apply_pending_spoken_fold(self, before: float, reason: str) -> bool:
        """覚えている「フォールド」の席がまだ手番のままなら、その席をフォールドにする。

        次のアクション・次のストリートの札の前に呼ぶ。ディーラーが「フォールド、コール」と続けて言ったときの
        「フォールド」は手番の人、「コール」はその次の人（札が席に残っていても）。反映したら True。
        """
        gs = self._game_state
        ctx = gs.legal_context()
        seat = ctx.actor_seat
        if seat is None:
            return False
        spoken = self._spoken_folds.get(seat)
        if spoken is None or spoken > before:
            return False
        del self._spoken_folds[seat]
        raw = self._spoken_fold_raw.pop(seat, "")
        street = gs.street
        can_fold = "fold" in ctx.legal_actions
        try:
            if can_fold:
                gs.apply_action(seat, "fold")
            else:
                gs.force_fold(seat)              # チェックできるときに降りた
        except ValueError:
            logger.exception("「フォールド」の席をフォールドにできませんでした（席 %s）", seat)
            return False
        # 札の離脱で決めたフォールドと同じ扱い（勝った人の離脱の判断・候補の絞り込み）。札はまだ席にある
        self._departures[seat] = {"t": spoken, "muck": False, "applied": True, "action": "fold",
                                  "facing_bet": can_fold, "spoken": True}
        self._append_rfid_record(ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._iso(spoken),
            street=street,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action="fold",
            amount=0,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source={"camera": False, "audio": True, "rfid": False},
            needs_review=False,
            confidence=SPOKEN_FOLD_CONFIDENCE,
            position=self._position_of(seat),
            actor_source="spoken_fold",
            reason=reason,
            apply_ok=True,
            raw_text=raw or None,
        ))
        self._last_action_at = spoken
        logger.info("「%s」の席%d をフォールドにしました（札は席に残ったまま, %s）", raw or "フォールド", seat, reason)
        self._resolve_departures()
        return True

    def _absent_seat_for_fold_word(self) -> tuple[Optional[int], float]:
        """「フォールド」と聞こえたときに、札が席から離れた（まだ行動する）席を手番の順に探す。

        このハンドで札が一度も読めていない席は数えない（札が席に無いのか、読めていないだけか分からない）。
        2026-10-01 のリハーサル: 席の設定が 1 つずれて席 7 のリーダーに札が無く、各ハンドの最初の「フォールド」が
        席 7 に付いて、その前の人がコールで補われた。読めていない席が手番なら、ふつうの手番の人の「フォールド」に
        なる（札が離れるか、次のアクションで入れる）。
        """
        if self._seat_presence is None:
            return None, 0.0
        try:
            snapshot = self._seat_presence() or {}
        except Exception:  # noqa: BLE001
            return None, 0.0
        for seat in self._game_state.seats_to_act():
            info = snapshot.get(seat) or {}
            if info.get("present") or seat in self._showdown_mucks:
                continue
            since = info.get("absent_since")
            if since is not None and self._hole_cards.get(seat):
                return seat, since
        return None, 0.0

    def _confirm_foldout(self) -> None:
        pending = self._foldout_pending
        if pending is None or self._rebuilding:
            return
        self._foldout_pending = None
        remaining = self._remaining_seats()
        if len(remaining) == 1 and self._hand_open:
            self._finalize_hand(remaining[0], winner_source="fold", ended_ts=pending["t"])

    def _showdown_after_foldout(self, t: float, why: str) -> None:
        """最後のフォールドとみた札の離脱は、ショーダウンで札を前に出したものだった → 取り消し、
        そのプレイヤーの最後のアクション（コール / チェック）を聞き取れなかったとみて補う。"""
        seat = self._foldout_pending["seat"]
        self._retract_departure(seat, why)
        self._close_betting(t)
        self._hand_needs_review = True
        self._maybe_finish_hand()

    def _close_betting(self, t: float) -> None:
        """ベッティングを最後まで閉じる（残っている人のチェック / コール = 聞き取れなかったとみる）。"""
        gs = self._game_state
        for _ in range(8 * max(1, len(self._game_seats()))):
            ctx = gs.legal_context()
            if ctx.actor_seat is None or not self._hand_open:
                break
            self._imply_action(ctx.actor_seat, ctx, t, "implied_before_showdown")

    def _retract_departure(self, seat: int, why: str) -> None:
        """札が戻った（または取り消す理由がある）席の離脱を、ハンドの記録から取り消して組み直す。"""
        dep = self._departures.pop(seat, None)
        if self._foldout_pending is not None and self._foldout_pending["seat"] == seat:
            self._foldout_pending = None
        index = next((i for i, (kind, item) in enumerate(self._hand_inputs)
                      if kind == "leave" and item.seat == seat), None)
        if dep is None or index is None:
            return
        checkpoint = self._checkpoint_at(index)
        rest = [(kind, item) for kind, item in self._hand_inputs[index:]
                if not (kind == "leave" and item.seat == seat)]
        self._hand_inputs = self._hand_inputs[:index]
        if checkpoint is None:                       # 記録を変えなかった離脱（ショーダウン中など）
            self._hand_inputs.extend(rest)
            return
        self._restore_checkpoint(checkpoint)
        self._replay_inputs(rest)
        self._hand_needs_review = True
        self._notice(f"{why}、席{seat} のフォールドを取り消して記録を組み直しました")
        if self._on_action:
            for record in self._current_actions[checkpoint["actions"]:]:
                self._on_action(record)
        if self._foldout_pending is None:
            self._maybe_finish_hand()

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
            if _SPLIT_WORDS.search(event.raw_text or "") and len(self._remaining_seats()) >= 2:
                # 「チョップ」= 2 人以上で分けた。手札で決められないとき 1 人を推定しない（最後に賭けた人にしていた）
                self._hand_needs_review = True
                self._notice(f"「{event.raw_text}」— 分けた席を手札で決められません。w <席> <席> で入力してください")
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

    def _check_seat_setup(self) -> None:
        """卓で使っていない席に手札が 2 枚ある = 起動時の席の設定が違う見込みを知らせる（ハンドごとに 1 回, live のみ）。

        2026-10-01 のリハーサル: ロガーは席 4〜7、札は席 3〜6 のリーダー（1 つずつずれていた）で、札の離脱・マックが
        隣の人のフォールドになり、手札の読めない席 7 に「フォールド」が付いて 4 ハンドとも崩れた（席をずらして
        推定し直すと 4/4）。配り終わるのを待ってから確かめる（配っている途中の席を「手札が無い」と言わない）。
        お知らせだけで記録は変えない（replay は在否を持たず、同じ記録を再現するため）。
        """
        if (not self._hand_open or self._seat_setup_warned or self._hand_started_epoch is None
                or self._clock() - self._hand_started_epoch < SEAT_SETUP_CHECK_SEC):
            return
        game = self._game_seats()
        extra = [s for s, cards in sorted(self._hole_cards.items()) if len(cards) >= 2 and s not in game]
        if not game or not extra:
            return
        self._seat_setup_warned = True
        with_cards = sorted(s for s, cards in self._hole_cards.items() if cards)
        missing = [s for s in game if not self._hole_cards.get(s)]
        if missing:
            self._notice(
                f"席の設定が違うかもしれません: 手札は 席{_seats_text(with_cards)} にありますが、ロガーの席は "
                f"{_seats_text(game)} です（席{_seats_text(missing)} に手札がありません）。札の離脱・マックが別の人の"
                "ものになります — q で終了して、札を置く席の番号で起動し直してください"
            )
        else:
            self._notice(
                f"席{_seats_text(extra)} に手札がありますが、ロガーの席（{_seats_text(game)}）に入っていません — "
                "席の設定を確かめてください"
            )

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

    def _seat_readers_live(self) -> bool:
        """この卓の席に RFID のリーダーがつながっているか（live のみ。replay は在否を持たない）。"""
        if self._seat_presence is None:
            return False
        try:
            snapshot = self._seat_presence() or {}
        except Exception:  # noqa: BLE001
            return False
        return any(seat in snapshot for seat in self._game_seats())

    def _forget_board_before_deal(self) -> None:
        """配る前にボードのリーダーが読んだ札（ウォッシュ）の位置を、RFID 側でも捨てる。

        RFID はハンドの中でボードの位置を解放しない（ISSUE-0026）ので、捨てないと本物の flop が
        4 枚目から数えられる。新しいハンドと同じ同期点を使う（engine のボードはまだ空）。
        """
        self._board_before_deal = False
        if self._on_new_hand is None:
            return
        try:
            self._on_new_hand()
        except Exception:  # noqa: BLE001 — フックの失敗でハンドを止めない
            logger.exception("on_new_hand hook failed (board before deal)")
            return
        logger.info("手札が配られたので、配る前にボードのリーダーが読んだ札の位置を捨てました")

    def _waiting_for_deal(self) -> bool:
        """配る前に「ハンド開始」/ n で始めたハンドで、まだ手札が 1 枚も届いていないか。

        席のリーダーがあるときだけそう判断する（無い構成では手札が届かないので、ボードを待たせない）。
        """
        return (
            self._hand_open and not any(self._hole_cards.values())
            and not self._hand_in_play() and self._seat_readers_live()
        )

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
        if self._recorded_deals:
            return                           # replay: 記録した配布の信号を待つ
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
        # 記録してから反映する（replay は在否を持たないので、この信号で同じ配布を再現する）
        deal_at = min(self._deal_since[s][0] for s in stable)
        self._emit_seat_signal(RFIDEvent(
            tag_id="", card="", reader_id="", role="seat", seat=None, timestamp=now, raw_tag_id="",
            kind="deal", observed_at=deal_at,
            cards=tuple(f"{seat}:{card}" for seat, cards in sorted(hands.items()) for card in cards),
        ))

    def _apply_deal_signal(self, ev: RFIDEvent) -> None:
        """記録した配布（在否で決めた live の判断, `cards` = "席:札"）を反映する。"""
        if self._deal_at is not None:
            return
        hands: dict[int, list[str]] = {}
        for item in ev.cards:
            seat, _, card = item.partition(":")
            if seat.isdigit() and card:
                hands.setdefault(int(seat), []).append(card)
        self._deal_detected(ev.observed_at if ev.observed_at is not None else ev.timestamp, hands)

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
        """ハンドが確定していなくても、卓に札が無い状態が続いたらプレーは終わったとみなす。

        まだ何も起きていないハンド（配る前に「ハンド開始」/ n で始めた・配り直し）の空の卓は片付けでは
        ない。ここでプレーを終えると、配ったあとのボードの札と声を読まなくなる（店舗の 5 回目の通しテスト:
        n のあと配るまでの 19 秒で聞き流しに入り、ボードを読まずにショーダウンの札をフォールドにした）。
        """
        if not (self._in_play and self._hand_open and self._deal_at is None) or self._seat_presence is None:
            return
        if not self._hand_in_play():
            self._table_empty_since = None
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

    def _check_silent_mic(self) -> None:
        """プレー中に `SILENT_MIC_SEC` 声が入らなければ 1 ハンドに 1 回知らせる（マイクの電池切れ等）。"""
        if self._voice_heard_at is None or self._silent_mic_warned or not (self._hand_open and self._in_play):
            return
        started = self._hand_started_epoch
        now = self._clock()
        if started is None or now - started < SILENT_MIC_SEC:
            return
        try:
            last = self._voice_heard_at()
        except Exception:  # noqa: BLE001
            return
        if last is not None and now - last < SILENT_MIC_SEC:
            return
        self._silent_mic_warned = True
        self._notice(
            f"{SILENT_MIC_SEC:.0f} 秒以上、マイクに声が入っていません — マイクの電池・受信機・音量を確かめて"
            r"ください（tools\audio_check.py level で入力の大きさを見られます）"
        )

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
            if self._speech_pending_since is not None and self._deal_at is not None:
                # 配ってから話し始めた発話は新しいハンドのものなので待たない。全部を待つと、話し声が続くあいだ
                # 始まらない（店舗 2026-10-01 b0a27270 ハンド 2: 配ってから 20 秒始まらず、そのあいだに降りた
                # 席6 に「八百」が付いた。7b897671 も 16〜26 秒が 3 回）
                oldest = self._speech_pending_since()
                pending = int(oldest is not None and oldest < self._deal_at)
            else:
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
        if self._recorded_deals and not force and not self._hand_start_due:
            return                           # replay: live が始めた時点（記録した hand_start）まで待つ
        self._hand_start_due = False
        if self._hand_open and self._hand_in_play():
            self._close_hand_for_next_deal()
        if self._hand_open:
            # 配る前に「ハンド開始」/ n で始めていたハンド → 配った札をこのハンドの手札にする
            self._adopt_deal_cards()
            started = True
        else:
            # 始められなければ（参加できる席が 2 つ未満）配布は保留のまま = 買い足し・参加の操作で始まる
            started = self._start_new_hand(started_at=self._deal_at, auto=True)
        if started and not self._recorded_deals and not self._rebuilding:
            self._record(RFIDEvent(
                tag_id="", card="", reader_id="", role="seat", seat=None, timestamp=self._clock(),
                raw_tag_id="", kind="hand_start",
            ))
        early, self._signals_before_start = self._signals_before_start, []
        if started:
            for ev in early:
                self._handle_seat_signal(ev)

    # ――― ボタンの置き忘れの救済（2026-09-29, 店舗 9d1d8536 ハンド 4）―――
    # ディーラーはボタンの次の席から 1 枚ずつ 2 周配る。手札を最初に読んだ順がほかの席をボタンとした配り方にだけ
    # 合うなら、ボタンを動かし忘れたとみてそのハンドを組み直す（`core.positions.button_from_deal`）。手で直すときは
    # ハンドの途中でも `button <席>`。

    def _check_deal_order(self) -> None:
        """手札がそろったら 1 回だけ、札ごとに最初に読んだ時刻を記録してから反映する（live のみ）。

        フロップが開いても手札がそろわなければ、読めた札だけで見る。比べられる組（違う席の札で、時刻の差が
        `DEAL_ORDER_TIE_SEC` 以上）が無ければ記録しない（全員の札を同時に置いた = 順が分からない）。
        """
        if self._deal_order_checked or not self._hand_open or self._seat_presence is None or self._rebuilding:
            return
        gs = self._game_state
        if getattr(gs, "button_seat", None) is None or not hasattr(gs, "restart_hand_with_button"):
            self._deal_order_checked = True
            return
        seats = self._seats_in_hand()
        if any(len(self._hole_cards.get(s, [])) < 2 for s in seats) and self._board_top() < 3:
            return                               # 手札がそろうまで（フロップまで）待つ
        try:
            snapshot = self._seat_presence() or {}
        except Exception:  # noqa: BLE001 — 在否が取れなければ見ない
            return
        self._deal_order_checked = True
        items: list[str] = []
        times: list[float] = []
        for seat in seats:
            since = (snapshot.get(seat) or {}).get("since") or {}
            for card in self._hole_cards.get(seat, []):
                if isinstance(since.get(card), (int, float)):
                    items.append(f"{seat}:{card}")
                    times.append(float(since[card]))
        seat_of = [int(item.partition(":")[0]) for item in items]
        if not any(seat_of[i] != seat_of[j] and abs(times[i] - times[j]) >= DEAL_ORDER_TIE_SEC
                   for i in range(len(items)) for j in range(i + 1, len(items))):
            return
        self._emit_seat_signal(RFIDEvent(
            tag_id="", card="", reader_id="", role="seat", seat=None, timestamp=self._clock(),
            raw_tag_id="", kind="deal_order", cards=tuple(items), times=tuple(times),
        ))

    def _apply_deal_order(self, ev: RFIDEvent) -> None:
        """記録した配った順（`deal_order`）から、ボタンの置き忘れを見つけたら組み直す（live も replay も）。"""
        gs = self._game_state
        current = getattr(gs, "button_seat", None)
        if not (self._button_from_deal and self._hand_open and current is not None):
            return
        times: dict[int, list[float]] = {}
        for item, t in zip(ev.cards, ev.times):
            seat, _, _ = item.partition(":")
            if seat.isdigit():
                times.setdefault(int(seat), []).append(float(t))
        found = button_from_deal(self._seats_in_hand(), times, current)
        if found is None:
            return
        order = " → ".join(
            f"席{int(i.partition(':')[0])}" for _, i in sorted(zip(ev.times, ev.cards))
        )
        self._rebutton(found, f"手札を読んだ順（{order}）は席{found} がボタンの配り方です（記録は席{current}）")

    def _rebutton(self, seat: int, why: str) -> bool:
        """いまのハンドのボタンを `seat` に直し、ハンドの入力を始めから流し直して記録を組み直す（要確認）。

        ディーラーがボタンを動かし忘れると、手番の順がずれて声のアクションが別の席に付く（店舗 2026-09-29
        9d1d8536 ハンド 4）。同じ持ち点・ブラインドでボタンだけ変えて始め直す。次のハンドのボタンはここから進む。
        """
        gs = self._game_state
        restart = getattr(gs, "restart_hand_with_button", None)
        origin = self._hand_origin
        if restart is None or origin is None or not self._hand_open or not gs.is_hand_active():
            return False
        if seat == getattr(gs, "button_seat", None):
            return False
        if seat not in self._seats_in_hand():
            self._notice(f"席{seat} はこのハンドに配られていません（ボタンは直しません）")
            return False
        inputs = list(self._hand_inputs)
        self._hand_inputs = []
        self._restore_checkpoint(origin)
        # 入力から作り直す状態（チェックポイントに無いもの）
        self._last_wager = None
        self._last_wager_point = None
        self._spoken_fold_raw = {}
        self._foldout_winner_left = False
        restart(seat)
        self._hand_origin = self._take_checkpoint(0)
        self._replay_inputs(inputs, reassign_spoken_folds=True)
        self._hand_needs_review = True
        positions = " ".join(f"席{s}={p}" for s, p in sorted(gs.position_map().items()))
        self._notice(f"{why} — ボタンを席{seat} に直して、このハンドの記録を組み直しました（{positions}。要確認）")
        if self._on_action:
            for record in self._current_actions:
                self._on_action(record)
        self._publish_table_state()
        if self._foldout_pending is None:
            self._maybe_finish_hand()
        return True

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

    def _close_hand_for_next_deal(self, ended_ts: Optional[float] = None,
                                  when: str = "次の手札が配られました") -> None:
        """次の手札が配られた（または終了した）のに確定していないハンドを、確定してから次へ進む。"""
        ended = self._deal_at if ended_ts is None else ended_ts
        if self._rfid_folds and self._hand_open:
            self._close_rounds_at_hand_end()
        if self._finish_by_rules(ended_ts=ended):
            return
        gs = self._game_state
        remaining = self._remaining_seats()
        if len(remaining) == 1:   # 全員フォールド（勝者の自動判定が off でも決まっている）
            self._finalize_hand(remaining[0], winner_source="fold", ended_ts=ended)
            return
        if (self._auto_winner and self._rules_aware and len(remaining) >= 2 and self._betting_over()
                and hasattr(gs, "end_hand_refund")):
            # ショーダウンで札が読めず勝者が分からない → チップは動かさない（オーナー 2026-09-29。仮の勝者にしない）
            gaps = "・".join(self._showdown_gaps) or "札が読めていません"
            self._hand_needs_review = True
            self._notice(
                f"ハンド {gs.hand_id} の勝者を手札で判定できないまま{when}（{gaps}）— "
                "チップは動かしません（要確認）"
            )
            self._finalize_hand(None, winner_source="undetermined", ended_ts=ended)
            return
        winner = self._fallback_winner_seat()
        if winner is None:
            remaining = self._remaining_seats()
            winner = remaining[0] if remaining else min(gs.get_stacks())
        self._hand_needs_review = True
        self._notice(
            f"ハンド {gs.hand_id} の勝者が決まらないまま{when} — "
            f"席{winner} を仮の勝者にします（要確認）"
        )
        self._finalize_hand(winner, winner_source="estimated", ended_ts=ended)

    def _close_rounds_at_hand_end(self) -> None:
        """次の配布の時点で、ボードの札まで進めていないラウンドを閉じる。ボードが 5 枚ならリバーも閉じて
        ショーダウンにする（このあとの札の離脱はショーダウン・片付けなのでフォールドにしない）。
        記録済みの観測だけで決まるので replay も同じになる。"""
        for count, (street, _) in _BOARD_STREETS.items():
            t = self._street_marks.get(street)
            if t is not None and street not in self._streets_synced:
                self._sync_to_street(RFIDEvent(
                    tag_id="", card="", reader_id="", role="board", seat=None, timestamp=t,
                    raw_tag_id="", board_index=count, kind="street", observed_at=t,
                ))
        river = self._board_top() >= 5 or self._game_state.street == "river"
        if river and not self._betting_over() and self._hand_open:
            # リバーまで配った（札が読めていなくても、ベッティングがリバーに進んでいる）= ショーダウン
            self._close_betting(self._deal_at or self._clock())

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
        ときだけ（残った全員が見せた（役名）/「ハンド終了」/ `SHOWDOWN_MUCK_SEC` マックが無い / 次の配布
        = `_finish_showdown`）。
        """
        if not (self._auto_winner and self._rules_aware and self._hand_open) or self._rebuilding:
            return False
        if not self._game_state.is_hand_active():
            return False
        remaining = self._remaining_seats()
        if len(remaining) == 1:
            if self._foldout_pending is not None:
                return False                     # 札の離脱で決めた最後のフォールドは確定待ち
            self._finalize_hand(remaining[0], event, winner_source="fold")
            return True
        if len(remaining) >= 2 and self._betting_over():
            if self._showdown_at is None:
                self._showdown_at = self._last_action_at if self._last_action_at is not None else self._clock()
            if not self._showdown_notice_shown:
                self._showdown_notice_shown = True
                self._notice(
                    "ショーダウン（" + "・".join(f"席{s}" for s in remaining) + "）— 見せたら役名、"
                    f"見せずにマックしたら「フォールド」（どちらも無いまま {SHOWDOWN_MUCK_SEC:.0f} 秒たったら"
                    "手札で判定）"
                )
        return False

    def _showdown_turn(self) -> list[int]:
        """ショーダウンでまだ見せても降りてもいない席（見せる順 = アウトオブポジションから）。"""
        remaining = self._remaining_seats()
        return [s for s in self._game_state.acting_order() if s in remaining and s not in self._showdown_shown]

    def _showdown_show(self, event: AudioEvent, name: str) -> None:
        """ショーダウンの役名 = 次に見せる人が見せた（オーナー 2026-09-30: アウトオブポジションが見せ、ディーラーが
        役名を言う。見せると札がリーダーから外れるので、役名は見せるたびに必ず言う運用）。

        誰が見せたかは見せる順（アウトオブポジションから）。手札で役名に合う席が見せていない中に 1 つだけあれば
        その席。残った全員が見せたら手札で決める。そうでなければ次の人（役名 / 「フォールド」）を待つ。
        """
        from core.showdown import HAND_NAMES_JA

        turn = self._showdown_turn()
        if not turn:
            if self._hand_open and self._game_state.is_hand_active() and len(self._board_cards) >= 5:
                # 全員がボードの出る前に手を開いた（オールイン）。いま言った役名はその役の席のもの。手札で決める
                unnamed = [s for s in self._remaining_seats() if not self._showdown_shown.get(s)]
                seat = self._seat_with_hand(unnamed, name)
                if seat is not None:
                    self._showdown_shown[seat] = name
                self._finish_by_rules(event, explicit=True)
                return
            self._check_announced_after_end(name)
            return
        if len(self._board_cards) < 5:
            # ボードが出る前（オールインで手を開いた）: 見せた順には数えるが、言った名前（「エースキング」）は
            # 最後の役ではないので、判定との突き合わせ・役名での勝者には使わない（2026-10-01）
            seat = turn[0]
            self._showdown_shown[seat] = None
            self._showdown_at = _spoken_at(event)
            self._notice(f"席{seat} が手を開きました（ボードが出る前: {HAND_NAMES_JA.get(name, name)}）")
            if not self._showdown_turn():
                self._finish_by_rules(event, explicit=True)
            return
        seat = self._seat_with_hand(turn, name) or turn[0]
        self._showdown_shown[seat] = name
        self._announced_hand = name
        self._showdown_at = _spoken_at(event)
        self._notice(f"席{seat} が見せました（{HAND_NAMES_JA.get(name, name)}）")
        if not self._showdown_turn():
            self._finish_by_rules(event, explicit=True)

    def _seat_with_hand(self, seats: list[int], name: str) -> Optional[int]:
        """手札とボードで役が `name` になる席が `seats` の中に 1 つだけあればその席。"""
        from core.showdown import evaluate_hands

        board = [c for c in self._board_cards if c != UNKNOWN_CARD]
        readable = {s: self._hole_cards[s] for s in seats if len(self._hole_cards.get(s, [])) == 2}
        if len(board) != 5 or not readable:
            return None
        try:
            hands = evaluate_hands(readable, board)
        except Exception:  # noqa: BLE001 — 読めた札が重なっている など
            return None
        matching = [s for s in seats if s in hands and hands[s].name == name]
        return matching[0] if len(matching) == 1 else None

    def _check_showdown_timeout(self) -> None:
        """ショーダウンで見せる・マックが `SHOWDOWN_MUCK_SEC` 無ければ、残った全員が見せたとして手札で決める
        （オーナー 2026-09-30: マックする人は素早くマックしてディーラーが「フォールド」と言うので、次の配布まで
        待たない）。その時刻までに話し始めた発話（「フォールド」かもしれない）を聞き終えてから。
        記録してから反映する（replay は記録した `showdown_end` で同じ時点に決める）。"""
        if (self._showdown_at is None or self._rebuilding or not (self._auto_winner and self._hand_open)
                or not self._betting_over() or len(self._remaining_seats()) < 2):
            return
        deadline = self._showdown_at + SHOWDOWN_MUCK_SEC
        river = self._board_dealt_at.get(5)
        if river is not None:
            deadline = max(deadline, river + SHOWDOWN_MUCK_SEC)   # オールインのあとのリバーから数える
        oldest = self._oldest_speech()
        if self._clock() < deadline or (oldest is not None and oldest <= deadline):
            return
        self._showdown_at = None
        self._emit_seat_signal(RFIDEvent(
            tag_id="", card="", reader_id="", role="seat", seat=None, timestamp=self._clock(),
            raw_tag_id="", kind="showdown_end", observed_at=deadline,
        ))

    def _end_showdown(self, ev: RFIDEvent) -> None:
        """`showdown_end`: マックが無いまま時間がたった = 残った全員が見せた。手札で決める。"""
        if not (self._auto_winner and self._hand_open) or not self._betting_over():
            return
        if len(self._remaining_seats()) < 2:
            return
        self._showdown_at = None
        self._finish_by_rules(explicit=True, ended_ts=ev.observed_at if ev.observed_at is not None else ev.timestamp)

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
        known = [c for c in board if c != UNKNOWN_CARD]
        gaps = [] if len(known) == 5 else [f"ボード {len(known)}/5 枚"]
        for seat in remaining:
            n = len(self._hole_cards.get(seat, []))
            if n < 2:
                gaps.append(f"席{seat} の手札 {n}/2 枚")
        cards = known + [c for s in remaining for c in self._hole_cards.get(s, [])]
        duplicated = len(set(cards)) != len(cards)
        if duplicated:
            gaps.append("同じ札が 2 か所にあります")
        if gaps:
            if self._showdown_shown and self._finish_by_announcement(remaining, board, event, ended_ts):
                return True
            if not duplicated and self._finish_despite_unknowns(remaining, board, gaps, event, ended_ts):
                return True
            self._showdown_gaps = gaps
            message = (f"勝者を手札で判定できません（{'・'.join(gaps)}）— w <席> で入力してください"
                       "（入力が無ければ次の手札が配られたときにチップを動かさずに終えます）")
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
        showdown = [self._with_announced(hands[s].to_dict()) for s in sorted(
            remaining, key=lambda s: order.index(s) if s in order else s)]
        winner = winners_by_pot[0][0]
        named_winner = self._check_shown_names(remaining, hands, winner)
        if named_winner is not None:
            self._finalize_hand(
                named_winner, event, winner_source="announced", showdown=showdown, ended_ts=ended_ts,
            )
            return True
        self._finalize_hand(
            winner, event, awards=awards, winner_source="cards",
            showdown=showdown, ended_ts=ended_ts,
        )
        return True

    def _finish_despite_unknowns(
        self, remaining: list[int], board: list[str], gaps: list[str],
        event: Optional[AudioEvent], ended_ts: Optional[float],
    ) -> bool:
        """読めていない札があっても、どの札でも配当が同じなら（勝者は分かる）確定する（要確認）。"""
        from core.showdown import award_with_unknown_cards

        gs = self._game_state
        others = [c for s, cards in self._hole_cards.items() if s not in remaining for c in cards]
        try:
            pots = gs.current_pots() or [{"amount": gs.pot, "eligible_seats": remaining}]
            decided = award_with_unknown_cards(
                {s: list(self._hole_cards.get(s, [])) for s in remaining}, board, pots,
                gs.acting_order(), excluded=others,
            )
        except Exception:  # noqa: BLE001 — 判定できなければ人に任せる
            logger.exception("読めていない札のある手札の判定に失敗しました")
            return False
        if decided is None:
            return False
        awards, winners_by_pot = decided
        winner = winners_by_pot[0][0]
        self._hand_needs_review = True
        self._notice(
            f"読めていない札があります（{'・'.join(gaps)}）が、どの札でも勝者は変わりません — "
            f"席{winner} の勝ちにします（要確認）"
        )
        self._finalize_hand(winner, event, awards=awards, winner_source="cards", ended_ts=ended_ts)
        return True

    def _finish_by_announcement(
        self, remaining: list[int], board: list[str],
        event: Optional[AudioEvent], ended_ts: Optional[float],
    ) -> bool:
        """札が読めていないとき、見せた席の役名（見せていない席は読めた手札の判定）から勝者を決める（要確認）。

        残った全員の役が分かり、役名だけで一番強い席が 1 つに決まるときだけ。それ以外は決めない（`w <席>` の
        案内に戻る）。
        """
        from core.showdown import best_by_names, evaluate_hands

        if len(board) < 5:
            # ボードが出きる前（オールインで手を開いたときに手札の名前「エースキング」などを言う）。役名では決めない
            return False
        names: dict[int, str] = {s: n for s, n in self._showdown_shown.items() if s in remaining and n}
        hands: dict = {}
        if len(board) == 5 and UNKNOWN_CARD not in board:
            readable = {s: self._hole_cards[s] for s in remaining if len(self._hole_cards.get(s, [])) == 2}
            try:
                hands = evaluate_hands(readable, board) if readable else {}
            except Exception:  # noqa: BLE001 — 読めた札が重なっている など
                hands = {}
            for s, h in hands.items():
                names.setdefault(s, h.name)
        if set(names) != set(remaining):
            return False
        winner = best_by_names(names)
        if winner is None:
            return False
        unreadable = [s for s in remaining if s not in hands]
        self._hand_needs_review = True
        self._notice(
            f"読めていない札があります（席{'・'.join(map(str, unreadable)) or '—'}・ボード）— "
            f"ディーラーの役名から席{winner} の勝ちにします（要確認）"
        )
        order = self._game_state.acting_order()
        showdown = [self._with_announced(hands[s].to_dict()) for s in sorted(
            hands, key=lambda s: order.index(s) if s in order else s)]
        self._finalize_hand(winner, event, winner_source="announced", showdown=showdown or None,
                            ended_ts=ended_ts)
        return True

    def _with_announced(self, entry: dict) -> dict:
        """ショーダウンの席の記録に、ディーラーがその席に言った役名を足す（見せた席だけ）。"""
        name = self._showdown_shown.get(entry.get("seat"))
        if name:
            entry["announced"] = name
        return entry

    def _check_shown_names(self, remaining: list[int], hands: dict, winner: int) -> Optional[int]:
        """見せた席の役名を手札の判定と突き合わせる。違えば要確認（札の読み違い・読み落としの疑い）。

        役名が残った全員にあり、役名だけで決まる一番強い席が判定の勝者と違えば、その席を返す（ディーラーが
        見たものを優先）。それ以外は None（判定のまま）。
        """
        from core.showdown import HAND_NAMES_JA, best_by_names

        mismatched = [(s, n) for s, n in self._showdown_shown.items()
                      if n and s in hands and hands[s].name != n]
        for seat, name in mismatched:
            self._hand_needs_review = True
            judged = hands[seat].name
            self._notice(
                f"席{seat} の役名「{HAND_NAMES_JA.get(name, name)}」が手札の判定"
                f"（{HAND_NAMES_JA.get(judged, judged)}）と違います — 札の読み違いの可能性があります（要確認）"
            )
        if not mismatched:
            return None
        names = {s: self._showdown_shown.get(s) for s in remaining}
        if not all(names.values()):
            return None
        best = best_by_names({s: n for s, n in names.items() if n})
        if best is None or best == winner:
            return None
        self._notice(f"ディーラーの役名では席{best} の勝ちです（手札の判定は席{winner}）— 席{best} の勝ちにします（要確認）")
        return best

    def _check_announced_after_end(self, name: str) -> None:
        """確定したあとに届いた役名: 手札で判定した役と違えば知らせる（記録は訂正画面で）。"""
        from core.showdown import HAND_NAMES_JA

        last = self._last_result or {}
        judged = last.get("hand")
        shown = last.get("hands") or ([judged] if judged else [])
        if last.get("source") == "cards" and judged and name not in shown:
            self._notice(
                f"ハンド {last.get('hand_id')} のディーラーの役名「{HAND_NAMES_JA.get(name, name)}」が"
                f"判定（{HAND_NAMES_JA.get(judged, judged)}）と違います — スマホの訂正画面で確かめてください"
            )
            return
        logger.info("役名 %s — ハンド %s は確定済み", name, last.get("hand_id"))

    def _mucked_stronger_hand(self, seat: int, remaining: list[int]) -> bool:
        """マックした席の手札が、残りの誰よりも強かったか（手札とボードが全部読めているときだけ）。"""
        from core.showdown import evaluate_hands

        board = list(self._board_cards)
        if (len(board) != 5 or UNKNOWN_CARD in board
                or any(len(self._hole_cards.get(s, [])) != 2 for s in remaining)):
            return False
        try:
            hands = evaluate_hands({s: self._hole_cards[s] for s in remaining}, board)
        except Exception:  # noqa: BLE001 — 参考情報
            return False
        others = [hands[s].value for s in remaining if s != seat]
        return bool(others) and hands[seat].value > max(others)

    def _apply_showdown_muck(self, event: AudioEvent) -> None:
        """ベッティングが終わったあとの「フォールド」= ショーダウンでのマック。

        見せずにマックした人は、手札が強くてもポットを受け取れない。席が言われなければ、まだ見せていない
        人の中で一番アウトオブポジション（ボタンの次の席から）の人がマックしたとみなす（店の運用。見せた人には
        ディーラーが役名を言う = `_showdown_show`。アウトオブポジションが見せたあとの「フォールド」は
        インポジションのマック, オーナー 2026-09-30）。
        手札で見るとマックした人の方が強かったときは、勝者はマックしなかった人のまま要確認にする。
        """
        gs = self._game_state
        remaining = self._remaining_seats()
        sensed = self._sensed_seat(event)
        by_order = sensed is None or sensed not in remaining
        turn = self._showdown_turn()
        if by_order:
            seat = turn[0] if turn else next((s for s in gs.acting_order() if s in remaining), None)
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
        if by_order and len(remaining) > 2 and len(turn) > 1:
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
        self._showdown_at = _spoken_at(event)
        if self._maybe_finish_hand(event):
            return
        if self._hand_open and self._betting_over() and len(self._remaining_seats()) >= 2 and not self._showdown_turn():
            self._finish_by_rules(event, explicit=True)     # 残った全員が見せている

    def _handle_end_hand(self, event: AudioEvent) -> None:
        """「ハンド終了」: 残った全員が見せたとして勝者を決める。役名（「ツーペア」）: 次の人が見せた。

        役名はディーラーがショーダウンで見せた手ごとに言う（オーナー 2026-09-30: アウトオブポジションが見せ、
        見せると札がリーダーから外れるので、役名は必ず言う運用）。残った全員が見せたら手札で決め、役名は
        見せた席の手札の判定と突き合わせる（`_finish_showdown`）。閉じていないベッティングは聞き取れなかったと
        みて閉じる。決められなければ `w <席>` を案内する。
        """
        gs = self._game_state
        name = event.hand_name
        if not self._hand_open or not gs.is_hand_active():
            if name:
                self._check_announced_after_end(name)
            else:
                logger.info("「ハンド終了」— 進行中のハンドはありません（確定済み）")
            return
        if name:
            self._announced_hand = name
        if not (self._auto_winner and self._rules_aware):
            self._notice("勝者を w <席> で入力してください")
            return
        if name:
            spoken = _spoken_at(event)
            if self._foldout_pending is not None and spoken >= self._foldout_pending["t"]:
                # 最後のフォールドとみた札の離脱は、ショーダウンで前に出したものだった
                self._showdown_after_foldout(spoken, "役名が言われた（ショーダウン）ので")
            if not self._betting_over() and len(self._board_cards) >= 5 and len(self._remaining_seats()) >= 2:
                self._close_betting(spoken)      # 役名を言った = ベッティングは終わっている（残りは聞き落とし）
            if self._betting_over() and len(self._remaining_seats()) >= 2:
                self._maybe_finish_hand(event)   # ショーダウンの始まり（時刻・お知らせ）
                self._showdown_show(event, name)   # 役名 = 次の人が見せた（ハンドはまだ終わらない）
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
        if last.get("source") == "undetermined":
            self._notice(
                f"ハンド {last.get('hand_id')} は勝者が分からず、チップを動かさずに確定しています — "
                "勝者が分かっていればスマホの訂正画面で直してください"
            )
            if seats:
                self._emit_unresolved(event, reason="winner_after_hand_end")
            return
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
        marker = self._provisional_stacks.pop(seat, None)
        if marker is not None and gs.hand_id <= marker[1]:
            if not self._rebuy_instead_of_restored(seat, event.amount, marker[0]):
                return
        else:
            was_out = gs.get_stacks().get(seat, 0) <= 0
            try:
                gs.rebuy(seat, event.amount)
            except ValueError:
                logger.exception("rebuy failed (seat=%s amount=%s)", seat, event.amount)
                return
            if was_out:
                self._notice(f"席{seat} に {event.amount} を買い足しました — 次のハンドから配られます")
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

    def _rebuy_instead_of_restored(self, seat: int, amount: int, restored: int) -> bool:
        """手札が配られたときに最初の持ち点に戻した席の買い足し = 戻した額の代わりにする（二重にしない）。

        ハンドの途中なら差はハンドのあとに直す（そのハンドの記録は戻した額のまま = 要確認）。
        """
        if amount <= 0:
            logger.warning("rebuy amount must be positive: seat=%s amount=%s", seat, amount)
            return False
        gs = self._game_state
        delta = amount - restored
        in_play = self._hand_open and gs.is_hand_active() and seat in self._seats_in_hand()
        if delta and in_play:
            self._stack_corrections[seat] = self._stack_corrections.get(seat, 0) + delta
        elif delta:
            gs.update_stack(seat, max(0, gs.get_stack(seat) + delta))
        self._notice(
            f"席{seat} の買い足し {amount} は、手札が配られたときに戻した持ち点 {restored} の代わりにします"
            + ("（差はこのハンドのあとに直します）" if delta and in_play else "")
        )
        return True

    def _apply_stack_corrections(self) -> None:
        """ハンドの途中に届いた「戻した額の代わりの買い足し」の差を、ハンドのあとに持ち点へ入れる。"""
        gs = self._game_state
        for seat, delta in self._stack_corrections.items():
            try:
                gs.update_stack(seat, max(0, gs.get_stack(seat) + delta))
            except ValueError:
                logger.exception("持ち点を直せませんでした（席 %s）", seat)
        self._stack_corrections = {}

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

    # ――― 席の参加・休み、ブラインドの変更（次のハンドから, 2026-09-26） ―――

    def _handle_sit(self, event: AudioEvent) -> None:
        """`name <席> -`（休み = 配られない）/ `name <席> <名前>`（参加）。次のハンドから反映する。"""
        gs = self._game_state
        seat = event.seat
        if seat is None:
            logger.warning("%s without seat: %r", event.action, event.raw_text)
            return
        try:
            changed = gs.sit_out(seat) if event.action == "sit_out" else gs.sit_in(seat)
        except ValueError:
            logger.warning("%s: 席 %d はありません", event.action, seat)
            return
        if changed is False:
            return                            # もともとその状態（名前の変更だけ）
        if event.action == "sit_out":
            self._notice(f"席{seat} は次のハンドから休みです（配られません）")
        elif gs.get_stacks().get(seat, 0) <= 0:
            self._notice(f"席{seat} は次のハンドから参加です（スタック 0 — r <席> <金額> で買い足すまで配られません）")
        else:
            self._notice(f"席{seat} は次のハンドから参加です")

    def _handle_script_hand(self, event: AudioEvent) -> None:
        """台本のハンドを始める（`tools/test_script.py` の画面の「このハンドを始める」, 2026-09-30）。

        台本のボタンと持ち点で新しいハンドにする（前のハンドの聞き違い・やり直しで持ち点やボタンがずれても、
        台本の正解と同じ卓から始まる）。前のハンドが確定していなければ「ハンド開始」と同じく先に確定する。
        """
        gs = self._game_state
        button, stacks = parse_script_hand(event.raw_text)
        try:
            if stacks and hasattr(gs, "force_next_stacks"):
                gs.force_next_stacks(stacks)
            if button is not None and hasattr(gs, "set_button"):
                gs.set_button(button)
        except ValueError as e:
            self._notice(f"台本のハンドのボタン・持ち点を使えません（{e}）")
        self._dispatch_audio_event(replace(event, action="new_hand"))

    def _handle_set_button(self, event: AudioEvent) -> None:
        """ボタンを手で指定する（`button <席>`, 仕様 FR-05g）。

        ハンドの途中なら、そのハンドのボタンを直して記録を組み直す（ディーラーがボタンを動かし忘れた,
        2026-09-29）。ハンドが無ければ次のハンドのボタン。
        """
        gs = self._game_state
        seat = event.seat
        if seat is None:
            logger.warning("set_button without seat: %r", event.raw_text)
            return
        if self._hand_open and self._hand_origin is not None and gs.is_hand_active():
            if seat == getattr(gs, "button_seat", None):
                self._notice(f"このハンドのボタンは席{seat} です（変えません）")
            elif seat in self._seats_in_hand():
                self._rebutton(seat, f"手で指定（{event.raw_text}）")
            else:
                self._notice(f"席{seat} はこのハンドに配られていません（ボタンは変えません）")
            return
        try:
            gs.set_button(seat)
        except ValueError:
            self._notice(f"席{seat} はこの卓にありません")
            return
        if self._rules_aware:
            self._notice(f"次のハンドのボタンを席{seat} にします（その席が配られなければ通常どおり進めます）")

    def _handle_set_blinds(self, event: AudioEvent) -> None:
        """ブラインドの変更（トーナメントのレベル上昇）。`raw_text` の「SB/BB」。次のハンドから。"""
        gs = self._game_state
        m = re.search(r"(\d+)\s*/\s*(\d+)", unicodedata.normalize("NFKC", event.raw_text or ""))
        if m is None:
            logger.warning("set_blinds without SB/BB: %r", event.raw_text)
            return
        sb, bb = int(m.group(1)), int(m.group(2))
        try:
            gs.set_blinds(sb, bb)
        except ValueError as e:
            self._notice(f"ブラインドを変えられません: {e}")
            return
        later = self._hand_open and gs.is_hand_active()
        self._notice(f"ブラインドを {sb}/{bb} にしました" + ("（次のハンドから）" if later else ""))

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
        self._board_cards = self._board_list()
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

    # ――― 進行中のハンド（真のアクション入力の画面, オーナー 2026-09-30）―――

    _FLOP_OR_LATER = frozenset({"flop", "turn", "river", "showdown"})

    def _publish_live_hand(self) -> None:
        """フロップが配られたハンドの、ここまでの記録を書く（変わったときだけ）。フロップの前・ハンドの外は消す。"""
        if not self._live_hand:
            return
        gs = self._game_state
        dealt = len(self._board_cards) >= 3 or getattr(gs, "street", None) in self._FLOP_OR_LATER
        if not (self._hand_open and dealt):
            self._clear_live_hand()
            return
        signature = (
            gs.hand_id, gs.street, len(self._current_actions), tuple(self._board_cards),
            tuple(sorted((s, tuple(c)) for s, c in self._hole_cards.items())), getattr(gs, "button_seat", None),
        )
        if signature == self._live_signature:
            return
        try:
            hand = self._live_hand_dict()
        except Exception:  # noqa: BLE001 — 表示用。失敗でハンドを止めない
            logger.exception("進行中のハンドの組み立てに失敗しました — スキップします")
            return
        self._json_writer.write_live_hand(hand)
        self._live_signature = signature

    def _clear_live_hand(self) -> None:
        if self._live_hand and self._live_signature is not None:
            self._json_writer.clear_live_hand()
            self._live_signature = None

    def _live_hand_dict(self) -> dict:
        """進行中のハンドを、確定したハンド（`HandSummary.to_dict`）と同じ形で（勝者・結果は無し）。"""
        gs = self._game_state
        stacks = gs.get_stacks()
        in_hand = set(self._seats_in_hand())
        players = []
        for seat in sorted(s for s in stacks if s in in_hand):
            hole = self._hole_cards.get(seat, [])
            players.append({
                "seat": seat,
                "name": gs.get_player_name(seat),
                "hole_cards": list(hole) if hole else None,
                "hole_cards_source": "rfid" if hole else "",
                "stack_start": self._stack_start.get(seat, 0),
            })
        return {
            "hand_id": gs.hand_id,
            "session_id": self._json_writer._session_id,  # noqa: SLF001
            "started_at": self._hand_started_at,
            "ended_at": None,
            "blinds": {"sb": gs._sb, "bb": gs._bb},  # noqa: SLF001
            "board": list(self._board_cards),
            "board_source": self._board_source,
            "board_timeline": self._build_board_timeline(),
            "button_seat": getattr(gs, "button_seat", None),
            "position_map": {str(k): v for k, v in self._safe_position_map().items()},
            "players": players,
            "winner_seat": None,
            "actions": [a.to_dict() for a in self._current_actions],
            "review_required": self._hand_needs_review or any(a.needs_review for a in self._current_actions),
            "street": gs.street,
            "in_progress": True,
            "updated_at": self._now_iso(),
        }

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

        if self._rfid_folds and sensed in self._game_state.seats_to_act():
            # 手番より先の席・ポジションが言われた: 間の人は札が離れていればフォールド（フォールドは RFID で
            # 決める。合成 fold で埋めない）、残っていればチェック / コールを聞き取れなかったとみる（要確認）。
            self._walk_to_sensed(sensed, _spoken_at(event))
            return sensed, False, [], "actor_sensed_over_prior"

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

    def _walk_to_sensed(self, sensed: int, t: float) -> None:
        """手番を `sensed` まで進める: 札が離れている席はフォールド、残っている席は聞き取れなかったチェック /
        コール（要確認）。"""
        gs = self._game_state
        for _ in range(len(self._game_seats()) + 1):
            ctx = gs.legal_context()
            actor = ctx.actor_seat
            if actor is None or actor == sensed:
                return
            dep = self._departures.get(actor)
            if dep is not None and actor not in self._showdown_mucks:
                dep["applied"] = True
                self._fold_departed(actor, ctx)
            else:
                self._imply_action(actor, ctx, t, "implied_before_spoken_seat")

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
        if self._apply_pending_spoken_fold(_spoken_at(event), "spoken_fold_before_next_action"):
            legal_ctx = gs.legal_context()
            if legal_ctx.actor_seat is None or not self._hand_open:
                self._emit_unresolved(event, reason="betting_over")   # そのフォールドで残り 1 人になった
                return
        index = self._current_input_index
        heard_raise = self._rfid_folds and index is not None and event.action in ("bet", "raise", "allin")
        # オールインの聞き違いを外して組み直せるように、この入力の前の状態を残す（`_drop_unanswered_allin`）
        checkpoint = self._take_checkpoint(index) if heard_raise else None
        # 額の言い直しで組み直せるように、ベット・レイズの前の状態も残す（`_restate_wager_amount`）
        wager_point = checkpoint
        if wager_point is None and index is not None and event.action in ("bet", "raise"):
            wager_point = self._take_checkpoint(index)
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
        if confidence < REVIEW_THRESHOLD:
            # 要確認の本当の理由を残す（店舗 2026-09-27: 理由が「amount_only」だけに見えて、実は聞き取りの
            # 自信の低さで要確認になっていた）
            reasons.append("low_asr_confidence")

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
        self._last_action_at = _spoken_at(event)
        if corrected.action in ("allin", "bet", "raise") and apply_ok:
            self._last_wager = (record, _spoken_at(event), event.amount)
            self._last_wager_point = (
                (wager_point, legal_ctx) if corrected.action in ("bet", "raise") and wager_point is not None else None
            )
        if corrected.action == "allin" and apply_ok and checkpoint is not None:
            # 聞き違いなら外して組み直せるように残す（誰も応えないまま「チェック」等が続いたとき）
            checkpoint["allin"] = record
            if not any(c is checkpoint for c in self._checkpoints):
                self._checkpoints.append(checkpoint)

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
        if self._rfid_folds and self._hand_open:
            self._resolve_departures()      # 次の手番の人の札が離れていればフォールド

    # ――― ハンド開始 / 終了 ―――

    def _start_new_hand(
        self, event: Optional[AudioEvent] = None, *,
        started_at: Optional[float] = None, auto: bool = False,
    ) -> bool:
        """新しいハンドを始める。`auto` = 手札の配布を検出して始めた（ADR-0062）。

        配られる席（休みでなく、チップがある席）が 2 つ未満なら始めない（False。知らせるのは理由が変わった
        ときだけ）。買い足し（`r`）・参加（`name`）の操作で始められるようになる。
        """
        gs = self._game_state
        for seat, name in self._pending_renames.items():   # ハンドの途中に届いた席替え（ADR-0059）
            self._apply_rename(seat, name)
        self._pending_renames = {}
        restored = self._restore_dealt_busted_seats() if auto else []
        if self._before_new_hand is not None:
            self._before_new_hand(gs.hand_id + 1)
        # S5（ADR-0047）: stack_start はブラインド post 前に取る。pokerkit backend は new_hand() で
        # ブラインドを自動 post するため、post 後に取ると result がブラインド分ずれる。台本のハンドは台本の持ち点。
        next_stacks = getattr(gs, "stacks_before_next_hand", None)
        self._stack_start = next_stacks() if next_stacks is not None else gs.get_stacks()
        try:
            gs.new_hand()
        except ValueError as e:
            self._refuse_start(str(e))
            return False
        self._start_refused = None
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
        self._hand_needs_review = bool(restored)
        self._hand_open = True
        self._hand_auto_started = auto
        self._showdown_mucks = []
        self._showdown_gaps = []
        self._showdown_notice_shown = False
        self._showdown_at = None
        self._showdown_shown = {}
        self._shown_holes = {}
        self._shown_board = 0
        self._seat_setup_warned = False
        self._departures = {}
        self._hand_inputs = []
        self._checkpoints = []
        self._held_betting_words = 0
        self._last_wager = None
        self._last_wager_point = None
        self._spoken_folds = {}
        self._spoken_fold_raw = {}
        self._foldout_pending = None
        self._foldout_winner_left = False
        self._last_action_at = self._hand_started_epoch
        self._street_marks = {}
        self._streets_synced = set()
        self._silent_mic_warned = False
        self._board_before_deal = False
        self._announced_hand = None
        self._deal_order_checked = False
        # ボタンを直して始め直すときに戻る状態（ブラインドを置いた直後、アクションは無い）
        self._hand_origin = (
            self._take_checkpoint(0) if hasattr(gs, "restart_hand_with_button") else None
        )
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
        override = getattr(gs, "last_button_override", None)
        if override:
            # 手動移動でボタンが通常の進み方と違う席になった（仕様 §9: 手違いの修正か、誤操作かを人が見る）
            self._notice(f"ボタンを席{override[1]} に動かしました（通常の進み方なら席{override[0]}）")
        out = [s for s in self._game_seats() if s not in self._seats_in_hand()]
        details = [d for d in (
            "手札が配られました" if auto else "",
            f"ボタン 席{button}" if button is not None else "",
            ("休み: " + "・".join(f"席{s}" for s in out)) if out else "",
        ) if d]
        self._notice(f"ハンド {gs.hand_id} 開始" + (f"（{' / '.join(details)}）" if details else ""))
        self._show_hole_cards()
        self._publish_table_state()
        return True

    def _refuse_start(self, why: str) -> None:
        """ハンドを始められない（配られる席が 2 つ未満）。同じ理由は 1 回だけ知らせる。"""
        message = (
            f"ハンドを始められません — {why}。r <席> <金額> で買い足すか、"
            "name <席> <名前> で参加させてください"
        )
        if message == self._start_refused:
            logger.debug("%s", message)
            return
        self._start_refused = message
        self._notice(message)

    def _restore_dealt_busted_seats(self) -> list[int]:
        """持ち点 0 の席に手札が配られた → 休みにせず、最初の持ち点に戻して配る（要確認）。戻した席を返す。

        持ち点 0 の人に札は配られないので、記録の誤り（聞き違いのオールイン等）か買い足しの入れ忘れ。休みにすると
        手番の順が狂い、そのハンドのアクションがすべてずれる（店舗 2026-09-27: 聞き違いのオールインで席4 が 0 に
        なり、次のハンドで 2♣ 2♦ が配られたのに休み扱いになった）。
        """
        if not (self._rules_aware and self._deal_hands):
            return []
        gs = self._game_state
        try:
            stacks = gs.get_stacks()
            playing = getattr(gs, "playing_seats", None)
            # チップがある席（ハンドの途中に入れた買い足しを含む）
            chips = set(playing()) if playing is not None else {s for s, st in stacks.items() if st > 0}
        except Exception:  # noqa: BLE001
            return []
        is_out = getattr(gs, "is_sitting_out", None)
        restored = []
        for seat, cards in sorted(self._deal_hands.items()):
            amount = self._initial_stacks.get(seat, 0)
            if (len(cards) < 2 or seat not in stacks or seat in chips or amount <= 0
                    or (is_out is not None and is_out(seat))):
                continue
            try:
                gs.rebuy(seat, amount)
            except ValueError:
                logger.exception("持ち点を戻せませんでした（席 %s）", seat)
                continue
            restored.append(seat)
            self._provisional_stacks[seat] = (amount, gs.hand_id + 1)
            self._notice(
                f"持ち点 0 の席{seat} に手札が配られました — 最初の持ち点 {amount} に戻して配ります（記録の誤りか"
                f"買い足しの入れ忘れ。要確認。買い足しなら r {seat} <額> で入れると、この額の代わりになります）"
            )
        return restored

    def _seats_in_hand(self) -> list[int]:
        """いまのハンドに配られた席（backend が区別しなければ全席）。"""
        method = getattr(self._game_state, "seats_in_hand", None)
        if method is None:
            return self._game_seats()
        try:
            return list(method())
        except Exception:  # noqa: BLE001
            return self._game_seats()

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
        winner_seat: Optional[int],
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
            if winner_seat is None:
                gs.end_hand_refund()             # 勝者が分からない: チップを動かさない
            elif awards is not None:
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
        in_hand = set(self._seats_in_hand())
        players_info = []
        for seat in sorted(s for s in stacks_end if s in in_hand):   # 配られた席だけ
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
            # 勝った人に言った役名（見せた人ごとに言う運用, オーナー 2026-09-30）
            announced_hand=(self._showdown_shown.get(winner_seat) if self._showdown_shown
                            else self._announced_hand),
        )

        self._json_writer.append_hand_summary(summary)
        self._clear_live_hand()
        if self._on_hand:
            self._on_hand(summary)
        self._apply_stack_corrections()       # そのハンドの結果には入れない
        logger.info("Hand %d finalized. Winner: seat %s", gs.hand_id, winner_seat)
        self._notice(self._describe_result(summary, awards))
        cards = self._describe_cards(summary)
        if cards:
            self._card_info(f"ハンド {summary.hand_id}: {cards}")
        self._last_result = {
            "hand_id": gs.hand_id,
            "winners": sorted(awards) if awards else ([] if winner_seat is None else [winner_seat]),
            "source": winner_source,
            "hand": next((h.get("hand") for h in (showdown or []) if h.get("seat") == winner_seat), None),
            "hands": [h.get("hand") for h in (showdown or []) if h.get("hand")],
        }
        self._publish_table_state()
        self._current_actions = []
        # _current_actions と対称にリセットし、stale フラグが次のサマリーへ
        # 漏れない（new_hand を挟まない再 finalize でも残らない）ようにする。
        self._hand_needs_review = False
        self._hand_open = False
        self._hand_auto_started = False
        self._showdown_mucks = []
        self._showdown_gaps = []
        self._foldout_pending = None
        self._departures = {}
        self._hand_inputs = []
        self._checkpoints = []
        self._street_marks = {}
        self._streets_synced = set()
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

        if summary.winner_seat is None:
            text = "勝者なし（チップは動かしていません）"
        elif awards and len(awards) > 1:
            text = "分配: " + "・".join(f"{who(s)} {amt}" for s, amt in sorted(awards.items()))
        else:
            text = f"勝ち: {who(summary.winner_seat)}"
        hands = {h["seat"]: h["hand"] for h in (summary.showdown or [])}
        detail = {
            "fold": "ほかは全員フォールド",
            "estimated": "勝者が決まらず仮",
            "undetermined": "札が読めず手札で判定できない",
        }.get(summary.winner_source or "", "")
        if summary.winner_source == "cards" and summary.winner_seat in hands:
            detail = HAND_NAMES_JA.get(hands[summary.winner_seat], hands[summary.winner_seat])
        elif summary.winner_source == "announced" and summary.announced_hand:
            detail = "ディーラーの役名 " + HAND_NAMES_JA.get(summary.announced_hand, summary.announced_hand)
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
