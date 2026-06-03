# Worklog: WS2-α — Session / Seating Viewer (desktop, read-only)

## Date

2026-06-03

## Scope / Task

WS2-α: S2 core（`SessionRepository`）に蓄積された session / hand-based seating を **人間が
確認するための read-only inspection GUI** を desktop に追加する。hand logger dashboard /
player registry とは別画面。編集機能は持たない（refresh のみ）。

## Goal

- 別画面の read-only Session / Seating Viewer を追加する。
- session 一覧 → 選択 → 概要 / current seating / hand ごとの seat assignments を見られる。
- `player_id` を `display_name` に解決（不能なら `(unknown)`）。
- 手動 refresh で repository をディスクから再読込できる。
- empty state / no-data state を明示する。
- hand logger / player registry / session core の既存挙動を壊さない。
- docs-as-code（CLAUDE.md / CHANGELOG / contracts / issue / decision-log / worklog）を更新する。

## Changed Files

- `gui/session_viewer.py` — 新規。`SessionViewerWindow`（customtkinter, read-only）。左ペイン
  session 一覧 / 右ペイン 概要・current seating・hand assignments / 再読込ボタン。表示用 read model
  （`_SessionListItem` / `_SeatRow` / `_SessionDetail`）と純粋な表示整形関数（`_format_*`）を
  widget 操作から分離してテスト可能にした。
- `core/session_repository.py` — additive read API を 2 つ追加。`list_hand_ids(session_id)`
  （記録済み hand_id を昇順列挙）/ `reload()`（ディスクから再読込）。既存 API・業務ルールは不変。
- `core/player_repository.py` — additive read API を 1 つ追加。`reload()`（ディスクから再読込）。
- `main.py` — `--sessions` フラグと `run_session_viewer()` を追加（hand logger / registry とは別起動。
  name 解決を一貫させるため同一 `PlayerRepository` を `SessionRepository` と viewer で共有）。
- `tests/test_session_viewer_gui.py` — 新規。GUI ロジックテスト（customtkinter をモック）。
- `CLAUDE.md` — § Session / Seating Viewer（WS2-α, read-only）を追加。ディレクトリ構成 / 実装状況表 /
  コマンド / Phase 2 WS2 状況を更新。
- `CHANGELOG.md` — Unreleased に viewer を追記。
- `docs/contracts/repository-interfaces.md` — session interface に `list hand ids` / `reload` を additive 追記。
- `docs/contracts/session-seating.md` — interface 草案表に `list hand ids` を追記。
- `docs/contracts/hand-integration.md` — Session / Seating Viewer が inspection 用であることを明記。
- `docs/issues/0008-session-viewer-data-source-and-enhancements.md` — 新規。viewer の data source 依存
  （Phase 2.2 未実装）と将来拡張の open question を登録。
- `docs/issues/0006-seat-selection-ux-at-hand-start.md` — viewer で seat change を可視化できる旨の関連注記。
- `docs/decision-log.md` — Major Issue Index に ISSUE-0008 を追加。

## Expected Behavior

- `python main.py --sessions` で read-only viewer が開く（hand logger / registry は無改修で従来通り）。
- session が無ければ「（セッションがありません）」を表示する。
- session を選ぶと、その session の概要・current seating（最新 hand 由来）・hand ごとの
  seat assignments が読める。
- `player_id` は `display_name` に解決され、未登録 player は `(unknown)` と表示されつつ
  `player_id` 自体は残る。
- 再読込ボタンで `SessionRepository` / `PlayerRepository` が disk から読み直され、別プロセスが
  追加した session が見えるようになる。
- viewer は session/seat/player を一切変更しない（read-only）。

## Implemented Behavior

期待どおり実装:

- `SessionViewerWindow` は `SessionRepository` / `PlayerRepository` の read API のみに依存。
  `_build_detail` が `get_session` + `current_seating` + `list_hand_ids` + `list_seat_assignments`
  を呼んで read model を組み立て、`_format_summary_text` / `_format_seating_text` /
  `_format_hands_text`（純粋関数）で整形、`_set_textbox` で 3 つの read-only textbox に流す。
- name 解決は `_resolve_display_name`（`PlayerRepository.get` → `PlayerNotFoundError` で
  `(unknown)`）。seat 行は `_SeatRow` に `player_id` を保持したまま `display_name` を付与。
- `_cmd_refresh` が `session_repo.reload()` + `player_repo.reload()` → `_refresh_session_list` →
  `_render_detail`。選択中 session が消えていた場合は選択解除。
- empty state（一覧空 / current seating 空 / hand 空）は専用文言で表示。
- `list_hand_ids` / `reload` は core 側に置き、GUI に enumeration / loading ロジックを複製していない
  （business logic は core が source of truth のまま）。

## Test Results

- `python -m pytest tests/test_session_viewer_gui.py -v` → **15 passed**。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` → **192 passed**
  （本タスク前のベースライン 177 passed に対し +15、回帰なし）。
- `python main.py --help` → `--sessions` フラグが表示されることを確認。
- 手動レンダリング sanity check（formatter 出力を print）: 概要 / current seating（最新 hand 由来）/
  hand ごとの assignments が seat_no 昇順で整形され、name 解決も期待どおり。
- 注: 本実行環境は headless（DISPLAY なし、customtkinter 未インストール）のため、実 GUI の目視確認は
  未実施。GUI ロジックは customtkinter をモックしたユニットテストで検証（player registry GUI テストと同方式）。

## Mismatches Found During Testing

- **タスク前提と実コードの乖離（重要）**: タスク説明は「Phase 2.2（hand logger × session/seating
  統合）」「Phase 2.3（seat selection UX）」が **実装済** と記載していたが、実コードでは **未実装**
  だった。確認した事実:
  - `main.py` / `gui/dashboard.py` / `integration/engine.py` に `SessionRepository` /
    `assign_seat` / `session_layer` への参照が無い。
  - `core/hand_log.py` の `HandSummary` に `player_id` が無い。
  - 最新コミットは planning-only（`docs/contracts/hand-integration.md` に「コードは未変更」と明記）。
  - 結論: 現状 Phase 2.x は ADR-0008 / hand-integration.md / ISSUE-0006/0007 の **planning のみ**。
- 本タスク（read-only viewer）は `SessionRepository` の read API に対して実装するため、この乖離は
  **viewer のブロッカーではない**。ただし viewer が「実運用で表示するデータの出どころ」が現状
  存在しない（hand logger からの write-through が未実装）点は明確化が必要 → ISSUE-0008 を起票。

## Fixes Applied

- 乖離に対する code 修正は不要（viewer は core read API に対して正しく動く）。代わりに以下で明確化:
  - ISSUE-0008 を新規起票（viewer の data-source 依存 + 将来拡張の open question）。
  - CLAUDE.md の実装状況表に「hand logger × session 接続 (Phase 2.2/2.3) ❌ 未実装」行を追加し、
    viewer 節の Out of scope に「write-through 未実装」を明記（誤認防止）。

## Remaining Gaps / Out-of-Scope

- [ ] 実 GUI の目視確認（display 付き環境での一覧 / 選択 / refresh / empty state 表示）。headless のため未実施。
- [ ] hand logger からの live push 更新（現状は手動 refresh）。
- [ ] session / seat の作成・編集・削除 UI（read-only のため意図的に持たない）。
- [ ] filters / search / CSV export、seat-change の差分ハイライト（ISSUE-0008）。
- [ ] mobile（WS3）側 viewer。
- [ ] **hand logger × session の write-through 接続（Phase 2.2/2.3）= viewer に実データを供給する経路**。
      未実装（ISSUE-0008 / ISSUE-0006）。これが入るまで viewer は主に直接 `SessionRepository` に
      書かれたデータ（テスト等）を表示する。

## Related ADRs

- `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md`（S2 contract）
- `docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md`（永続形・採番）
- `docs/adr/0008-hand-logger-session-integration-strategy.md`（write-through 接続戦略, 実装は planned）

## Related Issues

- `docs/issues/0008-session-viewer-data-source-and-enhancements.md`（新規）
- `docs/issues/0006-seat-selection-ux-at-hand-start.md`（seat change 可視化の関連注記）
- `docs/issues/0005-s2-session-seating-freeze-blockers.md`（schema freeze 残項目）

## Related Commits

- 本 worklog と同じコミット（WS2-α session/seating viewer）
