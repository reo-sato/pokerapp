# Worklog: Spec expansion — player registry / session ledger / store settlement scope

## Date

2026-05-22

## Scope / Task

S0 (spec expansion only): hand logger 単体プロジェクトに **player registry / session ledger /
point ledger / session settlement / cross-app boundary** を将来スコープとして追加し、
今後の Phase を安全に切れるよう CLAUDE.md / ADR / issue / decision-log / CHANGELOG を整備する。

## Goal

- code / tests には一切手を入れない。
- CLAUDE.md に新スコープを future scope として明確に追記する（既存実装と混在させない）。
- 新規 ADR 1 件で「なぜこのドメイン分割なのか」を残す。
- open question / risk を issue として 1 件以上残す。
- worklog / decision-log / CHANGELOG を docs-as-code ルールに沿って更新する。
- 既存機能を「実装済」のように書かないこと。未実装は `planned` / `future scope` を明示。

## Changed Files

- `CLAUDE.md` — 新規作成。現状仕様（Phase 7 まで）+ Future Scope セクション +
  Documentation and Traceability Rules を一体記述。
- `CHANGELOG.md` — 新規作成。`Unreleased` セクションに本タスクの docs/spec 追記を記録。
- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md` —
  新規 ADR（Accepted）。
- `docs/issues/0003-point-balance-source-of-truth.md` — point 残高の source of truth が
  未確定であることを open issue として登録。
- `docs/worklog/2026-05-22-spec-expand-session-ledger-scope.md` — 本ファイル。
- `docs/decision-log.md` — 新規作成。ADR-0003 / Issue-0001 を index に登録。
- `docs/templates/{adr,issue,worklog}-template.md` — テンプレート群を bootstrap。

## Expected Behavior

- 後続フェーズ（S1〜S5）が、本 worklog と ADR-0003 を参照するだけで「何を追加すべきか」
  「なぜ分離するのか」「未確定事項は何か」を読み取れる。
- CLAUDE.md は現時点の正仕様（hand logger Phase 7 まで実装済）を保ちつつ、新スコープを
  `Future Scope` セクション以下に隔離し、既存機能と混同させない。
- 共通 ID 契約（player_id / session_id / hand_id）と業務ルール（cash-only entry fee /
  cash+point 併用 / point 不足は cash 補完 / paid-unpaid / player-to-player なし）が
  明文化される。

## Implemented Behavior

期待挙動どおりに以下を確定:

- CLAUDE.md
  - 現時点の実装状況表に hand logger Phase 7 までの ✅／🔨 を記載。
  - `Future Scope` セクションを設け、`Product scope`, `Domain model`, `Business rules`,
    `Cross-app boundary`, `Phase candidates` を追加。
  - `Documentation and Traceability Rules` を末尾に追加（CLAUDE.md / worklog / ADR /
    issue / decision-log / changelog / 完了条件）。
- ADR-0003
  - 分割の動機、各モデルの責務、業務ルール、cross-app 契約を明示。
  - Alternatives として「全部 HandSummary に埋め込む」「session ledger を後付け JSON で」
    「player-to-player settlement まで先に入れる」「seat_assignment を session 単位で持つ」
    を明示的に却下。
  - Follow-up checklist として S1〜S5 着手前に必要な追加 ADR を列挙。
- Issue 0001
  - point 残高の source of truth（fold vs cached vs session-scoped）が未確定であること、
    関連する未決事項 4 件を Reproduction に列挙。
  - S3 着手時に追加する regression test 4 ケースを Fix セクションに予告。
- decision-log.md
  - ADR Index に ADR-0003 を登録。
  - Major Issue Index に ISSUE-0003 を登録。
- CHANGELOG.md
  - `Unreleased / Docs` に CLAUDE.md / ADR-0003 / Issue-0001 / decision-log / templates の
    追加を記録。

## Test Results

- code を変更していないため pytest は走らせていない（後述 "Mismatches" 参照）。
- docs のみのため CI 影響なし。
- 手動レビュー: CLAUDE.md の `Future Scope` セクションが既存実装セクションと混在していないこと、
  実装済表で新スコープを ❌ 未実装 と明示していることを確認。

## Mismatches Found During Testing

None observed — code / tests に手を入れていないため、ランタイム挙動の比較対象がない。
docs-as-code 文書の整合性レビューのみ実施し、以下を確認:

- CLAUDE.md の実装状況表と Future Scope セクションが二重定義になっていないこと（実装済 ✅ と
  planned 🔲 が同じ機能に重複していないこと）。
- ADR-0003 の "Decision" と CLAUDE.md の Domain model セクションの語彙が一致していること
  （`player` / `session` / `seat_assignment` / `hand_ref` / `ledger_entry` / `point_ledger_entry`
  / `session_settlement`）。
- ADR-0003 の Phase candidates と CLAUDE.md の Phase candidates 表が一致していること。

## Fixes Applied

- 当初 CLAUDE.md の実装状況表に「session ledger: ❌ 未実装」のような単独行を入れていたが、
  Future Scope セクションでも同じ機能を扱うため二重定義となっていた。実装状況表では
  「player registry / session ledger / store settlement は **future scope（本ファイル下部参照）**」
  と 1 行に集約し、詳細を下部に寄せた。
- ADR-0003 の "Alternatives Considered" に user 指定の 3 つ（HandSummary 埋め込み / 後付け JSON /
  player-to-player）が含まれることを確認。加えて seat_assignment を session 単位で持つ案を
  Alternative D として追記した（hand-based 採用の根拠を明確にするため）。

## Remaining Gaps / Out-of-Scope

- [ ] S1 着手: `player` データモデル（display_name のみ）と CRUD の最小実装 → 別 ADR/worklog。
- [ ] S2 着手: `session` / `seat_assignment` / `hand_ref` の hand-based 実装 → 別 ADR/worklog。
- [ ] S3 着手: `ledger_entry` / `point_ledger_entry`（point 残高 source of truth は
      `docs/issues/0001` で解決後）→ 別 ADR/worklog。
- [ ] S4 着手: `session_settlement` / paid-unpaid → 別 ADR/worklog。
- [ ] S5 着手: cross-app 参照同期方式 → 別 ADR。
- [ ] "cash-out" の語義整理（issue として別ファイル化候補）。
- [ ] session 中間集計の確定／途中値の UI 表現方針（issue として別ファイル化候補）。

## Related ADRs

- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`

## Related Issues

- `docs/issues/0003-point-balance-source-of-truth.md`

## Related Commits

- 本 worklog と同じコミット（spec expansion phase, code changes なし）
