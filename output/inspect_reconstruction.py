"""output/inspect_reconstruction.py

Phase 4-C1: ``output.reconstruct_session`` が出力した
``logs/reconstruct_<session>.jsonl`` を読んで、hand 単位に::

    hand 1 [OK] bootstrap=online_summary reason=reconstructed_no_diff
    hand 2 [REVIEW] bootstrap=raw reason=reconstructed_with_diff diff_fields=resolution_type,seat_payouts
    hand 3 [SKIPPED] bootstrap=None reason=reconstruction_skipped

の形で標準出力に一覧表示する read-only CLI。

このツールは **online JSON / PHH / GameStateManager / online_summary に一切
触らない**。`reconstruct_session` が吐いた JSONL を後から読むだけ。

使い方::

    python -m output.inspect_reconstruction --reconstruct logs/reconstruct_session_xxx.jsonl

オプション:
    --only-needs-review        ``needs_review=true`` の hand のみ表示
    --fields field1,field2     diff のうち指定 field のみを ``diff_fields=`` に出す
    --show-patches             Phase 5-A: hand の patch proposal を ``  PATCH: ...``
                               行で続けて表示する (proposal が無い hand には何も追加しない)
    --quiet                    info ログを抑制

ステータスラベル:
    [OK]      ``needs_review=False`` かつ summary がある (= reconstructed_no_diff)
    [REVIEW]  ``needs_review=True`` (典型: reconstructed_with_diff)
    [SKIPPED] ``offline_summary=None`` または ``reason="reconstruction_skipped"``
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


def load_reconstruct_jsonl(path: Path) -> Iterator[dict]:
    """JSONL を 1 行 1 dict に読み込む。

    壊れた行 (不正 JSON) は warning ログを出して skip する。
    """
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(
                    "inspect_reconstruction: line %d skipped (bad JSON): %s",
                    line_no, e,
                )


def _status_label(entry: dict) -> str:
    """``[OK]`` / ``[REVIEW]`` / ``[SKIPPED]`` のラベルを決める。

    優先順位:
      1. ``offline_summary is None`` または ``reason == "reconstruction_skipped"``
         → ``[SKIPPED]``
      2. ``needs_review`` truthy または ``reason == "reconstructed_with_diff"``
         → ``[REVIEW]``
      3. それ以外 → ``[OK]``
    """
    reason = str(entry.get("reason", ""))
    if entry.get("offline_summary") is None or reason == "reconstruction_skipped":
        return "[SKIPPED]"
    if entry.get("needs_review") or reason == "reconstructed_with_diff":
        return "[REVIEW]"
    return "[OK]"


def _diff_fields(
    entry: dict,
    only_fields: Optional[list[str]] = None,
) -> list[str]:
    """``entry["diff"]`` の key を sorted list で返す。

    ``only_fields`` が指定されたら、その field 集合との積集合のみ返す
    (典型は ``["resolution_type", "seat_payouts"]`` のような monitoring 用 filter)。
    """
    diff = entry.get("diff")
    if not isinstance(diff, dict) or not diff:
        return []
    keys = list(diff.keys())
    if only_fields:
        allow = set(only_fields)
        keys = [k for k in keys if k in allow]
    return sorted(keys)


def format_entry(entry: dict, only_fields: Optional[list[str]] = None) -> str:
    """1 hand を 1 行のテキストに整形する。"""
    hand_id = entry.get("hand_id", "?")
    label = _status_label(entry)
    bootstrap = entry.get("bootstrap_source")
    reason = entry.get("reason", "")

    parts = [
        f"hand {hand_id} {label}",
        f"bootstrap={bootstrap}",
        f"reason={reason}",
    ]
    fields = _diff_fields(entry, only_fields)
    if fields:
        parts.append(f"diff_fields={','.join(fields)}")
    return " ".join(parts)


def format_patch_lines(entry: dict) -> list[str]:
    """Phase 5-A: entry の patch_proposal から ``  PATCH: ...`` 行を組み立てる。

    proposal が無い / fields が空 / 形式不正なら空 list を返す
    (= 呼び元は単純に extend してもよい)。

    出力例 (1 hand に複数 patch がある場合):
        ``  PATCH: resolution_type  online=fold_win  offline=showdown``
        ``  PATCH: seat_payouts  online={'2': 300}  offline={'1': 150, '2': 150}``
    """
    proposal = entry.get("patch_proposal")
    if not isinstance(proposal, dict):
        return []
    fields = proposal.get("fields") or []
    if not isinstance(fields, list):
        return []
    lines: list[str] = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        name = f.get("field", "?")
        online = f.get("online")
        offline = f.get("offline")
        lines.append(f"  PATCH: {name}  online={online}  offline={offline}")
    return lines


def inspect(
    reconstruct_path: Path,
    only_needs_review: bool = False,
    only_fields: Optional[list[str]] = None,
    show_patches: bool = False,
) -> list[str]:
    """JSONL を読んで 1 hand 1 行のフォーマット文字列リストを返す (hand_id 昇順)。

    Args:
        reconstruct_path: ``reconstruct_<session>.jsonl`` のパス。
        only_needs_review: True なら ``needs_review=true`` の hand のみ含める。
        only_fields: 指定すると diff のうちこの field のみを表示対象にする。
        show_patches: Phase 5-A: True なら hand 行の直後に
            ``  PATCH: ...`` 行を続けて出す (proposal がある hand のみ)。

    Returns:
        フォーマット済み文字列のリスト (hand_id 昇順)。
    """
    entries = list(load_reconstruct_jsonl(reconstruct_path))
    # hand_id 昇順 (欠損は末尾)
    entries.sort(key=lambda e: (e.get("hand_id") is None, e.get("hand_id", 0)))

    output: list[str] = []
    for entry in entries:
        if only_needs_review and not entry.get("needs_review"):
            continue
        output.append(format_entry(entry, only_fields=only_fields))
        if show_patches:
            output.extend(format_patch_lines(entry))
    return output


def _split_fields_arg(s: str) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect reconstruct_<session>.jsonl (from "
            "`python -m output.reconstruct_session`) and print one summary "
            "line per hand. Read-only: never modifies online JSON / PHH."
        ),
    )
    parser.add_argument(
        "--reconstruct", required=True, type=Path,
        help="Path to reconstruct_<session>.jsonl",
    )
    parser.add_argument(
        "--only-needs-review", action="store_true",
        help="Show only hands with needs_review=true.",
    )
    parser.add_argument(
        "--fields", type=_split_fields_arg, default=None,
        help=(
            "Comma-separated list of diff field names to surface in "
            "diff_fields=. If omitted, all diff fields present in each "
            "hand are shown."
        ),
    )
    parser.add_argument(
        "--show-patches", action="store_true",
        help=(
            "Phase 5-A: append `  PATCH: <field> online=... offline=...` lines "
            "after each hand row when a patch proposal exists. Read-only — "
            "this does not apply any patch."
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress info logs from the loader.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    lines = inspect(
        args.reconstruct,
        only_needs_review=args.only_needs_review,
        only_fields=args.fields,
        show_patches=args.show_patches,
    )
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
