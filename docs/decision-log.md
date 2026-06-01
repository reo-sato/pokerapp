# Decision Log

このファイルは、リポジトリ内の **ADR (Architecture Decision Records)** と
**主要な issue / mismatch log** を横断参照するための索引。
個別の理由付け・経緯は各 ADR / issue ファイル本体を参照すること。
ここは「どの判断・どの不具合が、どのファイル・どの commit・どのテストに紐付くか」を
一覧するための薄い index に徹する。

## 運用ルール

詳細は `CLAUDE.md` の **Documentation and Traceability Rules** を参照。要点:

- 新しい ADR (`docs/adr/NNNN-*.md`) を追加したら、下の **ADR Index** に必ず 1 行追加する。
- 主要な issue / mismatch (`docs/issues/NNNN-*.md`) を追加したら、下の
  **Major Issue / Mismatch Index** に必ず 1 行追加する。
- ADR を supersede する場合は、新 ADR を追加した上で **旧 ADR の Status を `Superseded` に
  更新** し、両側に back-link を入れる（旧 ADR を削除しない）。
- 表は ID 昇順を維持。新規行は末尾に追加する。
- 各テンプレートの所在:
  - ADR: `docs/templates/adr-template.md`
  - Worklog: `docs/templates/worklog-template.md`
  - Issue: `docs/templates/issue-template.md`

> 注: ADR-0001 / 0002 と ISSUE-0001 / 0002 は hand logger の手動入力系（Phase 5-I/J）に由来。
> ISSUE-0003〜0006 は registry/contracts 系（旧 main の ISSUE-0001〜0004 を統合時に採番し直したもの）。
> ブランチ統合の経緯は `docs/worklog/2026-05-22-branch-consolidation.md` を参照。

## ADR Index

| ID       | Title                                                                              | Status   | Date       | Related Area                  | File                                                                                              | Supersedes / Superseded by |
|----------|------------------------------------------------------------------------------------|----------|------------|-------------------------------|---------------------------------------------------------------------------------------------------|----------------------------|
| ADR-0001 | Route GUI manual input through IntegrationThread (Phase 5-I)                       | Accepted | 2026-05-22 | hand logger / manual input    | `docs/adr/0001-route-manual-input-via-integration-thread.md`                                      | —                          |
| ADR-0002 | Manual action actor mismatch is strict reject (Phase 5-J)                          | Accepted | 2026-05-22 | hand logger / manual input    | `docs/adr/0002-manual-action-actor-mismatch-strict-reject.md`                                     | —                          |
| ADR-0003 | Expand domain from hand logging to session ledger and store settlement             | Accepted | 2026-05-22 | spec / domain model / scope   | `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`         | —                          |
| ADR-0004 | Contract-first parallel development with shared IDs and separate front-ends        | Accepted | 2026-05-22 | planning / workstreams        | `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`          | 関連: ADR-0003             |
| ADR-0005 | Contracts repository layout and freeze workflow                                    | Accepted | 2026-05-22 | contracts / drift detection   | `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`                                | 関連: ADR-0004             |
| ADR-0006 | RFID hardware migration to PN5180 + ESP32-S3 (HTTP transport contract unchanged)   | Accepted | 2026-06-01 | rfid / hardware boundary      | `docs/adr/0006-rfid-hardware-migration-pn5180-esp32s3.md`                                         | —                          |

## Major Issue / Mismatch Index

| ID         | Title                                                              | Status   | Date       | Area                       | File                                                          | Related Fix / Commit  |
|------------|--------------------------------------------------------------------|----------|------------|----------------------------|---------------------------------------------------------------|-----------------------|
| ISSUE-0001 | Manual GUI input bypasses BettingState, 5 件のポーカールール違反   | Resolved | 2026-05-22 | hand logger / manual input | `docs/issues/0001-manual-input-poker-rule-violations.md`      | Phase 5-I (ADR-0001)  |
| ISSUE-0002 | Manual action actor mismatch の permissive warning が live state 汚染 | Resolved | 2026-05-22 | hand logger / manual input | `docs/issues/0002-manual-action-permissive-warning-too-soft.md` | Phase 5-J (ADR-0002)  |
| ISSUE-0003 | point ledger の残高計算と source of truth が未確定                 | Open     | 2026-05-22 | spec / future-scope (S3)   | `docs/issues/0003-point-balance-source-of-truth.md`           | —                     |
| ISSUE-0004 | display_name の uniqueness 仕様の将来拡張が未確定                  | Open     | 2026-05-22 | player registry (S1)       | `docs/issues/0004-display-name-uniqueness-scope.md`           | —                     |
| ISSUE-0005 | 並行開発の contract drift / 凍結タイミング risk                   | Open     | 2026-05-22 | planning (WS0–WS3)         | `docs/issues/0005-parallel-dev-contract-drift.md`             | Phase 0a で部分緩和   |
| ISSUE-0006 | hand_id が int と cross-app 文字列契約で不整合                    | Open     | 2026-05-22 | contracts / shared-ids     | `docs/issues/0006-hand-id-int-vs-cross-app-string.md`         | S2 (hand_ref) で確定  |
| ISSUE-0007 | PN5180+ESP32-S3 移行の未確定事項（タグ規格 / USB-CDC transport） | Open     | 2026-06-01 | rfid / hardware boundary   | `docs/issues/0007-rfid-pn5180-esp32s3-open-questions.md`      | ADR-0006              |
