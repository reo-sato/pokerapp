"""rfid/reader_thread.py

Phase 6: RFID リーダーをポーリングして RFIDEvent を rfid_queue に投入するスレッド。

設計方針:
- 各リーダーを poll_interval_ms 間隔でポーリングする。
- **1 リーダーに複数枚**（席 = hole card 2 枚重ね / board = 1 台に 1〜3 枚, 契約 v1.1 §6）を
  想定し、bridge からは UID の**集合**を読む（`read_uids()`）。
- デバウンス: リーダーごとに前回の UID 集合を保持し、**新しく増えた UID だけ** RFIDEvent を
  1 件ずつ投入する。置きっぱなしは再発火しない。外れた UID は状態更新のみ（イベントなし）で、
  外して再度置けば同じ UID がもう一度発火する（契約 §8 を UID 単位に拡張）。
- reader_configs: [{"name": "...", "reader": 0, "role": "seat", "seat": 1}, ...] の形式。
  `reader`（任意・既定 0）は **物理リーダーの index**（Get UID の P2, 契約 v1.2 §6 / ADR-0052）。
  Windows の汎用 CCID ドライバは 1 インターフェース 1 slot しか公開しないため、PC/SC reader
  （`name`）は 1 つで、物理リーダー N 台は `reader` で選ぶ。`(name, reader)` の組で一意。
  role="board" は **役割だけ**を書く（位置は書かない）。ボード領域には board reader が N 台
  並んでいるだけで、どの台がどのストリートを受けるかは置き方次第（flop 3 枚が 3 台に散ることも、
  真ん中の 1 台に 2 枚載ることもある）。よって **board reader 全台を 1 つの論理ボード**として扱い、
  `board_index`（1..5）は **全台を通した検出順** = ディーラーが配った順で決める（契約 v1.3 §4 /
  ADR-0053）。旧 config の `index` / `cards` は廃止（あれば WARN して無視）。

設定例 (config.json, 本番 11 台 = 席 8 + board 3。reader 名は 1 つだけ):
    "rfid": {
      "enabled": true,
      "transport": "pcsc",
      "poll_interval_ms": 100,
      "pcsc_readers": [
        {"name": "PokerRFID PN5180-CCID 0", "reader": 0,  "role": "seat", "seat": 1},
        {"name": "PokerRFID PN5180-CCID 0", "reader": 8,  "role": "board"},
        {"name": "PokerRFID PN5180-CCID 0", "reader": 9,  "role": "board"},
        {"name": "PokerRFID PN5180-CCID 0", "reader": 10, "role": "board"}
      ],
      "card_master_file": "./rfid_cards.json"
    }
board reader は **左から右の順に並べて書く**（同じ poll で 2 枚以上増えたときの位置順が
config の記載順で決まるため。1 台に複数枚載ったぶんの左右順は UID 順で不定 = §4 の既知の制約）。

**卓の流れに合わせた解釈（ADR-0058, 本番の起動経路で有効）**:

- **席で読んだ札はボードの札にならない**（常に有効）。フォールドした手札は卓の中央へ押し出され、
  ボードのリーダーの上を通る。そのハンドで席に記録した UID を board reader が読んでも位置は
  与えず、その席の**マック（手札が中央を通過した）**として記録する。
- **ボードの札は載り続けてから確定する**（`commit_sec`）。通過する札は一瞬しか読めない。
  確定までは位置を与えず、確定した札の時刻は**最初に見えた時刻**のまま（ADR-0055）。
- **配り直しは入力なしで反映する**（`release_sec`）。前の札が `release_sec` 以上見えず、
  **新しい札が載り続けた**ときだけ差し替える。一瞬の読み落ちでは差し替わらない（ISSUE-0026）。
  ボードは次の 3 通り（どれにも当たらなければ次の空き位置 = 従来どおり）:
  1. **1 枚だけの差し直し**: 前の札が消えた近くに新しい札が載り続けたら、その位置を差し替える。
     前の札が `redeal_confirm_sec` 見えないのを確かめてから反映し、その間に戻れば新しい札は次の
     位置（読み落ちだった）。flop の札は「前の札を読んでいたリーダー」+ 消えてから
     `redeal_window_sec` 以内、**最後に配った turn / river** は「同じか隣のリーダー」+ 時間は
     問わない（位置がリーダーの境目にある / 間を空けて配り直す）。別のリーダーの札が読み落ちて
     いる間に次のストリートの札が来ても、次の位置のまま。**このハンドで一度でも読めなくなって
     戻った札は差し直しの対象にしない**（読みにくい位置の札 = 消えても取り除いたとは限らない）。
     差し替えた札が戻ってきて、差し替えた札（最後に配った札）も載っていれば、差し替えを取り消す
     （前の札を元の位置へ、差し替えた札を次の位置へ）。
  2. **flop 全体の配り直し**: flop の札が全部 `redeal_confirm_sec` 以上見えなければ、時間によらず
     flop の位置を若い順に差し替える（早すぎた flop を戻して後から配り直した）。
  3. **5 枚埋まったボード**: 新しい札が確定し、5 枚のうち 1 枚だけが `release_sec` 以上消えていれば、
     新しい札はその札の差し直し。**その位置に入れる**（river まで配ったあとで turn を差し直した）。ただし、
     消えた札のあとに空き位置へ入れた札があれば、そちらが見逃した差し直しなので、その札を消えた札の位置へ
     移して後ろを詰め、新しい札を 5 枚目にする（読みにくい札を本当に差し直した → river で直る）。消えた札が
     まだ `release_sec` に達していなければ待つ（戻れば 6 枚目 = WARN）。
  席では、同じハンドで別の席に記録した札が、元の席から消えて新しい席に載り続けたら移す
  （配り直しで席が変わった）。

既定値（`commit_sec=0` / `release_sec=None`）は従来の挙動（最初に見えた瞬間に確定・ハンド内は
append-only・差し替えは明示の訂正コマンドだけ）で、`tools/probe_pcsc.py` の検査が使う。

**読み取り装置（ESP32）の再起動（店舗 2026-10-06, 契約 v1.11 §6）**: 真ん中のボードのリーダーが 1 時間「札なし」と
答え続け（エラーは出ない）、読み取り装置の再起動で直った。`device`（`AutoRFIDSource`）を渡すと:

- 使えるリーダーの一覧（firmware が起動のときに初期化できた・いまも答える reader）を `READY_CHECK_SEC` おきに聞き、
  設定のリーダーが一覧に無ければ知らせる（`on_notice`）。
- **ボードのリーダーの見張り**: ボードに 3 枚以上配ったハンドで 1 枚も読まなかったボードのリーダー（いちばん右は 4 枚
  以上のハンドだけ数える）が `SILENT_HANDS` ハンド続いたら知らせる（店舗 10/06 の健全な 59 ハンドでは、どの
  ボードのリーダーも毎ハンド札を読んでいた。不調の 4 ハンドは真ん中が 1 枚も読まなかった）。一覧に出ない不調用。
- `request_restart()`（CLI の `rr`）: 卓に札が無くなったら再起動を頼み、つなぎ直して一覧を聞き直す。
- `auto_restart`: 使えないリーダーがあり、卓に札が無い状態が `AUTO_RESTART_EMPTY_SEC` 続いたら自動で頼む
  （1 つの不調につき `AUTO_RESTART_MAX` 回まで）。再起動のあいだは全リーダーが読めないので、卓に札が無いとき
  （ハンドの間）だけにする。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.event_queue import EventQueue
from core.events import RFIDEvent
from rfid.bridge import (
    MAX_READER_INDEX,
    PCSCBridge,
    bridge_read_uids,
    call_bridge_factory,
    pcsc_reader_present,
)
from rfid.card_master import CardMaster

logger = logging.getLogger(__name__)

# コミュニティカードの最大枚数（flop 3 + turn + river）。board 位置は 1..5。
_BOARD_MAX_CARDS = 5
# ハンドの終わりに札をまとめて動かす（ボードを片付ける・デッキやマックの束がボードのリーダーの上を通る）と、
# 新しい札がいくつも続けて載る。turn まで配ったあと（ボードに 4 枚以上）に、この秒数の間に新しい札が
# この枚数以上確定したら、差し直しではなく片付けとみる（店舗 2026-09-27: 片付けの札 6 枚でボードの 3 か所を
# 差し替えていた）。turn / river を続けて配っても新しい札は 2 枚まで。
_SWEEP_WINDOW_SEC = 10.0
_SWEEP_MIN_NEW_CARDS = 3
_SWEEP_MIN_BOARD = 4
# 席のホールカードの枚数（テキサスホールデム）。
_SEAT_MAX_CARDS = 2
# flop の位置。flop の札が全部消えたら、時間によらず次の新しい札から順に差し替える（ADR-0058）。
_FLOP_SLOTS: tuple[int, ...] = (1, 2, 3)

# 本番（main.py）の既定値。config の rfid.commit_sec / gap_sec / release_sec / redeal_window_sec で
# 上書きする（ADR-0058）。
DEFAULT_COMMIT_SEC = 2.0    # ボードの札はこの秒数載り続けてから確定（フォールドした札の通過を除く）
DEFAULT_GAP_SEC = 1.5       # この秒数までの途切れは「載り続けている」とみなす（間欠読みの許容）
DEFAULT_RELEASE_SEC = 6.0   # 前の札がこの秒数以上見えないときだけ、新しい札で差し替える（配り直し）
DEFAULT_REDEAL_WINDOW_SEC = 30.0  # ボードの札が消えてからこの秒数以内に同じリーダーへ置かれた札は差し直し
DEFAULT_REDEAL_CONFIRM_SEC = 3.0  # ボードの差し直しで、前の札が見えないことを確かめる秒数
# フロップは 3 枚が同時に出る（オーナー, 2026-09-29）。フロップの最初の札からこの秒数より後に見え始めた札は、
# フロップの札が読めていなくてもフロップの空き位置に入れない（ターン以降の位置にする）。店舗 20 ハンドの実測:
# フロップの 3 枚は最初の札から 5.5 秒以内、ターンは早いと 7 秒（どちらも一番右のリーダー = 下の位置の目安で分かる）。
DEFAULT_FLOP_WINDOW_SEC = 10.0
# board reader の左右の位置の目安（左端 0 〜 右端 1）。ボードの 5 枚は左から均等に並ぶとみて、フロップ（1〜3 枚目）は
# 0.6 より左、ターン・リバー（4・5 枚目）は 0.6 より右に載る。右端が 0.6 以下のリーダーはフロップの札しか読まず、
# 左端が 0.6 以上のリーダーはターン・リバーしか読まない（店舗の 3 台: 左 = フロップだけ・右 = ターン・リバーだけ・
# 真ん中 = どちらも。20 ハンドの実測で例外なし）。
_FLOP_EDGE = 0.6
# 起動時にリーダーが 1 台もつながらないとき、つながるまで試し直す間隔（秒）。前は 1 回試してスレッドを終え、
# ログのファイルにだけ書いていた（店舗 2026-09-30: 画面には何も出ず、手札を配ってもハンドが始まらないまま 2 ハンドが
# 記録されなかった）。USB の差し直し・リモートデスクトップのスマートカードの転送を止めたあと、起動し直さなくても使い始める。
DEFAULT_RECONNECT_SEC = 5.0
# 確定前のボードの札は、この秒数までの途切れを「載り続けている」とみなす（`gap_sec` より長い）。
# リーダーの境目・重ね置きの札は途切れながら読めるので、`gap_sec` のままだと確定まで数え直しを
# 繰り返して反映が遅れる（店舗の実卓, ADR-0058 追記 3）。一瞬の通過は 1 回きりなので影響しない。
_PENDING_GAP_SEC = 3.0
# 席の札を最初に読んだ時刻（配った順 = ボタンの置き忘れの救済, 2026-09-29）は、札がこの秒数より長く席から
# 離れていたら、次に載ったときに測り直す（前のハンドの札・片付けのあとに同じ席へ配られた札）。持ち上げて
# 見て戻した札は測り直さない。
_CARD_SINCE_FORGET_SEC = 10.0

# ――― 読み取り装置（ESP32）の見張りと再起動（契約 v1.11 §6, 店舗 2026-10-06）―――
READY_CHECK_SEC = 10.0          # 使えるリーダーの一覧を聞く間隔
SILENT_HANDS = 2                # ボードのリーダーがこのハンド数続けて 1 枚も読まなければ知らせる
_SILENT_EVAL_EMPTY_SEC = 3.0    # 卓に札が無い状態がこの秒数続いたら、そのハンドのボードのリーダーを数える
RESTART_EMPTY_SEC = 2.0         # rr: 卓に札が無い状態がこの秒数続いたら再起動を頼む
AUTO_RESTART_EMPTY_SEC = 10.0   # 自動: 使えないリーダーがあり、卓に札が無い状態がこの秒数続いたら頼む
AUTO_RESTART_MAX = 2            # 自動の再起動は 1 つの不調につきこの回数まで
AUTO_RESTART_GAP_SEC = 60.0     # 自動の再起動の間隔
RESTART_SETTLE_SEC = 2.0        # 再起動を頼んでから、つなぎ直しを試し始めるまで（読み取り装置が USB から外れる時間）
RESTART_RETRY_SEC = 1.0         # 再起動のあと、つなぎ直し・一覧の問い合わせを試す間隔
RESTART_RECONNECT_SEC = 40.0    # 再起動のあと、つながるまで待つ上限
RESTART_READY_SEC = 20.0        # つながったあと、使えるリーダーの一覧が届くまで待つ上限


@dataclass
class _Run:
    """ある場所（ボード / 席 N）で 1 枚の札が載り続けている期間。"""

    first_seen: float
    last_seen: float
    reader_id: str = ""     # 最初に見えた reader（event の reader_id に使う）
    # この期間に札を読んだ reader。ボードの差し直しを「同じ場所に置き直した」と判断する根拠。
    readers: set[str] = field(default_factory=set)
    fired: bool = False     # この期間について判定を済ませた（確定・再発火・却下のいずれか）
    noted: bool = False     # 保留理由をログに出した（毎 poll 出さないため）
    waiting: bool = False   # ボードの差し直しを確かめ中（卓モニタの「差し直し確認中」）


def _reader_index_of(cfg: dict, position: int) -> int:
    """config 要素の物理 reader index（Get UID の P2, 契約 v1.2 §6）。既定 0。

    不正値（int でない / 範囲外）は WARN して 0 にフォールバックする（起動は落とさない）。
    """
    value = cfg.get("reader", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_READER_INDEX:
        logger.warning(
            "Invalid 'reader' %r on pcsc_readers[%d] — 0 として扱います（0..%d, 契約 v1.2 §4）",
            value, position, MAX_READER_INDEX,
        )
        return 0
    return value


class RFIDThread(threading.Thread):
    """全 RFID リーダーをポーリングし、タッチ検出時に RFIDEvent を rfid_queue に投入する。"""

    def __init__(
        self,
        rfid_queue: EventQueue,
        card_master: CardMaster,
        reader_configs: list[dict],
        poll_interval_ms: int = 100,
        stop_event: Optional[threading.Event] = None,
        bridge_factory: Optional[object] = None,
        clock: Optional[Callable[[], float]] = None,
        commit_sec: float = 0.0,
        gap_sec: float = 0.0,
        release_sec: Optional[float] = None,
        redeal_window_sec: Optional[float] = None,
        redeal_confirm_sec: Optional[float] = None,
        flop_window_sec: Optional[float] = None,
        reconnect_sec: Optional[float] = DEFAULT_RECONNECT_SEC,
        reader_present: Optional[Callable[[str], bool]] = None,
        device: Optional[object] = None,
        on_notice: Optional[Callable[[str], None]] = None,
        auto_restart: bool = False,
    ) -> None:
        """
        Args:
            rfid_queue:       RFIDEvent を投入するキュー。
            card_master:      タグ ID → カード文字列 の対応表。
            reader_configs:   リーダー設定リスト。各要素は:
                              {"name": str, "reader": int (任意・既定 0 = Get UID の P2, 契約 v1.2 §6),
                               "role": "seat"|"board", "seat": int (roleが"seat"の場合)}
                              role="board" に位置指定は無い（全台を通した検出順で 1..5 を振る,
                              契約 v1.3 §4）。board reader は左から右の順に並べて書く。
            poll_interval_ms: ポーリング間隔 (ミリ秒)。
            stop_event:       セット時にスレッドを停止する。
            bridge_factory:   テスト用ブリッジファクトリ (reader_name: str, reader_index: int) -> bridge。
                              旧シグネチャ (reader_name) -> bridge も互換で受け付ける。
                              省略時は PCSCBridge を使用。
            clock:            epoch 秒を返す時計（既定 time.time）。マック観測時刻の源（ADR-0055）。
            commit_sec:       ボードの札を確定するまでに載り続けている秒数（ADR-0058）。0 = 最初に
                              見えた瞬間に確定（従来）。> 0 か `release_sec` を指定すると卓の流れに
                              合わせた解釈（下記）が有効になる。
            gap_sec:          この秒数以下の途切れは「載り続けている」とみなす（間欠読みの許容）。
            release_sec:      前の札がこの秒数以上見えず、新しい札が載り続けたら差し替える
                              （配り直し, ADR-0058）。None = 差し替えは明示の訂正コマンドだけ（従来）。
            redeal_window_sec: ボードの 1 枚だけの差し直しとみなす時間窓。前の札が消えてから
                              この秒数以内に、前の札を読んでいたリーダーへ新しい札が置かれたら差し替える。
                              None = 1 枚だけの差し直しは扱わない（flop 全体と 6 枚目の詰め直しだけ）。
                              最後に配った turn / river は時間窓によらない。
            redeal_confirm_sec: ボードの差し直し（1 枚 / flop 全体）で、前の札が見えないことを
                              確かめる秒数。None = `release_sec` と同じ。
            flop_window_sec:  フロップは 3 枚が同時に出る。フロップの最初の札からこの秒数より後に見え始めた
                              札は、フロップが 3 枚そろっていなくてもフロップの位置に入れない（ターン以降）。
                              一番左・一番右のリーダーの札はリーダーの位置で決める（`_FLOP_EDGE`）。
                              None = 従来どおり空いている位置の若い順（オーナー 2026-09-29）。
            reconnect_sec:    起動時にリーダーが 1 台もつながらないとき、つながるまで試し直す間隔（秒）。
                              None = 試し直さずに終える（従来）。
            reader_present:   リーダー名が PC/SC に見えているか（試し直すのは見えたときだけ = つながらない間に
                              ログを埋めない）。既定は pyscard の一覧。
            device:           読み取り装置の管理（`AutoRFIDSource`: `ready_readers(names)` / `restart(names)` /
                              `manages_connection()` / `restarting()`）。None = 一覧も再起動も扱わない（`rr` は
                              「できません」と知らせる。ボードのリーダーの見張りは知らせるだけ）。
            on_notice:        知らせ（日本語 1 文）を出す先（CLI の「● …」）。None = ログだけ。
            auto_restart:     使えないリーダーがあれば、卓に札が無いときに自動で再起動を頼む。
        """
        super().__init__(daemon=True, name="RFIDThread")
        self._queue = rfid_queue
        self._card_master = card_master
        self._reader_configs = reader_configs
        self._poll_interval = poll_interval_ms / 1000.0
        self._stop_event = stop_event or threading.Event()
        self._bridge_factory = bridge_factory or PCSCBridge
        self._clock: Callable[[], float] = clock or time.time
        self._reconnect_sec = None if reconnect_sec is None else max(0.1, float(reconnect_sec))
        self._reader_present = reader_present or pcsc_reader_present

        # デバウンス用: reader_id → 現在載っている UID の集合（空 = カードなし）
        self._last_uids: dict[str, set[str]] = {}
        # 死活表示（dashboard が読む。dict ごと差し替える = GIL で atomic、lock 不要）:
        #   state: starting | running | no_readers | stopped
        #   connected/configured: 接続できた/設定された reader 数 / last_event_at: unix 秒
        self.health: dict = {
            "state": "starting",
            "connected": 0,
            "configured": len(reader_configs),
            "last_event_at": None,
        }
        # board 位置割り当て（**board reader 全台で 1 つの論理ボードを共有**, 契約 v1.3 §4）:
        # uid → board_index 1..5。どの台に載ったかではなく「ボード全体で何枚目か」で決まる。
        # **ハンド内は append-only**（一度与えた位置は返さない, ISSUE-0026）。ポーカーでは
        # ハンド中にボードのカードが減ることはなく、engine 側の board も縮まないので、
        # 両者を同じ規則にして構造的にずれないようにする。捨てるのは新ハンドだけ。
        self._board_indexes: dict[str, int] = {}
        # role=board の reader_id（新ハンドでデバウンスを落とす対象。poll 時に学習する）。
        self._board_reader_ids: set[str] = set()
        # seat → reader_id（席のカード訂正でデバウンスを落とす対象。poll 時に学習する）。
        self._seat_reader_ids: dict[int, set[str]] = {}
        # seat → **カードが席から消えた時刻**（epoch）。戻ってきたら消す（ADR-0055）。
        # fold を「判定」するためではなく、合成 fold に**実時刻を与える**ために使う
        # （プレイヤーはカードを持ち上げて見ることがあるので、不在そのものは fold を意味しない）。
        self._seat_absent_since: dict[int, float] = {}
        # このハンドで一度でも札が載った席（載ったことのない席は「離れた」にしない）
        self._seat_seen: set[int] = set()
        # (席, UID) → [最初に読んだ時刻, 離れた時刻（載っていれば None）]。配った順を見るため（2026-09-29）。
        # 新しいハンドの同期点でも捨てない（配布を検出してからハンドを始めるまでに同期点が来る）。
        self._seat_card_seen: dict[tuple[int, str], list] = {}

        # ――― 卓の流れに合わせた解釈（ADR-0058）―――
        self._commit_sec = max(0.0, float(commit_sec))
        self._gap_sec = max(0.0, float(gap_sec))
        self._release_sec = None if release_sec is None else max(0.0, float(release_sec))
        self._redeal_window_sec = (
            None if redeal_window_sec is None else max(0.0, float(redeal_window_sec))
        )
        self._redeal_confirm_sec = (
            self._release_sec if redeal_confirm_sec is None else max(0.0, float(redeal_confirm_sec))
        )
        self._flop_window_sec = None if flop_window_sec is None else max(0.0, float(flop_window_sec))
        self._tracking = self._commit_sec > 0 or self._release_sec is not None
        self._pending_gap_sec = (
            max(self._gap_sec, _PENDING_GAP_SEC) if self._commit_sec > 0 else self._gap_sec
        )
        # board reader の左からの並び（config の記載順, 0 始まり）。差し直しの「近くのリーダー」と
        # ログの「左から N 台目」に使う。
        board_ids = [f"reader_{i}" for i, c in enumerate(reader_configs) if c.get("role") == "board"]
        self._board_order: dict[str, int] = {rid: k for k, rid in enumerate(board_ids)}
        # 別スレッド（IntegrationThread）からの新ハンド / 訂正と poll の状態更新を直列化する。
        # 保持するのは状態の更新の間だけ（リーダーとの通信中は持たない）。
        self._lock = threading.RLock()
        # そのハンドで**席に記録した** UID → 席番号。ボードの札にはならない（常に有効）。
        self._seat_owner: dict[str, int] = {}
        # 席 → 手札が卓の中央（board reader の上）を通過した最初の時刻 = マック（表示・記録用）。
        self._mucked_at: dict[int, float] = {}
        # 以下は tracking 時のみ使う。場所は "board" / "seat:N"。
        self._runs: dict[tuple[str, str], _Run] = {}             # (場所, uid) → 載り続けている期間
        self._loc_present: dict[str, set[str]] = {}               # 場所 → 直前に見えていた uid
        self._seat_committed: dict[int, list[str]] = {}           # 席 → 記録した uid（最大 2）
        self._board_first_seen: dict[str, float] = {}             # ボードの札 → 配った時刻（詰め直し用）
        self._board_pending: set[str] = set()                     # flop の配り直しで、次の札が差し替える札
        self._board_rereads: dict[str, int] = {}                  # 確定前の札を読み直した回数（ログ用）
        # ボードの札ごとの「載り続けている期間」の数（このハンド）。2 以上 = 途切れて読み直したことがある
        # = 読みにくい位置にある札。消えても取り除いたとは限らないので差し直しの対象にしない。
        self._board_runs: dict[str, int] = {}
        # 差し替えられた札 → (位置, 配った時刻, 差し替えた札, 読んでいたリーダー)。戻ってきたら差し替えを
        # 取り消す（_restore_board_card）。
        self._board_retired: dict[str, tuple[int, float, str, set[str]]] = {}
        # 差し替えで位置を得た札（空き位置に入れた札ではない）。5 枚埋まったボードで札が取り除かれたとき、
        # 「消えたあとに空き位置に入れた札 = 見逃した差し直し」の候補から外す（_replace_on_full_board）。
        self._board_swapped_in: set[str] = set()
        # 片付けの見分け（_check_board_sweep）: このハンドで新しく確定した札（UID → 時刻）/ 差し替えの記録
        # （時刻, 位置, 前の札, 前の札の配布時刻, 新しい札, reader_id）/ 片付けとみた（以降ボードを変えない）
        self._board_new_commits: dict[str, float] = {}
        self._board_replacements: list[tuple[float, int, str, float, str, str]] = []
        self._board_swept = False

        # ――― 読み取り装置の見張りと再起動（契約 v1.11 §6）。poll のスレッドだけが触る（rr は queue で受ける）―――
        self._device = device
        self._on_notice = on_notice
        self._auto_restart = bool(auto_restart)
        self._bridges: dict[str, tuple] = {}
        self._restart_requests: "queue.Queue[str]" = queue.Queue()
        self._manual_pending = False                   # rr を受けて、卓に札が無くなるのを待っている
        self._raw_present: dict[str, bool] = {}        # reader_id → いま 1 枚以上読んでいるか（記録とは無関係）
        self._table_empty_since: Optional[float] = None
        self._deal_board_seen: set[str] = set()        # このハンドで 1 枚でも読んだボードのリーダー
        self._deal_board_uids: set[str] = set()        # このハンドで位置を与えたボードの札
        self._silent_streak: dict[str, int] = {}       # ボードのリーダー → 続けて 1 枚も読まなかったハンド数
        self._silent: set[str] = set()                 # 見張りで「読んでいない」とみたボードのリーダー
        # 「読んでいない」で再起動したボードのリーダー。また札を読むまで自動の再起動の回数を戻さない（店の置き方で
        # 読まないだけのときに、2 ハンドごとに再起動を繰り返さない）
        self._awaiting_proof: set[str] = set()
        self._unusable: list[str] = []                 # 一覧に無い設定のリーダー（reader_id）
        self._next_ready_check = 0.0
        self._next_reconnect = 0.0
        self._auto_attempts = 0
        self._auto_gave_up = False
        self._last_restart_at: Optional[float] = None
        self._restart_phase: Optional[str] = None      # None | "settle" | "reconnect" | "ready"
        self._restart_deadline = 0.0
        self._restart_next_try = 0.0
        self._restart_reason = ""

    def stop(self) -> None:
        self._stop_event.set()

    def request_restart(self) -> None:
        """読み取り装置の再起動を頼む（CLI の `rr`。別スレッドから呼ばれるので queue で渡す）。卓に札が無くなったら
        poll のスレッドが命令を送る。"""
        self._restart_requests.put("manual")

    def _reader_names(self) -> set[str]:
        return {str(cfg.get("name", "")) for cfg in self._reader_configs if cfg.get("name")}

    def _connect_readers(self) -> dict[str, tuple]:
        """設定のリーダーにつなぐ。つながった reader_id → (bridge, 設定)。"""
        bridges: dict[str, tuple] = {}
        for i, cfg in enumerate(self._reader_configs):
            reader_name = cfg.get("name", "")
            reader_index = _reader_index_of(cfg, i)
            reader_id = f"reader_{i}"
            bridge = call_bridge_factory(self._bridge_factory, reader_name, reader_index)
            if bridge.connect():
                bridges[reader_id] = (bridge, cfg)
                self._last_uids[reader_id] = set()
                # 役割は接続時に登録する（poll 時にも学習するが、**まだ一度も札が載っていない席**も
                # 卓状態に「未配布」として出したいので、変化を待たずにここで揃える）。
                if cfg.get("role") == "board":
                    self._board_reader_ids.add(reader_id)
                    self._warn_obsolete_board_fields(cfg, reader_id)
                elif isinstance(cfg.get("seat"), int):
                    self._seat_reader_ids.setdefault(cfg["seat"], set()).add(reader_id)
                logger.info(
                    "RFID reader ready: %s (reader %d, %s)", reader_name, reader_index, reader_id,
                )
            else:
                logger.warning(
                    "Could not connect to RFID reader: %s (reader %d)", reader_name, reader_index,
                )
        return bridges

    def run(self) -> None:
        logger.info("RFIDThread started (%d reader(s))", len(self._reader_configs))
        bridges = self._bridges = self._connect_readers()
        if not bridges:
            self.health = {
                "state": "no_readers",
                "connected": 0,
                "configured": len(self._reader_configs),
                "last_event_at": None,
            }
            if self._reconnect_sec is None:
                logger.warning("No RFID readers connected. RFIDThread exiting.")
                return
            logger.warning(
                "No RFID readers connected. つながるまで %.0f 秒ごとに試し直します（設定のリーダー名: %s）",
                self._reconnect_sec, ", ".join(sorted(self._reader_names())) or "なし",
            )
        while not bridges and not self._stop_event.wait(self._reconnect_sec or 0):
            if any(self._reader_present(name) for name in self._reader_names()):
                bridges = self._bridges = self._connect_readers()
        if not bridges:
            return
        self.health = {
            "state": "running",
            "connected": len(bridges),
            "configured": len(self._reader_configs),
            "last_event_at": None,
        }

        try:
            while not self._stop_event.is_set():
                self.step_device(self._clock())
                if self._restart_phase is None:
                    for reader_id, (bridge, cfg) in list(self._bridges.items()):
                        self._poll_reader(bridge, cfg, reader_id)
                    self._note_table(self._clock())
                time.sleep(self._poll_interval)
        finally:
            for reader_id, (bridge, _) in self._bridges.items():
                bridge.close()
            self.health = {**self.health, "state": "stopped"}
            logger.info("RFIDThread stopped")

    def _poll_reader(self, bridge: object, cfg: dict, reader_id: str) -> None:
        """1 リーダーをポーリングし、確定した札ごとに RFIDEvent を投入する。

        重ね置き（席 2 枚 / flop 3 枚）に対応するため、状態は UID 1 個ではなく集合で持つ。
        リーダーとの通信はロックの外で行い、状態の更新だけを直列化する。
        """
        uids = bridge_read_uids(bridge)   # 旧 bridge（read_uid のみ）互換シム
        current: set[str] = set(uids)
        self._raw_present[reader_id] = bool(current)
        if current and cfg.get("role") == "board":
            self._deal_board_seen.add(reader_id)
        with self._lock:
            if self._tracking:
                events = self._poll_tracked(cfg, reader_id, uids, current)
            else:
                events = self._poll_immediate(cfg, reader_id, uids, current)
        for event in events:
            if event.role == "board" and event.board_index is not None:
                self._deal_board_uids.add(event.tag_id)
            self._queue.put(event)
            logger.debug(
                "RFIDEvent: reader=%s role=%s seat=%s board_index=%s tag=%s card=%r replaces=%r",
                event.reader_id, event.role, event.seat, event.board_index, event.tag_id,
                event.card, event.replaces,
            )
        if events:
            self.health = {**self.health, "last_event_at": time.time()}

    # ――― 読み取り装置の見張りと再起動（契約 v1.11 §6, 店舗 2026-10-06）―――

    def _notice(self, message: str, level: int = logging.WARNING) -> None:
        logger.log(level, "%s", message)
        if self._on_notice is not None:
            try:
                self._on_notice(message)
            except Exception:  # noqa: BLE001 — 知らせの失敗で読み取りを止めない
                logger.exception("on_notice failed")

    def _reader_desc(self, reader_id: str) -> str:
        """「ボードの左から 2 台目のリーダー（reader 9）」「席3 のリーダー（reader 2）」。"""
        try:
            i = int(reader_id.rsplit("_", 1)[1])
            cfg = self._reader_configs[i]
        except (IndexError, ValueError):
            return reader_id
        index = _reader_index_of(cfg, i)
        if cfg.get("role") == "board":
            k = self._board_order.get(reader_id)
            where = f"ボードの左から {k + 1} 台目" if k is not None else "ボード"
        elif isinstance(cfg.get("seat"), int):
            where = f"席{cfg['seat']} "
        else:
            where = reader_id + " "
        return f"{where}のリーダー（reader {index}）"

    def _remedy(self) -> str:
        if self._device is None:
            return "読み取り装置（ESP32）の USB を抜いて挿し直してください（卓に札が無いときに）。"
        if self._auto_restart and not self._auto_gave_up:
            return "卓に札が無くなったら読み取り装置を自動で再起動します（すぐなら rr）。"
        return "卓に札が無いときに rr で読み取り装置を再起動できます（直らなければ配線・電源）。"

    def _table_empty_for(self, now: float) -> float:
        return 0.0 if self._table_empty_since is None else max(0.0, now - self._table_empty_since)

    def _note_table(self, now: float) -> None:
        """卓に札が無い時間を測り、ハンドが終わって札が片付いたらそのハンドのボードのリーダーを数える。"""
        if any(self._raw_present.values()):
            self._table_empty_since = None
            return
        if self._table_empty_since is None:
            self._table_empty_since = now
        if now - self._table_empty_since >= _SILENT_EVAL_EMPTY_SEC and len(self._deal_board_uids) >= 3:
            self._evaluate_board_readers()

    def _evaluate_board_readers(self) -> None:
        """ボードに 3 枚以上配ったハンドで、1 枚も読まなかったボードのリーダーを数える（いちばん右はターン・リバー
        だけを読むので 4 枚以上のハンドだけ）。`SILENT_HANDS` ハンド続いたら知らせる（一覧に出ない不調）。"""
        placed, seen = len(self._deal_board_uids), self._deal_board_seen
        self._deal_board_uids, self._deal_board_seen = set(), set()
        board_ids = sorted(self._board_order, key=self._board_order.__getitem__)
        if len(board_ids) < 2:
            return                       # 1 台なら記録した札はその台が読んだ
        newly: list[str] = []
        for k, rid in enumerate(board_ids):
            if rid not in self._raw_present:
                continue                 # つながっていない（別に知らせる）
            if placed < (4 if k == len(board_ids) - 1 else 3):
                continue
            if rid in seen:
                self._silent_streak[rid] = 0
                if rid in self._silent or rid in self._awaiting_proof:
                    self._silent.discard(rid)
                    self._awaiting_proof.discard(rid)
                    self._notice(f"RFID: {self._reader_desc(rid)}がまた札を読みました。", logging.INFO)
                continue
            self._silent_streak[rid] = self._silent_streak.get(rid, 0) + 1
            if self._silent_streak[rid] >= SILENT_HANDS and rid not in self._silent:
                self._silent.add(rid)
                newly.append(rid)
        if newly:
            self._notice(
                "⚠ RFID: " + "・".join(self._reader_desc(r) for r in newly)
                + f"が {SILENT_HANDS} ハンド続けて 1 枚も札を読んでいません。" + self._remedy()
            )
        self._maybe_reset_attempts()

    def _maybe_reset_attempts(self) -> None:
        """直った証拠（一覧で全台使える・読んでいなかったリーダーがまた読んだ）があれば、自動の再起動の回数を戻す。"""
        if not self._unusable and not self._silent and not self._awaiting_proof:
            self._auto_attempts, self._auto_gave_up = 0, False

    def _ready_map(self) -> Optional[dict]:
        try:
            return self._device.ready_readers(sorted(self._reader_names()))
        except Exception:  # noqa: BLE001
            logger.exception("使えるリーダーの一覧を聞けませんでした")
            return None

    def _unusable_from(self, ready: dict) -> list[str]:
        """一覧（リーダー名 → 使える物理リーダーの番号 / None）に無い設定のリーダー。"""
        unusable = []
        for i, cfg in enumerate(self._reader_configs):
            listed = ready.get(str(cfg.get("name", "")))
            if listed is not None and _reader_index_of(cfg, i) not in listed:
                unusable.append(f"reader_{i}")
        return unusable

    def _set_unusable(self, unusable: list[str], announce: bool = True) -> None:
        added = [r for r in unusable if r not in self._unusable]
        changed = unusable != self._unusable
        self._unusable = unusable
        self.health = {**self.health, "unusable": [self._reader_desc(r) for r in unusable]}
        self._maybe_reset_attempts()
        if not (announce and changed):
            return
        if added:
            self._notice("⚠ RFID: 使えないリーダーがあります: " + "・".join(self._reader_desc(r) for r in added)
                         + "（札を置いても読めません）。" + self._remedy())
        elif not unusable:
            self._notice("RFID: 使えないリーダーはありません（全台使えます）。", logging.INFO)

    def _check_ready(self, now: float) -> None:
        if self._device is None or now < self._next_ready_check:
            return
        self._next_ready_check = now + READY_CHECK_SEC
        ready = self._ready_map()
        if not ready or any(v is None for v in ready.values()):
            return                       # 分からない（旧 firmware・旧中継・起動の途中）= いまの見立てのまま
        self._set_unusable(self._unusable_from(ready))

    def _device_call(self, name: str) -> bool:
        method = getattr(self._device, name, None)
        try:
            return bool(method()) if callable(method) else False
        except Exception:  # noqa: BLE001
            logger.exception("device.%s failed", name)
            return False

    def _take_restart_requests(self, now: float) -> None:
        while True:
            try:
                self._restart_requests.get_nowait()
            except queue.Empty:
                return
            if self._device is None:
                self._notice("RFID: この読み取りでは読み取り装置を再起動できません。読み取り装置（ESP32）の USB を"
                             "抜いて挿し直してください。")
            elif self._restart_phase is not None:
                self._notice("RFID: いま読み取り装置を再起動しています。", logging.INFO)
            elif self._manual_pending:
                self._notice("RFID: 卓の札が片付くのを待っています（片付けたら再起動します）。", logging.INFO)
            else:
                self._manual_pending = True
                if self._table_empty_for(now) < RESTART_EMPTY_SEC:
                    self._notice("RFID: 卓に札が載っています。札を片付けたら読み取り装置を再起動します"
                                 "（数秒、札が読めません）。", logging.INFO)

    def step_device(self, now: float) -> None:
        """rr の要求・再起動の進み・使えるリーダーの一覧・自動の再起動（poll のスレッドが 1 周ごとに呼ぶ）。"""
        self._take_restart_requests(now)
        if self._restart_phase is not None:
            self._step_restart(now)
            return
        self._reconnect_if_needed(now)
        self._check_ready(now)
        empty_for = self._table_empty_for(now)
        if self._manual_pending and empty_for >= RESTART_EMPTY_SEC:
            self._manual_pending = False
            self._begin_restart(now, "manual")
            return
        if not (self._auto_restart and self._device is not None and not self._auto_gave_up
                and (self._unusable or self._silent) and empty_for >= AUTO_RESTART_EMPTY_SEC):
            return
        if self._last_restart_at is not None and now - self._last_restart_at < AUTO_RESTART_GAP_SEC:
            return
        if self._auto_attempts >= AUTO_RESTART_MAX:
            self._auto_gave_up = True
            self._notice(f"⚠ RFID: 読み取り装置を {AUTO_RESTART_MAX} 回自動で再起動しても直りません: "
                         + "・".join(self._reader_desc(r) for r in self._unusable or sorted(self._silent))
                         + "。配線・電源を確かめてください（rr でもう一度試せます）。")
            return
        self._auto_attempts += 1
        self._begin_restart(now, "auto")

    def _begin_restart(self, now: float, reason: str) -> None:
        names = sorted(self._reader_names())
        self._last_restart_at = now
        try:
            statuses = self._device.restart(names)
        except Exception:  # noqa: BLE001
            logger.exception("読み取り装置に再起動を頼めませんでした")
            statuses = {name: "failed" for name in names}
        if not any(st == "accepted" for st in statuses.values()):
            if any(st == "relay_outdated" for st in statuses.values()):
                message = ("⚠ RFID: 中継が古い版で、読み取り装置の再起動の命令がありません。管理者で "
                           "installer\\rfid_relay_task.ps1 をもう一度実行して、中継を新しい版で動かし直してください。")
            elif any(st == "old_firmware" for st in statuses.values()):
                message = ("⚠ RFID: 読み取り装置のファームウェアに再起動の命令がありません（古い版）。読み取り装置"
                           "（ESP32）の USB を抜いて挿し直してください（ファームウェアを書き換えるとロガーから再起動できます）。")
            else:
                message = "⚠ RFID: 読み取り装置に再起動の命令を送れませんでした。USB のつながりを確かめてください。"
            self._notice(message)
            if reason == "auto":
                self._auto_gave_up = True      # 命令が無い・送れないなら自動では繰り返さない
            return
        self._restart_reason = reason
        self._restart_phase = "settle"
        self._restart_deadline = now + RESTART_SETTLE_SEC
        self._restart_next_try = now
        # 読んでいなかったリーダーは数え直す（また 2 ハンド読まなければ知らせる）。また読むまでは直ったとみない
        self._awaiting_proof |= self._silent
        self._silent, self._silent_streak = set(), {}
        self._deal_board_seen, self._deal_board_uids = set(), set()
        # CLI はつながり具合の行（health の state）で「再起動しています」と出す（知らせと二重にしない）
        self.health = {**self.health, "state": "restarting"}
        logger.warning("RFID: 読み取り装置を%s再起動しています（数秒、札が読めません）",
                       "自動で" if reason == "auto" else "")

    def _step_restart(self, now: float) -> None:
        if self._restart_phase == "settle":
            if now < self._restart_deadline:
                return
            if self._device_call("manages_connection"):
                # 中継から読んでいる: つなぎ直しは中継がする。一覧が届くのを待つ
                self._restart_phase = "ready"
                self._restart_deadline = now + RESTART_RECONNECT_SEC + RESTART_READY_SEC
            else:
                # 直接読んでいる: USB が列挙し直すので、接続を捨ててからつなぎ直す
                for bridge, _cfg in self._bridges.values():
                    bridge.close()
                self._bridges = {}
                self._restart_phase = "reconnect"
                self._restart_deadline = now + RESTART_RECONNECT_SEC
            self._restart_next_try = now
            return
        if now < self._restart_next_try:
            return
        self._restart_next_try = now + RESTART_RETRY_SEC
        if self._restart_phase == "reconnect":
            if any(self._reader_present(name) for name in self._reader_names()):
                bridges = self._connect_readers()
                if bridges:
                    self._bridges = bridges
                    self._restart_phase = "ready"
                    self._restart_deadline = now + RESTART_READY_SEC
                    return
            if now >= self._restart_deadline:
                self._finish_restart(now, connected=False)
            return
        # "ready": 使えるリーダーの一覧が届くのを待つ（起動直後の firmware は初期化が終わるまで返さない）
        if self._device_call("restarting"):
            if now >= self._restart_deadline:
                self._finish_restart(now, connected=False)
            return
        ready = self._ready_map()
        complete = bool(ready) and all(v is not None for v in ready.values())
        if complete or now >= self._restart_deadline:
            self._finish_restart(now, connected=True, ready=ready if complete else None)

    def _finish_restart(self, now: float, connected: bool, ready: Optional[dict] = None) -> None:
        self._restart_phase = None
        self._next_ready_check = now + READY_CHECK_SEC
        self._next_reconnect = now + (self._reconnect_sec or 0.0)
        self._table_empty_since = None
        self._raw_present = {}
        state = "running" if connected and (self._bridges or self._device_call("manages_connection")) else "no_readers"
        self.health = {**self.health, "state": state, "connected": len(self._bridges)}
        if state != "running":
            self._notice("⚠ RFID: 読み取り装置の再起動のあと、リーダーにつながりません。USB を確かめてください"
                         "（つながれば読み始めます）。")
            return
        if ready is None:
            self._notice("RFID: 読み取り装置を再起動しました（使えるリーダーの一覧は届いていません）。", logging.INFO)
            return
        unusable = self._unusable_from(ready)
        self._set_unusable(unusable, announce=False)
        if not unusable:
            self._notice(f"RFID: 読み取り装置を再起動しました — {len(self._reader_configs)} 台とも使えます。")
            return
        again = self._auto_restart and self._auto_attempts < AUTO_RESTART_MAX
        self._notice("⚠ RFID: 読み取り装置を再起動しましたが、まだ使えないリーダーがあります: "
                     + "・".join(self._reader_desc(r) for r in unusable) + "。"
                     + ("卓に札が無くなったらもう一度試します。" if again
                        else "配線・電源を確かめてください（rr でもう一度試せます）。"))

    def _reconnect_if_needed(self, now: float) -> None:
        """再起動のあとにつながらなかったリーダーを、つながるまで試し直す（`reconnect_sec` おき）。"""
        if self._bridges or self._reconnect_sec is None or now < self._next_reconnect:
            return
        self._next_reconnect = now + self._reconnect_sec
        if not any(self._reader_present(name) for name in self._reader_names()):
            return
        bridges = self._connect_readers()
        if bridges:
            self._bridges = bridges
            self.health = {**self.health, "state": "running", "connected": len(bridges)}
            self._next_ready_check = now
            self._notice(f"RFID: リーダーにつながりました（{len(bridges)} 台）。", logging.INFO)

    def _learn_role(self, cfg: dict, reader_id: str) -> None:
        if cfg.get("role") == "board":
            self._board_reader_ids.add(reader_id)
        elif isinstance(cfg.get("seat"), int):
            self._seat_reader_ids.setdefault(cfg["seat"], set()).add(reader_id)
            self._track_seat_presence(cfg["seat"])
            self._note_seat_cards(cfg["seat"])

    def _note_seat_cards(self, seat: int) -> None:
        """席の札ごとに最初に読んだ時刻を覚える（配った順, `presence_snapshot` の `since`）。"""
        now = self._clock()
        present: set[str] = set()
        for rid in self._seat_reader_ids.get(seat, ()):
            present |= self._last_uids.get(rid, set())
        for key, entry in list(self._seat_card_seen.items()):
            if key[0] != seat or key[1] in present:
                continue
            if entry[1] is None:
                entry[1] = now                                   # 離れた（持ち上げた・回収した）
            elif now - entry[1] > _CARD_SINCE_FORGET_SEC:
                del self._seat_card_seen[key]
        for uid in present:
            entry = self._seat_card_seen.get((seat, uid))
            if entry is None or (entry[1] is not None and now - entry[1] > _CARD_SINCE_FORGET_SEC):
                self._seat_card_seen[(seat, uid)] = [now, None]
            else:
                entry[1] = None                                  # 載り続けている / すぐ戻った

    def _make_event(
        self, uid: str, reader_id: str, role: str, seat: Optional[int], timestamp: float,
        board_index: Optional[int] = None, replaces: Optional[str] = None,
    ) -> RFIDEvent:
        return RFIDEvent(
            tag_id=uid,
            card=self._card_master.lookup(uid),
            reader_id=reader_id,
            role=role,
            seat=seat,
            timestamp=timestamp,
            raw_tag_id=uid,
            # board_index は board street 自動遷移に必須（engine が board_index!=None を分岐条件にする）。
            # **board reader 全台で共有する論理ボードの「何枚目か」**（契約 v1.3 §4）。
            board_index=board_index,
            replaces=replaces,
        )

    def _note_muck(self, seat: int, uid: str, now: float) -> None:
        """席に記録した札が board reader の上に現れた = 手札が卓の中央を通過した（マック）。"""
        if seat not in self._mucked_at:
            self._mucked_at[seat] = now
            logger.info(
                "席 %d の手札が卓の中央を通過しました（マック, tag=%s）— ボードの札にはしません",
                seat, uid,
            )

    # ――― 従来の解釈（最初に見えた瞬間に確定, ADR-0053/0054）―――

    def _poll_immediate(
        self, cfg: dict, reader_id: str, uids: list[str], current: set[str],
    ) -> list[RFIDEvent]:
        """**新しく増えた UID ごとに** 1 event（リーダー単位のデバウンス）。"""
        prev = self._last_uids.get(reader_id, set())
        # デバウンス: 集合が前回と同じなら何もしない
        if current == prev:
            return []
        self._last_uids[reader_id] = current

        removed = prev - current
        if removed:
            # カードが外れた（イベント不要）。**board の位置は解放しない**（ハンド内 append-only,
            # ISSUE-0026）。この解釈では、載せ替えは明示の訂正コマンドで解放する
            # （`forget_board_position` / `forget_seat_cards`, ADR-0054）。
            logger.debug("Card(s) removed from %s: %s", reader_id, sorted(removed))

        self._learn_role(cfg, reader_id)
        now = self._clock()
        role = cfg.get("role", "seat")
        events: list[RFIDEvent] = []
        seen: set[str] = set()
        for uid in uids:   # 読み取り順を保ったまま、増えた UID ごとに 1 event
            if uid in prev or uid in seen:
                continue
            seen.add(uid)
            if role == "board":
                owner = self._seat_owner.get(uid)
                if owner is not None:            # 手札は盤の札にならない（ADR-0058）
                    self._note_muck(owner, uid, now)
                    continue
                events.append(self._make_event(
                    uid, reader_id, "board", None, now, board_index=self._assign_board_index(uid),
                ))
            else:
                seat = cfg.get("seat")
                if isinstance(seat, int):
                    self._seat_owner.setdefault(uid, seat)
                events.append(self._make_event(uid, reader_id, role, seat, now))
        return events

    # ――― 卓の流れに合わせた解釈（ADR-0058）―――

    def _poll_tracked(
        self, cfg: dict, reader_id: str, uids: list[str], current: set[str],
    ) -> list[RFIDEvent]:
        """場所（ボード / 席）ごとに「載り続けている期間」を追い、確定した札だけ event にする。"""
        now = self._clock()
        self._last_uids[reader_id] = current
        self._learn_role(cfg, reader_id)
        if cfg.get("role") == "board":
            loc, readers, seat = "board", self._board_reader_ids, None
        else:
            seat = cfg.get("seat")
            if not isinstance(seat, int):
                return []
            loc, readers = f"seat:{seat}", self._seat_reader_ids.get(seat, set())

        union: set[str] = set()
        for rid in readers:
            union |= self._last_uids.get(rid, set())
        prev_present = self._loc_present.get(loc, set())
        # この reader が読んだ順を先に（同じ poll で増えた札の位置順を読み取り順にする）。
        for uid in list(dict.fromkeys(uids)) + sorted(union - current):
            self._sight(loc, uid, now, prev_present, reader_id, seen_here=uid in current)
        self._loc_present[loc] = union

        events: list[RFIDEvent] = []
        for (run_loc, uid), run in list(self._runs.items()):
            if run_loc != loc or run.fired or uid not in union:
                continue
            if loc == "board":
                events.extend(self._decide_board(uid, run, now))
            else:
                event = self._decide_seat(seat, uid, run, now)
                if event is not None:
                    events.append(event)
        return events

    def _sight(
        self, loc: str, uid: str, now: float, prev_present: set[str], reader_id: str,
        seen_here: bool,
    ) -> None:
        key = (loc, uid)
        run = self._runs.get(key)
        pending = loc == "board" and uid not in self._board_indexes and uid not in self._seat_owner
        tolerance = self._pending_gap_sec if pending else self._gap_sec
        if run is not None and (uid in prev_present or now - run.last_seen <= tolerance):
            run.last_seen = now
        else:
            # 新しく載った（または途切れが許容を超えた）→ 新しい期間。挿入順 = 最初に見えた順を保つ。
            gap = None if run is None else now - run.last_seen
            self._runs.pop(key, None)
            run = _Run(first_seen=now, last_seen=now, reader_id=reader_id)
            self._runs[key] = run
            if loc == "board":
                self._board_runs[uid] = self._board_runs.get(uid, 0) + 1
                owner = self._seat_owner.get(uid)
                if owner is not None:
                    self._note_muck(owner, uid, now)
                elif pending:
                    self._log_board_sighting(uid, reader_id, gap)
        if seen_here:
            run.readers.add(reader_id)

    def _log_board_sighting(self, uid: str, reader_id: str, gap: Optional[float]) -> None:
        """確定前のボードの札が見え始めた時刻を残す（置いてから読めるまでの遅れを切り分けるため）。"""
        where = self._reader_label({reader_id})
        if gap is None:
            logger.info("ボードに札 %s が載りました（%s）", self._card_name(uid), where)
            return
        n = self._board_rereads[uid] = self._board_rereads.get(uid, 0) + 1
        if n <= 3:  # 読めにくい位置の札は何度も途切れるので、最初の数回だけ
            logger.info(
                "ボードの札 %s を読み直しました（%.1f 秒読めなかった, %s）— 載り続けた時間を数え直します",
                self._card_name(uid), gap, where,
            )

    def _absent_for(self, loc: str, uid: str, now: float) -> float:
        """場所 `loc` で `uid` が見えていない秒数（見えていれば 0、記録が無ければ無限大）。"""
        if uid in self._loc_present.get(loc, set()):
            return 0.0
        run = self._runs.get((loc, uid))
        return float("inf") if run is None else now - run.last_seen

    def _run_event(
        self, uid: str, run: _Run, role: str, seat: Optional[int],
        board_index: Optional[int] = None, replaces: Optional[str] = None,
    ) -> RFIDEvent:
        # 時刻は**最初に見えた時刻**（確定を待った分だけ遅らせない, ADR-0055 の配布時刻）。
        return self._make_event(
            uid, run.reader_id, role, seat, run.first_seen,
            board_index=board_index, replaces=replaces,
        )

    def _decide_board(self, uid: str, run: _Run, now: float) -> list[RFIDEvent]:
        owner = self._seat_owner.get(uid)
        if owner is not None:
            run.fired = True        # 手札は盤の札にならない（マックは _sight で記録済み）
            return []
        existing = self._board_indexes.get(uid)
        if existing is not None:    # 既に位置を持つ札が戻ってきた → 同じ位置で再発火（従来どおり）
            run.fired = True
            return [self._run_event(uid, run, "board", None, board_index=existing)]
        if now - run.first_seen < self._commit_sec:
            return []               # まだ確定しない（中央を通過する札かもしれない）
        if self._board_swept:
            run.fired = True        # 片付けとみたあと: このハンドのボードは変えない
            return []
        if uid not in self._board_retired:
            swept = self._check_board_sweep(uid, now)
            if swept is not None:
                run.fired = True
                return swept

        slot_uid = {i: u for u, i in self._board_indexes.items()}
        restored = self._restore_board_card(uid, run, slot_uid, now)
        if restored:
            run.fired = True
            return restored
        index, waiting = self._flop_redeal_slot(uid, run, slot_uid, now)
        if index is None and not waiting:
            candidates = self._redeal_candidates(run, slot_uid)
            ready = [i for i in candidates
                     if self._absent_for("board", slot_uid[i], now) >= self._redeal_confirm_sec]
            if ready:
                index = ready[0]
            elif candidates:
                # 前の札が消えた直後に、その近くへ置かれた。差し直しか（前の札が戻らない）、
                # 読み落ちだったか（前の札が戻る）が分かるまで位置を決めない。
                waiting = candidates
                if not run.noted:
                    logger.info(
                        "ボードの新しい札 %s（%s）: %s が消えた直後です — 差し直しか確かめています"
                        "（前の札が %.0f 秒見えなければ差し替え）",
                        self._card_name(uid), self._reader_label(run.readers),
                        ", ".join(self._slot_label(i, slot_uid[i], now) for i in waiting),
                        self._redeal_confirm_sec,
                    )
                    run.noted = True
        run.waiting = bool(waiting)
        if waiting:
            return []

        if index is not None:
            run.fired = True
            return [self._replace_board_card(uid, run, index, slot_uid[index])]
        free = [i for i in range(1, _BOARD_MAX_CARDS + 1) if i not in slot_uid]
        late = self._not_a_flop_card(uid, run, slot_uid)
        if late:
            free = [i for i in free if i not in _FLOP_SLOTS]
        if free:
            run.fired = True
            self._place_board_card(uid, free[0], run.first_seen)
            if late:
                logger.info(
                    "ボードの札 %s はフロップと同時に出ていません（%s）— フロップの読めていない位置には入れず、"
                    "%d 枚目にします", self._card_name(uid), late, free[0],
                )
            self._log_board_commit(uid, run, free[0], slot_uid, now)
            return [self._run_event(uid, run, "board", None, board_index=free[0])]
        events = self._settle_full_board(uid, run, slot_uid, now)
        run.waiting = events is None
        if events is None:
            return []
        run.fired = True
        return events

    def _not_a_flop_card(self, uid: str, run: _Run, slot_uid: dict[int, str]) -> str:
        """フロップに空き位置があっても、この札をフロップに入れない理由（入れてよければ ""）。

        フロップは 3 枚が同時に出る（オーナー, 2026-09-29: フロップの 1 枚が読めていないとき、あとから出た
        ターンの札をフロップに充当してはいけない）。リーダーの位置で分かれば位置で（一番右のリーダーだけで
        読んだ札はターン・リバー、一番左のリーダーで読んだ札はフロップ）、分からなければ時刻で決める
        （フロップの最初の札から `flop_window_sec` より後に見え始めた札はフロップではない）。
        """
        if self._flop_window_sec is None:
            return ""
        flop = [u for i, u in slot_uid.items() if i in _FLOP_SLOTS]
        if not flop or len(flop) >= len(_FLOP_SLOTS):
            return ""                       # この札がフロップの最初 / フロップに空きが無い
        sides = {self._board_side(r) for r in run.readers}
        if sides == {"right"}:
            return f"{self._reader_label(run.readers)} = ターン・リバーの位置"
        if "left" in sides:
            return ""                       # フロップの位置で読んだ（遅れて読めたフロップの札）
        started = min(self._board_first_seen.get(u, run.first_seen) for u in flop)
        gap = run.first_seen - started
        if gap > self._flop_window_sec:
            return f"フロップの最初の札から {gap:.1f} 秒あと"
        return ""

    def _board_side(self, reader_id: str) -> str:
        """board reader がボードのどちら側を読むか: "left"（フロップだけ）/ "right"（ターン・リバーだけ）/ ""。"""
        k, n = self._board_order.get(reader_id), len(self._board_order)
        if k is None or n < 2:
            return ""
        if (k + 1) / n <= _FLOP_EDGE:
            return "left"
        if k / n >= _FLOP_EDGE:
            return "right"
        return ""

    def _settle_full_board(
        self, uid: str, run: _Run, slot_uid: dict[int, str], now: float,
    ) -> Optional[list[RFIDEvent]]:
        """5 枚埋まったボードに新しい札が載り続けた。None = まだ決めない（次の poll で見直す）。

        ボードに 6 枚目の札は無いので、1 枚が取り除かれていれば新しい札はどれかの差し直し。見えていない
        札がちょうど 1 枚で、`release_sec` 以上見えなければ差し替える（`_replace_on_full_board`）。まだ
        `release_sec` に達していない札があれば待つ（取ってすぐ置いた / 読み落ち。戻れば 6 枚目 = WARN）。
        見えていない札が無い・2 枚以上消えてどれか決められないときは WARN で位置なし（従来どおり）。
        """
        if self._release_sec is not None:
            absent = [i for i, u in sorted(slot_uid.items())
                      if self._absent_for("board", u, now) > self._gap_sec]
            gone = [i for i in absent if self._is_gone(slot_uid[i], now)]
            if len(absent) == 1 and gone:
                return self._replace_on_full_board(uid, run, slot_uid, gone[0])
            if len(gone) < len(absent):
                if not run.noted:
                    logger.info(
                        "ボードの新しい札 %s（%s）: ボードは %d 枚埋まっています — %s が取り除かれたか"
                        "確かめています（%.0f 秒見えなければ差し替え）",
                        self._card_name(uid), self._reader_label(run.readers), _BOARD_MAX_CARDS,
                        ", ".join(self._slot_label(i, slot_uid[i], now) for i in absent),
                        self._release_sec,
                    )
                    run.noted = True
                return None
        logger.warning(
            "ボードが %d 枚を超えました（tag=%s）— board_index なしで記録します。"
            "ハンドを始め直すなら新ハンド（n）でリセットされます",
            _BOARD_MAX_CARDS, uid,
        )
        return [self._run_event(uid, run, "board", None)]

    def _check_board_sweep(self, uid: str, now: float) -> Optional[list[RFIDEvent]]:
        """新しい札の確定を数え、片付けとみたら（`_SWEEP_*`）ボードを止めて直前の差し替えを戻す。

        片付けとみなければ None（この札は通常どおり決める）。片付けなら、窓の中で差し替えた位置に前の札を
        戻す event（`replaces` = 片付けの札）を返し、このハンドのボードはそれ以上変えない（次のハンドで戻る）。
        """
        if len(self._board_indexes) < _SWEEP_MIN_BOARD:
            return None                  # turn まで配っていない（オールインのランアウトは flop から続けて配る）
        self._board_new_commits.setdefault(uid, now)
        recent = [u for u, t in self._board_new_commits.items() if now - t <= _SWEEP_WINDOW_SEC]
        # 早すぎた turn と river を両方配り直すと、river + 差し直し 2 枚 = 3 枚になる（ADR-0058 追記 4）。
        # 3 枚なら、turn のあとで flop の位置まで差し替えていたときだけ片付けとみる。4 枚以上は片付け。
        flop_replaced = any(now - r[0] <= _SWEEP_WINDOW_SEC and r[1] <= 3 for r in self._board_replacements)
        if len(recent) < _SWEEP_MIN_NEW_CARDS or (len(recent) == _SWEEP_MIN_NEW_CARDS and not flop_replaced):
            return None
        self._board_swept = True
        undo = [r for r in self._board_replacements if now - r[0] <= _SWEEP_WINDOW_SEC]
        self._board_replacements = [r for r in self._board_replacements if r not in undo]
        logger.warning(
            "ボードに %.0f 秒の間に新しい札が %d 枚載りました（%s）— 札をまとめて動かした（片付け）とみて、"
            "このハンドのボードはこれ以上変えません%s",
            _SWEEP_WINDOW_SEC, len(recent), " ".join(self._card_name(u) for u in recent),
            f"（直前の差し替え {len(undo)} 件を戻します）" if undo else "",
        )
        events: list[RFIDEvent] = []
        for _, index, old, old_dealt, new, reader_id in reversed(undo):
            if self._board_indexes.get(new) != index:
                continue                 # もう別の位置へ動いている
            self._forget_board_uid(new)
            self._board_retired.pop(old, None)
            self._place_board_card(old, index, old_dealt)
            events.append(self._make_event(
                old, reader_id, "board", None, old_dealt,
                board_index=index, replaces=self._card_master.lookup(new) or None,
            ))
        return events

    def _is_gone(self, uid: str, now: float) -> bool:
        """ボードの札が `release_sec` 以上見えない（取り除かれたとみなしてよい）。"""
        return (self._release_sec is not None
                and self._absent_for("board", uid, now) >= self._release_sec)

    def _board_readers_of(self, uid: str) -> set[str]:
        run = self._runs.get(("board", uid))
        return run.readers if run is not None else set()

    def _flop_redeal_slot(
        self, uid: str, run: _Run, slot_uid: dict[int, str], now: float,
    ) -> tuple[Optional[int], bool]:
        """flop が丸ごと配り直されたとき、新しい札が差し替える位置と「まだ確かめ中か」。

        flop の札が**全部**見えず、新しい札が flop の札を読んでいたリーダーの上に載ったら、全部が
        `redeal_confirm_sec` 以上見えないのを確かめてから、**時間によらず** flop の若い位置から
        差し替える（早すぎた flop を戻し、ベッティングの後で配り直した）。残りの消えた flop の札は、
        続けて確定した新しい札が順に差し替える（新しい flop が前と違う並べ方でも取りこぼさない）。
        """
        if self._release_sec is None:
            return None, False
        present = self._loc_present.get("board", set())
        self._board_pending = {
            u for u in self._board_pending if u in self._board_indexes and u not in present
        }
        if self._board_pending:
            return min(self._board_indexes[u] for u in self._board_pending), False
        flop = [i for i in _FLOP_SLOTS if i in slot_uid]
        if not flop or any(slot_uid[i] in present for i in flop):
            return None, False
        if not any(self._board_readers_of(slot_uid[i]) & run.readers for i in flop):
            return None, False
        if any(self._absent_for("board", slot_uid[i], now) < self._redeal_confirm_sec for i in flop):
            if not run.noted:
                logger.info(
                    "ボードの新しい札 %s（%s）: flop の札が全部消えた直後です — 配り直しか確かめています",
                    self._card_name(uid), self._reader_label(run.readers),
                )
                run.noted = True
            return None, True
        self._board_pending = {slot_uid[i] for i in flop[1:]}
        return flop[0], False

    def _redeal_candidates(self, run: _Run, slot_uid: dict[int, str]) -> list[int]:
        """1 枚だけの差し直しで、新しい札が差し替えるかもしれない位置（若い順）。

        前の札が**いま見えない**ことに加えて:

        - **最後に配った turn / river**（いちばん後ろの 4・5 枚目）: 新しい札が**同じか隣の**
          リーダーで読まれた（位置がリーダーの境目にあると、置き直した札を隣の台が読む）。時間は
          問わない（早すぎた turn を外し、ベッティングの後で配り直すこともある）。
        - **それ以外**（flop の札・後ろに札がある札）: 前の札を読んでいたリーダーで新しい札が読まれ、
          前の札が消えてから `redeal_window_sec` 以内に新しい札が現れた。ディーラーは取り除いた札の
          場所に新しい札を置くので、同じリーダーが「同じ場所」の根拠になる。別のリーダーの札が読み
          落ちている間に次のストリートの札が来ても、ここには入らない（次の位置のまま）。

        **このハンドで一度でも読めなくなって戻った札**（確定前の途切れも含む）は対象にしない。
        読みにくい位置にある札は、載ったまま長く読めなくなる。そこへ次のストリートの札が近くに
        置かれると差し直しと取り違える（店舗の実卓: turn の Ts が読めない間に置いた river の Js が
        Ts を差し替え、戻った Ts が 5 枚目になって turn と river が逆になった, ADR-0058 追記 4）。
        そうした札を本当に差し直したときは、新しい札は次の位置に入り、6 枚目が来た時点で詰め直す
        （`_replace_on_full_board`）。
        """
        if self._release_sec is None or self._redeal_window_sec is None:
            return []
        present = self._loc_present.get("board", set())
        latest = max(slot_uid) if slot_uid else 0
        candidates: list[int] = []
        for index, old in sorted(slot_uid.items()):
            old_run = self._runs.get(("board", old))
            if old in present or old_run is None or self._board_runs.get(old, 1) > 1:
                continue
            if index == latest and index > _FLOP_SLOTS[-1]:
                if not self._near(old_run.readers, run.readers):
                    continue
            elif (old_run.last_seen < run.first_seen - self._redeal_window_sec
                  or not old_run.readers & run.readers):
                continue            # ずっと前から読めていない札 / 別の場所の札
            candidates.append(index)
        return candidates

    def _restore_board_card(
        self, uid: str, run: _Run, slot_uid: dict[int, str], now: float,
    ) -> list[RFIDEvent]:
        """差し替えた札が戻ってきたら、差し替えを取り消す（載ったまま読めていなかっただけ）。

        1 枚の差し直しは「前の札が見えない」ことしか根拠にできないので、読みにくい位置で初めて
        読めなくなった札は、次に配った札に差し替えられてしまう。戻ってきた札と、それを差し替えた札が
        **両方とも卓に載っている**なら、前の札は取り除かれていなかった（差し直しなら取り除く）。

        差し替えた札が**最後に配った札**で、戻ってきた札が元の場所（同じか隣のリーダー）で読め、
        後ろに空き位置があるときだけ取り消す: 戻ってきた札を元の位置（配った時刻も元のまま）に、
        差し替えた札を次の空き位置に置く。差し替えた札の後に別の札を配っていたら、戻ってきた札は
        並べ直さず新しい札として扱う（回収した札をボードの上に置いた、などと区別できないため）。
        差し替えた札が見えていないときは取り消さない（差し直しをさらに戻した = 通常の差し直し）。
        """
        retired = self._board_retired.get(uid)
        if retired is None:
            return []
        index, dealt_at, replacer, readers = retired
        replacer_dealt = self._board_first_seen.get(replacer)
        if (self._board_indexes.get(replacer) != index or replacer_dealt is None
                or self._absent_for("board", replacer, now) > self._gap_sec
                or not self._near(readers, run.readers)
                or any(self._board_first_seen.get(u, 0.0) > replacer_dealt
                       for u in slot_uid.values())):
            return []
        free = [i for i in range(1, _BOARD_MAX_CARDS + 1) if i not in slot_uid and i > index]
        if not free:
            return []
        self._place_board_card(uid, index, dealt_at)
        self._board_indexes[replacer] = free[0]
        self._board_swapped_in.discard(replacer)   # 差し直しではなく、次に配った札だった
        replacer_run = self._runs.get(("board", replacer))
        logger.info(
            "差し替えを取り消しました: %s が %d 枚目に戻りました（載ったまま読めていなかった, %s）"
            "— %s を %d 枚目にします",
            self._card_name(uid), index, self._reader_label(run.readers),
            self._card_name(replacer), free[0],
        )
        # 同じ札が一瞬 2 か所に並ばないよう、戻ってきた札で元の位置を上書きしてから送る。
        return [
            self._make_event(uid, run.reader_id, "board", None, dealt_at, board_index=index,
                             replaces=self._card_master.lookup(replacer) or None),
            self._make_event(
                replacer, replacer_run.reader_id if replacer_run is not None else run.reader_id,
                "board", None, replacer_dealt, board_index=free[0],
            ),
        ]

    def _near(self, a: set[str], b: set[str]) -> bool:
        """同じ board reader か、左右に隣り合う board reader（config の記載順）で読まれた。"""
        if a & b:
            return True
        oa = [self._board_order[r] for r in a if r in self._board_order]
        ob = [self._board_order[r] for r in b if r in self._board_order]
        return any(abs(x - y) <= 1 for x in oa for y in ob)

    def _card_name(self, uid: str) -> str:
        return self._card_master.lookup(uid) or uid

    def _reader_label(self, readers: set[str]) -> str:
        """board reader を「左から N 台目」で表す（現場で config の並びと突き合わせるため）。"""
        order = sorted(self._board_order[r] + 1 for r in readers if r in self._board_order)
        if order:
            return "左から " + "・".join(str(k) for k in order) + " 台目"
        return "・".join(sorted(readers)) or "リーダー不明"

    def _slot_label(self, index: int, uid: str, now: Optional[float] = None) -> str:
        """ログ用「7c（4 枚目・左から 2 台目[・3.2 秒前から][・読み直し 1 回]）」。

        `now` を渡すと見えなくなってからの秒数も。途中で読めなくなって戻ったことのある札は
        その回数も出す（差し直しの対象にしない札 = 次の位置に入った理由をログで分かるように）。
        """
        run = self._runs.get(("board", uid))
        parts = [f"{index} 枚目", self._reader_label(run.readers if run is not None else set())]
        if now is not None and run is not None:
            parts.append(f"{now - run.last_seen:.1f} 秒前から")
        rereads = self._board_runs.get(uid, 1) - 1
        if rereads > 0:
            parts.append(f"読み直し {rereads} 回")
        return f"{self._card_name(uid)}（{'・'.join(parts)}）"

    def _log_board_commit(
        self, uid: str, run: _Run, index: int, slot_uid: dict[int, str], now: float,
    ) -> None:
        """ボードの札を空き位置に入れたことを、読んだリーダーと「いま見えていない札」付きで残す。

        差し直しのつもりが次の位置に入ったとき、原因（前の札がまだ読めていた / 別の場所に置いた /
        時間が空いた）をログだけで切り分けるため。
        """
        present = self._loc_present.get("board", set())
        missing = [self._slot_label(i, u, now) for i, u in sorted(slot_uid.items())
                   if u not in present]
        logger.info(
            "ボードの札 %s を %d 枚目にしました（%s）%s",
            self._card_name(uid), index, self._reader_label(run.readers),
            f" — 見えていない札: {', '.join(missing)}" if missing else " — 見えていない札なし",
        )

    def _place_board_card(
        self, uid: str, index: int, dealt_at: float, swapped_in: bool = False,
    ) -> None:
        self._board_indexes[uid] = index
        self._board_first_seen[uid] = dealt_at
        self._board_retired.pop(uid, None)   # 位置を得た札は、もう「差し替えられた札」ではない
        if swapped_in:
            self._board_swapped_in.add(uid)
        else:
            self._board_swapped_in.discard(uid)

    def _replace_board_card(self, uid: str, run: _Run, index: int, old: str) -> RFIDEvent:
        old_dealt = self._board_first_seen.get(old, run.first_seen)
        old_readers = set(self._board_readers_of(old))
        self._forget_board_uid(old)
        # 前の札が載ったまま読めていなかっただけなら、戻ってきた時点で取り消す（_restore_board_card）。
        self._board_retired[old] = (index, old_dealt, uid, old_readers)
        self._place_board_card(uid, index, run.first_seen, swapped_in=True)
        self._board_replacements.append((self._clock(), index, old, old_dealt, uid, run.reader_id))
        replaces = self._card_master.lookup(old) or None
        logger.info(
            "配り直しを検出: ボード %d 枚目を差し替えました（%s → %s, %s）",
            index, replaces or old, self._card_name(uid), self._reader_label(run.readers),
        )
        return self._run_event(uid, run, "board", None, board_index=index, replaces=replaces)

    def _replace_on_full_board(
        self, uid: str, run: _Run, slot_uid: dict[int, str], index: int,
    ) -> list[RFIDEvent]:
        """5 枚埋まったボードで `index` の札が取り除かれ、新しい札が載り続けた。

        取り除いた札の代わりは次のどちらか:

        - 取り除いた札が消えたあとに**空き位置に入れた札**がある: それが取り除いた札の差し直しだった
          （その時点では読み落ちと区別できず次の位置に入れた = 読み落ちたことのある札・時間窓の外の
          差し直しなど）。その札を取り除いた札の位置へ移し、後ろの札を 1 つずつ前へ詰め、新しい札を
          5 枚目にする（早すぎた turn を外して配り直した → river が来た時点で直る）。
        - なければ、新しい札が取り除いた札の差し直し: **その位置に入れる**（river まで配ったあとで turn を
          差し直した。後ろへ入れると turn と river が逆になる）。

        engine はボードの位置を上書きするだけなので、**後ろの位置から**送る（前から送ると、動かした札が
        一瞬 2 か所に並び、重複の WARN が出る）。時刻はそれぞれの札を配った時刻。
        """
        dropped = slot_uid[index]
        first_seen_before = dict(self._board_first_seen)   # 差し替えの記録用（片付けなら戻す）
        dropped_run = self._runs.get(("board", dropped))
        dropped_at = dropped_run.last_seen if dropped_run is not None else float("-inf")
        superseded = [i for i in sorted(slot_uid)
                      if i > index and slot_uid[i] not in self._board_swapped_in
                      and self._board_first_seen.get(slot_uid[i], float("-inf")) > dropped_at]
        new = dict(slot_uid)
        if superseded:
            moved_from = superseded[0]
            new[index] = slot_uid[moved_from]
            tail = [slot_uid[i] for i in range(moved_from + 1, _BOARD_MAX_CARDS + 1)] + [uid]
            for offset, card in enumerate(tail):
                new[moved_from + offset] = card
            new_index = _BOARD_MAX_CARDS
        else:
            new[index] = uid
            new_index = index
        self._forget_board_uid(dropped)
        for i, card in new.items():
            self._board_indexes[card] = i
        self._place_board_card(uid, new_index, run.first_seen, swapped_in=not superseded)
        if superseded:
            self._board_swapped_in.add(new[index])
            logger.info(
                "配り直しを検出: ボード %d 枚目の %s は、消えたあとに置いた %s（%d 枚目）で差し直されて"
                "いました — %s を %d 枚目に移し、新しい札 %s を %d 枚目にします",
                index, self._card_name(dropped), self._card_name(new[index]), superseded[0],
                self._card_name(new[index]), index, self._card_name(uid), new_index,
            )
        else:
            logger.info(
                "配り直しを検出: ボード %d 枚目の %s が消えたまま新しい札 %s が置かれました — "
                "%d 枚目を差し替えます（%s）",
                index, self._card_name(dropped), self._card_name(uid), index,
                self._reader_label(run.readers),
            )
        events: list[RFIDEvent] = []
        for i in sorted(new, reverse=True):
            card, old = new[i], slot_uid[i]
            if card == old:
                continue
            self._board_replacements.append((
                self._clock(), i, old, first_seen_before.get(old, run.first_seen), card, run.reader_id,
            ))
            card_run = run if card == uid else self._runs.get(("board", card))
            events.append(self._make_event(
                card, card_run.reader_id if card_run is not None else run.reader_id, "board", None,
                self._board_first_seen.get(card, run.first_seen),
                board_index=i, replaces=self._card_master.lookup(old) or None,
            ))
        return events

    def _forget_board_uid(self, uid: str) -> None:
        self._board_indexes.pop(uid, None)
        self._board_first_seen.pop(uid, None)
        self._board_pending.discard(uid)
        self._board_swapped_in.discard(uid)

    def _decide_seat(self, seat: int, uid: str, run: _Run, now: float) -> Optional[RFIDEvent]:
        loc = f"seat:{seat}"
        card = self._card_master.lookup(uid) or uid
        if uid in self._board_indexes:
            if not run.noted:
                logger.info("ボードの札 %s が席 %d の上にあります — 手札としては記録しません", card, seat)
                run.noted = True
            run.fired = True
            return None
        owner = self._seat_owner.get(uid)
        committed = self._seat_committed.setdefault(seat, [])
        if owner == seat:           # 戻ってきた自分の札 → 再発火（従来どおり。engine は重複を無視）
            run.fired = True
            return self._run_event(uid, run, "seat", seat)

        moved_from: Optional[int] = None
        if owner is not None:
            # 同じハンドで別の席に記録した札。元の席から消えて、ここに載り続けたときだけ移す。
            ready = (self._release_sec is not None
                     and self._absent_for(f"seat:{owner}", uid, now) >= self._release_sec
                     and now - run.first_seen >= self._commit_sec)
            if not ready:
                if not run.noted:
                    logger.info(
                        "席 %d の札 %s が席 %d にあります — 元の席から消えて載り続けるまで記録しません",
                        owner, card, seat,
                    )
                    run.noted = True
                return None
            moved_from = owner

        if moved_from is None and len(committed) < _SEAT_MAX_CARDS:
            # 通常の配布: 最初に見えた瞬間に記録する（持ち上げて見られる前に拾う）。
            committed.append(uid)
            self._seat_owner[uid] = seat
            run.fired = True
            return self._run_event(uid, run, "seat", seat)

        # 差し替え / 移動は、新しい札が載り続けていることを確かめてから。
        if now - run.first_seen < self._commit_sec:
            return None
        victim: Optional[str] = None
        if len(committed) >= _SEAT_MAX_CARDS:
            gone = [] if self._release_sec is None else [
                (self._absent_for(loc, u, now), u) for u in committed
                if self._absent_for(loc, u, now) >= self._release_sec
            ]
            if not gone:
                if not run.noted:
                    logger.warning(
                        "席 %d に 3 枚目の札 %s があります — 前の札が消えるまで記録しません"
                        "（すぐ直すならハンドロガーで cs %d）", seat, card, seat,
                    )
                    run.noted = True
                return None
            victim = max(gone)[1]   # 一番長く消えている札を差し替える
            committed.remove(victim)
            self._seat_owner.pop(victim, None)
            logger.info(
                "配り直しを検出: 席 %d の %s を %s に差し替えました",
                seat, self._card_master.lookup(victim) or victim, card,
            )
        if moved_from is not None:
            prior = self._seat_committed.get(moved_from, [])
            if uid in prior:
                prior.remove(uid)
            logger.info("配り直しを検出: %s を席 %d から席 %d に移しました", card, moved_from, seat)
        committed.append(uid)
        self._seat_owner[uid] = seat
        run.fired = True
        replaces = (self._card_master.lookup(victim) or None) if victim else None
        return self._run_event(uid, run, "seat", seat, replaces=replaces)

    # ――― board の位置割り当て（契約 v1.3 §4: board reader 全台で 1 つの論理ボード） ―――

    def reset_board_positions(self) -> None:
        """ボード位置の割り当てを捨てて次のハンドに備える（新ハンドの同期点, ISSUE-0026）。

        `IntegrationThread` の `on_new_hand` フックから呼ばれる。board reader のデバウンス状態も
        落とすので、**ハンド開始時に盤上に残っているカードは改めて 1 番から検出し直す**
        （engine 側も `_board_positions` を空にするため、両者が必ず同じ状態から始まる）。

        poll スレッドとは別スレッド（IntegrationThread）から呼ばれる。状態の更新の間だけ
        ロックを取る（リーダーとの通信中は取らないので poll を止めない）。
        """
        with self._lock:
            self._board_indexes = {}
            for reader_id in self._board_reader_ids:
                self._last_uids[reader_id] = set()
            # 卓の流れに合わせた解釈（ADR-0058）: ボード上の「載り続けている期間」も捨てる。
            self._runs = {k: r for k, r in self._runs.items() if k[0] != "board"}
            self._loc_present.pop("board", None)
            self._board_first_seen = {}
            self._board_pending = set()
            self._board_rereads = {}
            self._board_runs = {}
            self._board_retired = {}
            self._board_swapped_in = set()
            self._board_new_commits = {}
            self._board_replacements = []
            self._board_swept = False
        logger.info("新ハンド: board の位置割り当てをリセットしました")

    # ――― マック観測（ADR-0055: 合成 fold に実時刻を与えるためだけに使う） ―――

    def _track_seat_presence(self, seat: int) -> None:
        """席のカードが「全部消えた」時刻を覚え、戻ってきたら忘れる。

        **fold の判定には使わない**。プレイヤーはカードを持ち上げて見る・手に持つので、
        不在それ自体は fold を意味しない。使い道は engine が合成する silent-fold に
        「実際に札が席から離れた時刻」を与えること（音声の時系列と突き合わせて再生するため）。
        """
        present = any(self._last_uids.get(rid) for rid in self._seat_reader_ids.get(seat, ()))
        if present:
            self._seat_seen.add(seat)
            self._seat_absent_since.pop(seat, None)
        elif seat in self._seat_seen and seat not in self._seat_absent_since:
            # 「消えた最初の瞬間」を採る（確認は engine 側。後から上書きしない）。このハンドで一度も札が
            # 載っていない席には付けない（卓モニタが配られていない席を「離席」と出していた, 2026-09-27）。
            self._seat_absent_since[seat] = self._clock()

    def seat_cards_absent_since(self, seat: int) -> Optional[float]:
        """その席のカードが消えたまま戻っていない場合、消えた時刻（epoch）。無ければ None。"""
        return self._seat_absent_since.get(seat)

    def presence_snapshot(self) -> dict[int, dict]:
        """席ごとの現在のカード在否（卓状態の表示用, `core/table_state.py`）。

        `{seat: {"present": bool, "absent_since": float | None, "uid_count": int,
        "mucked_at": float | None, "cards": [str, ...]}}`。
        **「載っている」であって「ゲームに残っている」ではない**（ADR-0056 D4）。
        まだ一度も検出していない席は現れない（= 未配布と区別できる）。
        `mucked_at` はその席の手札が卓の中央（board reader の上）を通過した時刻（ADR-0058）。
        `cards` はいまリーダーが読んでいる札（未登録の札は `?<UID>`）。手札の配布の検出に使う
        （ハンドの記録やデバウンスとは無関係の、読んだままの札, ADR-0063）。
        `since` は札 → その席で最初に読んだ時刻（いま載っている札と、少し前まで載っていた札）。配った順から
        ボタンの置き忘れを見つけるのに使う（2026-09-29）。
        """
        snapshot: dict[int, dict] = {}
        now = self._clock()
        with self._lock:
            for seat, reader_ids in self._seat_reader_ids.items():
                uids: set[str] = set()
                for reader_id in reader_ids:
                    uids |= self._last_uids.get(reader_id, set())
                snapshot[seat] = {
                    "present": bool(uids),
                    "absent_since": self._seat_absent_since.get(seat),
                    "uid_count": len(uids),
                    "mucked_at": self._mucked_at.get(seat),
                    "cards": sorted(self._card_master.lookup(u) or f"?{u}" for u in uids),
                    "since": {
                        self._card_master.lookup(uid) or f"?{uid}": first
                        for (s, uid), (first, gone) in self._seat_card_seen.items()
                        if s == seat and (gone is None or now - gone <= _CARD_SINCE_FORGET_SEC)
                    },
                }
        return snapshot

    def board_presence(self) -> dict[str, dict]:
        """卓モニタ用のボードの読み取り状況（ADR-0058）。記録（engine の board）は変えない = 表示だけ。

        `{"absent": {札: 見えなくなった時刻}, "pending": {札: {"since": 最初に見えた時刻,
        "swap": 差し直しを確かめ中か}}}`。

        - **absent**: 記録した札のうち、いま読めていない札（一瞬の読み落ち = `gap_sec` 以内は含めない）。
          札を外したことが伝わっているかを卓の脇から見る。
        - **pending**: 読めているがまだ数えていない札（載り続けるのを待っている / 差し直しを確かめ中）。
          置いた札が読めているか・なぜまだ出ないかを見る。
        従来の解釈（最初に見えた瞬間に確定）では両方とも空。
        - **present_count**: いま board reader が読んでいる札の枚数（記録とは無関係）。手札の配布は
          ボードが空のときだけ検出する（シャッフルでボードのリーダーに札が載る, ADR-0063）。
        """
        with self._lock:
            on_board: set[str] = set()
            for reader_id in self._board_reader_ids:
                on_board |= self._last_uids.get(reader_id, set())
        if not self._tracking:
            return {"absent": {}, "pending": {}, "present_count": len(on_board)}
        now = self._clock()
        absent: dict[str, float] = {}
        pending: dict[str, dict] = {}
        with self._lock:
            present = self._loc_present.get("board", set())
            for uid in self._board_indexes:
                run = self._runs.get(("board", uid))
                if uid in present or run is None or now - run.last_seen <= self._gap_sec:
                    continue
                card = self._card_master.lookup(uid)
                if card:
                    absent[card] = run.last_seen
            for (loc, uid), run in self._runs.items():
                if (loc != "board" or run.fired or uid in self._board_indexes
                        or uid in self._seat_owner or now - run.last_seen > self._pending_gap_sec):
                    continue
                card = self._card_master.lookup(uid)
                if card:
                    pending[card] = {"since": run.first_seen, "swap": run.waiting}
        return {"absent": absent, "pending": pending, "present_count": len(on_board)}

    def reset_for_new_hand(self) -> None:
        """新ハンドの同期点（board 位置 + マック観測 + 席の記録をまとめて捨てる）。

        ハンド終了時はディーラーが全席のカードを回収するので、観測を持ち越すと次のハンドの
        合成 fold に前のハンドの時刻が付く。`on_new_hand` フックはこちらを呼ぶ。
        """
        with self._lock:
            self.reset_board_positions()
            self._seat_absent_since = {}
            self._seat_seen = set()
            for reader_ids in self._seat_reader_ids.values():
                for reader_id in reader_ids:
                    self._last_uids[reader_id] = set()
            self._seat_owner = {}
            self._mucked_at = {}
            self._seat_committed = {}
            self._runs = {}
            self._loc_present = {}

    # ――― ミスディール訂正（ADR-0054: 明示コマンドで 1 枚だけ載せ替える） ―――

    def forget_board_position(self, index: int) -> Optional[str]:
        """board 位置 `index` の割り当てを 1 つだけ解放する（ミスディール訂正, ADR-0054）。

        ハンド内 append-only（ISSUE-0026）は「カードが見えなくなっただけでは解放しない」規則で、
        一瞬の読み落ちを誤って載せ替えと解釈しないための安全弁。ミスディールは **ディーラーが
        宣言する明示イベント**なので、推測ではなくこのコマンドで解放する。

        解放と同時に board reader のデバウンスも落とす。これで **物理的に載っているカードは
        全部再発火**し、位置を持っているカードは同じ位置に戻り（`_board_indexes` が残っている）、
        空いた `index` は次に現れた新しいカードが取る。取り消したカードを先に物理的に外して
        おけば 1 回で収束し、外す前に打っても同じカードが同じ位置に戻るだけなので **再実行で
        やり直せる**（隠れた状態を持たない）。

        Returns:
            解放した位置に載っていた UID（無ければ None）。
        """
        with self._lock:
            uid = next((u for u, i in self._board_indexes.items() if i == index), None)
            if uid is None:
                logger.warning("board 位置 %s には割り当てがありません（訂正は無効）", index)
                return None
            self._forget_board_uid(uid)
            self._board_swept = False       # 明示の訂正のあとは読み直した札を受け付ける
            for reader_id in self._board_reader_ids:
                self._last_uids[reader_id] = set()
            # 卓の流れに合わせた解釈（ADR-0058）: 解放した札がまだ載っていれば、次の poll で
            # 空き位置の最小（= 同じ位置）に戻る。外してあれば、次に確定した札がこの位置を取る。
            run = self._runs.get(("board", uid))
            if run is not None:
                run.fired = False
        logger.info(
            "board 位置 %d を解放しました（tag=%s）— 正しいカードを置き直してください", index, uid,
        )
        return uid

    def forget_seat_cards(self, seat: int) -> None:
        """席 `seat` のリーダーのデバウンスを落とし、載っているカードを読み直させる（ADR-0054）。

        engine 側は `_hole_cards[seat]` を空にするので、**物理的に載っている 2 枚が改めて
        記録される**。ミスディールしたカードを先に外してから打つこと（外す前に打つと同じ
        カードがまた記録されるだけなので、外して再実行すればよい）。
        """
        with self._lock:
            reader_ids = self._seat_reader_ids.get(seat)
            if not reader_ids:
                logger.warning("席 %s に対応する RFID リーダーがありません（訂正は無効）", seat)
                return
            for reader_id in reader_ids:
                self._last_uids[reader_id] = set()
            self._seat_absent_since.pop(seat, None)   # 訂正後の観測をやり直す（ADR-0055）
            self._seat_seen.discard(seat)
            # この席に記録した札を解放する（載っていれば読み直しで改めて記録される, ADR-0058）。
            self._seat_owner = {u: s for u, s in self._seat_owner.items() if s != seat}
            self._seat_committed.pop(seat, None)
            self._mucked_at.pop(seat, None)
            loc = f"seat:{seat}"
            for (run_loc, _uid), run in self._runs.items():
                if run_loc == loc:
                    run.fired = False
                    run.noted = False
        logger.info("席 %d のカードを読み直します — 正しいカードを置き直してください", seat)

    def _assign_board_index(self, uid: str) -> Optional[int]:
        """新規 board UID に **ボード全体での位置**（1..5）を割り当てる。

        物理配置は「ボード領域に board reader が N 台並んでいるだけ」で、どの台がどの
        ストリートを受けるかは **置き方次第**（flop 3 枚が 3 台に散ることも、真ん中の 1 台に
        2 枚載ることもある）。よって位置は reader ごとの固定 offset ではなく
        **全 board reader を通した検出順**（= ディーラーが配った順）で決める。

        **ハンド内は append-only**（ISSUE-0026）。一度与えた位置は、そのカードが盤上から
        消えても返さない。理由は 2 つ:

        - **ポーカーではハンド中にボードのカードが減らない**。engine 側の board も縮まない
          （`_handle_board_rfid` は埋めるだけ）ので、同じ規則にしておけば構造的にずれない。
        - 位置を解放すると、**一瞬の読み落ちや札の入れ替えで空いたスロットを別の札が奪い**、
          戻ってきた札が別位置を取って「同じ札が 2 か所」「枚数の水増し」が起きる。
          実機 2026-09-12 で `7c` が 1→2、`Qh` が 2→4 と動いたのがこれ。

        したがって:

        - **既に位置を持っている UID はその位置を返す**（隣接リーダーの磁界が重なって 1 枚を
          2 台が読むと同じ UID で 2 回発火するが、位置は 1 つに保たれる, ISSUE-0025）。
        - 新規 UID には空き位置の最小を与える（= ディーラーが配った順に 1..5）。
        - 5 枚を超えたら WARN + None（engine は末尾に追記する）。ボードに 6 枚目が現れるのは
          misdeal かカードの置きっぱなしなので、黙って位置を回さず異常として出す。
        - 捨てるのは `reset_board_positions()`（新ハンド）だけ。
        """
        existing = self._board_indexes.get(uid)
        if existing is not None:
            return existing

        taken = set(self._board_indexes.values())
        index = next((i for i in range(1, _BOARD_MAX_CARDS + 1) if i not in taken), None)
        if index is None:
            logger.warning(
                "ボードが %d 枚を超えました（tag=%s）— board_index なしで記録します。"
                "ハンドを始め直すなら新ハンド（n）でリセットされます",
                _BOARD_MAX_CARDS, uid,
            )
            return None

        self._board_indexes[uid] = index
        return index

    def _warn_obsolete_board_fields(self, cfg: dict, reader_id: str) -> None:
        """旧 config（board reader ごとの `index` / `cards`）を使っていたら一度だけ警告する。

        v1.1/v1.2 は「1 台 = 1 ストリート専用（flop は 1 台に 3 枚重ね）」前提だったが、
        実機は 3 台が並んでいるだけなので前提が成立しない（ISSUE-0024 / ADR-0053）。
        現在は全台を 1 つの論理ボードとして検出順に 1..5 を振るため、両フィールドは無視する。
        """
        stale = [k for k in ("index", "cards") if k in cfg]
        if stale:
            logger.warning(
                "%s: board reader の %s は廃止されました（無視します）。ボード位置は "
                "board reader 全台を通した検出順で決まります（ADR-0053）。config から削除してください",
                reader_id, " / ".join(stale),
            )
