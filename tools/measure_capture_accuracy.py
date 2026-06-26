#!/usr/bin/env python3
"""tools/measure_capture_accuracy.py

Phase A 捕捉精度の計測ハーネス（`docs/dogfood/measurement-plan.md` §1）。

自動記録された `logs/{session_id}.json` と、人手で作成した
`logs/{session_id}.ground_truth.json` を突き合わせ、ハンドレビュー × GTO solver
統合（提案 rev.1 §6）の Phase A 通過判定に使う 3 軸（hand coverage / action accuracy /
board accuracy）を出す。診断用に action 種別/金額の内訳・hole card・winner_seat も算出する。

訂正（ADR-0036）は **既定で適用** し、ユーザー可視の最終状態を計測する。`--raw` で
訂正前の素地も測れる（systematic な誤認識が訂正で隠れていないかの diagnostic）。

使い方:
  python tools/measure_capture_accuracy.py \
      --session logs/<sid>.json \
      --ground-truth logs/<sid>.ground_truth.json \
      [--corrections hand_corrections.json] \
      [--raw] [--json] [--threshold 0.95]

exit code: 0 = 全 3 軸が threshold 以上、1 = いずれかが下回る、2 = 入力エラー。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.hand_correction import HandCorrection, apply_hand_corrections  # noqa: E402

_DEFAULT_THRESHOLD = 0.95
_PASS_AXES = ("hand_coverage", "action_accuracy", "board_accuracy")


@dataclass
class HandAccuracy:
    hand_id: int
    captured: bool
    action_total: int
    action_correct: int
    action_type_correct: int
    action_amount_correct: int
    board_match: bool
    hole_total: int
    hole_correct: int
    winner_match: bool | None


@dataclass
class SessionAccuracy:
    session_id: str
    threshold: float
    hand_coverage: float
    action_accuracy: float
    action_type_accuracy: float
    action_amount_accuracy: float
    board_accuracy: float
    hole_card_accuracy: float | None
    winner_seat_accuracy: float | None
    missed_hands: list[int]
    phantom_hands: list[int]
    per_hand: list[HandAccuracy] = field(default_factory=list)

    @property
    def phase_a_pass(self) -> bool:
        return all(
            getattr(self, axis) >= self.threshold for axis in _PASS_AXES
        )


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_corrections_for_session(
    path: Path, session_id: str
) -> list[HandCorrection]:
    """`hand_corrections.json` 全体を読み、当該 session の訂正だけ返す。

    ファイル不在は空 list を返す（訂正が無い session は正常パス）。
    """
    if not path.exists():
        return []
    data = _load_json(path)
    raw = data.get("corrections") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [
        HandCorrection.from_dict(d)
        for d in raw
        if isinstance(d, dict) and d.get("session_id") == session_id
    ]


def _normalize_action_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_amount(value: Any, action_type: str) -> int:
    if action_type in ("fold", "check"):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_board(board: Any) -> list[str]:
    if not isinstance(board, list):
        return []
    cards = [str(c).strip() for c in board if c]
    return sorted(cards, key=str.lower)


def _index_by_hand_id(hands: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for h in hands:
        hid = h.get("hand_id")
        if isinstance(hid, int):
            out[hid] = h
    return out


def _seat_to_hole_cards(players: list[dict]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for p in players or []:
        seat = p.get("seat")
        hole = p.get("hole_cards")
        if isinstance(seat, int) and isinstance(hole, list) and hole:
            out[seat] = sorted(str(c).strip() for c in hole if c)
    return out


def measure_hand(
    gt_hand: dict, captured_hand: dict | None
) -> HandAccuracy:
    """1 ハンド分の精度を計算。captured_hand=None は missed として扱う。"""
    gt_actions = gt_hand.get("actions") or []
    cap_actions = (captured_hand or {}).get("actions") or []
    action_total = max(len(gt_actions), len(cap_actions))

    type_correct = 0
    amount_correct = 0
    both_correct = 0
    for i in range(action_total):
        gt_a = gt_actions[i] if i < len(gt_actions) else None
        cap_a = cap_actions[i] if i < len(cap_actions) else None
        if gt_a is None or cap_a is None:
            continue
        gt_type = _normalize_action_type(gt_a.get("action"))
        cap_type = _normalize_action_type(cap_a.get("action"))
        gt_amount = _normalize_amount(gt_a.get("amount"), gt_type)
        cap_amount = _normalize_amount(cap_a.get("amount"), cap_type)
        t_ok = gt_type == cap_type
        a_ok = gt_amount == cap_amount
        if t_ok:
            type_correct += 1
        if a_ok:
            amount_correct += 1
        if t_ok and a_ok:
            both_correct += 1

    board_match = _normalize_board(gt_hand.get("board")) == _normalize_board(
        (captured_hand or {}).get("board")
    )

    gt_hole = _seat_to_hole_cards(gt_hand.get("players") or [])
    cap_hole = _seat_to_hole_cards((captured_hand or {}).get("players") or [])
    hole_total = len(gt_hole)
    hole_correct = sum(1 for seat, cards in gt_hole.items() if cap_hole.get(seat) == cards)

    winner_match: bool | None
    if "winner_seat" in gt_hand and gt_hand.get("winner_seat") is not None:
        winner_match = gt_hand.get("winner_seat") == (captured_hand or {}).get("winner_seat")
    else:
        winner_match = None

    return HandAccuracy(
        hand_id=int(gt_hand.get("hand_id", -1)),
        captured=captured_hand is not None,
        action_total=action_total,
        action_correct=both_correct,
        action_type_correct=type_correct,
        action_amount_correct=amount_correct,
        board_match=board_match,
        hole_total=hole_total,
        hole_correct=hole_correct,
        winner_match=winner_match,
    )


def measure_session(
    session_log: dict,
    ground_truth: dict,
    corrections: list[HandCorrection] | None = None,
    threshold: float = _DEFAULT_THRESHOLD,
) -> SessionAccuracy:
    """セッション全体の精度を計算。

    corrections が与えられたら captured 側の各 hand に
    `apply_hand_corrections` を通す（ADR-0036 と同じ read-time オーバーレイ）。
    """
    captured_by_id = _index_by_hand_id(session_log.get("hands") or [])
    gt_hands = ground_truth.get("hands") or []

    corrections = corrections or []
    by_hand: dict[int, list[HandCorrection]] = {}
    for c in corrections:
        by_hand.setdefault(c.hand_id, []).append(c)

    per_hand: list[HandAccuracy] = []
    missed: list[int] = []
    gt_ids: set[int] = set()
    for gt in gt_hands:
        hid = gt.get("hand_id")
        if not isinstance(hid, int):
            continue
        gt_ids.add(hid)
        captured = captured_by_id.get(hid)
        if captured is None:
            missed.append(hid)
            per_hand.append(measure_hand(gt, None))
            continue
        if by_hand.get(hid):
            captured = apply_hand_corrections(captured, by_hand[hid])
        per_hand.append(measure_hand(gt, captured))

    phantom = sorted(set(captured_by_id) - gt_ids)

    n_gt = len(gt_ids)
    coverage = (n_gt - len(missed)) / n_gt if n_gt else 1.0

    total_actions = sum(h.action_total for h in per_hand)
    action_acc = (
        sum(h.action_correct for h in per_hand) / total_actions
        if total_actions else 1.0
    )
    type_acc = (
        sum(h.action_type_correct for h in per_hand) / total_actions
        if total_actions else 1.0
    )
    amount_acc = (
        sum(h.action_amount_correct for h in per_hand) / total_actions
        if total_actions else 1.0
    )

    board_acc = (
        sum(1 for h in per_hand if h.board_match) / n_gt if n_gt else 1.0
    )

    total_hole = sum(h.hole_total for h in per_hand)
    hole_acc: float | None = (
        sum(h.hole_correct for h in per_hand) / total_hole
        if total_hole else None
    )

    winner_results = [h.winner_match for h in per_hand if h.winner_match is not None]
    winner_acc: float | None = (
        sum(1 for x in winner_results if x) / len(winner_results)
        if winner_results else None
    )

    session_id = (
        ground_truth.get("session_id")
        or session_log.get("session_id")
        or ""
    )

    return SessionAccuracy(
        session_id=str(session_id),
        threshold=threshold,
        hand_coverage=coverage,
        action_accuracy=action_acc,
        action_type_accuracy=type_acc,
        action_amount_accuracy=amount_acc,
        board_accuracy=board_acc,
        hole_card_accuracy=hole_acc,
        winner_seat_accuracy=winner_acc,
        missed_hands=missed,
        phantom_hands=phantom,
        per_hand=per_hand,
    )


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "  n/a"
    return f"{value * 100:5.1f}%"


def _print_human(result: SessionAccuracy) -> None:
    pass_str = "PASS" if result.phase_a_pass else "FAIL"
    print(f"Session : {result.session_id}")
    print(f"Threshold: {result.threshold * 100:.1f}%")
    print(f"Phase A : {pass_str}")
    print()
    print("Pass-gate axes (3 軸 ≥ threshold で通過):")
    print(f"  hand_coverage  : {_fmt_pct(result.hand_coverage)}")
    print(f"  action_accuracy: {_fmt_pct(result.action_accuracy)}")
    print(f"  board_accuracy : {_fmt_pct(result.board_accuracy)}")
    print()
    print("Diagnostic (Phase A 通過判定には不使用):")
    print(f"  action_type_accuracy  : {_fmt_pct(result.action_type_accuracy)}")
    print(f"  action_amount_accuracy: {_fmt_pct(result.action_amount_accuracy)}")
    print(f"  hole_card_accuracy    : {_fmt_pct(result.hole_card_accuracy)}")
    print(f"  winner_seat_accuracy  : {_fmt_pct(result.winner_seat_accuracy)}")
    if result.missed_hands:
        print()
        print(f"Missed hands ({len(result.missed_hands)}): {result.missed_hands}")
    if result.phantom_hands:
        print(f"Phantom hands ({len(result.phantom_hands)}): {result.phantom_hands}")


def _result_to_json(result: SessionAccuracy) -> dict:
    d = asdict(result)
    d["phase_a_pass"] = result.phase_a_pass
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Phase A 捕捉精度の計測（docs/dogfood/measurement-plan.md §1）"
    )
    p.add_argument("--session", required=True, help="logs/<sid>.json")
    p.add_argument("--ground-truth", required=True, help="logs/<sid>.ground_truth.json")
    p.add_argument(
        "--corrections",
        default=None,
        help="hand_corrections.json（既定で適用）。--raw 指定時は無視",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="訂正（ADR-0036）を適用せず、素の logs を ground truth と比較する",
    )
    p.add_argument("--json", action="store_true", help="JSON で出力（CI/script 用）")
    p.add_argument(
        "--threshold",
        type=float,
        default=_DEFAULT_THRESHOLD,
        help=f"Phase A pass-gate の閾値（既定 {_DEFAULT_THRESHOLD}）",
    )
    args = p.parse_args(argv)

    session_path = Path(args.session)
    gt_path = Path(args.ground_truth)
    if not session_path.exists():
        print(f"error: session log not found: {session_path}", file=sys.stderr)
        return 2
    if not gt_path.exists():
        print(f"error: ground truth not found: {gt_path}", file=sys.stderr)
        return 2

    session_log = _load_json(session_path)
    ground_truth = _load_json(gt_path)

    corrections: list[HandCorrection] | None
    if args.raw:
        corrections = None
    else:
        cpath = Path(args.corrections) if args.corrections else Path("hand_corrections.json")
        sid = ground_truth.get("session_id") or session_log.get("session_id") or ""
        corrections = load_corrections_for_session(cpath, str(sid))

    result = measure_session(
        session_log,
        ground_truth,
        corrections=corrections,
        threshold=args.threshold,
    )

    if args.json:
        print(json.dumps(_result_to_json(result), ensure_ascii=False, indent=2))
    else:
        _print_human(result)

    return 0 if result.phase_a_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
