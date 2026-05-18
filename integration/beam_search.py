"""integration/beam_search.py

ベイズ推定アクション推定レイヤ (v6.0+ M3) の Beam Search エンジン。

各粒子 (BeamParticle) = (アクション列, log_weight, BettingState の deep copy)。
1 観測ごとに各粒子から ``infer_action_distribution()`` で候補を列挙し、
``log_weight`` を加算した上で top-K に剪定する決定論的 Beam Search。

学習や確率的サンプリングは M3 ではスコープ外 (``enable_resample=False`` で固定)。
将来 (v6.0+ B4) で同じデータ構造を粒子フィルタへ拡張する。
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.events import AudioEvent
from integration.action_inference import BettingState, infer_action_distribution
from integration.observation_model import (
    NEG_INF,
    ActionHypothesis,
    EvidenceInterval,
    PriorParams,
    default_priors,
    evidence_from_audio,
)

logger = logging.getLogger(__name__)


@dataclass
class BeamParticle:
    """並行宇宙 1 つ分の状態。"""

    actions: list[ActionHypothesis]                # この粒子が辿ったアクション列
    log_weight: float                              # 累積 log 事後 (未正規化)
    state: BettingState = field(default_factory=BettingState)  # 粒子ごとの BettingState

    def clone(self) -> "BeamParticle":
        return BeamParticle(
            actions=list(self.actions),
            log_weight=self.log_weight,
            state=copy.deepcopy(self.state),
        )


class BeamEngine:
    """sequence 事後分布の MAP を Beam Search で近似する。

    使い方:
      ``reset_with_state(base_state)``  # 新ハンド開始時
      ``step_audio(event, actor_seat)``  # AudioEvent 1 つごとに
      ``map_action()``                   # 最新 MAP (リアルタイム速報用)
      ``apply_winner_filter(seat, pot)`` # ハンド終了時、後方修正の MAP 列を返す
    """

    # 各粒子から取り出す候補の最大数 (top-K への剪定前)。
    # K * MAX_BRANCHING までが各 step の中間粒子数の上限。
    MAX_BRANCHING_PER_PARTICLE: int = 4

    def __init__(
        self,
        K: int = 8,
        prior: Optional[PriorParams] = None,
        enable_resample: bool = False,
        sink: Optional[Callable[[EvidenceInterval, list[dict]], None]] = None,
    ) -> None:
        self.K = max(1, int(K))
        self.prior = prior or default_priors()
        self.enable_resample = bool(enable_resample)
        self.sink = sink
        self._particles: list[BeamParticle] = [
            BeamParticle(actions=[], log_weight=0.0, state=BettingState())
        ]

    # ──────────────────────────────────────────────────────────────────────
    # ライフサイクル
    # ──────────────────────────────────────────────────────────────────────

    def reset_with_state(self, state: BettingState) -> None:
        """新ハンド開始時に呼ぶ。全粒子を 1 つの初期粒子に潰す。"""
        self._particles = [
            BeamParticle(actions=[], log_weight=0.0, state=copy.deepcopy(state))
        ]

    # ──────────────────────────────────────────────────────────────────────
    # 1 観測ステップ
    # ──────────────────────────────────────────────────────────────────────

    def step_audio(self, event: AudioEvent, actor_seat: Optional[int]) -> None:
        """1 つの AudioEvent で全粒子を展開・剪定する。"""
        evidence = evidence_from_audio(event)
        candidates: list[BeamParticle] = []

        for p in self._particles:
            hyps = infer_action_distribution(event, p.state, actor_seat, self.prior)
            # 各粒子の中で上位 MAX_BRANCHING_PER_PARTICLE 件まで展開
            branched = 0
            for h in hyps:
                if h.log_likelihood == NEG_INF:
                    continue
                if branched >= self.MAX_BRANCHING_PER_PARTICLE:
                    break
                child = p.clone()
                if h.action:
                    try:
                        child.state.update_after_action(actor_seat, h.action, h.amount)
                    except Exception:
                        # state 適用失敗 → 採用しない (legal_actions と同じ扱い)
                        continue
                child.actions.append(h)
                child.log_weight += h.log_likelihood
                candidates.append(child)
                branched += 1

        if not candidates:
            # 何も追加できなかった: 既存粒子を維持 (極端ケース)
            return

        candidates.sort(key=lambda part: part.log_weight, reverse=True)
        self._particles = candidates[: self.K]

        if self.sink is not None:
            try:
                self.sink(evidence, self.snapshot_top(3))
            except Exception:
                logger.exception("BeamEngine.sink raised")

    # ──────────────────────────────────────────────────────────────────────
    # 読み出し
    # ──────────────────────────────────────────────────────────────────────

    def map_action(self) -> Optional[ActionHypothesis]:
        """top 粒子の最新アクション (リアルタイム MAP)。"""
        top = self._top_particle()
        if top is None or not top.actions:
            return None
        return top.actions[-1]

    def map_sequence(self) -> list[ActionHypothesis]:
        top = self._top_particle()
        return list(top.actions) if top else []

    def particle_count(self) -> int:
        return len(self._particles)

    def snapshot_top(self, n: int = 3) -> list[dict]:
        """top-n particle を辞書化 (evidence_log 用)。"""
        out: list[dict] = []
        for p in self._particles[:n]:
            out.append({
                "log_weight": p.log_weight,
                "actions": [
                    {"action": a.action, "amount": a.amount, "seat": a.seat, "reason": a.reason}
                    for a in p.actions
                ],
            })
        return out

    # ──────────────────────────────────────────────────────────────────────
    # 後方修正
    # ──────────────────────────────────────────────────────────────────────

    def apply_winner_filter(
        self,
        winner_seat: int,
        final_pot: Optional[int] = None,
    ) -> list[ActionHypothesis]:
        """WINNER と整合しない粒子を NEG_INF に落とし、新 MAP の action 列を返す。

        現在の整合性チェック (M3 初版):
          - 「winner_seat が fold した」粒子は不整合 → NEG_INF
          - final_pot が与えられ、粒子終端の pot 推定値と乖離が大きい場合 (>20%) も NEG_INF

        Returns:
            修正後 MAP のアクション列。全粒子が NEG_INF なら空リスト。
        """
        for p in self._particles:
            if any(
                (a.action == "fold" and a.seat == winner_seat)
                for a in p.actions
            ):
                p.log_weight = NEG_INF
                continue
            if final_pot is not None and final_pot > 0:
                # 粒子の累積投入額が観測 pot と大きく乖離していたら不整合とみなす
                particle_pot = sum(
                    a.amount for a in p.actions
                    if a.action in ("bet", "call", "raise", "allin")
                )
                # ±50% の許容 (preflop/postflop で誤差が出るため広め)
                if particle_pot > 0 and (
                    particle_pot < final_pot * 0.5 or particle_pot > final_pot * 2.0
                ):
                    p.log_weight = NEG_INF

        self._particles.sort(key=lambda part: part.log_weight, reverse=True)
        top = self._top_particle()
        if top is None or top.log_weight == NEG_INF:
            return []
        return list(top.actions)

    # ──────────────────────────────────────────────────────────────────────
    # 内部ユーティリティ
    # ──────────────────────────────────────────────────────────────────────

    def _top_particle(self) -> Optional[BeamParticle]:
        if not self._particles:
            return None
        return self._particles[0]
