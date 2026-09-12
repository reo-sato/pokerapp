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
  `reader`（任意・既定 0）は **物理リーダーの index**（Get UID の P2, 契約 v1.2 §6 / ADR-0041）。
  Windows の汎用 CCID ドライバは 1 インターフェース 1 slot しか公開しないため、PC/SC reader
  （`name`）は 1 つで、物理リーダー N 台は `reader` で選ぶ。`(name, reader)` の組で一意。
  role="board" は **役割だけ**を書く（位置は書かない）。ボード領域には board reader が N 台
  並んでいるだけで、どの台がどのストリートを受けるかは置き方次第（flop 3 枚が 3 台に散ることも、
  真ん中の 1 台に 2 枚載ることもある）。よって **board reader 全台を 1 つの論理ボード**として扱い、
  `board_index`（1..5）は **全台を通した検出順** = ディーラーが配った順で決める（契約 v1.3 §4 /
  ADR-0042）。旧 config の `index` / `cards` は廃止（あれば WARN して無視）。

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
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from core.event_queue import EventQueue
from core.events import RFIDEvent
from rfid.bridge import MAX_READER_INDEX, PCSCBridge, bridge_read_uids, call_bridge_factory
from rfid.card_master import CardMaster

logger = logging.getLogger(__name__)

# コミュニティカードの最大枚数（flop 3 + turn + river）。board 位置は 1..5。
_BOARD_MAX_CARDS = 5


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
            clock:            epoch 秒を返す時計（既定 time.time）。マック観測時刻の源（ADR-0044）。
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
        # seat → **カードが席から消えた時刻**（epoch）。戻ってきたら消す（ADR-0044）。
        # fold を「判定」するためではなく、合成 fold に**実時刻を与える**ために使う
        # （プレイヤーはカードを持ち上げて見ることがあるので、不在そのものは fold を意味しない）。
        self._seat_absent_since: dict[int, float] = {}

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
            return

        try:
            while not self._stop_event.is_set():
                for reader_id, (bridge, cfg) in bridges.items():
                    self._poll_reader(bridge, cfg, reader_id)
                time.sleep(self._poll_interval)
        finally:
            for reader_id, (bridge, _) in bridges.items():
                bridge.close()
            logger.info("RFIDThread stopped")

    def _poll_reader(self, bridge: object, cfg: dict, reader_id: str) -> None:
        """1 リーダーをポーリングし、**新しく増えた UID ごとに** RFIDEvent を投入する。

        重ね置き（席 2 枚 / flop 3 枚）に対応するため、状態は UID 1 個ではなく集合で持つ。
        """
        uids = bridge_read_uids(bridge)   # 旧 bridge（read_uid のみ）互換シム
        current: set[str] = set(uids)
        prev = self._last_uids.get(reader_id, set())

        # デバウンス: 集合が前回と同じなら何もしない
        if current == prev:
            return

        self._last_uids[reader_id] = current

        removed = prev - current
        if removed:
            # カードが外れた（イベント不要）。**board の位置は解放しない**（ハンド内 append-only,
            # ISSUE-0026）。ミスディールで載せ替えるときは明示の訂正コマンドで解放する
            # （`forget_board_position` / `forget_seat_cards`, ADR-0043）。
            logger.debug("Card(s) removed from %s: %s", reader_id, sorted(removed))

        if cfg.get("role") == "board":
            self._board_reader_ids.add(reader_id)
        elif isinstance(cfg.get("seat"), int):
            self._seat_reader_ids.setdefault(cfg["seat"], set()).add(reader_id)
            self._track_seat_presence(cfg["seat"])

        # 新規タッチ検出（読み取り順を保ったまま、増えた UID ごとに 1 event）
        seen: set[str] = set()
        for uid in uids:
            if uid in prev or uid in seen:
                continue
            seen.add(uid)
            self._emit_event(cfg, reader_id, uid)

    def _emit_event(self, cfg: dict, reader_id: str, uid: str) -> None:
        """新規検出 UID 1 件を RFIDEvent にして投入する。"""
        card = self._card_master.lookup(uid)
        role = cfg.get("role", "seat")
        seat = cfg.get("seat") if role == "seat" else None
        # board_index は board street 自動遷移に必須（engine が board_index!=None を分岐条件にする,
        # engine.py:294）。**board reader 全台で共有する論理ボードの「何枚目か」**（契約 v1.3 §4）。
        board_index = self._assign_board_index(uid) if role == "board" else None

        event = RFIDEvent(
            tag_id=uid,
            card=card,
            reader_id=reader_id,
            role=role,
            seat=seat,
            timestamp=time.time(),
            raw_tag_id=uid,
            board_index=board_index,
        )
        self._queue.put(event)
        logger.debug(
            "RFIDEvent: reader=%s role=%s seat=%s board_index=%s tag=%s card=%r",
            reader_id, role, seat, board_index, uid, card,
        )

    # ――― board の位置割り当て（契約 v1.3 §4: board reader 全台で 1 つの論理ボード） ―――

    def reset_board_positions(self) -> None:
        """ボード位置の割り当てを捨てて次のハンドに備える（新ハンドの同期点, ISSUE-0026）。

        `IntegrationThread` の `on_new_hand` フックから呼ばれる。board reader のデバウンス状態も
        落とすので、**ハンド開始時に盤上に残っているカードは改めて 1 番から検出し直す**
        （engine 側も `_board_positions` を空にするため、両者が必ず同じ状態から始まる）。

        poll スレッドとは別スレッド（IntegrationThread）から呼ばれるが、dict/set の
        差し替えは GIL 下で原子的で、poll 側は「見えている UID との差分」で動くため、
        取りこぼしは次の poll で回復する（ロックは持たない = poll を止めない）。
        """
        self._board_indexes = {}
        for reader_id in self._board_reader_ids:
            self._last_uids[reader_id] = set()
        logger.info("新ハンド: board の位置割り当てをリセットしました")

    # ――― マック観測（ADR-0044: 合成 fold に実時刻を与えるためだけに使う） ―――

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

        `{seat: {"present": bool, "absent_since": float | None, "uid_count": int}}`。
        **「載っている」であって「ゲームに残っている」ではない**（ADR-0045 D4）。
        まだ一度も検出していない席は現れない（= 未配布と区別できる）。
        """
        snapshot: dict[int, dict] = {}
        for seat, reader_ids in self._seat_reader_ids.items():
            uids: set[str] = set()
            for reader_id in reader_ids:
                uids |= self._last_uids.get(reader_id, set())
            snapshot[seat] = {
                "present": bool(uids),
                "absent_since": self._seat_absent_since.get(seat),
                "uid_count": len(uids),
            }
        return snapshot

    def reset_for_new_hand(self) -> None:
        """新ハンドの同期点（board 位置 + マック観測をまとめて捨てる）。

        ハンド終了時はディーラーが全席のカードを回収するので、観測を持ち越すと次のハンドの
        合成 fold に前のハンドの時刻が付く。`on_new_hand` フックはこちらを呼ぶ。
        """
        self.reset_board_positions()
        self._seat_absent_since = {}
        for reader_ids in self._seat_reader_ids.values():
            for reader_id in reader_ids:
                self._last_uids[reader_id] = set()

    # ――― ミスディール訂正（ADR-0043: 明示コマンドで 1 枚だけ載せ替える） ―――

    def forget_board_position(self, index: int) -> Optional[str]:
        """board 位置 `index` の割り当てを 1 つだけ解放する（ミスディール訂正, ADR-0043）。

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
        uid = next((u for u, i in self._board_indexes.items() if i == index), None)
        if uid is None:
            logger.warning("board 位置 %s には割り当てがありません（訂正は無効）", index)
            return None
        self._board_indexes = {u: i for u, i in self._board_indexes.items() if u != uid}
        for reader_id in self._board_reader_ids:
            self._last_uids[reader_id] = set()
        logger.info(
            "board 位置 %d を解放しました（tag=%s）— 正しいカードを置き直してください", index, uid,
        )
        return uid

    def forget_seat_cards(self, seat: int) -> None:
        """席 `seat` のリーダーのデバウンスを落とし、載っているカードを読み直させる（ADR-0043）。

        engine 側は `_hole_cards[seat]` を空にするので、**物理的に載っている 2 枚が改めて
        記録される**。ミスディールしたカードを先に外してから打つこと（外す前に打つと同じ
        カードがまた記録されるだけなので、外して再実行すればよい）。
        """
        reader_ids = self._seat_reader_ids.get(seat)
        if not reader_ids:
            logger.warning("席 %s に対応する RFID リーダーがありません（訂正は無効）", seat)
            return
        for reader_id in reader_ids:
            self._last_uids[reader_id] = set()
        self._seat_absent_since.pop(seat, None)   # 訂正後の観測をやり直す（ADR-0044）
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
        実機は 3 台が並んでいるだけなので前提が成立しない（ISSUE-0024 / ADR-0042）。
        現在は全台を 1 つの論理ボードとして検出順に 1..5 を振るため、両フィールドは無視する。
        """
        stale = [k for k in ("index", "cards") if k in cfg]
        if stale:
            logger.warning(
                "%s: board reader の %s は廃止されました（無視します）。ボード位置は "
                "board reader 全台を通した検出順で決まります（ADR-0042）。config から削除してください",
                reader_id, " / ".join(stale),
            )
