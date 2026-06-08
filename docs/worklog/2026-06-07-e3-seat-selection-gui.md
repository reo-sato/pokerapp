# Worklog: E3 — seat→player_id 選択 GUI + session レイヤ live 有効化

## Date

2026-06-07

## Scope / Task

Phase 2.x E3（v1 issue #10 / Epic #4, ISSUE-0006）: hand logger に seat→player_id を選ぶ GUI を追加し、
`config.session_layer.enabled=true` で S2 session レイヤへの write-through を **GUI から live に有効化**する。
E1+E2-core（`IntegrationThread` の `session_repo`/`seat_player_map` DI + write-through）は実装済で、
本タスクは残っていた seat 選択 UI と `main.py` 結線を additive に入れる。

## Goal

- operator が GUI で seat→player を割り当て、`assign_seat` write-through と `HandSummary.players[i].player_id`
  埋め込みが live で動く。
- UX（ISSUE-0006）は user 決定どおり: **セッション開始時に一度設定 + 必要時のみ「座席設定」ボタンで編集**
  （毎ハンドは出さず carry-forward）、**未登録 player はダイアログ内でその場作成**、空席は割り当てない。
- **既定 off では完全に従来動作**（rollback path）。CI は skip 0 を維持。

## Changed Files

- `gui/seat_selection.py`（新規）— `SeatSelectionDialog`（customtkinter モーダル Toplevel）。席ごとに
  `CTkOptionMenu` で player を選択／未登録はその場 `create_player`／空席は skip。`get_result()` で
  `dict[int,str]`（OK）or `None`（キャンセル）を返す。map 構築・重複検証・carry-forward 解決は
  module-level 純関数（`build_result_map` / `find_duplicate_player` / `resolve_initial_selections`）に分離。
- `integration/engine.py` — `IntegrationThread.set_seat_player_map()` を追加。`_seat_player_map` を更新し
  `_session_layer_active`（`session_repo` 有り **かつ** map 非空）を再評価する。GUI が構築後に seating を
  確定/変更するための setter。
- `gui/dashboard.py` — `__init__` に `player_repo` / `session_layer_enabled` を additive 追加。接続時のみ
  「座席設定」ボタンを表示し、`run()` で起動時に一度ダイアログを promote。OK で `set_seat_player_map` に反映。
- `main.py`（`run_gui`）— `session_layer.enabled=true` のとき `PlayerRepository`/`SessionRepository` を構築し、
  `create_session` の UUID4 hex を `JsonWriter` の session_id に採用、`session_repo` を `IntegrationThread` に DI。
  既定 off では従来どおり timestamp session_id・`session_repo=None`。
- `config_default.json` — `session_layer._comment` を E3 実装済の内容に更新（既定値は false のまま）。
- `tests/test_seat_selection.py`（新規）/ `tests/test_engine_session_setter.py`（新規）。
- docs: `CLAUDE.md`（実装状況表 + dir tree + Phase 2.x）/ `CHANGELOG.md` / `docs/issues/0006`（Resolved）/
  `docs/decision-log.md` / `docs/contracts/hand-integration.md`（§5 + Phase 2.3）。

## Expected Behavior

- 接続時（enabled）: 起動時に座席ダイアログ → OK で map 確定 → 次の「新ハンド」から `assign_seat` が
  session レイヤへ書かれ、確定 `HandSummary.players[i].player_id` に UUID hex が載る。再度「座席設定」で
  編集すると carry-forward された現 map が初期表示され、変更が次ハンドから反映される。
- 非接続時（既定 off）: ボタン非表示、timestamp session_id、`player_id` キーを足さない（byte 互換）。

## Implemented Behavior

期待どおり。補足:

- `set_seat_player_map` は `session_repo` 未注入時は map を入れても `_session_layer_active=False` のまま
  （rollback path 維持）。`main.py` は off で `session_repo=None` を DI するため、フラグ off では setter を
  呼んでも接続は有効化されない。
- 空席（`（空席）`）は `build_result_map` で除外され `assign_seat` されない。同一 player を複数席に割り当てると
  OK 時に client 側 `find_duplicate_player` が弾く（`assign_seat` の `player_already_seated` と二重防御）。
- customtkinter import はダイアログ/ダッシュボードとも `__init__` 内の遅延 import。module import は GUI 非依存。

## Test Results

- `pytest tests/test_seat_selection.py tests/test_engine_session_setter.py tests/test_phase_e_session_integration.py -v`
  → **16 passed**。
- `pytest tests/ --ignore=tests/test_vision.py -q` → **302 passed, 0 skipped**（回帰なし）。
- `python -m py_compile integration/engine.py gui/seat_selection.py gui/dashboard.py main.py` → rc 0。
- import smoke（customtkinter 未導入環境）: `import gui.seat_selection, gui.dashboard, main` → OK
  （遅延 import を確認）。
- enabled-branch 配線 smoke（GUI/スレッド無し, temp dir）: `create_session` の UUID4 hex（32 hex）を
  `JsonWriter` が採用することを確認。
- Manual（GUI, customtkinter 必要）: 実機 GUI smoke は本環境（CI/headless, customtkinter 未導入）では未実施。
  enabled で `python main.py` → 起動時ダイアログ → 新ハンド → `sessions.json` の assign と
  `logs/<uuid>.json` の `players[].player_id` を目視確認する手動手順を Remaining に残す。

## Mismatches Found During Testing

None observed.

## Fixes Applied

なし（新規実装、回帰なし）。

## Remaining Gaps / Out-of-Scope

- [ ] 実機 GUI E2E（customtkinter 環境での目視: ダイアログ操作・carry-forward・その場 create・
      `sessions.json`/`logs/<uuid>.json` 確認）。本環境は customtkinter 未導入のため未実施。
- [ ] session の選択/再開 UI（現状は起動時に 1 つ auto-create のみ）。
- [ ] explicit `sitting_out` status（現状は空席=skip で代替）。
- [ ] CLI（`--cli`）の session 対応（GUI のみ実装）。mobile（WS3）。
- [ ] S2 schema `1.0` freeze（ISSUE-0005 残）/ legacy log 取り込み（ISSUE-0007）。

## Related ADRs

- `docs/adr/0008-hand-logger-session-integration-strategy.md` — Pattern A（write-through）。E3 はその live 有効化。
- `docs/adr/0006-...md` / `docs/adr/0007-...md` — S2 seating 契約・永続/採番。

## Related Issues

- `docs/issues/0006-seat-selection-ux-at-hand-start.md` — **Resolved**（本タスクで UX 確定 + 実装）。
- `docs/issues/0007-legacy-hand-log-migration-policy.md` — 取り込みは後続（変更なし）。

## Related Commits

- （本タスクのコミット。push 先ブランチ: `claude/phaseE3-seat-selection-gui`）
