# Worklog: schema `1.0` freeze（S2 / S3 / viewer model, 統合後）

## Date

2026-06-13

## Scope / Task

ロードマップ #1（schema `1.0` freeze, S4）。統合後に全 consumer が安定したため、draft `0.x` だった
8 model を依存順に `1.0` へ freeze する（ADR-0019）。上流 blocker の ISSUE-0005 を解消。

## Goal

session/seat_assignment/hand_ref（S2）+ ledger_entry/point_ledger_entry（S3）+ session_settlement（S4）
+ order_request/player_session_summary（viewer）の schema を `1.0` 化し、全 model に code↔contract
drift gate を整備。以後 additive-only に移行。

## Changed Files

- `docs/contracts/schemas/{session,seat_assignment,hand_ref,ledger_entry,point_ledger_entry,session_settlement,order_request,player_session_summary}.schema.json`
  — version `0.1`→`1.0`、description の「draft (未 freeze)」→「1.0 freeze 済 (ADR-0019)」。
- `tests/test_contracts.py` — `test_core_session_matches_contract`（Session/SeatAssignment/HandRef）
  と `test_core_settlement_matches_contract`（SessionSettlement, commit 後）を追加（ledger は既存、
  order_request/player_session_summary は viewer テストでカバー済み）。
- `docs/adr/0019-schema-1_0-freeze-post-integration.md`（新規, Accepted）。
- `docs/issues/0005-s2-session-seating-freeze-blockers.md` — Resolved（残 blocker は E1/E2/E3 で解消）。
- `docs/contracts/versioning-and-freeze.md` — freeze order 表を frozen に更新（残 draft は #6 interface のみ）。
- `docs/contracts/session-seating.md` / `ledger-overview.md` / `ledger-schema.md` — Status を frozen `1.0` に。
- `docs/decision-log.md` — ADR-0019 追加 + ISSUE-0005 Resolved。
- `CLAUDE.md` — contracts 説明・Domain model・ロードマップ S4 行・残作業・実装状況の freeze 表記更新。
- `CHANGELOG.md` — docs/contract freeze の Unreleased 追記。

## Expected / Implemented Behavior

schema の version と description のみ変更（構造・fixtures は不変 = breaking なし）。8 model が `1.0`、
全 model に runtime code↔contract test。freeze 後の変更は additive-only（versioning-and-freeze.md §2）。

## Test Results

- `pytest tests/ --ignore=tests/test_vision.py -q` — **478 passed**（+2 = session/settlement code↔contract）。
- `pytest tests/test_contracts.py -q` — 18 passed。
- `ruff check .` — clean。

## Mismatches Found During Testing

None。settlement の code↔contract は `commit_settlement`（settled_at を埋める）で検証し、
speculative 行（settled_at 未設定）の null 問題を回避した。

## Remaining Gaps / Out-of-Scope

- freeze order #6（repository / service interface・sync 契約, S5）の freeze は cross-app sync 着手時。
- settlement の partial-paid は additive 拡張（必要時に worklog）。
- 実機 E2E（Phase H）・PN5180 firmware 契約（ISSUE-0014/0015）はロードマップ別項目。

## Related ADRs / Issues

- ADR-0019（本 freeze）/ ADR-0006/0007（S2）/ ADR-0016（S3）/ ADR-0017/0018（viewer）
- ISSUE-0005（Resolved）/ ISSUE-0001（Resolved, fold）/ ISSUE-0011（hand/action freeze 既済）

## Related Commits

- 本 commit（schema 1.0 freeze）
