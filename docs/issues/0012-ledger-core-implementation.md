# Issue 0012: S3.1 ledger / points core schema + repository の実装

## Date

2026-06-10

## Status

Fixed（S3.1 core 実装済。schema version は `0.x` のまま = 未 freeze）

## Update (2026-06-10, S3.1 実装)

ADR-0011 の方針に沿って S3.1 core を実装した:

- 実 schema: `docs/contracts/schemas/{ledger_entry,point_ledger_entry,session_settlement}.schema.json`
  （v0.1）+ `fixtures/`（canonical / valid-* / invalid-*）。`tests/test_contracts.py::_MODELS` に 3 model 登録。
- domain: `core/ledger.py`（`LedgerEntry` / `PointLedgerEntry` / `SessionSettlement`）。
- repository: `core/ledger_repository.py`（`LedgerRepository`）。add_entry / reverse_entry / list_entries /
  grant_points / point_balance / list_point_entries / compute_settlement / commit_settlement /
  list_settlements / set_payment_status。別ストア `ledger.json`（アトミックリネーム、`.gitignore`）。
- invariants（core enforce）: append-only / 残高=fold / 残高非負 + cash 補完 / ledger↔point 整合 /
  entry fee cash only / 非ゼロ移動 / settlement derived・player→店・closed 限定確定。
- errors: `error-shapes.md` の ledger セクションと 1:1（`LedgerError` 階層）。
- tests: `tests/test_ledger_repository.py`（23）+ contract（ledger 3 model）= **緑**
  （`pytest tests/test_ledger_repository.py tests/test_contracts.py` → 33 passed）。

残（freeze 前 / 後続）: idempotency_key の運用詳細・speculative の UI 表現（S3.2, ISSUE-0013）、
ledger schema の `1.0` freeze（session freeze #3 後）。settlement の CSV export は S3.3（ISSUE-0014）。

## Severity / Priority

- Severity: High（S3 の最上流。desktop / export はこれに依存）
- Priority: P1

## Area

core / contracts — ledger / points (S3.1)

## Expected Behavior

ADR-0011 / `docs/contracts/ledger-overview.md` / `ledger-schema.md` の契約に対し、ledger / points の
**core 実装と実 schema/fixtures** が揃い、core が業務ルール（invariants）の source of truth になる:

- 実 schema: `schemas/{ledger_entry,point_ledger_entry,session_settlement}.schema.json`（v0.x）+
  `fixtures/`（canonical / valid-minimal / invalid-*）+ `tests/test_contracts.py::_MODELS` 登録。
- ドメイン: `core/ledger.py`（`LedgerEntry` / `PointLedgerEntry` / `SessionSettlement` データクラス,
  to_dict / from_dict）。
- repository: `core/ledger_repository.py`（`LedgerRepository`）。`core/session_repository.py` /
  `core/player_repository.py` と同じパターン（アトミックリネーム、`ledger.json`、error 階層）。
- 永続: プロジェクト直下 `ledger.json`（`{schema_version, ledger_entries, point_ledger_entries,
  settlements}`）。`.gitignore` に追加。
- code↔contract test（`test_core_*_matches_contract` 相当）緑。

## Actual Behavior

未実装（S3 planning は docs/契約 draft のみ）。`core/ledger*.py` / 実 schema / fixtures / `ledger.json` は
存在しない。

## Reproduction

仕様レビュー（実装前タスク出し）:

1. `docs/contracts/ledger-overview.md` § invariants / interface 草案を参照。
2. `docs/contracts/ledger-schema.md` の擬似 schema を実 `schemas/*.schema.json` に落とす。
3. `core/session_repository.py` をテンプレートに `LedgerRepository` を実装する。

## Root Cause

S3 planning（ADR-0011）で契約方針のみ確定。実装は本 issue（S3.1）で行う。

## Fix

未対応。S3.1 で以下を実装:

- **schema/fixtures**: 3 model の `schemas/*.schema.json` + `fixtures/`。`tests/test_contracts.py` の
  `_MODELS` に `ledger_entry` / `point_ledger_entry` / `session_settlement` を登録。
- **domain**: `core/ledger.py`。
- **repository**: `core/ledger_repository.py`。最低限の操作（`ledger-overview.md` interface 草案）:
  `add_entry` / `reverse_entry` / `list_entries` / `grant_points` / `point_balance` /
  `compute_settlement` / `commit_settlement` / `set_payment_status`。
- **invariants（core enforce）**: append-only / point 残高 = fold / 残高非負 + cash 補完 /
  ledger↔point 整合 / 参照整合 / entry_fee cash only / 非ゼロ移動 / settlement derived・player→店。
- **errors**（`error-shapes.md` ledger セクション）: `insufficient_points` /
  `entry_fee_requires_cash` / `invalid_amount` / `duplicate_grant` / `session_not_closed` /
  `already_settled`、共通 `not_found` / `unknown_player` 再利用。
- `.gitignore` に `ledger.json` 追加。

## Regression Test

S3.1 で追加予定:

- `tests/test_ledger_repository.py::test_point_balance_matches_fold_of_entries` — 残高 = fold（ISSUE-0001）。
- `tests/test_ledger_repository.py::test_insufficient_points_rejected` — 残高非負 / cash 補完。
- `tests/test_ledger_repository.py::test_entry_fee_rejects_points` — entry fee cash only（rule 1）。
- `tests/test_ledger_repository.py::test_grant_idempotency` — manual/campaign grant 重複防止（ISSUE-0001 Q4）。
- `tests/test_ledger_repository.py::test_reverse_entry_is_append_only` — 訂正は reversal（mutate 禁止）。
- `tests/test_ledger_repository.py::test_settlement_net_due_signed_sum_cash` — net_due = 符号付き Σ cash。
- `tests/test_ledger_repository.py::test_commit_requires_closed_session` — closed のみ確定。
- `tests/test_ledger_repository.py::test_core_ledger_matches_contract` — code↔contract。

## Affected Files

- `core/ledger.py`（新規, planned）
- `core/ledger_repository.py`（新規, planned）
- `docs/contracts/schemas/{ledger_entry,point_ledger_entry,session_settlement}.schema.json`（新規, planned）
- `docs/contracts/fixtures/{ledger_entry,point_ledger_entry,session_settlement}/`（新規, planned）
- `tests/test_contracts.py`（`_MODELS` 登録）
- `tests/test_ledger_repository.py`（新規, planned）
- `.gitignore`（`ledger.json`）

## Related Worklog

- `docs/worklog/2026-06-10-s3-ledger-planning.md`

## Related ADRs

- `docs/adr/0011-ledger-points-settlement-design-direction.md`
- `docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md`（永続パターン）

## Related Commits

- 起票は S3 planning と同じコミット。実装は別タスク（S3.1）。

## Notes

- ISSUE-0001 の残サブ問題（idempotency_key 運用、speculative 表示）を本 issue / ISSUE-0013 で具体化する。
- 実 schema の freeze（`1.0` 昇格）は ISSUE-0001 残項目 + session schema freeze（#3）後。本 issue は
  draft schema（`0.x`）+ core 実装まで。
