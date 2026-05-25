# Issue 0003: 並行開発における contract drift / 凍結タイミングの blocking risk

## Date

2026-05-22

## Status

Open

## Severity / Priority

- Severity: High（並行開発の根幹を揺るがしうる）
- Priority: P1

## Area

planning / parallel development (WS0–WS3)

## Expected Behavior

ADR-0004 の contract-first 方針では、ある domain model の schema・共有 ID・validation・
error 形・repository interface が **凍結された後** に WS1 (core) / WS2 (desktop) /
WS3 (mobile mock) が並行実装に入る。契約が単一の source（`docs/contracts/`, planned）で
管理され、変更は全 WS に追跡可能な形で波及する。

## Actual Behavior

未実装（計画段階）。並行開発を始めると以下の blocking risk が顕在化しうる。

1. **Contract drift**: 契約凍結後に core が schema を「実装の都合で」変更し、mobile mock /
   desktop が古い契約のまま進んでしまう。結合時に食い違う。
2. **凍結タイミングの曖昧さ**: 「どの状態をもって凍結とみなすか」が未定義だと、各 WS が
   別々の前提で着手し、early start による手戻りが発生する。
3. **mock と実 repository の乖離**: mobile の mock が契約 fixtures から逸脱し、実 repository
   差し替え時に UI 層が壊れる。
4. **ISSUE-0001 依存**: S3 の残高計算 API 契約は point 残高 source of truth（ISSUE-0001）が
   決まらないと凍結できず、ledger 系の全 WS がブロックされる。

## Reproduction

計画レビューによる想定（バグではなく risk register）:

1. WS0 が player schema を凍結 → WS3 が mock を書く。
2. その後 WS1 が `display_name` 正規化ポリシー（ISSUE-0002）を変更 → 契約が drift。
3. WS3 の mock とテストが古い前提のまま緑になり、結合時に初めて食い違いが発覚。

## Root Cause

契約の凍結状態・変更手順・drift 検出手段が未整備。`docs/contracts/` がまだ存在せず、
契約に対する自動検証（fixtures vs schema テスト）もない。

## Fix

未対応（Phase 0 で対処）。想定する対策:

- `docs/contracts/` を新設し、schema とサンプル fixtures を単一 source として置く。
- 契約変更は ADR / decision-log を伴う明示的プロセスにし、「凍結 = ADR Accepted + fixtures 確定」
  と定義する。
- core / front-end の双方に **同じ fixtures に対する契約テスト** を持たせ、drift を CI で検出。
- mobile mock は契約 fixtures のみを読み、独自データを持たない。
- ISSUE-0001 を S3 着手の gate にする（残高契約凍結まで ledger WS を本実装に入れない）。

## Regression Test

未実装。Phase 0 で契約テスト（fixtures に対する schema/validation 検証）を導入予定。
導入後は契約変更が全 front-end のテストに波及するようにする。

## Affected Files

現時点ではなし（計画）。Phase 0 で `docs/contracts/` と契約テストを新設予定。

## Related Worklog

- `docs/worklog/2026-05-22-parallel-dev-plan.md`

## Related ADRs

- `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`
- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`

## Related Commits

- 本 issue と同じコミット（parallel development planning）

## Notes

関連 open question:

- ISSUE-0001（point 残高 source of truth）は S3 ledger WS の直接 blocker。
- ISSUE-0002（display_name uniqueness）は player 契約変更時の drift 例として参照。
