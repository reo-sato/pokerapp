# Worklog: M3 (= E3) — seat 選択の main.py 結線 + dashboard 席設定（ISSUE-0006 決着）

## Date

2026-06-11

## Scope / Task

プレイヤー向け参照アプリのロードマップ M3（ADR-0013）= 既存計画の Phase 2.3/E3（ADR-0008,
ISSUE-0006）。E1+E2-core で engine に入っていた write-through を main.py / GUI から実際に
使えるようにし、viewer（M1/M2）に実データが流れる状態を作る。

## Goal

- `session_layer.enabled=true` で、セッション設定時に registry から seat→player を選択でき、
  UUID session_id で記録される（= viewer API がそのまま参照できる）。
- mid-session の seat change（席替え・入退店）を GUI から差分入力でき、次ハンドから
  帰属と表示名に反映される。
- 既定 `false` の挙動は完全不変（rollback path 維持）。

## Changed Files

- `integration/engine.py` — `seat_assign` イベント処理（`_handle_seat_assign` で
  `_pending_seat_changes` に保留 → `_start_new_hand` 冒頭の `_apply_pending_seat_changes` で
  map + 表示名へ反映）。`_session_layer_active` を property 化（空 map からの後追い有効化）。
- `core/game_state.py` / `core/poker_engine.py` — `set_player_name(seat, name)` additive 追加
  （両 backend + Protocol。ルール状態に影響しない表示名更新）。
- `core/session_repository.py` — `get_player(player_id)` additive 追加（display_name 解決用）。
- `main.py` — `_make_session_layer`（config フラグで repo 構築）/
  `_prompt_session_config(player_repo)`（席ごとの registry 番号選択・`n` 新規登録・空 Enter 割当なし・
  重複割当拒否）/ `_create_layer_session`（ラベル入力 + UUID session_id）/
  `_maybe_close_session`（終了時 y/N）。run_cli / run_gui 両方に結線。
- `gui/dashboard.py` — `player_repo`/`seat_player_map` を受け取り「席設定」ボタン + modal
  ダイアログを追加。差分のみ `AudioEvent(action="seat_assign", seat, raw_text=player_id)` を
  queue 投入（ISSUE-0012 の一元化規約）。`_refresh_player_row` が名前 text も更新。
- `config_default.json` — `session_layer._comment` を実装後の説明に更新。
- `tests/test_phase_m3_seat_selection.py` — 新規 11 テスト（下記）。
- docs: `docs/issues/0006`（Fixed 化 + 決定記録）/ `docs/contracts/hand-integration.md`
  （E3 UX 仕様）/ `docs/adr/0008`（Validation チェック更新）/ `docs/decision-log.md` /
  `CLAUDE.md`（Session & Seating・実装状況・Phase 2.x）/ `docs/usage.md`
  （「プレイヤー紐付けと閲覧」+ config 表）/ `CHANGELOG.md`。

## Expected Behavior

- on: プロンプトで seat→player 選択 → UUID session で記録 → 「席設定」で次ハンドから変更反映 →
  終了時 close 確認。viewer read model で player 別のハンドが読める。
- off（既定）: プロンプト・出力とも従来と同一。

## Implemented Behavior

Expected どおり。設計上の決定（ISSUE-0006 の回答、詳細は issue / hand-integration.md）:

- seat change は**次ハンド開始時に適用**（mid-hand の帰属・名前の揺れを防ぐ。`seat_assign` は
  即時でなく `_pending_seat_changes` に保留）。
- carry-forward 方式（毎 hand 確認なし）。sitting_out は割当解除のみ。
- mid-session の新規 player 登録は不可（registry 書き込みはスレッド起動前のプロンプトに限定）。
- CLI は初回 seating のみ（mid-session 変更は GUI。CLI は fallback 用途のため）。
- `session_layer.enabled` の**既定 false は維持**（実機 E2E = Phase H 未了のため。E2E 後に再検討）。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **362 passed**（+11、skip 0）
- 新規 `tests/test_phase_m3_seat_selection.py`:
  - seat_assign が次ハンドから map/表示名に反映（現行ハンド不変）/ 割当解除 / 空 map からの
    後追い有効化 / session レイヤ off では無視
  - `set_player_name`（legacy / pokerkit 両 backend）
  - プロンプト: registry 選択・新規登録・割当なし・重複割当の再入力
  - `_maybe_close_session` の y/N
  - **E2E**: write-through 出力（席替え含む 2 hands）が viewer read model
    （`list_player_sessions` / `list_player_hands`）で正しく player 別に読める
- GUI ダイアログ本体（customtkinter）はロジックを `_send_seat_changes` に分離。表示は
  実機スモーク待ち（headless 環境のため）。

## Mismatches Found During Testing

None observed.

## Fixes Applied

—

## Remaining Gaps / Out-of-Scope

- [ ] GUI「席設定」ダイアログの実機スモーク（運営 PC で `python main.py` + session_layer on）
- [ ] `session_layer.enabled` 既定切替の判断（実機 E2E = Phase H 後）
- [ ] sitting_out の explicit `status` 記録 / CLI からの mid-session 変更
- [ ] viewer API の log_dir が config `session.log_dir` 固定（起動時プロンプトで別の場所を
  選ぶと viewer から見えない。usage.md に「同じ場所にする」注記で運用回避）
- [ ] legacy log reconciler（Phase 2.4, ISSUE-0007）/ S2 schema `1.0` freeze（ISSUE-0005）

## Related ADRs

- `docs/adr/0008-hand-logger-session-integration-strategy.md` / `docs/adr/0013-player-facing-viewer-api-first-architecture.md`

## Related Issues

- `docs/issues/0006-seat-selection-ux-at-hand-start.md`（Fixed）/ `docs/issues/0013-player-viewer-privacy-model.md`

## Related Commits

- （本 commit。M1 = `b5eadc8`, M2 = `ba0ad46`）
