"""core/patch_apply.py

Phase 5-G / 5-H: ``HandPatchProposal`` を ``HandSummary`` に対して **安全な field
だけ** 適用する pure helper 群 + 表示用 helper。

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

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


# ────────────────────────────────────────────────────────────────────────────
# Phase 5-H: patch proposal の GUI 表示用 view helper
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class FieldDiffView:
    """Phase 5-H: 1 field 分の patch proposal を GUI 表示用に整形したエントリ。

    Attributes:
        field:          field 名 (e.g., ``"resolution_type"``)
        online_repr:    online (= proposal 生成時の "現状" 値) を表示用に format
                        した文字列
        offline_repr:   offline (= reconstructor が提案する値) の表示用文字列
        is_applicable:  この field が ``PATCH_APPLY_FIELDS`` に含まれるか
                        (= ``apply_patch_proposal_to_summary`` で実際に適用される)。
                        False の field は detail view で skip マーカー付きで表示する
        note:           proposal の ``FieldPatch.note`` (= ``compute_patch_proposal``
                        が ``_NOTE_TEMPLATES`` から format した短い説明)。
                        無い場合は None
    """
    field: str
    online_repr: str
    offline_repr: str
    is_applicable: bool
    note: Optional[str] = None


def _format_value_for_view(value: Any) -> str:
    """Phase 5-H: patch proposal の値を GUI 表示用に文字列化する。

    - None → ``"—"`` (em dash)
    - str → そのまま
    - int / float / bool → ``str(value)``
    - dict → ``"{k1: v1, k2: v2}"`` (int キーは昇順 sort、それ以外は dict 順)
    - list → ``"[v1, v2, v3]"``
    - dataclass / 不明型 → ``repr(value)`` (= フォールバック)

    実装は deterministic (= テストで具体的な文字列を assert できる)。
    """
    if value is None:
        return "—"
    # bool は int の subclass なので先に判定する
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        keys = list(value.keys())
        # int 一様 (= seat_payouts / showdown_revealed_cards) なら昇順 sort で
        # deterministic に。str/mixed のときは挿入順を維持する (= blinds の
        # "sb"/"bb" 順を壊さない)。
        if keys and all(isinstance(k, int) for k in keys):
            keys = sorted(keys)
        parts = [f"{k}: {_format_value_for_view(value[k])}" for k in keys]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_format_value_for_view(x) for x in value) + "]"
    # dataclass / その他は repr() で defensive fallback
    try:
        return repr(value)
    except Exception:
        return f"<{type(value).__name__}>"


def summarize_patch_proposal_for_view(
    summary: "HandSummary | None",
    proposal: "HandPatchProposal | None",
) -> list[FieldDiffView]:
    """Phase 5-H: ``HandPatchProposal`` を ``list[FieldDiffView]`` に投影する。

    GUI の detail view (= "Show patch details" Toplevel) が読む構造化データ。
    pure helper なので ``customtkinter`` / ``tkinter`` には依存しない (= テスト
    容易)。

    動作:
      - proposal が None / fields が空 → ``[]`` を返す
      - 各 ``FieldPatch`` を ``FieldDiffView`` に変換 (online / offline 値を
        ``_format_value_for_view`` で文字列化)
      - ``is_applicable`` = ``field in PATCH_APPLY_FIELDS``
      - field 順は ``proposal.fields`` の順序を維持 (= deterministic 出力)
      - dataclass / dict 両形の proposal に対応 (live hook + JSONL ロード両方)
      - 不正 entry (= field 名が無い等) は skip して残りを処理

    Args:
        summary:  対象 hand の ``HandSummary`` (現状は表示には未使用、
                  将来 "current value" カラム追加時のための前方互換引数)。
                  None でも安全 (= 単に view を生成するだけ)。
        proposal: 表示対象の ``HandPatchProposal`` (dataclass / dict 両形)。

    Returns:
        各 field の ``FieldDiffView`` のリスト (proposal.fields の順)。
    """
    if proposal is None:
        return []
    raw_fields = getattr(proposal, "fields", None)
    if raw_fields is None and isinstance(proposal, dict):
        raw_fields = proposal.get("fields")
    if not raw_fields:
        return []

    views: list[FieldDiffView] = []
    for fp in raw_fields:
        name = getattr(fp, "field", None)
        if name is None and isinstance(fp, dict):
            name = fp.get("field")
        if not isinstance(name, str) or not name:
            continue
        online_val = getattr(fp, "online", None)
        offline_val = getattr(fp, "offline", None)
        if isinstance(fp, dict):
            if online_val is None:
                online_val = fp.get("online")
            if offline_val is None:
                offline_val = fp.get("offline")
        note = getattr(fp, "note", None)
        if note is None and isinstance(fp, dict):
            note = fp.get("note")
        views.append(FieldDiffView(
            field=name,
            online_repr=_format_value_for_view(online_val),
            offline_repr=_format_value_for_view(offline_val),
            is_applicable=name in PATCH_APPLY_FIELDS,
            note=note if isinstance(note, str) and note else None,
        ))
    return views
