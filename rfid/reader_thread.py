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
     いる間に次のストリートの札が来ても、次の位置のまま。途中で読めなくなったことのある札は
     厳しい側（同じリーダー・時間窓・`release_sec`）で判断する。
  2. **flop 全体の配り直し**: flop の札が全部 `redeal_confirm_sec` 以上見えなければ、時間によらず
     flop の位置を若い順に差し替える（早すぎた flop を戻して後から配り直した）。
  3. **6 枚目**: 5 枚埋まったあとに新しい札が確定し、5 枚のうち 1 枚だけが `release_sec` 以上
     消えていれば、その札を抜いて後ろを詰める。
  席では、同じハンドで別の席に記録した札が、元の席から消えて新しい席に載り続けたら移す
  （配り直しで席が変わった）。

既定値（`commit_sec=0` / `release_sec=None`）は従来の挙動（最初に見えた瞬間に確定・ハンド内は
append-only・差し替えは明示の訂正コマンドだけ）で、`tools/probe_pcsc.py` の検査が使う。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.event_queue import EventQueue
from core.events import RFIDEvent
from rfid.bridge import MAX_READER_INDEX, PCSCBridge, bridge_read_uids, call_bridge_factory
from rfid.card_master import CardMaster

logger = logging.getLogger(__name__)

# コミュニティカードの最大枚数（flop 3 + turn + river）。board 位置は 1..5。
_BOARD_MAX_CARDS = 5
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
# 確定前のボードの札は、この秒数までの途切れを「載り続けている」とみなす（`gap_sec` より長い）。
# リーダーの境目・重ね置きの札は途切れながら読めるので、`gap_sec` のままだと確定まで数え直しを
# 繰り返して反映が遅れる（店舗の実卓, ADR-0058 追記 3）。一瞬の通過は 1 回きりなので影響しない。
_PENDING_GAP_SEC = 3.0


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
                              確かめる秒数。None = `release_sec` と同じ。途中で読めなくなったことの
                              ある札は常に `release_sec` を使う。
        """
        super().__init__(daemon=True, name="RFIDThread")
        self._queue = rfid_queue
        self._card_master = card_master
        self._reader_configs = reader_configs
        self._poll_interval = poll_interval_ms / 1000.0
        self._stop_event = stop_event or threading.Event()
        self._bridge_factory = bridge_factory or PCSCBridge
        self._clock: Callable[[], float] = clock or time.time

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

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("RFIDThread started (%d reader(s))", len(self._reader_configs))

        # ブリッジを初期化
        bridges: dict[str, object] = {}
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

        if not bridges:
            logger.warning("No RFID readers connected. RFIDThread exiting.")
            self.health = {
                "state": "no_readers",
                "connected": 0,
                "configured": len(self._reader_configs),
                "last_event_at": None,
            }
            return
        self.health = {
            "state": "running",
            "connected": len(bridges),
            "configured": len(self._reader_configs),
            "last_event_at": None,
        }

        try:
            while not self._stop_event.is_set():
                for reader_id, (bridge, cfg) in bridges.items():
                    self._poll_reader(bridge, cfg, reader_id)
                time.sleep(self._poll_interval)
        finally:
            for reader_id, (bridge, _) in bridges.items():
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
        with self._lock:
            if self._tracking:
                events = self._poll_tracked(cfg, reader_id, uids, current)
            else:
                events = self._poll_immediate(cfg, reader_id, uids, current)
        for event in events:
            self._queue.put(event)
            logger.debug(
                "RFIDEvent: reader=%s role=%s seat=%s board_index=%s tag=%s card=%r replaces=%r",
                event.reader_id, event.role, event.seat, event.board_index, event.tag_id,
                event.card, event.replaces,
            )
        if events:
            self.health = {**self.health, "last_event_at": time.time()}

    def _learn_role(self, cfg: dict, reader_id: str) -> None:
        if cfg.get("role") == "board":
            self._board_reader_ids.add(reader_id)
        elif isinstance(cfg.get("seat"), int):
            self._seat_reader_ids.setdefault(cfg["seat"], set()).add(reader_id)
            self._track_seat_presence(cfg["seat"])

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

        slot_uid = {i: u for u, i in self._board_indexes.items()}
        index, waiting = self._flop_redeal_slot(uid, run, slot_uid, now)
        if index is None and not waiting:
            candidates = self._redeal_candidates(run, slot_uid)
            ready = [i for i, confirm in candidates
                     if self._absent_for("board", slot_uid[i], now) >= confirm]
            if ready:
                index = ready[0]
            elif candidates:
                # 前の札が消えた直後に、その近くへ置かれた。差し直しか（前の札が戻らない）、
                # 読み落ちだったか（前の札が戻る）が分かるまで位置を決めない。
                waiting = [i for i, _ in candidates]
                if not run.noted:
                    logger.info(
                        "ボードの新しい札 %s（%s）: %s が消えた直後です — 差し直しか確かめています"
                        "（前の札が %.0f 秒見えなければ差し替え）",
                        self._card_name(uid), self._reader_label(run.readers),
                        ", ".join(self._slot_label(i, slot_uid[i], now) for i in waiting),
                        min(confirm for _, confirm in candidates),
                    )
                    run.noted = True
        run.waiting = bool(waiting)
        if waiting:
            return []

        run.fired = True
        if index is not None:
            return [self._replace_board_card(uid, run, index, slot_uid[index])]
        free = [i for i in range(1, _BOARD_MAX_CARDS + 1) if i not in slot_uid]
        if free:
            self._board_indexes[uid] = free[0]
            self._board_first_seen[uid] = run.first_seen
            self._log_board_commit(uid, run, free[0], slot_uid, now)
            return [self._run_event(uid, run, "board", None, board_index=free[0])]
        rebuilt = self._rebuild_board(uid, run, slot_uid, now)
        if rebuilt:
            return rebuilt
        logger.warning(
            "ボードが %d 枚を超えました（tag=%s）— board_index なしで記録します。"
            "ハンドを始め直すなら新ハンド（n）でリセットされます",
            _BOARD_MAX_CARDS, uid,
        )
        return [self._run_event(uid, run, "board", None)]

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

    def _redeal_candidates(
        self, run: _Run, slot_uid: dict[int, str],
    ) -> list[tuple[int, float]]:
        """1 枚だけの差し直しで、新しい札が差し替えるかもしれない位置と、前の札が見えないことを
        確かめる秒数（位置の若い順）。

        前の札が**いま見えない**ことに加えて:

        - **最後に配った turn / river**（いちばん後ろの 4・5 枚目）: 新しい札が**同じか隣の**
          リーダーで読まれた（位置がリーダーの境目にあると、置き直した札を隣の台が読む）。時間は
          問わない（早すぎた turn を外し、ベッティングの後で配り直すこともある）。
        - **それ以外**（flop の札・後ろに札がある札）: 前の札を読んでいたリーダーで新しい札が読まれ、
          前の札が消えてから `redeal_window_sec` 以内に新しい札が現れた。ディーラーは取り除いた札の
          場所に新しい札を置くので、同じリーダーが「同じ場所」の根拠になる。別のリーダーの札が読み
          落ちている間に次のストリートの札が来ても、ここには入らない（次の位置のまま）。

        確定したあとに一度途切れて戻った札（読めにくい場所にある）は、消えても取り除いたとは
        限らないので、常に同じリーダー・時間窓・`release_sec` の厳しい側で判断する。
        """
        if self._release_sec is None or self._redeal_window_sec is None:
            return []
        present = self._loc_present.get("board", set())
        latest = max(slot_uid) if slot_uid else 0
        candidates: list[tuple[int, float]] = []
        for index, old in sorted(slot_uid.items()):
            old_run = self._runs.get(("board", old))
            if old in present or old_run is None:
                continue
            flaky = old_run.first_seen > self._board_first_seen.get(old, old_run.first_seen)
            newest = index == latest and index > _FLOP_SLOTS[-1] and not flaky
            if not newest and old_run.last_seen < run.first_seen - self._redeal_window_sec:
                continue            # 新しい札よりずっと前から読めていない札（読み落ち続けている）
            if newest:
                if not self._near(old_run.readers, run.readers):
                    continue
            elif not old_run.readers & run.readers:
                continue            # 別の場所の札
            candidates.append((index, self._release_sec if flaky else self._redeal_confirm_sec))
        return candidates

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
        """ログ用「7c（4 枚目・左から 2 台目[・3.2 秒前から]）」。`now` を渡すと見えなくなってからの秒数も。"""
        run = self._runs.get(("board", uid))
        parts = [f"{index} 枚目", self._reader_label(run.readers if run is not None else set())]
        if now is not None and run is not None:
            parts.append(f"{now - run.last_seen:.1f} 秒前から")
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

    def _replace_board_card(self, uid: str, run: _Run, index: int, old: str) -> RFIDEvent:
        self._forget_board_uid(old)
        self._board_indexes[uid] = index
        self._board_first_seen[uid] = run.first_seen
        replaces = self._card_master.lookup(old) or None
        logger.info(
            "配り直しを検出: ボード %d 枚目を差し替えました（%s → %s, %s）",
            index, replaces or old, self._card_name(uid), self._reader_label(run.readers),
        )
        return self._run_event(uid, run, "board", None, board_index=index, replaces=replaces)

    def _rebuild_board(
        self, uid: str, run: _Run, slot_uid: dict[int, str], now: float,
    ) -> list[RFIDEvent]:
        """5 枚埋まったあとに新しい札が確定した。1 枚だけ消えていれば、抜いて後ろを詰める。

        早すぎた turn / river を外し、ベッティングの後で配り直すと、新しい札は時間窓の外で次の位置を
        取る（その時点では読み落ちと区別できない）。本物の river が来て 6 枚目になった時点で、消えた
        札が 1 枚だけなら配り直しと確定できるので、その札を抜いて並びを直す。消えた札が 0 枚や 2 枚
        以上なら判断できないので従来どおり WARN にする。

        engine はボードの位置を上書きするだけなので、**後ろの位置から**送る（前から送ると、詰めた札が
        一瞬 2 か所に並び、重複の WARN が出る）。時刻はそれぞれの札を配った時刻。
        """
        gone = [i for i, u in slot_uid.items() if self._is_gone(u, now)]
        if len(gone) != 1:
            return []
        dropped = slot_uid[gone[0]]
        order = [slot_uid[i] for i in sorted(slot_uid) if i != gone[0]] + [uid]
        self._forget_board_uid(dropped)
        self._board_first_seen[uid] = run.first_seen
        logger.info(
            "配り直しを検出: ボード %d 枚目の %s が消えたまま 6 枚目 %s が置かれました — 抜いて詰めます",
            gone[0], self._card_master.lookup(dropped) or dropped,
            self._card_master.lookup(uid) or uid,
        )
        events: list[RFIDEvent] = []
        for index in range(len(order), 0, -1):
            new, old = order[index - 1], slot_uid[index]
            self._board_indexes[new] = index
            if new == old:
                continue
            new_run = run if new == uid else self._runs.get(("board", new))
            events.append(self._make_event(
                new, new_run.reader_id if new_run is not None else run.reader_id, "board", None,
                self._board_first_seen.get(new, run.first_seen),
                board_index=index, replaces=self._card_master.lookup(old) or None,
            ))
        return events

    def _forget_board_uid(self, uid: str) -> None:
        self._board_indexes.pop(uid, None)
        self._board_first_seen.pop(uid, None)
        self._board_pending.discard(uid)

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
            self._seat_absent_since.pop(seat, None)
        elif seat not in self._seat_absent_since:
            # 「消えた最初の瞬間」を採る（確認は engine 側。後から上書きしない）。
            self._seat_absent_since[seat] = self._clock()

    def seat_cards_absent_since(self, seat: int) -> Optional[float]:
        """その席のカードが消えたまま戻っていない場合、消えた時刻（epoch）。無ければ None。"""
        return self._seat_absent_since.get(seat)

    def presence_snapshot(self) -> dict[int, dict]:
        """席ごとの現在のカード在否（卓状態の表示用, `core/table_state.py`）。

        `{seat: {"present": bool, "absent_since": float | None, "uid_count": int,
        "mucked_at": float | None}}`。
        **「載っている」であって「ゲームに残っている」ではない**（ADR-0056 D4）。
        まだ一度も検出していない席は現れない（= 未配布と区別できる）。
        `mucked_at` はその席の手札が卓の中央（board reader の上）を通過した時刻（ADR-0058）。
        """
        snapshot: dict[int, dict] = {}
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
        """
        if not self._tracking:
            return {"absent": {}, "pending": {}}
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
        return {"absent": absent, "pending": pending}

    def reset_for_new_hand(self) -> None:
        """新ハンドの同期点（board 位置 + マック観測 + 席の記録をまとめて捨てる）。

        ハンド終了時はディーラーが全席のカードを回収するので、観測を持ち越すと次のハンドの
        合成 fold に前のハンドの時刻が付く。`on_new_hand` フックはこちらを呼ぶ。
        """
        with self._lock:
            self.reset_board_positions()
            self._seat_absent_since = {}
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
