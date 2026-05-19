"""gui/reconstruction_badges.py

Phase 4-C2: ``HandReconstructionResult`` を GUI 表示用の小さな状態
(``ReconstructionBadgeState``) に投影する pure helper。

このモジュールは ``customtkinter`` / ``tkinter`` に依存しないので、GUI 起動
不要でユニットテストできる。badge の判定ルールは Phase 4-C1 CLI
(``output.inspect_reconstruction``) と統一する。

優先順位 (label 判定):
  1. ``result`` が None、または ``summary`` が None、または
     ``reason == "reconstruction_skipped"`` → ``status="skipped"``
  2. ``needs_review`` truthy、または ``reason == "reconstructed_with_diff"``
     → ``status="review"``
  3. それ以外 → ``status="ok"``

RAW バッジ:
  ``bootstrap_source == "raw"`` のときだけ ``show_raw_badge=True``。
  Phase 4-B raw bootstrap が成立した hand を一目で見分けるため。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.hand_reconstructor import HandReconstructionResult


# 色は GUI のデザインシステムに合わせる。既存 _log_box の "review" tag が
# #FF5252 を使っているので review badge も同色にして視覚的に揃える。
BADGE_COLOR_REVIEW = "#FF5252"     # warning red
BADGE_COLOR_SKIPPED = "#888888"    # neutral grey
BADGE_COLOR_RAW = "#4DB6AC"        # muted teal accent
BADGE_COLOR_OK = "#A0D468"         # subtle green


@dataclass
class ReconstructionBadgeState:
    """GUI 表示用の advisory 要約。

    GUI コードは ``HandReconstructionResult`` を直接読まずこの dataclass を
    介して状態を取り出す (= ロジックと描画を分離)。
    """

    status: str                                  # "ok" | "review" | "skipped"
    show_raw_badge: bool = False
    reason: Optional[str] = None
    bootstrap_source: Optional[str] = None
    diff_fields: list[str] = field(default_factory=list)
    button_inferred: bool = False


def summarize_reconstruction(
    result: "Optional[HandReconstructionResult]",
) -> ReconstructionBadgeState:
    """``HandReconstructionResult`` を ``ReconstructionBadgeState`` に投影する。

    Args:
        result: IntegrationThread が保持する advisory 結果。Phase 4-A の
            ``_last_reconstruction_by_hand_id[hand_id]`` から取り出すか、
            CLI の reconstruct JSONL を読んだ後に組み立ててもよい。
            None なら "skipped" 扱い。

    Returns:
        GUI が表示するためのフラット dataclass。
    """
    if result is None:
        return ReconstructionBadgeState(status="skipped")

    reason = getattr(result, "reason", "") or ""
    summary = getattr(result, "summary", None)
    if summary is None or reason == "reconstruction_skipped":
        return ReconstructionBadgeState(
            status="skipped",
            reason=reason or None,
            bootstrap_source=getattr(result, "bootstrap_source", None),
        )

    raw_diff = getattr(result, "diff", None)
    if isinstance(raw_diff, dict):
        diff_fields = sorted(raw_diff.keys())
    else:
        diff_fields = []

    needs_review = bool(getattr(result, "needs_review", False))
    if needs_review or reason == "reconstructed_with_diff":
        status = "review"
    else:
        status = "ok"

    bootstrap_source = getattr(result, "bootstrap_source", None)
    meta = getattr(result, "bootstrap_meta", None) or {}
    button_inferred = bool(meta.get("button_inferred"))

    return ReconstructionBadgeState(
        status=status,
        show_raw_badge=(bootstrap_source == "raw"),
        reason=reason or None,
        bootstrap_source=bootstrap_source,
        diff_fields=diff_fields,
        button_inferred=button_inferred,
    )


def format_history_line(
    hand_id: int,
    winner_seat: Optional[int],
    pot_total: Optional[int],
    badge_state: ReconstructionBadgeState,
) -> str:
    """ハンド履歴パネル 1 行のテキスト整形 (Phase 4-C1 CLI と同じ語彙)。

    出力例:
        ``#1  winner=seat1  pot=300  [OK]  bootstrap=online_summary``
        ``#2  winner=seat2  pot=600  [REVIEW] [RAW]  bootstrap=raw  diff=resolution_type,seat_payouts``
        ``#3  ?  pot=?  [SKIPPED]  bootstrap=None``
    """
    label_map = {"ok": "[OK]", "review": "[REVIEW]", "skipped": "[SKIPPED]"}
    label = label_map.get(badge_state.status, "[?]")

    parts = [f"#{hand_id}"]
    if winner_seat is not None:
        parts.append(f"winner=seat{int(winner_seat)}")
    else:
        parts.append("winner=?")
    if pot_total is not None:
        parts.append(f"pot={int(pot_total):,}")
    else:
        parts.append("pot=?")
    parts.append(label)
    if badge_state.show_raw_badge:
        parts.append("[RAW]")
    parts.append(f"bootstrap={badge_state.bootstrap_source}")
    if badge_state.diff_fields:
        parts.append(f"diff={','.join(badge_state.diff_fields)}")
    if badge_state.button_inferred:
        parts.append("(button inferred)")
    return "  ".join(parts)
