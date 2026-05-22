# Decision Log

このファイルは、リポジトリ内の **ADR (Architecture Decision Records)** と
**主要な issue / mismatch log** を横断参照するための索引。
個別の理由付け・経緯は各 ADR / issue ファイル本体を参照すること。
ここは「どの判断・どの不具合が、どのファイル・どの commit・どのテストに
紐付くか」を一覧するための薄い index に徹する。

## 運用ルール

詳細は `CLAUDE.md` の **Documentation and Traceability Rules** を参照。要点:

- 新しい ADR (`docs/adr/NNNN-*.md`) を追加したら、下の **ADR Index** に
  必ず 1 行追加する。
- 主要な issue / mismatch (`docs/issues/NNNN-*.md`) を追加したら、下の
  **Major Issue / Mismatch Index** に必ず 1 行追加する。
- ADR を supersede する場合は、新 ADR を追加した上で **旧 ADR の Status を
  `Superseded` に更新** し、両側に back-link を入れる (旧 ADR を削除しない)。
- 表は ID 昇順を維持。新規行は末尾に追加する。
- 各テンプレートの所在:
  - ADR: `docs/templates/adr-template.md`
  - Worklog: `docs/templates/worklog-template.md`
  - Issue: `docs/templates/issue-template.md`

## ADR Index

| ID       | Title         | Status   | Date       | Related Area | File                              | Supersedes / Superseded by |
|----------|---------------|----------|------------|--------------|-----------------------------------|----------------------------|
| ADR-0001 | _placeholder_ | Proposed | YYYY-MM-DD | —            | `docs/adr/0001-placeholder.md`    | —                          |

## Major Issue / Mismatch Index

| ID         | Title         | Status | Date       | Area | File                                  | Related Fix / Commit |
|------------|---------------|--------|------------|------|---------------------------------------|----------------------|
| ISSUE-0001 | _placeholder_ | Open   | YYYY-MM-DD | —    | `docs/issues/0001-placeholder.md`     | —                    |
