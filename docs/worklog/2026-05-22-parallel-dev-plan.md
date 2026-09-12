# Worklog: Parallel development plan (core + desktop + mobile)

## Date

2026-05-22

## Scope / Task

planning only: hand logger core / desktop 別画面 / iOS・Android 別アプリ を **並行的** に
育てるための、依存関係つき実行計画と workstream 分割を docs として整備する。
production code の大規模実装は行わない（mobile 技術選定案と screen skeleton 範囲の提案まで）。

## Goal

- WS0 (contract) / WS1 (core) / WS2 (desktop) / WS3 (mobile) の 4 workstream を定義。
- parallelizable / blocker を明確化し、先に凍結すべき contract を列挙。
- phase 0〜5 を goal / prerequisites / parallel tasks / blockers / done criteria で整理。
- mobile が mock で先行する範囲、core が独立実装できる範囲、desktop/mobile 責務分離、
  将来 API/sync を入れても壊れにくい境界を明文化。
- docs-as-code（CLAUDE.md / ADR / worklog / issue / decision-log / CHANGELOG）を更新。

## Changed Files

- `CLAUDE.md` — `# Parallel development plan` 節を新設（workstreams / parallelizable・blocker /
  freeze order / mobile mock 範囲 / 将来 boundary / phase 0–5 計画）。
- `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md` —
  新規 ADR（Accepted）。contract-first / shared-ID / separate-frontends の判断。
- `docs/issues/0003-parallel-dev-contract-drift.md` — 新規 issue（Open）。並行開発の
  contract drift / 凍結タイミングの blocking risk。
- `docs/decision-log.md` — ADR-0004 と ISSUE-0003 を index に追加。
- `CHANGELOG.md` — Unreleased / Doc&Planning に追記。
- `docs/worklog/2026-05-22-parallel-dev-plan.md` — 本ファイル。

## Expected Behavior

- 4 workstream とその依存関係が一目で分かる。
- 「契約凍結（WS0）が常に上流、凍結後は WS1/WS2/WS3 が並行可能」という原則が明文化される。
- 各 phase の blocker（特に ISSUE-0001 が S3 の gate、ISSUE-0003 が全体の drift risk）が
  追跡可能。
- mobile を mock で先行させる前提と、実 repository への差し替え境界が読み取れる。

## Implemented Behavior

期待どおり整備:

- CLAUDE.md に workstream 表・責務分離・parallelizable/blocker・freeze order（共有 ID →
  player → session/seat → ledger/point → settlement → interface）・mobile mock 範囲・
  将来 API/sync 境界・phase 0–5 の構造化計画を追加。
- ADR-0004 で contract-first を採用し、Alternatives（core-first 逐次 / front-end-owned logic /
  implementation-first 暗黙契約 / mobile = hand logger 移植）を明示的に却下。
- ISSUE-0003 で contract drift / 凍結タイミング / mock 乖離 / ISSUE-0001 依存を risk 登録。

## Test Results

- 計画 / docs のみのため code 変更なし。pytest 影響なし（既存 152 passed のまま）。
- 手動レビュー: CLAUDE.md の phase 計画が Phase candidates 表（S0–S5）と整合し、S1 を
  「実装済」と二重表記していないことを確認。

## Mismatches Found During Testing

None observed — code 変更なし。docs 整合性レビューのみ:

- 共有 ID（player_id / session_id / hand_id）の表記が ADR-0003 / ADR-0004 / CLAUDE.md で一致。
- phase 0–5 の依存順が Phase candidates 表（S0–S5）と矛盾しないことを確認。

## Fixes Applied

- なし（不整合は発生しなかった）。

## Remaining Gaps / Out-of-Scope

- [ ] `docs/contracts/` の新設と schema/fixtures・契約テスト（Phase 0 で着手）。
- [ ] mobile 技術選定の確定（React Native 案、Phase 1 着手時に別 worklog）。
- [ ] ISSUE-0001（point 残高 source of truth）の決着（S3 の gate）。
- [ ] repository / service interface の具体定義（Phase 0）。
- [ ] production 実装（S2 以降）は本タスクの scope 外。

## Related ADRs

- `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`
- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`

## Related Issues

- `docs/issues/0003-parallel-dev-contract-drift.md`
- `docs/issues/0001-point-balance-source-of-truth.md`（S3 の直接 blocker）

## Related Commits

- 本 worklog と同じコミット（parallel development planning）
