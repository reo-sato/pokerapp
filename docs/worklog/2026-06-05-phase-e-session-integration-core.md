# Worklog: Phase E part 1 — hand logger × session 統合（E1 + E2-core write-through）

## Date

2026-06-05

## Scope / Task

v1 リリーストラックの **Phase E**（GitHub issue #10, S2.x）。ADR-0008 Pattern A（write-through, additive）に
従い、hand logger を S2 session レイヤへ接続する **core 結線（E1 + E2-core）** を実装。`main.py` の session
選択 UX（E2 UX）と seat→player_id 選択 **GUI（E3, ISSUE-0006）は本増分の対象外**（対話/GUI が必要なため分離）。
統合ブランチ `v1-integration`、作業ブランチ `claude/phaseE-session-integration`（docs の #25 とは独立）。

## Goal

- `config_default.json` に `session_layer.enabled`（既定 false）を追加（E1）。
- `IntegrationThread` に `session_repo` / `seat_player_map` を additive DI（E2-core）:
  hand 開始で `assign_seat` write-through、確定時に `HandSummary.players[i].player_id` を additive 埋め込み。
- 非接続時は従来どおり（player_id キーを足さない＝byte 互換, rollback）。
- 出力は F3b の `hand` schema に適合。

## Changed Files

- `config_default.json`: `session_layer.enabled`（既定 false）+ 説明。
- `integration/engine.py`:
  - `__init__`: `session_repo` / `seat_player_map` 追加、`_session_layer_active`（両方揃ったときのみ）。
  - `_assign_seats_for_hand`（新）: hand 開始時に `assign_seat` バッチ（失敗は log 留め、hand 継続）。
  - `_start_new_hand`: 有効時に上記を呼ぶ。
  - `_finalize_hand`: `resolve_seat_map_for_hand` で seat→player_id を解決し additive 埋め込み（接続時のみ）。
  - TYPE_CHECKING import に `SessionRepository`。
- `tests/test_phase_e_session_integration.py`（新規 3）。
- docs: CHANGELOG / CLAUDE.md（§ Session & Seating・実装状況）/ ISSUE-0006（土台済の注記）/ 本 worklog。

## Expected vs Implemented

- 接続時: `assign_seat` が session レイヤに永続（`list_seat_assignments` で確認）、`HandSummary.players[i]`
  に `player_id`（registry の UUID hex）が載り、`hand` schema（player_id pattern）に適合。
- 非接続時: `players[i]` に `player_id` キーが付かない（従来どおり）。`PHHExporter` は不変（PHH に
  player_id を載せない）。`GameStateManager` 不変。
- assign の個別失敗（seat/player 重複等）は hand を止めず log（hand logger の記録継続性を優先）。

## Test Results

- `pytest tests/test_phase_e_session_integration.py -v` — **3 passed**（接続 2 + 非接続 1）。
- `pytest tests/ -q --ignore=tests/test_vision.py` — **289 passed, 0 skipped**（回帰なし、legacy/PHH 不変）。

## Mismatches Found During Testing

- なし。テストは legacy backend（GameStateManager）で実施（session 結線は backend 非依存のため）。

## Fixes Applied

- なし（additive DI + write-through）。

## Remaining Gaps / Out-of-Scope（Phase E の残り）

- [ ] **E2 UX**: `main.py`（run_cli/run_gui）で `session_layer.enabled` を読み、`SessionRepository` で
      session を作成/選択（session_id = UUID4 hex）し、`seat_player_map` を構築して DI する結線。
- [ ] **E3 GUI**: seat→player_id 選択 UI（`gui/dashboard.py`、carry-forward、seat change）= **ISSUE-0006** 本体。
- [ ] mid-session seat change の per-hand 差分 UX（現 core は固定 map を全 hand に適用）。

## Related ADRs / Issues

- ADR-0008（hand logger × session 統合 Pattern A）/ ADR-0006・0007（S2 core）。
- ISSUE-0006（seat 選択 UX、E3 で決着）/ ISSUE-0007（legacy log 取り込み、別途）。
- GitHub issue #10（Session 統合）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
