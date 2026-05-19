"""output/reconstruct_session.py

Phase 3 CLI: セッション JSON と evidence JSONL から各 hand を再構成し、
online vs offline の diff を 1 hand 1 行の JSONL として書き出す。

使い方:
    python -m output.reconstruct_session --session logs/session_xxx.json
    # → logs/reconstruct_session_xxx.jsonl が生成される

オプション:
    --output PATH        出力先 (省略時は session.parent / "reconstruct_<session_id>.jsonl")
    --evidence PATH      evidence JSONL 明示指定 (省略時は session.parent / "evidence_<session_id>.jsonl")

出力 1 行のスキーマ:
    {
      "hand_id": int,
      "needs_review": bool,
      "reason": "reconstructed_no_diff" | "reconstructed_with_diff" | "reconstruction_skipped",
      "diff": {field: {"online": ..., "offline": ...}} or null,
      "confidence": float or null,
      "online_summary": {... HandSummary.to_dict() ...},
      "offline_summary": {...} or null,
    }

このツールは **online の HandSummary / JSON / PHH を mutate しない**。差分情報を
別ファイルに書き出すだけ。後段 (GUI / 監視ツール) がこれを読んで人手 review を
誘導したり、Phase 4+ で自動 patch ロジックを足したりする想定。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from core.hand_log import ActionRecord, HandSummary, PotSettlement
from core.hand_reconstructor import HandReconstructor
from output.replay_hand import extract_hand_windows, load_evidence_log

logger = logging.getLogger(__name__)


def _extract_session_blinds(session_data: dict) -> tuple[Optional[int], Optional[int]]:
    """session JSON から (sb_default, bb_default) を取り出す (Phase 4-B 用)。

    優先順位:
      1. session_data["blinds"]["sb"/"bb"]  (一部 writer はトップレベルに置く)
      2. session_data["hands"][0]["blinds"]["sb"/"bb"] (通常はここに入っている)
    どちらも見つからない場合は ``(None, None)`` を返し、raw-only bootstrap を
    成立させない (= online_summary fallback or skipped に倒す)。
    """
    blinds = session_data.get("blinds") or {}
    sb = blinds.get("sb") if isinstance(blinds, dict) else None
    bb = blinds.get("bb") if isinstance(blinds, dict) else None
    if sb is None or bb is None:
        hands = session_data.get("hands") or []
        if hands and isinstance(hands[0], dict):
            hb = hands[0].get("blinds") or {}
            if isinstance(hb, dict):
                sb = sb if sb is not None else hb.get("sb")
                bb = bb if bb is not None else hb.get("bb")
    try:
        return (int(sb) if sb is not None else None,
                int(bb) if bb is not None else None)
    except (TypeError, ValueError):
        return None, None


def _hand_summary_from_dict(d: dict) -> HandSummary:
    """JSON dict (JsonWriter / HandSummary.to_dict() 由来) を HandSummary に復元する。

    JSON 化で int key が str になっている dict (``seat_payouts`` /
    ``showdown_revealed_cards``) は int に正規化する。``pots`` は ``PotSettlement``、
    ``actions`` は ``ActionRecord`` にデシリアライズ。
    """
    return HandSummary(
        hand_id=int(d["hand_id"]),
        session_id=str(d.get("session_id", "")),
        started_at=str(d.get("started_at", "")),
        ended_at=str(d.get("ended_at", "")),
        blinds=dict(d.get("blinds") or {}),
        board=list(d.get("board") or []),
        board_source=str(d.get("board_source", "")),
        players=list(d.get("players") or []),
        pot_total=int(d.get("pot_total", 0)),
        winner_seat=int(d.get("winner_seat", 0)),
        actions=[ActionRecord(**a) for a in (d.get("actions") or [])],
        review_required=bool(d.get("review_required", False)),
        folded_seats=list(d.get("folded_seats") or []),
        all_in_seats=list(d.get("all_in_seats") or []),
        resolution_status=str(d.get("resolution_status", "final")),
        resolution_type=d.get("resolution_type"),
        seat_payouts={int(k): int(v) for k, v in (d.get("seat_payouts") or {}).items()},
        showdown_revealed_cards={
            int(k): list(v) for k, v in (d.get("showdown_revealed_cards") or {}).items()
        },
        pots=[PotSettlement(**p) for p in (d.get("pots") or [])],
    )


def reconstruct_session(
    session_path: Path,
    output_path: Optional[Path] = None,
    evidence_path: Optional[Path] = None,
    reconstructor: Optional[HandReconstructor] = None,
) -> Path:
    """``session_path`` を読み、各 hand を reconstruct して JSONL に書く。

    Args:
        session_path: ``logs/<session_id>.json`` (JsonWriter 出力)。
        output_path: 出力 JSONL。省略時は同ディレクトリの
            ``reconstruct_<session_id>.jsonl``。
        evidence_path: evidence JSONL。省略時は同ディレクトリの
            ``evidence_<session_id>.jsonl``。
        reconstructor: テスト用 inject point。省略時は ``HandReconstructor()``。

    Returns:
        生成された出力ファイルのパス。
    """
    session_path = Path(session_path)
    session_data = json.loads(session_path.read_text(encoding="utf-8"))
    session_id = str(session_data.get("session_id", session_path.stem))
    log_dir = session_path.parent

    if evidence_path is None:
        evidence_path = log_dir / f"evidence_{session_id}.jsonl"
    if output_path is None:
        output_path = log_dir / f"reconstruct_{session_id}.jsonl"

    records = load_evidence_log(evidence_path)
    windows = extract_hand_windows(records)

    if reconstructor is None:
        # Phase 4-B: session-level の blinds 設定を raw-only bootstrap の default として
        # HandReconstructor に渡す。session JSON のトップレベルに ``blinds`` が無い場合は
        # 各 hand_dict の最初のもの (= 通常は全 hand 共通) から拾う fallback。
        sb_default, bb_default = _extract_session_blinds(session_data)
        rc = HandReconstructor(default_sb=sb_default, default_bb=bb_default)
    else:
        rc = reconstructor
    written = 0
    with Path(output_path).open("w", encoding="utf-8") as f:
        for hand_dict in session_data.get("hands", []):
            try:
                hand_id = int(hand_dict["hand_id"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping hand without valid hand_id: %r", hand_dict)
                continue
            window = windows.get(hand_id, [])
            try:
                online = _hand_summary_from_dict(hand_dict)
            except Exception:
                logger.exception("Could not parse online summary hand=%d", hand_id)
                continue
            result = rc.reconstruct_from_events(window, online_summary=online)
            entry = {
                "hand_id": hand_id,
                "needs_review": bool(result.needs_review),
                "reason": result.reason,
                "diff": result.diff,
                "confidence": result.confidence,
                # Phase 4-B: bootstrap の出所と heuristic 情報
                "bootstrap_source": result.bootstrap_source,
                "bootstrap_meta": result.bootstrap_meta,
                "online_summary": hand_dict,
                "offline_summary": result.summary.to_dict() if result.summary else None,
            }
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            written += 1

    logger.info(
        "reconstruct_session: %d hand(s) processed → %s",
        written, output_path,
    )
    return Path(output_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct each hand in a session JSON and write online↔offline diff log.",
    )
    parser.add_argument(
        "--session", required=True,
        help="Path to session JSON (e.g. logs/session_xxx.json)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output JSONL path (default: <session_dir>/reconstruct_<session_id>.jsonl)",
    )
    parser.add_argument(
        "--evidence", default=None,
        help="Evidence JSONL path (default: <session_dir>/evidence_<session_id>.jsonl)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress info logs",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    out = reconstruct_session(
        Path(args.session),
        output_path=Path(args.output) if args.output else None,
        evidence_path=Path(args.evidence) if args.evidence else None,
    )
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
