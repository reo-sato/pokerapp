# Worklog: settlement 確定 GUI（S4, ADR-0016）

## Date

2026-06-13

## Scope / Task

ロードマップ残作業の「settlement の GUI からの確定（commit）UI」。settlement core
（compute/commit/paid-unpaid）は S3.1 で実装済だが、確定操作が CLI/CSV export 経由だったため、
スタッフ用 ledger 画面（`gui/ledger_view.py`）に GUI を追加する。

## Goal

closed session を GUI から精算確定（commit）し、確定済 settlement の paid/unpaid を player ごとに
切り替えられる。business logic は core が source of truth（GUI は呼んで error を表示するだけ）。

## Changed Files

- `gui/ledger_view.py`: 「精算（確定 commit / 支払状態 paid・unpaid）」パネルを追加。
  - `_refresh_settlement()`: commit ボタン + 確定済 settlement 一覧（paid は緑、各行に paid/unpaid
    切替ボタン）。
  - `_cmd_commit_settlement()`: `commit_settlement` を呼ぶ。session 未選択は error、open は core の
    `SessionNotClosedError`、二重は `AlreadySettledError` を表示。
  - `_cmd_set_payment(player_id, status)`: `set_payment_status` を呼ぶ。未確定は `LedgerNotFoundError`。
  - grid を再採番（settlement frame = row 4、order frame = row 5、status = row 5/6）。docstring 更新。
- `tests/test_ledger_view_gui.py`: `TestSettlement`（6 件）— commit 要 session / open→error /
  closed→確定 / 二重→already_settled / paid・unpaid トグル / 未確定での set_payment→error。
- `CLAUDE.md`（実装状況に S4 settlement GUI 行、Ledger 構成/Out-of-scope、Phase 4、残作業更新）/
  `CHANGELOG.md`。

## Expected / Implemented Behavior

Expected どおり。GUI は customtkinter（headless 不可）だが、コマンドメソッドのロジックは
customtkinter モック下で repository 効果 + error フラグを検証（既存 ledger_view テストと同パターン）。

## Test Results

- `pytest tests/test_ledger_view_gui.py -q` — 22 passed（+6）。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **517 passed, 0 skipped**。
- `ruff check .` — clean。

## Mismatches Found During Testing

None observed.

## Remaining Gaps / Out-of-Scope

- mobile での settlement 表示（read）、partial-paid（schema 拡張 additive）、auto ledger 生成。
- GUI の実機スモーク（customtkinter 描画）は headless 環境のため未実施（ロジックはテスト済）。

## Related ADRs / Issues

- ADR-0016（ledger/points/settlement）/ ADR-0019（settlement schema 1.0 freeze）/ ISSUE-0018（CSV export）

## Related Commits

- 本 commit
