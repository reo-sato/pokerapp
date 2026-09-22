#!/usr/bin/env python3
"""tools/play_hand_text.py

マイク / Whisper モデルなしで、再構築パイプライン全体（parse → rules-aware engine → JSON）を
テキストから検証するドライバ CLI。

各入力行を `audio/recognizer.py:parse_action`（live の `audio/recorder.py` と同じ呼び出し）で
`AudioEvent` 化し、`integration/replay.py:replay_events`（決定的 driver, clock 注入）に流す。
`tools/replay_hand.py` が「記録済み events.jsonl」を replay するのに対し、本ツールは
「ディーラーの読み上げ相当の生テキスト」から同じ live 経路（actor 推定 / 合法手射影 / silent-fold /
side-pot / 派生 confidence）を駆動する。出力 JSON は `--out-dir`（既定 ./logs）に書かれ、
そのまま `python main.py --export-phh logs/<session>.json` で PHH 化できる。

入力行の語彙は `docs/usage.md` のディーラー読み上げ語彙と同じ:
  ハンド開始 / シートN ベット 600 / シートN コール / チェック / フォールド / オールイン /
  ショーダウン / シートN ウィナー
（`#` で始まる行と空行は無視。認識できない行は警告してスキップ。）

使用例:
  python tools/play_hand_text.py hand.txt
  printf 'ハンド開始\\nシート1 レイズ 600\\nシート2 コール\\nショーダウン\\nシート1 ウィナー\\n' \\
      | python tools/play_hand_text.py -
  python tools/play_hand_text.py hand.txt --seats 6 --stack 30000 --backend legacy
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# repo ルートを import パスに追加（スクリプト直接実行のため）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio.recognizer import parse_action  # noqa: E402
from core.events import AudioEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from core.hand_log import HandSummary  # noqa: E402
from integration.replay import replay_events  # noqa: E402


def utterances_to_events(
    lines: list[str], *, start_ts: float = 0.0, step: float = 1.0
) -> list[AudioEvent]:
    """テキスト行を AudioEvent 列に変換する（決定的タイムスタンプ付与）。

    空行・`#` コメント行・認識不能行はスキップ（不能行は stderr に警告）。
    各イベントの timestamp は `start_ts + i*step`（採用イベント順 i）に固定し、replay の
    clock 注入と合わせて出力を再現可能にする。
    """
    events: list[AudioEvent] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 意図的なテキスト入力（ASR ノイズなし）なので信頼度は満点を明示する。
        # None のままだと欠測扱い（MISSING_WHISPER_CONF=0.5, ADR-0033 追記）で
        # 全アクションが needs_review になってしまう。
        ev = parse_action(line, confidence=1.0)
        if ev is None:
            print(f"[skip] 認識できない行: {line!r}", file=sys.stderr)
            continue
        ev.timestamp = start_ts + len(events) * step
        events.append(ev)
    return events


def run_text_hand(
    lines: list[str],
    *,
    backend: str,
    players: list[PlayerState],
    sb: int,
    bb: int,
    session_id: str,
    out_dir: str | Path,
    start_ts: float = 0.0,
    step: float = 1.0,
) -> list[HandSummary]:
    """テキスト行から HandSummary 群を再構築する（replay_events を再利用）。"""
    events = utterances_to_events(lines, start_ts=start_ts, step=step)
    return replay_events(
        events,
        backend=backend,
        players=players,
        sb=sb,
        bb=bb,
        session_id=session_id,
        out_dir=out_dir,
    )


def _default_players(seats: int, stack: int) -> list[PlayerState]:
    return [PlayerState(seat=s, name=f"P{s}", stack=stack) for s in range(1, seats + 1)]


def _load_setup(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="テキストから再構築パイプラインを駆動する（mic 不要のローカル QA）",
    )
    parser.add_argument("input", help="入力ファイル、または '-' で標準入力")
    parser.add_argument("--setup", default=None, help="fixture 形式の setup.json（players/sb/bb 等）")
    parser.add_argument("--backend", default="pokerkit", help="game-state backend（既定 pokerkit / legacy）")
    parser.add_argument("--seats", type=int, default=2, help="--setup 無し時の席数（既定 2）")
    parser.add_argument("--stack", type=int, default=30000, help="--setup 無し時の初期スタック（既定 30000）")
    parser.add_argument("--sb", type=int, default=100, help="スモールブラインド（既定 100）")
    parser.add_argument("--bb", type=int, default=200, help="ビッグブラインド（既定 200）")
    parser.add_argument("--session-id", default=None, help="出力セッション ID（既定 text_<ts>）")
    parser.add_argument("--out-dir", default="./logs", help="出力ディレクトリ（既定 ./logs）")
    parser.add_argument("--start-ts", type=float, default=None, help="開始タイムスタンプ（既定 現在時刻）")
    parser.add_argument("--step", type=float, default=1.0, help="イベント間秒（既定 1.0）")
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    lines = text.splitlines()

    start_ts = args.start_ts if args.start_ts is not None else time.time()

    if args.setup:
        setup = _load_setup(args.setup)
        players = [
            PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
            for p in setup["players"]
        ]
        backend = setup.get("backend", args.backend)
        sb, bb = setup["sb"], setup["bb"]
        session_id = args.session_id or setup.get("session_id") or f"text_{int(start_ts)}"
    else:
        players = _default_players(args.seats, args.stack)
        backend, sb, bb = args.backend, args.sb, args.bb
        session_id = args.session_id or f"text_{int(start_ts)}"

    try:
        summaries = run_text_hand(
            lines, backend=backend, players=players, sb=sb, bb=bb,
            session_id=session_id, out_dir=args.out_dir,
            start_ts=start_ts, step=args.step,
        )
    except ImportError as exc:
        print(
            f"[error] backend={backend!r} の読み込みに失敗: {exc}\n"
            f"        pokerkit 未導入なら `--backend legacy` を試すか requirements を入れてください。",
            file=sys.stderr,
        )
        return 1

    out_path = Path(args.out_dir) / f"{session_id}.json"
    print(json.dumps([s.to_dict() for s in summaries], ensure_ascii=False, indent=2))
    print(
        f"\n# {len(summaries)} hand(s) → {out_path}\n"
        f"# PHH 出力: python main.py --export-phh {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
