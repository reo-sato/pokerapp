"""core/ground_truth.py

ADR-0043: Phase A 計測のための ground truth エントリ。

`logs/{session_id}.ground_truth.json` の 1 ハンド分。schema は
`docs/dogfood/measurement-plan.md` §2.2 に additive 拡張（per-hand に
`annotated_by` / `annotated_at` / `source` を追加。`additionalProperties:true` の
方針に従う）。

`tools/measure_capture_accuracy.py` は本データを直接 parse する。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ✓ 流す（capture verbatim）か ✏ 修正（annotator-edited）かを区別する。
SOURCE_PASSTHROUGH = "captured-passthrough"
SOURCE_EDITED = "manual-edit"
_VALID_SOURCES = frozenset({SOURCE_PASSTHROUGH, SOURCE_EDITED})


@dataclass
class GroundTruthHand:
    """1 ハンド分の ground truth（per-hand annotator metadata 付き）。"""

    hand_id: int
    annotator: str
    annotated_at: str
    source: str
    hand: dict[str, Any]

    def to_dict(self) -> dict:
        """measurement-plan §2.2 の hand エントリ shape にして返す（per-hand metadata を additive で含む）。"""
        d = dict(self.hand)
        d["hand_id"] = self.hand_id
        d["annotator"] = self.annotator
        d["annotated_at"] = self.annotated_at
        d["source"] = self.source
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GroundTruthHand":
        body = {
            k: v for k, v in d.items()
            if k not in ("annotator", "annotated_at", "source")
        }
        return cls(
            hand_id=int(d["hand_id"]),
            annotator=str(d.get("annotator", "")),
            annotated_at=str(d.get("annotated_at", "")),
            source=str(d.get("source", SOURCE_EDITED)),
            hand=body,
        )


class GroundTruthError(Exception):
    """ground truth の不正（code=invalid_amount を再利用）。"""


def validate_source(source: str) -> None:
    if source not in _VALID_SOURCES:
        raise GroundTruthError(
            f"source は {sorted(_VALID_SOURCES)} のいずれかです: {source!r}"
        )


def hand_has_needs_review(captured_hand: dict) -> bool:
    """ADR-0043 §3 (C-2 ガード): `review_required` か任意 action.needs_review が True。

    captured_hand は `api/read_models.py:get_hand()` の戻り値（訂正適用済）を想定。
    訂正後も needs_review が残っているハンドは「✓ 流す」を許さない。
    """
    if bool(captured_hand.get("review_required")):
        return True
    for action in captured_hand.get("actions") or []:
        if isinstance(action, dict) and bool(action.get("needs_review")):
            return True
    return False
