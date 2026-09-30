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


# オールインと同じ額なら同じアクションとみなす種類（持ち点を全部出すコール・ベット・レイズ = オールイン。
# 真のアクションの画面・記録のどちらで「オールイン」と書いても、出したチップが同じなら同じ, 2026-09-30）
_ALLIN_EQUIVALENT = frozenset({"call", "bet", "raise"})


def same_action_type(gt_type: str, cap_type: str, gt_amount: int, cap_amount: int) -> bool:
    """種類が同じか。オールインは、同じ額（0 でない）のコール / ベット / レイズと同じ。"""
    if gt_type == cap_type:
        return True
    pair = {gt_type, cap_type}
    return "allin" in pair and bool(pair & _ALLIN_EQUIVALENT) and gt_amount == cap_amount and gt_amount > 0


def same_place(gt_action: dict, cap_action: dict) -> bool:
    """同じ人の同じストリートの行か。

    アライメントの「置き換え」の区間では、席やストリートの違う行どうしが並ぶ（店舗 a6ee12e4 ハンド 1: 真のアクション
    「席5 フォールド」と記録「席4 フォールド」が種類と額だけで一致扱いになっていた, 2026-09-30）。ストリートは
    どちらかが無ければ比べない（ストリートの無い古い真のアクションの行）。
    """
    if gt_action.get("seat") != cap_action.get("seat"):
        return False
    gt_street = str(gt_action.get("street") or "").strip().lower()
    cap_street = str(cap_action.get("street") or "").strip().lower()
    return not gt_street or not cap_street or gt_street == cap_street


def row_correct(gt_action: dict, cap_action: dict) -> bool:
    """1 行が正しいか: 同じ人・同じストリートで、種類（オールインの扱いは `same_action_type`）と額が一致。"""
    if not same_place(gt_action, cap_action):
        return False
    gt_type = _normalize_action_type(gt_action.get("action"))
    cap_type = _normalize_action_type(cap_action.get("action"))
    gt_amount = _normalize_amount(gt_action.get("amount"), gt_type)
    cap_amount = _normalize_amount(cap_action.get("amount"), cap_type)
    return same_action_type(gt_type, cap_type, gt_amount, cap_amount) and gt_amount == cap_amount


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


def _action_key(a: dict) -> tuple[str, Any, str]:
    """アライメント用キー。(street, seat, action 種別)。金額は含めない（金額誤りで
    アライメントが崩れないように）。"""
    action_type = _normalize_action_type(a.get("action"))
    return (str(a.get("street") or "").strip().lower(), a.get("seat"), action_type)


def _align_actions(
    gt_actions: list[dict], cap_actions: list[dict]
) -> tuple[list[tuple[dict, dict]], int, int]:
    """GT と captured のアクション列をシーケンスアライメントする（ADR-A G6）。

    従来の index 厳密比較は、誤合成 fold 1 件の挿入で以降の全アクションがズレて
    action_accuracy が崩壊した。difflib.SequenceMatcher（キー = (street, seat, action)）で
    最長一致を取り、挿入（phantom, 例: 誤合成 fold）/欠落（missed）は**各 1 誤り**として数える。

    Returns: (対応づいたペア列, GT 側の未対応数(delete), captured 側の未対応数(insert))
    """
    import difflib

    gt_keys = [_action_key(a) for a in gt_actions]
    cap_keys = [_action_key(a) for a in cap_actions]
    sm = difflib.SequenceMatcher(a=gt_keys, b=cap_keys, autojunk=False)

    pairs: list[tuple[dict, dict]] = []
    n_delete = 0
    n_insert = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pairs.extend(zip(gt_actions[i1:i2], cap_actions[j1:j2]))
        elif tag == "replace":
            # 双方に何かある区間は位置対応で比較（type/amount の個別誤りとして数える）。
            n = min(i2 - i1, j2 - j1)
            pairs.extend(zip(gt_actions[i1:i1 + n], cap_actions[j1:j1 + n]))
            n_delete += (i2 - i1) - n
            n_insert += (j2 - j1) - n
        elif tag == "delete":
            n_delete += i2 - i1
        elif tag == "insert":
            n_insert += j2 - j1
    return pairs, n_delete, n_insert


def measure_hand(
    gt_hand: dict, captured_hand: dict | None
) -> HandAccuracy:
    """1 ハンド分の精度を計算。captured_hand=None は missed として扱う。

    アクション列は index 厳密比較ではなくシーケンスアライメント（ADR-A G6）。
    分母 = GT アクション数 + captured 側の余剰（insert）数。
    """
    gt_actions = gt_hand.get("actions") or []
    cap_actions = (captured_hand or {}).get("actions") or []
    pairs, _n_delete, n_insert = _align_actions(gt_actions, cap_actions)
    action_total = len(gt_actions) + n_insert

    type_correct = 0
    amount_correct = 0
    both_correct = 0
    for gt_a, cap_a in pairs:
        gt_type = _normalize_action_type(gt_a.get("action"))
        cap_type = _normalize_action_type(cap_a.get("action"))
        gt_amount = _normalize_amount(gt_a.get("amount"), gt_type)
        cap_amount = _normalize_amount(cap_a.get("amount"), cap_type)
        # 別の人・別のストリートの行は、種類や額がたまたま同じでも正しくない
        place_ok = same_place(gt_a, cap_a)
        t_ok = place_ok and same_action_type(gt_type, cap_type, gt_amount, cap_amount)
        a_ok = place_ok and gt_amount == cap_amount
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


def _without_showdown_rows(hand: dict) -> dict:
    return dict(hand, actions=[a for a in hand.get("actions") or [] if a.get("street") != "showdown"])


def hand_fully_correct(gt_hand: dict, captured_hand: dict | None) -> bool:
    """ハンドが丸ごと正しいか（ハンドの整合, オーナー 2026-09-30: 発話の読みが 9 割当たっても、ハンドが丸ごと
    正しい割合は全然足りない）: 真のアクションの行が全部正しく・余計な行が無く・勝者が合い・真のアクションにボードが
    あればボードも合う。

    ショーダウンのマックの行（street = showdown）は比べない: 真のアクションの入力では入れても入れなくてもよく
    （入れ方がハンドごとに違う）、結果は勝者で見ている。
    """
    if captured_hand is None:
        return False
    gt_hand, captured_hand = _without_showdown_rows(gt_hand), _without_showdown_rows(captured_hand)
    m = measure_hand(gt_hand, captured_hand)
    rows = len(gt_hand.get("actions") or [])
    if not (m.action_correct == m.action_total == rows) or m.winner_match is False:
        return False
    return m.board_match or not _normalize_board(gt_hand.get("board"))


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
