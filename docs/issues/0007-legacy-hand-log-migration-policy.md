# Issue 0007: Legacy hand log（timestamp session_id / player_id 無し）の取り込み方針が未確定

## Date

2026-06-03

## Status

Open

## Severity / Priority

- Severity: Low（Phase 2.x の本筋を妨げない。Phase 3 の集計対象範囲に影響）
- Priority: P3

## Area

hand logger × session 接続 / data migration

## Expected Behavior

過去の `logs/{YYYY-MM-DD_HHMMSS_session1}.json`（timestamp session_id、`players[i]` に
`player_id` が無い）を、必要に応じて SessionRepository / 将来 ledger 集計の view に取り込めるか、
取り込まないかの方針が決まっている。

## Actual Behavior

未確定。具体的な open question:

1. **取り込み是非**: そもそも legacy 取り込みは必要か？（Phase 3 settlement の集計対象を
   「Phase 2.2 以降の新形式 hand のみ」に限定するなら不要）
2. **player_id への fuzzy match**: `players[i].name`（人間表示名）と `PlayerRepository` の
   `display_name` の照合を自動でやるか、人間 confirm 必須にするか。同名 player や typo の扱い。
3. **元ファイルを書き換えるか**: 既存 logs/*.json を additive 編集する（破壊リスク）か、
   sidecar `logs/{session_id}.player_id.json` を別に持つ（join 必須）か。
4. **session の作り方**: 各 legacy timestamp を `SessionRepository.create_session(label="legacy:...")`
   で 1 session ずつ作るか、まとめて 1 仮想 session に集約するか。
5. **失敗時の扱い**: 取り込めなかった hand の表示・集計上の扱い（null player_id を許容 / 除外 / 警告）。

## Reproduction

設計レビュー（バグではなく未確定の data policy）:

1. `logs/` 配下の既存 JSON は `HandSummary.players[i]` に `player_id` を持たない。
2. `docs/contracts/hand-integration.md` § 5 の「legacy log の扱い」を参照。

## Root Cause

ADR-0008 が write-through 戦略を採用したことで「Phase 2.2 以降の hand」は session レイヤから
完全な参照を得られるが、「Phase 2.2 以前の hand」は元情報に player_id を持たないため、後追い
reconcile しない限り session レイヤ・ledger 集計の対象にできない。reconcile の優先度・必要性が
プロダクト要件（過去ログまで settlement で扱うか）次第。

## Fix

未対応（Phase 2.4 着手判断時に決める）。決まったら:

- 取り込み「不要」の場合: 本 issue を Resolved（won't fix）にして閉じる。
- 取り込み「必要」の場合: `tools/reconcile_legacy_logs.py`（planned）の仕様を `hand-integration.md` に
  追記し、sidecar / fuzzy match / confirm UX を決める。

## Regression Test

- 現状ではテスト対象なし。実装する場合は:
  - fuzzy match のテスト（同名 player / 別名 / 未登録）
  - sidecar ファイル生成と join のテスト
  - 元 logs/*.json が一切変更されないことの不変性テスト
  を追加する。

## Affected Files

- `logs/*.json`（読み取り対象, 不変前提）
- `tools/reconcile_legacy_logs.py`（planned, 未作成）
- `core/session_repository.py`（取り込み時に既存 create_session / assign_seat を呼ぶ。API 不変）
- `docs/contracts/hand-integration.md`（方針の住み処）

## Related Worklog

- `docs/worklog/2026-06-03-s2.x-hand-integration-planning.md`

## Related ADRs

- `docs/adr/0008-hand-logger-session-integration-strategy.md`（write-through + legacy 不可侵）

## Related Commits

- 本 issue と同じコミット（S2.x integration planning）

## Notes

優先度は Phase 3（ledger / settlement）の集計対象がプロダクトとして「過去含む」必要があるかで決まる。
そうでなければ Phase 2.2 以降の hand に限定して問題ない可能性が高い。
