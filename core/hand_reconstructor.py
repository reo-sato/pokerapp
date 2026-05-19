"""core/hand_reconstructor.py

Phase 2-C: hand window 単位の **遡及的 (retrospective) 再推定** のための skeleton。

役割 (Phase 3 以降で本実装):
  1 ハンドの全イベント (EvidenceRecord 列) を受け取り、既存の online 推定系
  (BettingState / infer_action / BeamEngine / HandFinalizer) を「まとめて再生」
  することで、online では決定できなかった ambiguous なアクション列を
  hand 全体の context (winner / showdown / final pot / RFID 区間) で再評価する。

設計の意図 (docstring に明記):
  - Phase 2-C ではこのクラスは **skeleton** で、実装は ``reconstruct_from_events``
    が呼び出し可能な hook としてだけ存在する
  - 既存 online 経路 (IntegrationThread._finalize_hand → HandFinalizer) は本クラス
    に依存しない。本クラスは「window が確定したあとに optional に走る診断 /
    自動修正パス」として設計
  - Phase 3+ で:
    * BeamEngine を頭から再生して sequence MAP を再評価
    * 当該 hand window 中の WINNER / 最終 pot / showdown hole cards を
      apply_winner_filter で flagged constraint として食わせる
    * HandFinalizer に通して新 ``HandSummary`` を生成
    * online で出した summary との diff を取り、needs_review / 自動 patch を判断
  - hand_id ごとに ``HandReconstructionResult`` を返す

Phase 2-C では中身は ``NotImplementedError`` ベースの stub だが、IntegrationThread
の終局フローから ``reconstructor.reconstruct_from_events(events)`` を呼べる経路
だけ通しておく (実装は Phase 3 以降)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from core.hand_log import ActionRecord, HandSummary

if TYPE_CHECKING:
    from integration.action_inference import BettingState
    from output.replay_hand import EvidenceRecord


@dataclass
class HandReconstructionResult:
    """retrospective inference の結果。

    fields:
      actions:       再評価後のアクション列 (Phase 3+ で beam の MAP 等から復元)
      summary:       再評価で得られた HandSummary (online と差し替え候補)
      needs_review:  online と diff があった / 信頼度が低かった等で人手レビューを推奨
      reason:        分類タグ (例: ``"online_consistent"``, ``"action_revised"``,
                     ``"settlement_mismatch"``, ``"reconstruction_skipped"``)
    """

    actions: list[ActionRecord] = field(default_factory=list)
    summary: Optional[HandSummary] = None
    needs_review: bool = False
    reason: str = ""


class HandReconstructor:
    """hand window 単位で観測列を頭から再生して action / HandSummary を再推定する。

    Phase 2-C: skeleton。``reconstruct_from_events`` は **dummy pass-through** で
    「再評価をスキップした」結果を返す。これにより IntegrationThread から呼ぶ
    経路 (hook) は安全に通せる。

    Phase 3+ の予定実装 (TODO):
      1. ``initial_state`` から BettingState を初期化 (なければ events から推測)
      2. ``BeamEngine(K=8, prior=default_priors())`` を新規構築
      3. events を時刻順に走査して:
         - AudioEvent → infer_action_distribution + beam.step_audio
         - RFIDEvent  → fold-on-release / showdown reveal の制約として食わせる
         - CameraEvent → (将来) chip motion から bet size の弱い prior
      4. window 終端の winner audio / 最終 pot / showdown hole cards を
         ``BeamEngine.apply_winner_filter(...)`` に投入し sequence MAP を絞り込む
      5. 結果を ``HandFinalizer.finalize(...)`` で HandSummary に組み立てる
      6. online 出力との diff があれば needs_review=True を立てる
    """

    def reconstruct_from_events(
        self,
        events: "list[EvidenceRecord]",
        initial_state: "Optional[BettingState]" = None,
    ) -> HandReconstructionResult:
        """events 列から hand を再構成する。

        Phase 2-C: dummy pass-through (online 経路を変えない / Phase 3 で本実装)。

        Args:
            events: 単一 hand の EvidenceRecord 列 (boundary 始点〜終点)。
            initial_state: hand 開始時の BettingState。None なら events から推測
                (Phase 3 で実装)。

        Returns:
            ``reason="reconstruction_skipped"`` の空の HandReconstructionResult。
            これにより呼び出し側 (IntegrationThread) は安全に hook を持てる。
        """
        # Phase 2-C: skeleton — online 推定結果に介入しない。
        # Phase 3+: ここに BeamEngine 再生 + HandFinalizer 呼び出しを実装する。
        return HandReconstructionResult(
            actions=[],
            summary=None,
            needs_review=False,
            reason="reconstruction_skipped",
        )
