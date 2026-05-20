"""core/patch_apply.py

Phase 5-G: ``HandPatchProposal`` を ``HandSummary`` に対して **安全な field
だけ** 適用する pure helper 群。

**重要な約束** (Phase 5-G):
  - online JSON / PHH / GameStateManager / settlement / live BettingState は
    **一切触らない** (= ここの helper は HandSummary を copy → field 上書き →
    返すだけ)
  - 元の summary は **mutate しない** (= caller が safe に "before/after" を比較できる)
  - whitelist (``PATCH_APPLY_FIELDS``) 外の field は **無視** する (proposal に
    含まれていても黙って捨てる)
  - apply 後の summary を JsonWriter で永続化することは Phase 5-G では **しない**
    (in-memory の advisory correction のみ)

**whitelist の選定理由**:
  - ``resolution_type`` / ``seat_payouts`` / ``pots`` / ``showdown_revealed_cards``:
    settlement 系の中でも canonical な terminal state 表現。online ⇄ offline
    の差分があった hand では offline 側 (= reconstructor の結果) を採用しても
    rake / 各 seat の収支等の運用上の正しさが回復する
  - ``blinds``: Phase 5-D の advisory で追加された field。レベル変更後 replay
    した過去 hand の blind を訂正できる
  - 除外:
      * ``winner_seat`` / ``pot_total``: compatibility field / 集計値であり、
        ``seat_payouts`` の整合性が取れていれば派生で再計算できる。直接 apply
        すると ``seat_payouts`` と矛盾するリスクがある
      * ``actions``: 重い修正 + 既存 ActionRecord に副作用のリスク
      * ``players`` / ``stacks`` / ``hand_id`` / ``session_id`` / timestamps:
        運用 metadata であり patch スコープ外
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.hand_log import HandSummary
    from core.patch_proposal import HandPatchProposal


# Phase 5-G: GUI / API 経由で apply 可能な field の whitelist。
# 将来拡張する場合は慎重に: 各 field が独立して上書き可能か、依存 field との
# 整合性チェック (e.g., ``seat_payouts`` と ``pots`` の同時 apply) も必要。
PATCH_APPLY_FIELDS: frozenset[str] = frozenset({
    "resolution_type",
    "seat_payouts",
    "pots",
    "showdown_revealed_cards",
    "blinds",
})


def applicable_patch_fields(proposal: "HandPatchProposal | None") -> list[str]:
    """``proposal.fields`` のうち ``PATCH_APPLY_FIELDS`` に含まれる field 名を返す。

    apply ロジックを呼ぶ前の「適用可能 field が 1 つでもあるか」判定用。
    順序は ``proposal.fields`` の元順を維持する (= deterministic)。

    proposal が None / fields が無い / dict 形 (JSONL から読み戻し) でも安全に動く。
    """
    if proposal is None:
        return []
    raw_fields = getattr(proposal, "fields", None)
    if raw_fields is None and isinstance(proposal, dict):
        raw_fields = proposal.get("fields")
    if not raw_fields:
        return []
    names: list[str] = []
    for fp in raw_fields:
        name = getattr(fp, "field", None)
        if name is None and isinstance(fp, dict):
            name = fp.get("field")
        if isinstance(name, str) and name in PATCH_APPLY_FIELDS:
            names.append(name)
    return names


def apply_patch_proposal_to_summary(
    summary: "HandSummary",
    proposal: "HandPatchProposal | None",
) -> "HandSummary":
    """``proposal`` の whitelist field のみを ``summary`` に適用した **新オブジェクト** を返す。

    - 元の ``summary`` は **mutate しない** (deepcopy で出発)
    - ``proposal.fields`` のうち ``PATCH_APPLY_FIELDS`` に該当しないものは **無視** する
      (e.g., ``winner_seat`` / ``pot_total`` / ``actions``)
    - ``offline`` 値は ``deepcopy`` してから set する (proposal と summary 間で
      mutable state を共有しないため)
    - ``seat_payouts`` / ``showdown_revealed_cards`` の dict 形 field では
      **int key 正規化** を行う (JSON round-trip で str 化される可能性に備える)
    - proposal が None や形が壊れていても安全に summary の deepcopy を返す
    """
    patched = copy.deepcopy(summary)
    if proposal is None:
        return patched
    raw_fields = getattr(proposal, "fields", None)
    if raw_fields is None and isinstance(proposal, dict):
        raw_fields = proposal.get("fields")
    if not raw_fields:
        return patched

    for fp in raw_fields:
        name = getattr(fp, "field", None)
        if name is None and isinstance(fp, dict):
            name = fp.get("field")
        if not isinstance(name, str) or name not in PATCH_APPLY_FIELDS:
            continue
        offline_val = getattr(fp, "offline", None)
        if offline_val is None and isinstance(fp, dict):
            offline_val = fp.get("offline")
        new_val: Any
        try:
            if name == "seat_payouts":
                if not isinstance(offline_val, dict):
                    continue
                new_val = {int(k): int(v) for k, v in offline_val.items()}
            elif name == "showdown_revealed_cards":
                if not isinstance(offline_val, dict):
                    continue
                new_val = {int(k): list(v) for k, v in offline_val.items()}
            elif name == "blinds":
                if not isinstance(offline_val, dict):
                    continue
                # {"sb": int, "bb": int} は shallow copy で十分
                new_val = dict(offline_val)
            else:
                # resolution_type (str) / pots (list[PotSettlement]) は deepcopy
                new_val = copy.deepcopy(offline_val)
        except (TypeError, ValueError):
            # 型が壊れた entry は skip (= apply しない)
            continue
        try:
            setattr(patched, name, new_val)
        except AttributeError:
            # HandSummary 側に該当属性が無い (= 互換性チェック失敗) は skip
            continue
    return patched
