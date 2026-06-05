#!/usr/bin/env python3
"""tools/replay_hand.py

Phase F1 (#8) — 記録済み events.jsonl を決定的に再構築し HandSummary を表示する CLI (R4)。

使い方:
  python tools/replay_hand.py <fixture_dir>                    # setup.json + events.jsonl を読む
  python tools/replay_hand.py <events.jsonl> --setup <setup.json>

setup.json = {"backend","sb","bb","session_id","players":[{"seat","name","stack"},...]}。
出力は HandSummary.to_dict() の JSON 配列（確定ハンド順）。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

# repo ルートを import パスに追加（スクリプト直接実行のため）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.game_state import PlayerState  # noqa: E402
from integration.replay import load_events, replay_events, replay_fixture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="記録済みハンドを決定的 replay する (F1)")
    parser.add_argument("target", help="fixture ディレクトリ、または events.jsonl ファイル")
    parser.add_argument("--setup", default=None, help="events.jsonl 指定時の setup.json パス")
    args = parser.parse_args(argv)

    target = Path(args.target)
    with tempfile.TemporaryDirectory() as out_dir:
        if target.is_dir():
            summaries = replay_fixture(target, out_dir)
        else:
            if not args.setup:
                parser.error("events.jsonl 指定時は --setup が必要です")
            setup = json.loads(Path(args.setup).read_text(encoding="utf-8"))
            players = [
                PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
                for p in setup["players"]
            ]
            summaries = replay_events(
                load_events(target),
                backend=setup.get("backend", "legacy"), players=players,
                sb=setup["sb"], bb=setup["bb"],
                session_id=setup["session_id"], out_dir=out_dir,
            )

    print(json.dumps([s.to_dict() for s in summaries], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
