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
| ADR-0005 | Contracts repository layout and freeze workflow                                    | Accepted | 2026-05-22 | contracts / drift detection | `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`                                | 関連: ADR-0004             |
| ADR-0006 | S2 session/seating contract boundary and hand_id cross-app reference               | Accepted | 2026-05-25 | contracts / shared-ids (S2) | `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md`                    | 関連: ADR-0003 / ISSUE-0004 |
| ADR-0007 | S2 session layer persistence and session_id issuance                               | Accepted | 2026-05-25 | core / session (S2)         | `docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md`                                   | 関連: ADR-0006 / ISSUE-0005 |
| ADR-0008 | Hand logger × session/seating integration strategy (Phase 2.x)                     | Accepted | 2026-06-03 | hand logger × session (S2.x) | `docs/adr/0008-hand-logger-session-integration-strategy.md`                                       | 関連: ADR-0006 / ADR-0007 / ISSUE-0005 / 0006 / 0007 |
| ADR-0009 | Remove camera / vision input per sprc_v4.docx (2-source: RFID + audio)              | Accepted | 2026-06-04 | hand logger / vision removal | `docs/adr/0009-remove-camera-vision-per-v4-spec.md`                                               | 関連: ADR-0003 / sprc_v4.docx |

<!--
Note: ADR-0001 / ADR-0002 は本リポジトリの spec expansion phase (S0) 時点で空番。
過去判断のうち ADR 化したい既存決定が出てきた場合は、その時点で 0001/0002 を遡及採番する。
-->

## Major Issue / Mismatch Index

| ID         | Title                                                  | Status | Date       | Area                     | File                                                       | Related Fix / Commit |
|------------|--------------------------------------------------------|--------|------------|--------------------------|------------------------------------------------------------|----------------------|
| ISSUE-0001 | point ledger の残高計算と source of truth が未確定     | Open   | 2026-05-22 | spec / future-scope (S3) | `docs/issues/0001-point-balance-source-of-truth.md`        | —                    |
| ISSUE-0002 | display_name の uniqueness 仕様の将来拡張が未確定      | Open   | 2026-05-22 | player registry (S1)     | `docs/issues/0002-display-name-uniqueness-scope.md`        | —                    |
| ISSUE-0003 | 並行開発の contract drift / 凍結タイミング risk       | Open   | 2026-05-22 | planning (WS0–WS3)       | `docs/issues/0003-parallel-dev-contract-drift.md`          | Phase 0a で部分緩和  |
| ISSUE-0004 | hand_id が int と cross-app 文字列契約で不整合        | Resolved | 2026-05-22 | contracts / shared-ids   | `docs/issues/0004-hand-id-int-vs-cross-app-string.md`      | ADR-0006（複合キー採用） |
| ISSUE-0005 | S2 session/seating freeze の未確定事項               | Open   | 2026-05-25 | contracts / session (S2) | `docs/issues/0005-s2-session-seating-freeze-blockers.md`   | ADR-0007（#1/#2 を core で確定）。hand logger 接続は ADR-0008 / Phase 2.2 で config-gated 実装済（freeze は引き続き保留） |
| ISSUE-0006 | Hand 開始時の seat→player_id 選択 UX が未確定        | Partially Resolved | 2026-06-03 | hand logger × session / GUI | `docs/issues/0006-seat-selection-ux-at-hand-start.md`      | Phase 2.3 で最小 seat UX 実装（`gui/seat_assignment.py`）。高度 UX（sitting_out 等）は残 Open |
| ISSUE-0007 | Legacy hand log（timestamp/no player_id）の取り込み方針 | Open | 2026-06-03 | data migration / S2.x       | `docs/issues/0007-legacy-hand-log-migration-policy.md`     | Phase 2.4 着手判断時 |
| ISSUE-0008 | Session / Seating Viewer の拡張（filter / live / export 等） | Open | 2026-06-04 | desktop GUI (WS2) / inspection | `docs/issues/0008-session-viewer-enhancements.md`          | WS2-α で read-only 最小版を実装。拡張は保留 |
