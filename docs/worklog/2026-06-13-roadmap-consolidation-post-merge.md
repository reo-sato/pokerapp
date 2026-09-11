# Worklog: ロードマップ整理（verify-v1 統合後）

## Date

2026-06-13

## Scope / Task

viewer/mobile/注文（M1/M2/M5）を verify-v1（S 系会計）にマージした後、CLAUDE.md の前向きロードマップが
2 つの採番（S 系 / M 系）の同居で断片化していたため、3 トラック（S=会計 / M=player 向け / R=hand core）
統合後の実態に合わせて整理する（docs-only）。

## Goal

CLAUDE.md の Future Scope / Phase 計画を「現状（大半実装済）+ 残作業」が一目で分かる単一の
一貫したロードマップにし、陳腐化した記述（「すべて未実装」「未着手」等）を実態へ更新する。

## Changed Files

- `CLAUDE.md`:
  - Future Scope 冒頭の「すべて未実装」を統合後の実態（3 トラック大半実装済 + 残作業）に書き換え。
  - 「Phase candidates」表を **「ロードマップ（3 トラック統合後）」** に再構成: 会計 S / player 向け M /
    hand core R をトラック別に状態表記。M4 破棄・M6/M7 は S3 に合流・採番替え（ADR-0017/0018・
    ISSUE-0019）を明記。**残作業（次にやること）8 項目**を列挙。
  - Domain model / Business rules の「未実装・今後実装」注記を「core 実装済・契約定義として残す」へ。
  - 実装状況表の「settlement schema freeze (S4)」行を schema freeze 全体に拡張 + 実機 E2E/PN5180
    firmware / S5 sync 行を追加。
  - Product scope の cross-app 行を「read-only API は M1 で前倒し実現」に更新。
  - Phase 別計画の Phase 2（mobile session 実装済）/ Phase 3（ledger desktop=S3.2・mobile=M2 実装済、
    Done 達成）/ Phase 4（settlement core 済・残は freeze と commit UI）/ Phase 5（read-only boundary
    は M1 で実現）の done-criteria を実態へ更新。存在しない `tests/test_point_ledger.py` 参照を修正。
- `docs/worklog/2026-06-13-roadmap-consolidation-post-merge.md`（本ファイル）
- `CHANGELOG.md`: docs 更新の Unreleased 追記。

## Expected / Implemented Behavior

docs-only。コード・テスト・契約に変更なし。CLAUDE.md が現状（S1〜S3 + M1/M2/M5 + R0–G 実装済、
S4 schema freeze / Phase H 実機 E2E / PN5180 firmware / S5 sync が残）を正しく表す。

## Test Results

- `pytest tests/ --ignore=tests/test_vision.py -q` — 476 passed（docs-only のため不変）。
- `pytest tests/test_contracts.py -q` — 16 passed。

## Remaining Gaps / Out-of-Scope

整理した残作業そのもの（schema `1.0` freeze、実機 E2E、PN5180 firmware 契約、S5 sync、settlement
拡張、R 系較正、ISSUE-0019 再評価）は本タスクの対象外（ロードマップとして記述したのみ）。

## Related ADRs / Issues

- ADR-0016（ledger/points/settlement）/ ADR-0017（viewer API）/ ADR-0018（注文）/ ISSUE-0005（freeze blocker）
- ISSUE-0014/0015（PN5180 firmware 契約）/ ISSUE-0019（プライバシー）

## Related Commits

- 直前の統合 merge commit（viewer/mobile/orders → verify-v1）
