# Worklog: sync の settlement マージ partial-paid 対応（ADR-0024）

## Date

2026-06-13

## Scope / Task

ADR-0023（settlement partial-paid）導入時に flag した follow-up。`core/sync.py` の settlement
マージが 2 値前提（paid>unpaid）で、partial を取りこぼす収束バグを修正する。

## Goal

両 committed settlement のマージを `paid_amount` の monotonic max にし、`partial` が早い settled_at の
`unpaid` に上書きされないようにする（可換・冪等・収束を維持）。

## Changed Files

- `core/sync.py`: `_resolve_settlement` を書き換え。committed>uncommitted は不変。両 committed は
  `paid_amount=max(a,b)`、`payment_status` を `_derive_payment_status(base.net, paid)` で導出
  （core の単一導出点を再利用）、frozen totals/settled_at は決定的 base（settled_at 最早、同値は
  安定シリアライズ tiebreak）から。`_stable_key` ヘルパ追加。
- `tests/test_sync.py`: `_settlement` ヘルパを paid_amount ベース（status は導出）に更新。
  旧 `test_settlement_paid_beats_unpaid` を `test_settlement_max_paid_amount_wins` /
  `test_settlement_partial_not_overwritten_by_unpaid` / `test_settlement_merge_idempotent` /
  `test_settlement_both_committed_same_paid_keeps_earliest` に差し替え。snapshot fixture の
  `payment=` 呼び出しを修正。
- docs: `docs/adr/0024-...md`（新規）、`docs/decision-log.md`、ADR-0022 本文に更新注記、
  `viewer-api.md` sync 節、`CLAUDE.md`、`CHANGELOG.md`、ADR-0023 worklog の follow-up を済に。

## Expected / Implemented Behavior

partial（paid_amount=4000）と unpaid（paid_amount=0, settled_at 早い）をどちら向きにマージしても
paid_amount=4000・status=partial に収束。max + 決定的 base なので可換・冪等。

## Test Results

- `pytest tests/test_sync.py tests/test_viewer_api_sync.py -q` — 22 passed。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **526 passed, 0 skipped**。
- `ruff check .` — clean。

## Mismatches Found During Testing

- 既存 sync テストが旧 `_settlement(..., payment=...)` を使っていたため失敗 → helper を
  paid_amount ベースに更新し、payment_status を導出で自己整合化。

## Remaining Gaps / Out-of-Scope

- player rename 伝播（`updated_at` additive）/ hand log の file-level union / sync の auto-trigger
  （ADR-0022 の残課題、本タスク外）。

## Related ADRs / Issues

- ADR-0024（本件）/ ADR-0022（sync）/ ADR-0023（partial-paid）

## Related Commits

- 本 commit
