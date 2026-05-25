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

## ADR Index

| ID       | Title                                                                              | Status   | Date       | Related Area                | File                                                                                              | Supersedes / Superseded by |
|----------|------------------------------------------------------------------------------------|----------|------------|-----------------------------|---------------------------------------------------------------------------------------------------|----------------------------|
| ADR-0003 | Expand domain from hand logging to session ledger and store settlement             | Accepted | 2026-05-22 | spec / domain model / scope | `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`         | —                          |
| ADR-0004 | Contract-first parallel development with shared IDs and separate front-ends        | Accepted | 2026-05-22 | planning / workstreams      | `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`          | 関連: ADR-0003             |

<!--
Note: ADR-0001 / ADR-0002 は本リポジトリの spec expansion phase (S0) 時点で空番。
過去判断のうち ADR 化したい既存決定が出てきた場合は、その時点で 0001/0002 を遡及採番する。
-->

## Major Issue / Mismatch Index

| ID         | Title                                                  | Status | Date       | Area                     | File                                                       | Related Fix / Commit |
|------------|--------------------------------------------------------|--------|------------|--------------------------|------------------------------------------------------------|----------------------|
| ISSUE-0001 | point ledger の残高計算と source of truth が未確定     | Open   | 2026-05-22 | spec / future-scope (S3) | `docs/issues/0001-point-balance-source-of-truth.md`        | —                    |
| ISSUE-0002 | display_name の uniqueness 仕様の将来拡張が未確定      | Open   | 2026-05-22 | player registry (S1)     | `docs/issues/0002-display-name-uniqueness-scope.md`        | —                    |
| ISSUE-0003 | 並行開発の contract drift / 凍結タイミング risk       | Open   | 2026-05-22 | planning (WS0–WS3)       | `docs/issues/0003-parallel-dev-contract-drift.md`          | —                    |
