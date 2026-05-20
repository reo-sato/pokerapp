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
from core.patch_proposal import FieldPatch, HandPatchProposal, compute_patch_proposal

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
      patch_applied:     Phase 5-G で追加。GUI / API 経由で
                         ``IntegrationThread.apply_patch_proposal`` が呼ばれて
                         in-memory summary に whitelist field を適用済みなら True。
                         default False で非破壊。
      applied_fields:    Phase 5-G で追加。``patch_applied=True`` のときに
                         実際に適用された field 名のリスト
                         (= ``core.patch_apply.PATCH_APPLY_FIELDS`` の subset)。
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
    # Phase 5-G: GUI から apply された場合の advisory フラグ。
    patch_applied: bool = False
    applied_fields: list[str] = field(default_factory=list)


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


# Phase 5-B+: audio seat hint の出所カテゴリ。
# - "action": fold / call / bet / raise / check / allin など、その seat が hand 中に
#   行動したことを示す mention。RFID と overlap すれば cross-modal corroboration になる
# - "winner": 終局の WINNER 発話 (= 既に winner_seat_hint で扱っている終端制約)
# - "other":  new_hand / showdown / amount_only / 不明 action。active 推定には弱い
_POKER_ACTION_KEYWORDS_FOR_SEAT_HINT = frozenset({
    "fold", "call", "bet", "raise", "check", "allin",
})


def _categorize_audio_seat_hint(action: str) -> str:
    """``AudioEvent.action`` を seat-hint カテゴリにマップする。

    - ``"winner"`` → ``"winner"``
    - 通常の poker action → ``"action"``
    - その他 (new_hand / showdown / amount_only / 空 / 不明) → ``"other"``
    """
    a = (action or "").lower()
    if a == "winner":
        return "winner"
    if a in _POKER_ACTION_KEYWORDS_FOR_SEAT_HINT:
        return "action"
    return "other"


def _compute_raw_bootstrap_confidence(
    *,
    rfid_count: int,
    audio_action_overlap: bool,
    button_inferred_from_prev: bool,
) -> float:
    """Phase 5-B+: raw bootstrap の **operational confidence** を staged で返す。

    依然 calibrated probability ではない (= モデルが計算した posterior でも
    Brier-calibrated な値でもない)。複数 signal が揃えば値が上がる discrete な
    signal-stacker。値域は ``[0.4, 0.7]``。フル確率化は将来課題。

    内訳:
      - baseline 0.4: conservative gate (RFID >= 2 seat) を満たした
      - +0.1 if rfid_count >= 3: multi-seat 観測 (2 seat より信頼度が上)
      - +0.1 if audio_action_overlap: action-derived audio seat が
        RFID seat と overlap = cross-modal corroboration
      - +0.1 if button_inferred_from_prev: button が前 hand の左隣 (history-grounded)
    """
    score = 0.4
    if rfid_count >= 3:
        score += 0.1
    if audio_action_overlap:
        score += 0.1
    if button_inferred_from_prev:
        score += 0.1
    return round(score, 2)   # 0.4 + 0.1 + 0.1 + 0.1 = 0.7 (浮動小数点誤差除去)


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
        # Phase 5-C: ``update_blinds`` が呼ばれたら現在値で上書きする (= session 中の
        # blind level 変更に追従)。``_blinds_updated_at_runtime`` で「constructor の
        # default のままか / runtime に更新されたか」を区別し、bootstrap_meta の
        # ``blind_source`` field に反映する。
        self._default_sb = int(default_sb) if default_sb is not None else None
        self._default_bb = int(default_bb) if default_bb is not None else None
        self._blinds_updated_at_runtime: bool = False

        # Phase 5-B: **直近 successfully bootstrapped hand** の button seat を覚える。
        # 「直前 hand」ではなく「直前 *成功* hand」である点が重要:
        #   - bootstrap 失敗で skipped になった hand は _prev_button_seat を更新しない
        #     (= ``reconstruct_from_events`` の早期 return 経由)
        #   - したがって [成功 hand A → skipped hand B → hand C] の場合、hand C の
        #     raw bootstrap は **hand A の button** を seed に使う (= hand B が skipped
        #     でも button history は失われない)
        # 経路は問わない (raw / online_summary / initial_state のいずれの bootstrap
        # でも、成功時に bs.button_seat を記録する)。
        # 注意: これは truth ではなく next-hand 推定の seed。online で button が手動
        # 補正された場合や、live で seat 構成が大きく変わった場合は次 hand で
        # 誤推定する可能性がある (= 後段 ``_compute_diff`` で actions ズレが現れて
        # ``needs_review`` が立つ前提)。
        self._prev_button_seat: Optional[int] = None

    def update_blinds(self, sb: int, bb: int) -> None:
        """Phase 5-C: session 中の blind level 変更を反映する read/write setter。

        ``IntegrationThread.update_blinds`` から呼ばれることを想定 (= GUI 操作起点)。
        次回 ``reconstruct_from_events`` から **current_state** の blind として
        ``bootstrap_meta["blind_source"]`` に記録される。

        Args:
            sb: 新しい small blind 金額 (>0)
            bb: 新しい big blind 金額 (>0)

        不正値 (TypeError / 0 以下) は黙殺する (= 呼び側でバリデーション済み想定、
        ここで例外を投げると IntegrationThread の callback で困るため)。
        """
        try:
            sb_i = int(sb)
            bb_i = int(bb)
        except (TypeError, ValueError):
            return
        if sb_i <= 0 or bb_i <= 0:
            return
        self._default_sb = sb_i
        self._default_bb = bb_i
        self._blinds_updated_at_runtime = True

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

        # Phase 5-B: 次 hand の raw bootstrap で使う prev_button を更新する。
        # **「直近 successfully bootstrapped hand の seed」** という意味論を維持する
        # ため、bootstrap 失敗 (bs=None) で抜けた場合は更新せず、ここまで来た成功
        # ケースだけで上書きする。経路は問わない (raw / online_summary / initial_state
        # のいずれでも bs.button_seat は確定している)。
        # 実際の button は truth ではないので「次 hand 推定の seed」として扱うのみ。
        # online で button が手動補正された場合に推定が外れることは想定内
        # (= 後段の _compute_diff で actions ズレが現れる前提)。
        try:
            prev_btn = getattr(bs, "button_seat", None)
            if prev_btn is not None:
                self._prev_button_seat = int(prev_btn)
        except (TypeError, ValueError):
            pass

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

        result = HandReconstructionResult(
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
        # Phase 5-D: blind state mismatch を検出して advisory を強化する。
        # online_summary.blinds と bootstrap_meta.sb_amount/bb_amount のズレ、
        # および ``update_blinds`` 後も ``blind_source=="session_default"`` のままに
        # なっている case を検出。検出時は result を mutate (needs_review / reason /
        # patch_proposal)。online JSON / PHH / settlement には触らない。
        self._apply_blind_mismatch_advisory(result, online_summary)
        return result

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
        """Phase 4-B/5-B: raw EvidenceRecord 列から BettingState を起こす。

        **Phase 5-B での signal source 拡張** (Phase 4-B は RFID のみだった):
          - **primary signal (gate)**: ``RFID role="seat"`` で hole card を観測した
            seat。**2 seat 未満なら raw bootstrap は失敗** (= conservative gate)。
            Audio で seat ヒントだけ拾えても hole card 不在なら raw は諦める。
          - **補助 signal**: Audio 由来の seat ヒント (``audio_seat_hints``)。
            - ``AudioEvent.seat`` が明示的に int で入っていれば採用 (将来の event
              拡張に備えた前向き互換)
            - 加えて ``AudioEvent.raw_text`` から ``_extract_seat_from_text``
              (``シートN`` / ``seatN``) で抽出した seat も採用
            これらは **active_seats を増やす方向にのみ** 使う (RFID で 2 seat 観測
            済みの状況で「もう 1 seat も参加していた」を補完)。
          - **prev_button 補助**: 前 hand で raw / online_summary / initial_state
            のいずれの経路で立ち上がった ``bs.button_seat`` を覚えておき、現 hand の
            active set にその seat が含まれていれば「左隣の seat」を button として
            採用する。これがライブポーカーの実際の button 進行ルール (左回り) に
            一致する。``bootstrap_meta["button_inferred_from_prev"]`` で消費側に
            「prev 由来か / min(active_seats) fallback か」を伝える。
          - blinds amount: ``default_sb`` / ``default_bb`` 経由のまま (Phase 4-B
            互換)。**どちらかが None なら raw bootstrap 失敗**。
          - blinds 変更 TODO: session 中に blinds level が上がるケースは現状未対応。
            ``bootstrap_meta["sb_amount"]`` / ``["bb_amount"]`` に値を埋めてフックを
            残しておくので、Phase 5-C 以降で「actual blinds と meta を突き合わせて
            mismatch を検出」する経路が作れる。

        **依然 conservative**: Phase 4-B で raw が失敗したケース (RFID 0/1 seat、
        default blinds 未設定) は Phase 5-B でも raw 失敗のまま (=
        ``online_summary`` fallback or skipped に倒す)。

        Returns:
            ``(BettingState, meta)`` または ``None`` (失敗時)。
        """
        from integration.action_inference import BettingState
        from integration.action_order import compute_blinds

        if self._default_sb is None or self._default_bb is None:
            return None
        if self._default_sb <= 0 or self._default_bb <= 0:
            return None

        seats_with_hole_cards: set[int] = set()
        # Phase 5-B+: audio seat hint を出所別に集計する。
        # - action: その seat が hand 中に実行動 (fold/call/...) したと音声が示唆 → 強い
        # - winner: 終局 WINNER 発話の seat → live 推定としては弱い (誤認識耐性)
        # - other:  new_hand / showdown / amount_only など → 弱い
        # `audio_seat_hints` (flat) は backwards compat のため union を sorted で残す。
        audio_action_seats: set[int] = set()
        audio_winner_seats: set[int] = set()
        audio_other_seats: set[int] = set()
        for rec in events_sorted:
            if rec.kind == "rfid" and isinstance(rec.event, RFIDEvent):
                ev_rfid: RFIDEvent = rec.event
                if ev_rfid.role == "seat" and ev_rfid.seat is not None and ev_rfid.card:
                    seats_with_hole_cards.add(int(ev_rfid.seat))
                continue
            if rec.kind == "audio" and isinstance(rec.event, AudioEvent):
                ev_audio: AudioEvent = rec.event
                # seat 抽出: 明示属性 (将来拡張) > raw_text 解析。両方無ければ skip。
                explicit_seat = getattr(ev_audio, "seat", None)
                if isinstance(explicit_seat, int):
                    seat = int(explicit_seat)
                else:
                    text_seat = _extract_seat_from_text(getattr(ev_audio, "raw_text", "") or "")
                    if text_seat is None:
                        continue
                    seat = int(text_seat)
                category = _categorize_audio_seat_hint(getattr(ev_audio, "action", "") or "")
                if category == "action":
                    audio_action_seats.add(seat)
                elif category == "winner":
                    audio_winner_seats.add(seat)
                else:
                    audio_other_seats.add(seat)

        # Phase 5-B の conservative gate: RFID は依然 primary signal。
        # Audio ヒントだけで bootstrap には踏み込まない (= 誤検知より skip を優先)。
        if len(seats_with_hole_cards) < 2:
            return None

        # active_seats = RFID ∪ Audio (Phase 5-B 拡張)。
        # 音声で言及されたが RFID 未観測の seat も参加とみなす (= active を増やす方向)。
        # winner/other 含めて union しておく (gate は RFID 主導なので overshoot は限定的)。
        audio_all_seats = audio_action_seats | audio_winner_seats | audio_other_seats
        active_seats = sorted(seats_with_hole_cards | audio_all_seats)

        # button heuristic:
        #   - prev_button が active set に含まれていれば「ring 上で左隣 (= 次 index)」
        #     を採用 (button は左回りで進む、というライブの基本ルールに沿う)
        #   - そうでなければ最小 seat 番号 (Phase 4-B 互換、deterministic seed)
        button_inferred_from_prev = False
        prev = self._prev_button_seat
        if prev is not None and prev in active_seats:
            idx = active_seats.index(int(prev))
            button_seat = active_seats[(idx + 1) % len(active_seats)]
            button_inferred_from_prev = True
        else:
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

        # Phase 5-B+: staged operational confidence (依然 calibrated probability ではない)。
        # action-derived audio が RFID と overlap した時のみ cross-modal boost を入れる
        # (winner-derived は active 推定としては弱いので confidence boost には含めない)。
        audio_action_overlap = bool(audio_action_seats & seats_with_hole_cards)
        confidence = _compute_raw_bootstrap_confidence(
            rfid_count=len(seats_with_hole_cards),
            audio_action_overlap=audio_action_overlap,
            button_inferred_from_prev=button_inferred_from_prev,
        )

        meta: dict[str, Any] = {
            "source": "raw",
            "active_seats": list(active_seats),
            "sb_seat": int(sb_seat),
            "bb_seat": int(bb_seat),
            "button_seat": int(button_seat),
            # truth claim ではない (deterministic seed or prev 由来) 旨の印。
            # Phase 4-B から維持。
            "button_inferred": True,
            # Phase 5-B: prev_button から左隣を採用した場合のみ True。
            # False のときは min(active_seats) fallback (Phase 4-B と同等)。
            "button_inferred_from_prev": button_inferred_from_prev,
            "prev_button": int(prev) if prev is not None else None,
            "blinds_inferred": True,    # default_sb/bb 由来 (raw からは推定不可)
            # Phase 5-B TODO フック: blinds level 変更検出のため amount を残す。
            "sb_amount": int(self._default_sb),
            "bb_amount": int(self._default_bb),
            # Phase 5-C: 使った blind の出所。
            #   "current_state"   = ``update_blinds`` で runtime 更新済の現在 state
            #   "session_default" = constructor 渡しの初期 default のまま
            # consumer はこれで「その hand の blinds は古い default か現在 state か」を
            # 区別できる (blinds level 変更を跨いだ古い hand の再構成 = "session_default"
            # のまま、変更後の hand = "current_state")。
            "blind_source": (
                "current_state" if self._blinds_updated_at_runtime else "session_default"
            ),
            "signals": {
                "rfid_seat_observations": sorted(int(s) for s in seats_with_hole_cards),
                # Phase 5-B: audio raw_text から抽出した seat 集合 (union, flat list)。
                # backwards compat: Phase 5-B 初版の consumer はこの flat list を読む。
                "audio_seat_hints": sorted(int(s) for s in audio_all_seats),
                # Phase 5-B+: 出所カテゴリ別の細分化。"action" だけが confidence boost
                # の対象 (winner / other は active 推定として弱い signal)。
                "audio_seat_hint_sources": {
                    "action": sorted(int(s) for s in audio_action_seats),
                    "winner": sorted(int(s) for s in audio_winner_seats),
                    "other":  sorted(int(s) for s in audio_other_seats),
                },
            },
            # Phase 5-B+: staged operational confidence。calibrated probability ではない
            # (= モデル posterior でも Brier-calibrated でもない)。
            # 値域 [0.4, 0.7]、内訳は ``_compute_raw_bootstrap_confidence`` の docstring 参照。
            "confidence": confidence,
        }
        return bs, meta

    # ──────────────────────────────────────────────────────────────────────
    # Phase 5-D: blind mismatch advisory
    # ──────────────────────────────────────────────────────────────────────

    def _apply_blind_mismatch_advisory(
        self,
        result: HandReconstructionResult,
        online_summary: Optional[HandSummary],
    ) -> None:
        """Phase 5-D: blind state の mismatch を検出して ``result`` を mutate する。

        **検出する mismatch パターン**:

        (A) **propagation health check** (= update_blinds が呼ばれたのに meta は
            session_default のまま):
              ``self._blinds_updated_at_runtime is True`` かつ
              ``bootstrap_meta.get("blind_source") == "session_default"``
              通常の Phase 5-C 配線が正しく動いていれば発生しない。defensive な
              consistency check として残し、もし fire したら propagation 経路が
              壊れていることを operator に通知する。

        (B) **amount mismatch** (= reconstructor が古い / 違う blind で raw bootstrap
            している):
              ``online_summary.blinds`` (canonical) と ``bootstrap_meta.sb_amount`` /
              ``["bb_amount"]`` がズレている。典型: blind 変更後に過去 hand を
              reconstruct し直すと、過去の online は旧 blind で書かれているのに
              reconstructor は新 blind を使っているのでズレる。

        **検出時の result への反映** (online JSON / PHH には触らない):
          - ``result.needs_review = True``
          - ``result.reason`` が ``"reconstructed_no_diff"`` / ``"reconstructed"``
            なら ``"reconstructed_with_blind_mismatch"`` に昇格
            (``"reconstructed_with_diff"`` の場合はそのまま、summary_note に追記)
          - (B) のとき ``result.patch_proposal`` に
            ``FieldPatch(field="blinds", online=..., offline=...)`` を append
            (proposal が無ければ blind だけの proposal を新規作成)
          - ``patch_proposal.summary_note`` に blind mismatch の情報を追記

        いずれの場合も **patch apply はしない** (= Phase 5-A 約束を維持、
        ``can_patch_automatically=False`` のまま)。
        """
        meta = result.bootstrap_meta or {}
        issues: list[str] = []
        blind_patch: Optional[FieldPatch] = None

        # ── Pattern (A): propagation health check ───────────────────────────
        if (
            self._blinds_updated_at_runtime
            and meta.get("blind_source") == "session_default"
        ):
            issues.append("blinds_session_default_after_update")

        # ── Pattern (B): canonical (online.blinds) と meta amounts のズレ ───
        meta_sb = meta.get("sb_amount")
        meta_bb = meta.get("bb_amount")
        if online_summary is not None and meta_sb is not None and meta_bb is not None:
            online_blinds = getattr(online_summary, "blinds", None) or {}
            online_sb_raw = online_blinds.get("sb")
            online_bb_raw = online_blinds.get("bb")
            if online_sb_raw is not None and online_bb_raw is not None:
                try:
                    online_sb = int(online_sb_raw)
                    online_bb = int(online_bb_raw)
                    meta_sb_i = int(meta_sb)
                    meta_bb_i = int(meta_bb)
                    if online_sb != meta_sb_i or online_bb != meta_bb_i:
                        issues.append("blind_amount_mismatch")
                        blind_patch = FieldPatch(
                            field="blinds",
                            online={"sb": online_sb, "bb": online_bb},
                            offline={"sb": meta_sb_i, "bb": meta_bb_i},
                            note=(
                                f"blind amounts differ "
                                f"(online sb/bb={online_sb}/{online_bb}, "
                                f"reconstructor sb/bb={meta_sb_i}/{meta_bb_i})"
                            ),
                        )
                except (TypeError, ValueError):
                    pass

        if not issues:
            return

        # ── result の advisory を強化 ─────────────────────────────────────
        result.needs_review = True

        # reason 昇格: 既存 reason が "更なる diff 無し" 系のときだけ
        # "reconstructed_with_blind_mismatch" に上げる。settlement diff が既にある
        # 場合 ("reconstructed_with_diff") はそのまま (= summary_note で補足する)。
        if result.reason in ("reconstructed_no_diff", "reconstructed"):
            result.reason = "reconstructed_with_blind_mismatch"

        # patch_proposal の summary_note 追記 + (B のとき) blind FieldPatch 追加
        blind_note = "blind mismatch: " + ", ".join(issues)
        if result.patch_proposal is None:
            # 既存 proposal が無い場合: blind mismatch だけの proposal を新規作成
            # (= operator が "blinds だけ怪しい hand" を見つけられるようにする)
            hand_id_value = 0
            if online_summary is not None:
                try:
                    hand_id_value = int(getattr(online_summary, "hand_id", 0))
                except (TypeError, ValueError):
                    hand_id_value = 0
            fields_list = [blind_patch] if blind_patch is not None else []
            result.patch_proposal = HandPatchProposal(
                hand_id=hand_id_value,
                can_patch_automatically=False,   # Phase 5-D も apply はしない
                fields=fields_list,
                summary_note=blind_note,
            )
        else:
            if blind_patch is not None:
                result.patch_proposal.fields.append(blind_patch)
            existing = result.patch_proposal.summary_note or ""
            result.patch_proposal.summary_note = (
                f"{existing}; {blind_note}" if existing else blind_note
            )

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
