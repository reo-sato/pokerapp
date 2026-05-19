"""core/hand_reconstructor.py

Phase 3: hand window 単位の **遡及的 (retrospective) 再推定** 本実装。

役割:
  1 ハンドの全イベント (EvidenceRecord 列) を受け取り、既存の online 推定系
  (BettingState / BeamEngine / HandFinalizer) を **window 内だけで再生** することで、
  online では決定できなかった ambiguous なアクション列を hand 全体の context
  (winner / showdown / final pot / RFID 区間) で再評価する。

実装方針:
  1. ``initial_state`` 優先、なければ ``online_summary`` から BettingState を bootstrap
     (button / SB-BB seat は ``online_summary.actions`` の SB_POST / BB_POST から逆算)
  2. ``BeamEngine(K=8, prior=default_priors())`` を新規構築し ``reset_with_state(bs)``
  3. events を時刻順に走査:
     - AudioEvent の通常 action → ``beam.step_audio`` で MAP 取得、bs.update_after_action
     - AudioEvent ``winner`` → ``winner_seat_hint`` に格納
     - AudioEvent ``new_hand`` / ``showdown`` → 制御 event として skip
     - RFIDEvent ``seat`` → hole_cards に蓄積
     - RFIDEvent ``board`` → board に蓄積、5 枚に達したら street を river へ昇格
  4. ``winner_seat_hint`` が判明したら ``beam.apply_winner_filter(...)`` で粒子集合を絞る
  5. ``HandFinalizer.finalize(...)`` で offline HandSummary を生成
  6. ``online_summary`` が提供されていれば diff 計算 → ``needs_review=True`` を立てる

**重要な約束** (Phase 3 スコープ):
  - online HandSummary / JSON / PHH を **自動で書き換えない**。
    ``HandReconstructor`` は **追加のオフライン / 後処理パス**としてのみ動作する。
  - online と offline の差分は ``HandReconstructionResult.diff`` (機械可読 dict) と
    ``needs_review`` フラグで返し、消費側 (CLI / GUI / 監視ツール) が判断する。

**Phase 3 MVP の仕様・制約事項** (明文化):

  - **bootstrap 経路 (Phase 4-B で raw-only を追加)**:
    Phase 3 では ``online_summary`` を bootstrap の入力に使う
    *online-bootstrap-assisted reconstruction* のみだった。Phase 4-B で
    ``_bootstrap_from_events`` を追加し、**initial_state → raw events → online_summary
    の順** で bootstrap を試みる。raw-only bootstrap が成立する条件:
      - RFID role="seat" で 2 seat 以上の hole_card 観測
      - コンストラクタの ``default_sb`` / ``default_bb`` が設定されている
    成立時は ``HandReconstructionResult.bootstrap_source="raw"`` + ``bootstrap_meta``
    に推定情報が入る。button_seat は raw からは確定できないため最小 seat 番号で
    deterministic に置く (``button_inferred=True`` でマーク)。

    **raw-only bootstrap の意味論 (Phase 4-B で意図的に固定した点)**:
      - **Phase 4-B raw bootstrap は RFID-centric**: active seats は
        ``RFID role="seat"`` 観測のみから取る。AudioEvent は seat 情報を持たない
        ので、現時点では bootstrap の signal source ではない (Phase 4-C 以降で
        「シート N が fold」等の自然言語 seat 推定を加えるのは別 commit の候補)。
      - **``button = min(active_seats)`` は deterministic seed であって truth 推定
        ではない**: 「最も button らしい seat」を確率的に推定したものではなく、
        ``BettingState.start_hand`` を起こすために必要な値を確定的に選んでいる。
        実際の button が誰だったかは raw からは確定不能。
        ``bootstrap_meta["button_inferred"]=True`` で消費側にこの事実を伝える。
      - **``bootstrap_meta["confidence"] = 0.5`` は fixed heuristic confidence で
        あって calibrated probability ではない**: モデルが計算した posterior でも
        Brier-calibrated でもなく、**「この heuristic は truth ではない」という印**
        (= 結果を 0.5 weight で扱って下さい、というメッセージ)。signal 強度に
        応じた動的計算は Phase 4-C+ の課題。

  - **``winner_seat_hint`` は oracle ではなく終端の補助制約**:
    ``AudioEvent(action="winner")`` から抽出した seat は **強観測ではなく**、
    ``beam.apply_winner_filter(winner_seat_hint, final_pot=None)`` への入力として
    使う「終端の補助制約の 1 つ」として扱う。粒子集合の絞り込みに使うのみで、
    settlement の確定は ``HandFinalizer`` 側に委ねる (``HandFinalizer`` は
    ``winner_seat_hint`` と settlement (= payouts) が食い違えば
    ``review_required=True`` を立てる)。

  - **``confidence`` は operational metric であってモデル事後確率ではない**:
    ``HandReconstructionResult.confidence = consumed_count / audio_count`` は
    **audio evidence の消費率** (= ``beam.step_audio`` → ``bs.update_after_action``
    が成功した割合) を示す operational metric。値域は [0, 1] だが、Bayes posterior
    や top-1 確率としては解釈しないこと。低い値は「再構成中に illegal action や
    beam fail が多発した」ことを示すヒントに過ぎない。モデル確率
    (top-1 vs top-2 の log 差、エントロピー、Brier score 等) は Phase 4+ の課題。
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from core.events import AudioEvent, RFIDEvent
from core.hand_finalizer import HandFinalizer
from core.hand_log import ActionRecord, HandSummary, RevealedHand
from core.patch_proposal import HandPatchProposal, compute_patch_proposal

if TYPE_CHECKING:
    from integration.action_inference import BettingState
    from integration.observation_model import PriorParams
    from output.replay_hand import EvidenceRecord

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Result
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class HandReconstructionResult:
    """retrospective inference の結果。

    fields:
      actions:           再評価後のアクション列 (SB_POST / BB_POST + beam MAP のアクション)
      summary:           offline HandSummary' (成功時のみ)
      needs_review:      online と diff があった / 信頼度が低い等のレビュー要否
      reason:            ``"reconstructed_no_diff"`` / ``"reconstructed_with_diff"`` /
                         ``"reconstructed"`` (online_summary 無しで参照不可) /
                         ``"reconstruction_skipped"`` (bootstrap 失敗等)
      diff:              online vs offline の差分。``None`` なら一致 or 比較不能。
                         dict 形式 ``{field_name: {"online": ..., "offline": ...}}``
      confidence:        **operational metric** = ``consumed_count / audio_count`` ∈ [0, 1]
                         or ``None``。モデル事後確率ではなく **audio evidence の消費率**
                         を示す (詳細はモジュール docstring を参照)。Phase 3 MVP で
                         確率としては解釈しないこと。
      bootstrap_source:  ``"initial_state"`` / ``"raw"`` / ``"online_summary"`` /
                         ``None`` (bootstrap 試行前 = skipped)。Phase 4-B で追加。
      bootstrap_meta:    bootstrap で使った signal や heuristic の診断情報。raw-only
                         成功時は active_seats / sb_seat / bb_seat / button_seat /
                         button_inferred / confidence などを記録。Phase 4-B で追加。
      patch_proposal:    diff があった hand の修正提案 (Phase 5-A 追加)。
                         ``can_patch_automatically`` は Phase 5-A では常に False。
                         CLI / GUI で提案表示のみに使い、自動 apply はしない。
    """

    actions: list[ActionRecord] = field(default_factory=list)
    summary: Optional[HandSummary] = None
    needs_review: bool = False
    reason: str = ""
    diff: Optional[dict[str, Any]] = None
    confidence: Optional[float] = None
    bootstrap_source: Optional[str] = None
    bootstrap_meta: Optional[dict[str, Any]] = None
    patch_proposal: Optional[HandPatchProposal] = None


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


_WINNER_SEAT_RE = re.compile(r"(?:シート|seat)\s*(\d+)", re.IGNORECASE)


def _extract_seat_from_text(text: str) -> Optional[int]:
    m = _WINNER_SEAT_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _current_street(board: list[str]) -> str:
    n = len([c for c in board if c])
    if n >= 5:
        return "river"
    if n >= 4:
        return "turn"
    if n >= 3:
        return "flop"
    return "preflop"


def _iso_from_unix(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="milliseconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _compute_diff(online: HandSummary, offline: HandSummary) -> Optional[dict[str, Any]]:
    """online と offline の構造差分を機械可読 dict にする。

    比較対象 (Phase 3 MVP):
      - resolution_status / resolution_type
      - winner_seat
      - pot_total
      - seat_payouts (key を int 正規化)
      - actions (seat, action, amount) のみ比較 (timestamp / pot_after は無視)
      - showdown_revealed_cards (key を int 正規化)

    返り値:
      差分が 1 件もなければ ``None``、あれば dict。
    """
    diff: dict[str, Any] = {}

    if online.resolution_type != offline.resolution_type:
        diff["resolution_type"] = {
            "online": online.resolution_type,
            "offline": offline.resolution_type,
        }
    if online.resolution_status != offline.resolution_status:
        diff["resolution_status"] = {
            "online": online.resolution_status,
            "offline": offline.resolution_status,
        }
    if int(online.winner_seat) != int(offline.winner_seat):
        diff["winner_seat"] = {
            "online": int(online.winner_seat),
            "offline": int(offline.winner_seat),
        }
    if int(online.pot_total) != int(offline.pot_total):
        diff["pot_total"] = {
            "online": int(online.pot_total),
            "offline": int(offline.pot_total),
        }

    # seat_payouts: JSON round-trip 後 key が str になっている可能性に備えて int 正規化
    op = {int(k): int(v) for k, v in (online.seat_payouts or {}).items()}
    fp = {int(k): int(v) for k, v in (offline.seat_payouts or {}).items()}
    if op != fp:
        diff["seat_payouts"] = {"online": op, "offline": fp}

    # showdown_revealed_cards: 同様
    orc = {int(k): list(v) for k, v in (online.showdown_revealed_cards or {}).items()}
    frc = {int(k): list(v) for k, v in (offline.showdown_revealed_cards or {}).items()}
    if orc != frc:
        diff["showdown_revealed_cards"] = {"online": orc, "offline": frc}

    # actions: timestamp / pot_after / stack_after はノイズなので (seat, action, amount) のみ比較
    def _keys(actions: list[ActionRecord]) -> list[tuple]:
        return [(int(a.seat), str(a.action), int(a.amount)) for a in (actions or [])]

    oa = _keys(online.actions)
    fa = _keys(offline.actions)
    if oa != fa:
        diff["actions"] = {"online": oa, "offline": fa}

    return diff or None


# ────────────────────────────────────────────────────────────────────────────
# HandReconstructor
# ────────────────────────────────────────────────────────────────────────────


class HandReconstructor:
    """hand window 単位で観測列を頭から再生して action / HandSummary を再推定する。

    Phase 3 で本実装に置き換わった。``online_summary`` を渡せば diff も計算され、
    ``needs_review`` が立つ。

    既存の online 推定系 (``HandFinalizer`` / ``BeamEngine`` / ``BettingState``) を
    そのまま再利用するため、別 variant や別 prior を試したい場合はコンストラクタ
    引数で差し替えられる。

    Phase 3 MVP の制約 (モジュール docstring も参照):
      - winner audio は終端の **補助制約** として ``apply_winner_filter`` に渡すのみ
      - ``confidence`` は audio 消費率の operational metric (確率ではない)

    Phase 4-B 以降:
      - bootstrap は ``initial_state`` → raw-only → ``online_summary`` の順で試す。
        raw-only が成功する条件は ``_bootstrap_from_events`` の docstring を参照。
      - ``default_sb`` / ``default_bb`` は raw-only bootstrap 時の blinds 額として使う
        (audio events から SB/BB POST 観測を得る手段が無いため)。CLI / live hook 側
        から session-level の設定を渡すことを想定。
    """

    def __init__(
        self,
        beam_K: int = 8,
        prior: "Optional[PriorParams]" = None,
        finalizer: Optional[HandFinalizer] = None,
        default_sb: Optional[int] = None,
        default_bb: Optional[int] = None,
    ) -> None:
        self._beam_K = int(beam_K)
        self._prior = prior
        self._finalizer = finalizer or HandFinalizer()
        # Phase 4-B: raw-only bootstrap で blinds 額が必要。None なら raw bootstrap 失敗。
        self._default_sb = int(default_sb) if default_sb is not None else None
        self._default_bb = int(default_bb) if default_bb is not None else None

    # ──────────────────────────────────────────────────────────────────────
    # public API
    # ──────────────────────────────────────────────────────────────────────

    def reconstruct_from_events(
        self,
        events: "list[EvidenceRecord]",
        initial_state: "Optional[BettingState]" = None,
        online_summary: Optional[HandSummary] = None,
    ) -> HandReconstructionResult:
        """events 列から hand を再構成し、必要なら online_summary と diff する。

        Args:
            events: 1 hand の EvidenceRecord 列 (start 〜 end 境界の events)。
            initial_state: hand 開始時の ``BettingState``。指定があれば deepcopy して
                使用する (= ``bootstrap_source="initial_state"``)。
            online_summary: online で生成済みの ``HandSummary``。raw bootstrap が
                失敗した場合の fallback (``bootstrap_source="online_summary"``) +
                diff 比較対象として使う。None なら diff は行わない。

        Bootstrap 優先順位 (Phase 4-B):
          1. ``initial_state`` (テスト等で外部注入されたとき)
          2. raw events からの推定 (``_bootstrap_from_events``)
          3. ``online_summary`` からの逆算 (``_bootstrap_from_online_summary``)

        Returns:
            ``HandReconstructionResult``。bootstrap 失敗時は ``summary=None`` で
            ``reason="reconstruction_skipped"`` を返す。``bootstrap_source`` /
            ``bootstrap_meta`` は試行結果を反映する。
        """
        # 時刻順を保証 (evidence log は append-only で順序通りだが防御的に sort)
        events_sorted = sorted(events or [], key=lambda r: float(r.timestamp))

        bs, bootstrap_source, bootstrap_meta = self._bootstrap(
            events_sorted, initial_state, online_summary,
        )
        if bs is None:
            return HandReconstructionResult(
                reason="reconstruction_skipped",
                bootstrap_source=bootstrap_source,
                bootstrap_meta=bootstrap_meta,
            )

        # 遅延 import: integration 層は CLI 起動時に重い依存を引かないため
        from integration.beam_search import BeamEngine
        from integration.observation_model import default_priors

        prior = self._prior or default_priors()
        beam = BeamEngine(K=self._beam_K, prior=prior)
        beam.reset_with_state(bs)

        # ── hand metadata の確定 ─────────────────────────────────────────
        hand_id = int(online_summary.hand_id) if online_summary else 0
        players_info = self._materialize_players_info(online_summary, bs)
        name_by_seat = {int(p["seat"]): str(p.get("name", f"P{p['seat']}"))
                        for p in players_info}
        stack_start_by_seat = {int(p["seat"]): int(p.get("stack_start", 0))
                               for p in players_info}

        # ── 再構成 state ─────────────────────────────────────────────────
        board: list[str] = []
        hole_cards: dict[int, list[str]] = {}
        winner_seat_hint: Optional[int] = None
        reconstructed_actions: list[ActionRecord] = []
        audio_count = 0
        consumed_count = 0

        # SB_POST / BB_POST は bs.start_hand 時に action_history に入っている。
        # それを ActionRecord として再現する (Phase 2-B engine 経路と整合させる)。
        self._inject_blind_post_records(
            bs, hand_id, name_by_seat, stack_start_by_seat, reconstructed_actions,
        )

        # ── events を時刻順に走査 ─────────────────────────────────────────
        for rec in events_sorted:
            if rec.event is None:
                continue

            if rec.kind == "audio" and isinstance(rec.event, AudioEvent):
                audio_count += 1
                ae: AudioEvent = rec.event
                action_name = (ae.action or "").lower()

                if action_name == "winner":
                    extracted = _extract_seat_from_text(ae.raw_text)
                    if extracted is not None:
                        winner_seat_hint = extracted
                    continue
                if action_name in ("new_hand", "showdown", ""):
                    # new_hand: 既に bootstrap 済み / showdown: state は board=5 で river 扱い
                    continue

                if bs.actor_seat is None:
                    continue
                seat = int(bs.actor_seat)

                try:
                    beam.step_audio(ae, seat)
                except Exception:
                    logger.exception(
                        "Reconstructor: beam.step_audio failed (hand=%d seat=%s)",
                        hand_id, seat,
                    )
                    continue

                top = beam.map_action()
                if top is None or top.action is None:
                    continue

                try:
                    bs.update_after_action(seat, top.action, int(top.amount))
                except Exception:
                    logger.exception(
                        "Reconstructor: bs.update_after_action failed "
                        "(hand=%d seat=%d action=%s amount=%d)",
                        hand_id, seat, top.action, int(top.amount),
                    )
                    continue

                consumed_count += 1
                reconstructed_actions.append(ActionRecord(
                    hand_id=hand_id,
                    timestamp=_iso_from_unix(rec.timestamp),
                    street=_current_street(board),
                    seat=seat,
                    player_name=name_by_seat.get(seat, f"P{seat}"),
                    action=str(top.action),
                    amount=int(top.amount),
                    pot_after=sum(bs.player_contrib_hand.values()),
                    stack_after=stack_start_by_seat.get(seat, 0)
                                - bs.player_contrib_hand.get(seat, 0),
                    source={"audio": True, "camera": False, "rfid": False},
                    needs_review=False,
                    confidence=1.0,
                ))

            elif rec.kind == "rfid" and isinstance(rec.event, RFIDEvent):
                self._absorb_rfid(rec.event, bs, board, hole_cards)

        # ── winner filter (beam 終局粒子の絞り込み) ─────────────────────
        # winner_seat_hint は **oracle ではなく終端の補助制約**。粒子集合のうち
        # winner_seat_hint が fold した宇宙を弱める / 削るのみで、settlement の
        # 確定は HandFinalizer に委ねる。詳細はモジュール docstring 参照。
        if winner_seat_hint is not None:
            try:
                beam.apply_winner_filter(winner_seat_hint, final_pot=None)
            except Exception:
                logger.exception(
                    "Reconstructor: beam.apply_winner_filter failed (hand=%d)",
                    hand_id,
                )

        # board の placeholder を除去 (board_index が飛ぶケース対策)
        board_clean = [c for c in board if c]

        # ── revealed_hands を構築 ──────────────────────────────────────
        revealed_hands = [
            RevealedHand(seat=int(s), cards=list(c), source="rfid", observed_at=None)
            for s, c in sorted(hole_cards.items())
            if c
        ]

        # players_info に hole cards を反映 (online から流用した dict を更新)
        for p in players_info:
            seat = int(p["seat"])
            if seat in hole_cards and hole_cards[seat]:
                p["hole_cards"] = list(hole_cards[seat])
                p["hole_cards_source"] = "rfid"

        pot_total = sum(bs.player_contrib_hand.values())

        # ── HandFinalizer で offline summary を構築 ────────────────────
        try:
            summary = self._finalizer.finalize(
                betting_state=bs,
                board=list(board_clean),
                revealed_hands=revealed_hands,
                pot_total=int(pot_total),
                players_info=list(players_info),
                hand_id=hand_id,
                session_id=(online_summary.session_id if online_summary else ""),
                started_at=(online_summary.started_at if online_summary else ""),
                ended_at=(online_summary.ended_at if online_summary else ""),
                blinds=(
                    dict(online_summary.blinds)
                    if online_summary
                    else {"sb": int(bs.sb_amount), "bb": int(bs.bb_amount)}
                ),
                actions=list(reconstructed_actions),
                board_source=(online_summary.board_source if online_summary else ""),
                winner_seat_hint=winner_seat_hint,
            )
        except Exception:
            logger.exception("Reconstructor: HandFinalizer.finalize failed (hand=%d)", hand_id)
            return HandReconstructionResult(
                actions=reconstructed_actions,
                reason="reconstruction_skipped",
                bootstrap_source=bootstrap_source,
                bootstrap_meta=bootstrap_meta,
            )

        # ── diff と needs_review ────────────────────────────────────────
        diff: Optional[dict[str, Any]] = None
        needs_review = False
        patch_proposal: Optional[HandPatchProposal] = None
        if online_summary is not None:
            diff = _compute_diff(online_summary, summary)
            needs_review = diff is not None
            reason = "reconstructed_with_diff" if needs_review else "reconstructed_no_diff"
            # Phase 5-A: diff があれば patch proposal を組み立てる (提案のみ、apply 無し)
            if diff is not None:
                patch_proposal = compute_patch_proposal(
                    hand_id=hand_id,
                    online=online_summary,
                    offline=summary,
                    diff=diff,
                )
        else:
            reason = "reconstructed"

        confidence = (consumed_count / audio_count) if audio_count > 0 else None

        return HandReconstructionResult(
            actions=reconstructed_actions,
            summary=summary,
            needs_review=needs_review,
            reason=reason,
            diff=diff,
            confidence=confidence,
            bootstrap_source=bootstrap_source,
            bootstrap_meta=bootstrap_meta,
            patch_proposal=patch_proposal,
        )

    # ──────────────────────────────────────────────────────────────────────
    # bootstrap
    # ──────────────────────────────────────────────────────────────────────

    def _bootstrap(
        self,
        events_sorted: "list[EvidenceRecord]",
        initial_state: "Optional[BettingState]",
        online_summary: Optional[HandSummary],
    ) -> "tuple[Optional[BettingState], Optional[str], Optional[dict[str, Any]]]":
        """3 段階で BettingState の bootstrap を試みる (Phase 4-B)。

        優先順位:
          1. ``initial_state`` (deepcopy。テスト等で明示注入されたとき)
          2. raw events からの推定 (``_bootstrap_from_events``)
          3. ``online_summary`` からの逆算 (``_bootstrap_from_online_summary``)

        Returns:
            ``(bs, bootstrap_source, bootstrap_meta)``。すべて失敗すると
            ``(None, None, None)``。
        """
        if initial_state is not None:
            return copy.deepcopy(initial_state), "initial_state", None

        # Phase 4-B: raw-only を online_summary より先に試す。これにより live hook
        # から online_summary=None で渡された hand でも reconstruct できる確率が上がる。
        raw_result = self._bootstrap_from_events(events_sorted)
        if raw_result is not None:
            bs, meta = raw_result
            return bs, "raw", meta

        if online_summary is not None:
            bs = self._bootstrap_from_online_summary(online_summary)
            if bs is not None:
                return bs, "online_summary", None

        return None, None, None

    def _bootstrap_from_events(
        self,
        events_sorted: "list[EvidenceRecord]",
    ) -> "Optional[tuple[BettingState, dict[str, Any]]]":
        """Phase 4-B: raw EvidenceRecord 列から BettingState を起こす。

        **Phase 4-B のスコープ (signal source)**:
          この実装は **RFID-centric**。active seats は ``RFID role="seat"`` 観測
          のみから推定する。AudioEvent は seat 情報を持たないため、現時点で
          bootstrap signal にはなっていない (Phase 4-C 以降の signal 強化候補:
          音声 raw_text から「シート N が fold」等の自然言語 seat 抽出、camera
          dependency など)。

        **戦略 (conservative)**:
          - active seats: ``RFID(role="seat", card=非空)`` を観測した seat 集合。
            音声 (AudioEvent) には seat 情報が無いので、現状 RFID 観測が唯一の
            強 signal。**2 seat 未満なら bootstrap 失敗**。
          - blinds amount: コンストラクタの ``default_sb`` / ``default_bb`` から取る
            (audio に SB_POST/BB_POST の seat 情報は無いため raw-only では推定不可)。
            **どちらかが None なら bootstrap 失敗**。
          - button_seat: ``active_seats[0]`` (= 最小 seat 番号) を **deterministic
            seed** として採用する。これは「最も button らしい seat」を確率的に
            推定したものではなく、``BettingState.start_hand`` を起こすために
            確定的に選ぶ値。実際の button が誰だったかは raw からは確定不能なので、
            ``bootstrap_meta["button_inferred"]=True`` で消費側に "truth claim では
            ない" 旨を伝える。
          - SB/BB seat は ``BettingState.start_hand`` 側のルール (HU: BTN=SB、
            non-HU: SB = BTN の左隣) に従って導出される。

        **``bootstrap_meta["confidence"]`` の意味**:
          固定値 ``0.5`` を返すが、これは **fixed heuristic confidence** であって
          calibrated probability ではない (= モデルが計算した posterior でも
          Brier-calibrated な値でもない)。「この heuristic は truth ではない」
          という印 (= 0.5 weight で扱って下さいというメッセージ) に過ぎない。
          signal 強度に応じた動的 confidence は Phase 4-C+ の課題。

        Returns:
            ``(BettingState, meta)`` または ``None`` (失敗時)。``meta`` は
            ``bootstrap_meta`` 用の診断 dict。
        """
        from integration.action_inference import BettingState
        from integration.action_order import compute_blinds

        if self._default_sb is None or self._default_bb is None:
            return None
        if self._default_sb <= 0 or self._default_bb <= 0:
            return None

        seats_with_hole_cards: set[int] = set()
        for rec in events_sorted:
            if rec.kind != "rfid" or not isinstance(rec.event, RFIDEvent):
                continue
            ev: RFIDEvent = rec.event
            if ev.role == "seat" and ev.seat is not None and ev.card:
                seats_with_hole_cards.add(int(ev.seat))

        if len(seats_with_hole_cards) < 2:
            return None

        active_seats = sorted(seats_with_hole_cards)
        # button は deterministic seed: 「最も button らしい」推定ではなく、
        # BettingState.start_hand を起こすために確定的に選ぶ値。実際の button は
        # raw からは確定不能 (bootstrap_meta["button_inferred"]=True でマーク)。
        button_seat = active_seats[0]
        sb_seat, bb_seat = compute_blinds(button_seat, active_seats)

        bs = BettingState()
        try:
            bs.start_hand(
                button_seat=button_seat,
                active_seats=active_seats,
                sb_amount=self._default_sb,
                bb_amount=self._default_bb,
            )
        except Exception:
            logger.exception("Reconstructor: bs.start_hand raw bootstrap failed")
            return None

        meta: dict[str, Any] = {
            "source": "raw",
            "active_seats": list(active_seats),
            "sb_seat": int(sb_seat),
            "bb_seat": int(bb_seat),
            "button_seat": int(button_seat),
            "button_inferred": True,    # truth 推定ではなく deterministic seed である印
            "blinds_inferred": True,    # default_sb/bb 由来 (raw からは推定不可)
            "signals": {
                "rfid_seat_observations": sorted(int(s) for s in seats_with_hole_cards),
            },
            # `confidence` は fixed heuristic confidence。raw bootstrap が truth では
            # ないことの印 (= 0.5 weight で扱って下さいというメッセージ) であって、
            # calibrated probability (posterior / Brier-calibrated 値) ではない。
            # signal 強度に応じた動的計算は Phase 4-C+ の課題。
            "confidence": 0.5,
        }
        return bs, meta

    def _bootstrap_from_online_summary(
        self,
        online_summary: HandSummary,
    ) -> "Optional[BettingState]":
        """``online_summary`` から button / SB / BB を逆算して BettingState を起こす。

        Phase 3 で導入した online-bootstrap-assisted reconstruction の本体。
        Phase 4-B では raw-only が失敗した時の fallback として使う。

        手順:
          - active_seats: stack_start > 0 の seat
          - sb / bb amount: ``online_summary.blinds``
          - button_seat: online_summary.actions の SB_POST seat から逆算
              (HU: BTN = SB、それ以外: BTN = active 内で SB の 1 つ前)
        bootstrap に必要な情報が揃わない場合は ``None`` を返す。
        """
        from integration.action_inference import BettingState

        sb = int((online_summary.blinds or {}).get("sb", 0))
        bb = int((online_summary.blinds or {}).get("bb", 0))
        active = sorted({
            int(p["seat"]) for p in (online_summary.players or [])
            if int(p.get("stack_start", 0)) > 0
        })
        if len(active) < 2:
            return None

        sb_seat: Optional[int] = None
        bb_seat: Optional[int] = None
        for a in (online_summary.actions or []):
            if a.action == "SB_POST" and sb_seat is None:
                sb_seat = int(a.seat)
            elif a.action == "BB_POST" and bb_seat is None:
                bb_seat = int(a.seat)
            if sb_seat is not None and bb_seat is not None:
                break

        if sb_seat is None or sb_seat not in active:
            return None

        if len(active) == 2:
            button_seat = sb_seat  # HU: BTN = SB
        else:
            idx = active.index(sb_seat)
            button_seat = active[(idx - 1) % len(active)]

        bs = BettingState()
        try:
            bs.start_hand(
                button_seat=button_seat,
                active_seats=active,
                sb_amount=sb,
                bb_amount=bb,
            )
        except Exception:
            logger.exception("Reconstructor: bs.start_hand online_summary bootstrap failed")
            return None
        return bs

    # ──────────────────────────────────────────────────────────────────────
    # helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _materialize_players_info(
        online_summary: Optional[HandSummary],
        bs: "BettingState",
    ) -> list[dict]:
        """players_info を online_summary から流用 (deepcopy)、無ければ bs から合成。"""
        if online_summary and online_summary.players:
            return [dict(p) for p in online_summary.players]
        return [
            {
                "seat": int(s),
                "name": f"P{s}",
                "hole_cards": None,
                "hole_cards_source": "",
                "stack_start": 0,
                "stack_end": 0,
                "result": 0,
            }
            for s in (bs.active_seats or [])
        ]

    @staticmethod
    def _inject_blind_post_records(
        bs: "BettingState",
        hand_id: int,
        name_by_seat: dict[int, str],
        stack_start_by_seat: dict[int, int],
        out_actions: list[ActionRecord],
    ) -> None:
        """``bs.start_hand`` で記録された SB_POST / BB_POST を ActionRecord 化。"""
        running_pot = 0
        for entry in (bs.action_history or []):
            label = entry.get("action")
            if label not in ("SB_POST", "BB_POST"):
                continue
            seat = int(entry.get("seat", 0))
            amount = int(entry.get("amount", 0))
            running_pot += amount
            out_actions.append(ActionRecord(
                hand_id=hand_id,
                timestamp="",
                street="preflop",
                seat=seat,
                player_name=name_by_seat.get(seat, f"P{seat}"),
                action=label,
                amount=amount,
                pot_after=running_pot,
                stack_after=stack_start_by_seat.get(seat, 0) - amount,
                source={"audio": False, "camera": False, "rfid": False},
                needs_review=False,
                confidence=1.0,
            ))

    @staticmethod
    def _absorb_rfid(
        ev: RFIDEvent,
        bs: "BettingState",
        board: list[str],
        hole_cards: dict[int, list[str]],
    ) -> None:
        """RFIDEvent を board / hole_cards に蓄積し、street 昇格を発火させる。"""
        if ev.role == "seat" and ev.seat is not None and ev.card:
            cards = hole_cards.setdefault(int(ev.seat), [])
            if ev.card not in cards and len(cards) < 2:
                cards.append(str(ev.card))
            return

        if ev.role == "board" and ev.card:
            if ev.board_index is not None:
                idx = max(0, int(ev.board_index) - 1)
                while len(board) <= idx:
                    board.append("")
                board[idx] = str(ev.card)
            else:
                if ev.card not in board:
                    board.append(str(ev.card))
            new_street = _current_street(board)
            if new_street != "preflop" and getattr(bs, "street", None) != new_street:
                try:
                    bs.reset_for_new_street()
                    bs.street = new_street
                except Exception:
                    logger.exception("Reconstructor: bs.reset_for_new_street failed")
