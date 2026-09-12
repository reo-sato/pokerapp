"""core/hand_correction.py

ADR-0036 (B4): ハンド訂正の append-only オーバーレイ。

音声自動記録の誤認識を、元 hand log を mutate せずに訂正するための **訂正レコード** と、
read 時に元ハンドへ重ねる **オーバーレイ適用関数**を提供する（ledger の append-only 思想を踏襲）。
対象キーは `(session_id, hand_id, action_index)`（アクションは安定 ID を持たないため hand 内の位置）。
hand レベル訂正（winner_seat 等）は `action_index=None`。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

# v1.0 で訂正できるフィールド（ADR-0036 §3）。
ACTION_FIELDS = ("action", "amount")          # action_index あり
HAND_FIELDS = ("winner_seat",)                # action_index = None（hand レベル）


@dataclass
class HandCorrection:
    """1 件の訂正（append-only）。"""

    correction_id: str
    session_id: str
    hand_id: int
    action_index: int | None       # None = hand レベル
    field: str                     # ACTION_FIELDS | HAND_FIELDS
    new_value: Any                 # str（action）/ int（amount, winner_seat）
    corrected_by: str              # スタッフ識別（自由記述、既定 "staff"）
    corrected_at: str              # ISO 8601
    note: str | None = None

    def to_dict(self) -> dict:
        d = {
            "correction_id": self.correction_id,
            "session_id": self.session_id,
            "hand_id": self.hand_id,
            "action_index": self.action_index,
            "field": self.field,
            "new_value": self.new_value,
            "corrected_by": self.corrected_by,
            "corrected_at": self.corrected_at,
        }
        if self.note is not None:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HandCorrection":
        return cls(
            correction_id=d["correction_id"],
            session_id=d["session_id"],
            hand_id=d["hand_id"],
            action_index=d.get("action_index"),
            field=d["field"],
            new_value=d.get("new_value"),
            corrected_by=d.get("corrected_by", "staff"),
            corrected_at=d.get("corrected_at", ""),
            note=d.get("note"),
        )


def apply_hand_corrections(
    hand: dict, corrections: "list[HandCorrection]"
) -> dict:
    """元 hand（dict）に訂正を時系列順（後勝ち）で重ねた**訂正済みビュー**を返す（元は不変）。

    - アクション訂正: `actions[action_index]` の field を new_value に。元値を `_original` に保持し、
      `corrected=true` を立て `needs_review` を解除（訂正済みなので）。
    - hand レベル訂正: hand の field を new_value に。
    - 監査痕として hand に `_corrections`（適用した訂正 dict の列）を残す。
    範囲外 index / 未知 field は安全に無視する（壊れた訂正で read を落とさない）。
    """
    h = deepcopy(hand)
    applied: list[dict] = []
    for c in sorted(corrections, key=lambda c: (c.corrected_at, c.correction_id)):
        if c.action_index is None:
            if c.field in HAND_FIELDS:
                h[c.field] = c.new_value
                applied.append(c.to_dict())
            continue
        actions = h.get("actions") or []
        if not (0 <= c.action_index < len(actions)) or c.field not in ACTION_FIELDS:
            continue
        a = actions[c.action_index]
        orig = a.setdefault("_original", {})
        if c.field not in orig:
            orig[c.field] = a.get(c.field)
        a[c.field] = c.new_value
        a["corrected"] = True
        a["needs_review"] = False
        applied.append(c.to_dict())
    if applied:
        h["_corrections"] = applied
        # hand レベルの review_required も、未解決の needs_review が無ければ下げる。
        if not any(act.get("needs_review") for act in h.get("actions") or []):
            h["review_required"] = False
    return h
