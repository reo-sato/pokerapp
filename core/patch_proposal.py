"""core/patch_proposal.py

Phase 5-A: online HandSummary と offline HandSummary' の diff から **patch proposal
(修正案)** を構造化して出すための型と helper。

**重要**: Phase 5-A は「提案を構造化する」だけのフェーズ。
  - online JSON / PHH / GameStateManager は自動で書き換えない
  - ``HandPatchProposal.can_patch_automatically`` は Phase 5-A では常に False
    (Phase 5-B 以降で安全条件 + apply ロジックを追加する想定)

Patch proposal の対象フィールド (``PATCHABLE_FIELDS``):
  - ``resolution_type``           — fold_win / showdown / ... のラベル
  - ``seat_payouts``              — seat 別の正味払出 (canonical な勝者情報)
  - ``winner_seat``               — compatibility field の primary winner
  - ``pot_total``                 — 終局時の累積投入額
  - ``showdown_revealed_cards``   — JSON 投影用の hole cards

``actions`` は Phase 5-A スコープ外 (ログ修正は heavy なので別フェーズ)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from core.hand_log import HandSummary


# Phase 5-A: ここに列挙した field だけが patch proposal の対象。
# 他の diff key (例: actions) は無視する。順序は固定 (出力の deterministic 性)。
PATCHABLE_FIELDS: tuple[str, ...] = (
    "resolution_type",
    "seat_payouts",
    "winner_seat",
    "pot_total",
    "showdown_revealed_cards",
)


# field name → note の format テンプレート。
# template.format(online=..., offline=...) で文章化する。
_NOTE_TEMPLATES: dict[str, str] = {
    "resolution_type":         "resolution_type differs (online={online}, offline={offline})",
    "seat_payouts":            "seat_payouts differs (online vs offline settlement)",
    "winner_seat":             "primary winner differs (online={online}, offline={offline})",
    "pot_total":               "total pot differs; offline derived from betting_state",
    "showdown_revealed_cards": "revealed hole cards differ",
}


@dataclass
class FieldPatch:
    """1 フィールド分の修正提案。

    Attributes:
        field:   修正対象のフィールド名 (``PATCHABLE_FIELDS`` のいずれか)
        online:  online HandSummary の現在値
        offline: offline (reconstruct) の値 = 推奨される修正後の値
        note:    operator 向けの短い説明 (オプション)
    """
    field: str
    online: Any
    offline: Any
    note: Optional[str] = None


@dataclass
class HandPatchProposal:
    """1 hand 分の patch proposal (Phase 5-A: 提案のみ、apply はしない)。

    Attributes:
        hand_id:                  対象 hand
        can_patch_automatically:  自動 apply 可否 (Phase 5-A では常に False)
        fields:                   修正候補フィールドのリスト (1 件以上)
        summary_note:             全体の短いまとめ (オプション)
    """
    hand_id: int
    can_patch_automatically: bool = False
    fields: list[FieldPatch] = field(default_factory=list)
    summary_note: Optional[str] = None


def compute_patch_proposal(
    hand_id: int,
    online: "Optional[HandSummary]",
    offline: "Optional[HandSummary]",
    diff: Optional[dict[str, Any]],
) -> Optional[HandPatchProposal]:
    """diff から ``HandPatchProposal`` を組み立てる。

    Args:
        hand_id:  対象 hand
        online:   online HandSummary (Phase 5-A 時点では参照用、note 用に値を読む)
        offline:  offline HandSummary (同上)
        diff:     ``_compute_diff(online, offline)`` の返り値。
                  ``{field_name: {"online": ..., "offline": ...}}`` の形。

    Returns:
        proposal が 1 件でも作れたら ``HandPatchProposal``。
        diff が None / 空、または ``PATCHABLE_FIELDS`` に該当する field が
        含まれない場合は ``None``。
    """
    if not diff or not isinstance(diff, dict):
        return None

    field_patches: list[FieldPatch] = []
    for fname in PATCHABLE_FIELDS:
        if fname not in diff:
            continue
        entry = diff[fname]
        if not isinstance(entry, dict):
            continue
        online_val = entry.get("online")
        offline_val = entry.get("offline")
        template = _NOTE_TEMPLATES.get(fname, f"{fname} differs")
        try:
            note = template.format(online=online_val, offline=offline_val)
        except (KeyError, IndexError, ValueError):
            note = f"{fname} differs"
        field_patches.append(FieldPatch(
            field=fname,
            online=online_val,
            offline=offline_val,
            note=note,
        ))

    if not field_patches:
        return None

    summary_note = _build_summary_note(field_patches)
    return HandPatchProposal(
        hand_id=int(hand_id),
        can_patch_automatically=False,  # Phase 5-A: 提案のみ
        fields=field_patches,
        summary_note=summary_note,
    )


def _build_summary_note(field_patches: list[FieldPatch]) -> str:
    """全体まとめ note を組み立てる。"""
    names = ", ".join(p.field for p in field_patches)
    return f"{names} differ; candidate to update settlement fields"
