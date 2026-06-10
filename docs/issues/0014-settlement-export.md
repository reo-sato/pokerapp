# Issue 0014: S3.3 settlement 確定 + paid/unpaid + CSV export

## Date

2026-06-10

## Status

Open

## Severity / Priority

- Severity: Medium（core（S3.1）後着手。settlement schema の `1.0` freeze は S4）
- Priority: P2

## Area

core / export — settlement (S3.3)

## Expected Behavior

session 終了時に player ごとの `session_settlement` を **確定（commit）** し、外部で再集計できる形で
**export** できる:

- `compute_settlement(session_id)` で player ごとの derived 集計を出す（open=中間/speculative）。
- closed session で `commit_settlement(session_id)` を呼ぶと `(session, player)` 1 行を **凍結** する
  （`net_due_to_store = 符号付き Σ cash_amount`、`payment_status=unpaid` 初期）。
- `set_payment_status(session_id, player_id, paid|unpaid)` で支払状態を操作（partial なし, rule 4）。
- **CSV export**: 「今日のキャッシュフロー」「session の buy-in/payout」「player の過去 N 回の収支」を
  家計簿 / Excel で再集計できる列構成で出力。

## Actual Behavior

未実装。S3.3 で着手。

## Reproduction

仕様レビュー（実装前タスク出し）:

1. `docs/contracts/ledger-overview.md` § session_settlement / 計算式を参照。
2. `output/json_writer.py` / `output/phh_exporter.py`（既存 export の参照実装）を参照。

## Root Cause

settlement は ledger / point（S3.1）の fold であり、core 実装後に確定・export を組む。

## Fix

未対応。S3.3 で実装:

- `LedgerRepository.compute_settlement` / `commit_settlement` / `set_payment_status`（S3.1 で骨子、
  S3.3 で確定挙動・状態遷移）。
- `output/ledger_csv_exporter.py`（新規, planned）等の CSV writer。session 単位 / player 単位の
  cashflow 列。
- 確定（commit）後の settlement は不変、`payment_status` のみ可変（訂正で `paid↔unpaid`）。

## Regression Test

S3.3 で追加予定:

- `test_commit_freezes_settlement_rows` — close→commit で 1 行/player 凍結。
- `test_payment_status_toggle` — unpaid→paid→unpaid（partial 不可）。
- `test_csv_export_columns_and_totals` — CSV 列 / 合計が fold と一致。
- `test_settlement_player_to_store_only` — player→店の 1 方向（player 間精算なし）。

## Affected Files

- `output/ledger_csv_exporter.py`（新規, planned）
- `core/ledger_repository.py`（settlement 確定・状態遷移）
- `main.py`（export エントリ, 例 `--export-settlement`）

## Related Worklog

- `docs/worklog/2026-06-10-s3-ledger-planning.md`

## Related ADRs

- `docs/adr/0011-ledger-points-settlement-design-direction.md`

## Related Commits

- 起票は S3 planning と同じコミット。実装は別タスク（S3.3）。

## Notes

- `session_settlement` schema の **`1.0` freeze は freeze order #5（S4）** に残す。S3.3 は derived view +
  確定 + CSV export を先行実装し、paid/unpaid の状態機械強化・schema freeze は S4 で行う（ADR-0011）。
- 銀行 / QR / 送金など決済手段連携は scope 外（外部 / 法務領域）。
