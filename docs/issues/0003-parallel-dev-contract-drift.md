# Issue 0003: 並行開発における contract drift / 凍結タイミングの blocking risk

## Date

2026-05-22

## Status

Open（Phase 0a で部分緩和。残りは Phase 0b〜各 phase の freeze で対処）

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

**Phase 0a で部分緩和済**（contracts bootstrap）。実施:

- `docs/contracts/` を新設し、schema・fixtures・shared IDs・freeze/versioning ルールを単一
  source として置いた（ADR-0005）。
- 「凍結 = schema commit + fixtures 揃い + contract test 通過 + 承認 ADR Accepted」と定義した
  （`docs/contracts/versioning-and-freeze.md`）。
- `tests/test_contracts.py` で schema↔fixture（valid 通過 / invalid 違反）と code↔contract
  （core 生成 Player が schema 適合）を検証する最小 drift detection を導入した。
- breaking change は新 ADR / issue + version bump を要求するルールを明文化した。

**残（Phase 0b〜）**:

- core / front-end の **双方** が同じ fixtures を読む構造（mobile mock の実装は WS3 着手時）。
- CI に `jsonschema` を含め contract test を必須化する。
- ISSUE-0001 を S3 着手の gate にする（残高契約凍結まで ledger WS を本実装に入れない）。
- schema 変更 PR での front-end stub 影響確認の運用徹底。

## Regression Test

- `tests/test_contracts.py`（Phase 0a 追加）: schema 妥当性 / fixtures 整合 / code↔contract。
  `jsonschema` 未導入環境では skip、導入環境では必ず実行。
- 将来: 各 model（session / ledger / settlement）の schema・fixtures 追加時に同 test を拡張する。

## Affected Files

- `docs/contracts/**`（新設, Phase 0a）
- `tests/test_contracts.py`（新設, Phase 0a）
- `requirements.txt`（`jsonschema` 追加）

## Related Worklog

- `docs/worklog/2026-05-22-parallel-dev-plan.md`

## Related ADRs

- `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`（Phase 0a の緩和実体）
- `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`
- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`

## Related Commits

- 本 issue と同じコミット（parallel development planning）

## Notes

関連 open question:

- ISSUE-0001（point 残高 source of truth）は S3 ledger WS の直接 blocker。
- ISSUE-0002（display_name uniqueness）は player 契約変更時の drift 例として参照。
