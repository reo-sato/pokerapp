"""core/hand_boundary.py

Phase 2-C: ハンドの開始 / 終了境界 (hand window) を観測の流れから検出する。

役割:
  - 音声 ``new_hand`` / ``winner`` イベント、RFID 由来の board / hole cards 状態変化
    から ``BoundaryEvent`` (start / end) を発行する。
  - IntegrationThread と replay (オフライン) の両方で同じ detector が使える純粋
    オブジェクト。state は内部で持ち、外部入力 (event) で進める。

**重要: boundary は ground truth ではなく boundary hint / segmentation 観測**:
  ``audio_new_hand`` / ``audio_winner`` / ``board_cleared`` / ``hole_cards_appeared``
  はいずれも「hand segmentation のための観測」であって絶対的な truth ではない。
  音声誤認識、RFID 取り逃し、ディーラーの手順前後など、シグナルが間違うことは
  起こりうる。Phase 3+ では:

    - シグナル種別ごとに重み / prior を変える (例: audio_winner と board_cleared
      が両方観測されたら confidence を上げる、片方だけなら下げる)
    - 矛盾するシグナルから confidence を計算して provisional / incomplete 判定を
      返す
    - HandReconstructor で hand window を後ろ向きに再評価する際、boundary 自体
      も再評価対象に含める

といった拡張を予定している。本フェーズではすべて等価な「決定論的シグナル」
として扱うが、これは初期 heuristics であり、上位レイヤがそのまま truth として
信じてはならない (例: HandFinalizer は boundary に依存しない設計を維持する)。

Phase 2-C 初期 heuristics:

  start シグナル:
    - ``AudioEvent(action="new_hand")``                       (audio_new_hand)
    - 前 hand 終了後に board が空で 2+ seat に hole cards が現れる (hole_cards_appeared)

  end シグナル:
    - ``AudioEvent(action="winner")``                          (audio_winner)
    - board が非空 → 空 になり、その後 ``BOARD_EMPTY_QUIET_SEC`` 秒以上空のまま   (board_cleared)
    - 全 seat の hole cards が absent (現在は spec のみで未実装、Phase 2-D 候補)

board が「空のまま静止」を検出するため、新規イベントが届かない場合でも
``tick(now)`` で時刻を進めて detector に問い合わせできるようにしている。

Phase 2-C では engine の online flow を壊さないため、`new_hand` / `winner` audio
を一次シグナルとして優先する。RFID-only シナリオは tests と replay ユーティリティ
で実証する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from core.events import AudioEvent, CameraEvent, RFIDEvent


BoundaryKind = Literal["start", "end"]


@dataclass
class BoundaryEvent:
    """hand 境界 (start / end) を表す。

    ``t_start`` / ``t_end`` は境界そのものの時刻 (start なら start 時刻、end なら
    end 時刻)。1 hand の window 全体の (start_ts, end_ts) を作るには、
    detector が emit する start event と end event をペアで集めてから合成する。
    """

    kind: BoundaryKind
    hand_id: int
    t_start: float           # この境界 event 自体の時刻
    t_end: float             # 同上 (現状 start とは同値、将来「区間」を表現するなら拡張)
    reason: str


class HandBoundaryDetector:
    """hand window を検出する純粋なステートマシン。

    Phase 2-C: ``new_hand`` / ``winner`` の audio を主シグナルとし、
    ``observe_board_state(board, now)`` で board cleared end を補強する。
    返り値はすべて ``list[BoundaryEvent]``: 0 件 (no transition)、1 件 (普通)、
    2 件 (e.g., ``new_hand`` while in_hand → [end, start])。
    """

    # board が空のまま何秒経過したら end とみなすか (初期 heuristic、設定可)
    BOARD_EMPTY_QUIET_SEC: float = 1.5

    def __init__(self, board_empty_quiet_sec: Optional[float] = None) -> None:
        if board_empty_quiet_sec is not None:
            self.BOARD_EMPTY_QUIET_SEC = float(board_empty_quiet_sec)
        self._current_hand_id: int = 0
        self._in_hand: bool = False
        self._hand_start_ts: float = 0.0
        # 内部状態 (snapshot ベース)
        self._board_seen: set[str] = set()
        self._hole_seen: dict[int, set[str]] = {}
        # board が空に転落した時刻 (None = 空ではない / 既に end 発行済み)
        self._board_emptied_at: Optional[float] = None

    # ─────────────────────────────────────────────────────────────────────
    # 観測 API (各種 event / state snapshot)
    # ─────────────────────────────────────────────────────────────────────

    def observe_audio_event(self, event: AudioEvent) -> list[BoundaryEvent]:
        """``new_hand`` で start、``winner`` で end を発行する。

        in_hand 中に ``new_hand`` が来た場合は ``[end, start]`` の 2 件を返す
        (前 hand を暗黙終了して次 hand を開始)。
        """
        out: list[BoundaryEvent] = []
        action = (event.action or "").lower()
        ts = float(event.timestamp)

        if action == "new_hand":
            if self._in_hand:
                out.append(self._close_hand(ts, "audio_new_hand_implicit_end"))
            out.append(self._open_hand(ts, "audio_new_hand"))
        elif action == "winner":
            if self._in_hand:
                out.append(self._close_hand(ts, "audio_winner"))
        # その他の audio (bet/call/fold 等) は境界トリガーではない
        return out

    def observe_rfid_event(self, event: RFIDEvent) -> list[BoundaryEvent]:
        """RFID 由来の board / hole state を内部に蓄積。

        現状は state 更新のみ。board 全消滅判定は ``observe_board_state`` 側 (snapshot
        API) で行う設計。ここで board の add は ``_board_seen`` に積み、quiet タイマー
        を解除する。
        """
        ts = float(event.timestamp)
        if event.role == "board" and event.card:
            self._board_seen.add(event.card)
            # 一度でも board が観測されたら quiet タイマーは reset (= board 非空に戻った)
            self._board_emptied_at = None
        elif event.role == "seat" and event.card and event.seat is not None:
            self._hole_seen.setdefault(event.seat, set()).add(event.card)
        # 状態更新だけ。境界判定は snapshot API か tick で。
        return self._maybe_end_due_to_board_quiet(ts)

    def observe_camera_event(self, event: CameraEvent) -> list[BoundaryEvent]:
        """camera は Phase 2-C では境界判定に使わない。state を進めるトリガー
        として ``tick(event.timestamp)`` 同等の副作用だけ持つ。
        """
        return self._maybe_end_due_to_board_quiet(float(event.timestamp))

    def observe_board_state(
        self,
        board_cards: list[str],
        now: float,
    ) -> list[BoundaryEvent]:
        """外部から board state スナップショットを渡す。

        非空 → 空 遷移を検出すると ``_board_emptied_at`` を立て、その後 ``now`` が
        ``BOARD_EMPTY_QUIET_SEC`` を超えたら end を発行する。
        """
        was_non_empty = bool(self._board_seen)
        is_empty = not board_cards
        self._board_seen = set(board_cards)

        if self._in_hand and was_non_empty and is_empty:
            self._board_emptied_at = float(now)
        elif not is_empty:
            # board が再び非空 → 進行中、quiet タイマー解除
            self._board_emptied_at = None
        return self._maybe_end_due_to_board_quiet(float(now))

    def observe_hole_state(
        self,
        hole_cards: dict[int, list[str]],
        now: float,
    ) -> list[BoundaryEvent]:
        """外部から seat 別 hole cards スナップショットを渡す。

        idle 状態かつ board が空のとき、2+ seat に hole cards が確認できた瞬間
        を start とみなす (heuristic)。
        """
        present_seats = [s for s, cards in (hole_cards or {}).items() if cards]
        # 内部状態の同期
        self._hole_seen = {s: set(c) for s, c in (hole_cards or {}).items() if c}

        if (not self._in_hand) and (not self._board_seen) and len(present_seats) >= 2:
            return [self._open_hand(float(now), "hole_cards_appeared")]
        return []

    def tick(self, now: float) -> list[BoundaryEvent]:
        """時刻を進める。外部から「ある時刻 now 時点での detector 状態を問い合わせ
        る」ときに呼ぶ。board の quiet 期間判定にのみ使う。"""
        return self._maybe_end_due_to_board_quiet(float(now))

    # ─────────────────────────────────────────────────────────────────────
    # アクセサ
    # ─────────────────────────────────────────────────────────────────────

    def current_hand_id(self) -> int:
        return self._current_hand_id

    @property
    def in_hand(self) -> bool:
        return self._in_hand

    # ─────────────────────────────────────────────────────────────────────
    # 内部ヘルパ
    # ─────────────────────────────────────────────────────────────────────

    def _maybe_end_due_to_board_quiet(self, now: float) -> list[BoundaryEvent]:
        if (
            self._in_hand
            and self._board_emptied_at is not None
            and (now - self._board_emptied_at) >= self.BOARD_EMPTY_QUIET_SEC
        ):
            return [self._close_hand(now, "board_cleared")]
        return []

    def _open_hand(self, ts: float, reason: str) -> BoundaryEvent:
        # 開始時に内部状態をリセット (前 hand の board/hole 痕跡を持ち越さない)
        self._current_hand_id += 1
        self._in_hand = True
        self._hand_start_ts = ts
        self._board_seen = set()
        self._hole_seen = {}
        self._board_emptied_at = None
        return BoundaryEvent(
            kind="start",
            hand_id=self._current_hand_id,
            t_start=ts,
            t_end=ts,
            reason=reason,
        )

    def _close_hand(self, ts: float, reason: str) -> BoundaryEvent:
        ev = BoundaryEvent(
            kind="end",
            hand_id=self._current_hand_id,
            t_start=self._hand_start_ts,
            t_end=ts,
            reason=reason,
        )
        self._in_hand = False
        self._board_emptied_at = None
        return ev
