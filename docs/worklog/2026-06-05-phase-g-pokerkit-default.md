# Worklog: Phase G — pokerkit live 既定切替（+ F3c 確認）

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の **Phase G**（GitHub issue #9）: live 既定 game-state backend を `legacy` →
`pokerkit` に切替。あわせて **F3c**（PHH の call/check 区別）を調査し、PHH 標準では不要と確認。
統合ブランチ `v1-integration`、作業ブランチ `claude/phaseG-pokerkit-default`。実機 E2E は Phase H。

## Goal

- `config_default.json` の既定を pokerkit に。pokerkit を pin。legacy rollback を維持。
- F3c: PHH の cc 統一が正しいことを確認し、誤修正を防ぐコメント + docs。
- 回帰ゼロ（テストは明示 backend のため既定切替の影響を受けない）。

## Changed Files

- `config_default.json`: `engine.backend` `legacy` → `pokerkit`（_comment 更新）。
- `requirements.txt`: `pokerkit>=0.5.0` → `pokerkit>=0.7.0,<0.8.0`（golden fixtures の凍結挙動に合わせ pin）。
- `output/phh_exporter.py`: `cc`（check-or-call）統一が PHH 標準である旨のコメント追加（F3c）。
- `tests/test_phase_g_default.py`（新規 4: 既定 pokerkit / fresh install / legacy rollback / pokerkit 構築）。
- docs: ADR-0012 + decision-log / CHANGELOG / CLAUDE.md（実装状況・既定の記述更新）/ 本 worklog。

## Decisions

- **G**: 既定 = pokerkit（config_default 経由）。legacy は config rollback + 未知 backend fallback として残す。
  pokerkit を hard dependency として pin。切替の必要条件（全 golden 緑）は達成済。
- **F3c**: PHH 標準では check/call は同一 `cc`。区別は非標準で round-trip 不能になるため**変更しない**
  （既存 test_call/test_check も `cc` を期待）。check/call の別は JSON ログ側に保持。

## Expected vs Implemented

- fresh install（config.json 不在）→ config_default コピー → `backend=pokerkit`。
- `create_game_state("legacy")` → `GameStateManager`（rollback 可）、`create_game_state("pokerkit")` →
  `PokerkitGameState`（合法手あり）。
- 既存 config.json（gitignore・未 track）は影響なし。fresh clone は config_default の pokerkit を得る。

## Test Results

- `pytest tests/test_phase_g_default.py tests/test_phh_exporter.py -q` — **39 passed**。
- `pytest tests/ -q --ignore=tests/test_vision.py` — **286 passed, 0 skipped**（回帰なし）。

## Mismatches Found During Testing

- F3c は「バグ」ではなかった（PHH 標準で cc 統一が正）。ロードマップの想定を訂正し、変更せず確認に留めた。

## Fixes Applied

- なし（設定既定の切替 + pin + コメント）。

## Remaining Gaps / Out-of-Scope

- [ ] **Phase H**: クリーン環境で実機 E2E（音声のみ→JSON/PHH、pokerkit 既定）。依存 pin 全体・vision 系
      （opencv/easyocr）除外・PyInstaller・CI（skip 0）。
- [ ] 重み（confidence / cap）の最終較正、camera 源。
- [ ] **Phase E**（Session 統合）は並行・独立で未着手。

## Related ADRs / Issues

- ADR-0012（本タスク）/ ADR-0009（pokerkit authority, 当初 default-off）。
- GitHub issue #9（Phase G）/ Epic #4。リスク R-2（既定切替回帰）。

## Related Commits

- （本タスクの commit を後で追記）
