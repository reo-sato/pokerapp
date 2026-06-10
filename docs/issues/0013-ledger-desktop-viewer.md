# Issue 0013: S3.2 desktop ledger viewer / editor（別画面）

## Date

2026-06-10

## Status

Open

## Severity / Priority

- Severity: Medium（core（S3.1）後着手。hand logger UI は触らない）
- Priority: P2

## Area

gui / desktop — ledger / points (S3.2, WS2)

## Expected Behavior

desktop に **別画面** の ledger viewer / editor を追加し、`LedgerRepository`（S3.1）越しに:

- session の player ごとに **ledger entry を追加**（buy_in / rebuy / add_on / order / entry_fee /
  adjustment、cash+point 併用）できる。
- player ごとの **point 残高**（fold）と、session の **中間集計**（buy-in 合計 / order 合計 /
  net 見込み）を表示できる。中間集計は **speculative（途中値）** として明示（ISSUE-0001 Q2）。
- entry の訂正は **reversal**（append-only）で行う UI（直接編集・削除はしない）。
- **roster 先読み**: S2 seating（`current_seating` / `hand_ref`）から session の参加 player を一覧化し、
  host は金額入力に集中できる（migration 方針, `ledger-overview.md`）。

`gui/player_registry.py` と同様、**hand logger の `gui/dashboard.py` とは独立した別 window**。
validation は core / repository の error code を表示するだけ（再実装しない）。

## Actual Behavior

未実装。S3.2 で着手。

## Reproduction

仕様レビュー（実装前タスク出し）:

1. `gui/player_registry.py`（別画面の参照実装）/ Session・Seating Viewer（WS2-α, read-only）を参照。
2. `docs/contracts/ledger-overview.md` interface 草案 / `error-shapes.md` ledger code を参照。

## Root Cause

S3.2 はフロントエンドであり、core（S3.1, ISSUE-0012）の repository / error 契約に依存する。

## Fix

未対応。S3.2 で実装:

- `gui/ledger_view.py`（新規, planned）等の別 window。`main.py` に起動エントリ（例 `--ledger`）。
- list / add（entry form）/ reverse / per-player session 集計 / point 残高表示。
- speculative（途中値）と confirmed（settlement 確定後）の **UI 区別表現**。
- error code → UI 分岐（`insufficient_points` で cash 補完を促す等）。

## Regression Test

S3.2 で追加予定（GUI ロジック / repository 連携の薄いテスト）:

- entry add → list 反映 / reverse の append-only 反映。
- speculative 表示が settlement 確定で confirmed に変わる。

## Affected Files

- `gui/ledger_view.py`（新規, planned）
- `main.py`（起動エントリ追加）
- 依存: `core/ledger_repository.py`（S3.1）/ `core/session_repository.py` / `core/player_repository.py`

## Related Worklog

- `docs/worklog/2026-06-10-s3-ledger-planning.md`

## Related ADRs

- `docs/adr/0011-ledger-points-settlement-design-direction.md`

## Related Commits

- 起票は S3 planning と同じコミット。実装は別タスク（S3.2）。

## Notes

- WS2 原則: hand logger 既存 UI（`gui/dashboard.py`）は汚さない。ledger は常に別画面（CLAUDE.md
  § Parallel development plan）。
- mobile（WS3）の ledger UX は本 issue の scope 外（future phase）。
