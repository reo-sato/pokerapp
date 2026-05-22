# Changelog

This project's changelog follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project does not yet have versioned releases; entries are organized
per implementation phase under `[Unreleased]` and grouped per phase when
a release is cut.

For implementation-level history (per-task worklog, decision rationale,
issue / mismatch log) see `docs/worklog/`, `docs/adr/`, `docs/issues/`.

## [Unreleased]

### Fixed

- **Phase 5-I** — GUI 手動入力 (席ドロップダウン + action ボタン + amount entry)
  が `GameStateManager.apply_action` を直叩きしていたため、`BettingState` が
  更新されず以下 5 件のポーカールール違反が通っていた:
  - 同一 seat 連続 raise (turn order bypass),
  - `call 0` がそのまま記録 (to_call 補完なし),
  - bet vs raise 混同 (current_bet 追跡なし),
  - street が preflop のまま (BettingState 未更新),
  - SB/BB auto-post が manual hand では見えない。

  `ManualActionEvent` + `IntegrationThread.manual_queue` +
  `_handle_manual_action_event` を新設し、音声経路と同等品質で処理する
  ように修正。詳細:
  - `docs/issues/0001-manual-input-poker-rule-violations.md` (バグレポート)
  - `docs/adr/0001-route-manual-input-via-integration-thread.md` (設計判断)
  - `docs/worklog/2026-05-22-phase-5-I.md` (実装ログ)

### Added

- `ManualActionEvent(seat, action, amount, timestamp)` in `core/events.py`.
- `IntegrationThread.manual_queue` property + `_drain_manual_queue` +
  `_handle_manual_action_event`.
- Tests: 11 件 (7 in `tests/test_manual_action.py`, 4 in `tests/test_gui.py`)。
- Docs-as-code infrastructure: `CHANGELOG.md` + `docs/{worklog,adr,issues,decision-log}`。
- "Documentation and Traceability Rules" section in `CLAUDE.md`.

### Changed

- `gui/dashboard.py:_cmd_manual_action` を `manual_queue` push 経路に書き換え。
  GUI スレッドから `gs.apply_action` の直叩きはしない。
