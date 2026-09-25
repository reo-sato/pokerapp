"""tools/set_config.py — config.json の 1 項目を表示・変更する（店舗 PC 向け, ADR-0059）。

メモ帳や PowerShell で config.json を書き換えると、文字コード（BOM）や JSON の書式を壊しやすい。
このツールは項目名（`.` 区切り）と値だけを受け取り、UTF-8（BOM なし）で書き戻す。

使い方:
    python tools/set_config.py session_layer.enabled true     # 変更（値は JSON: true / 8788 / "文字列"）
    python tools/set_config.py session_layer.enabled          # 今の値を表示
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.atomic_io import atomic_write_json  # noqa: E402
from core.config import _CONFIG_PATH, load_config  # noqa: E402

_MISSING = object()


def parse_value(raw: str):
    """JSON として読めればその値（true / 12 / "x"）、読めなければ文字列のまま。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def get_item(cfg: dict, key: str):
    node = cfg
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def set_item(cfg: dict, key: str, value) -> None:
    parts = key.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def main(argv: list[str] | None = None, path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key", help="項目名（例: session_layer.enabled）")
    parser.add_argument("value", nargs="?", help="新しい値（JSON。省略すると表示だけ）")
    args = parser.parse_args(argv)
    target = path or _CONFIG_PATH
    cfg = load_config(target)
    before = get_item(cfg, args.key)
    shown = "（未設定）" if before is _MISSING else json.dumps(before, ensure_ascii=False)
    if args.value is None:
        print(f"{args.key} = {shown}")
        return 0
    value = parse_value(args.value)
    set_item(cfg, args.key, value)
    atomic_write_json(target, cfg)
    print(f"{args.key}: {shown} -> {json.dumps(value, ensure_ascii=False)}（{target}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
