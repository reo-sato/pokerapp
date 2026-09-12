# Issue 0006: Seat 選択 UX（hand 開始時の seat → player_id 確定方法）が未確定

## Date

2026-06-03

## Status

Resolved（2026-06-07, E3 で UX 確定＋実装）

## Severity / Priority

- Severity: Medium（Phase 2.3 の前に確定が必要。Phase 2.1/2.2 の planning は本 issue を待たずに進む）
- Priority: P2

## Area

hand logger × session 接続（Phase 2.x） / GUI

## Expected Behavior

各 hand 開始時に operator が「現在の seat → player_id」を確定でき、`SessionRepository.assign_seat`
バッチが安定に呼ばれる。seat change（離席・着席・席移動）が hand 間差分として記録される
（ADR-0006 のとおり、明示的 move イベントは持たない）。

## Actual Behavior

未確定。具体的な open question:

1. **初回 seating の入力 UX**: session 作成直後の hand 0 / hand 1 開始時に operator が seat→player_id
   を入力するフォーム形態（一覧から選択 / 検索 / 並び替え）。
2. **carry-forward の挙動**: 直前 hand の seat map をデフォルト表示するか、毎 hand 確認を促すか。
3. **seat change の入力方法**: 「次 hand の前に変更がある場合だけ差分を入力」「常に全 seat を再確認」
   のどちらか。後者は誤入力リスク低だが UX 重い。
4. **sitting_out（一時離席）の扱い**: skip / explicit `status=sitting_out` の選択肢提示要件。
5. **player_id が未登録の player**: その場で `PlayerRepository.create_player` への遷移を許すか、
   事前登録を強制するか。
6. **dashboard との両立**: 既存 `gui/dashboard.py`（hand logger 画面）に組み込むか、別 widget か。
   Player Registry 画面は別 window という前例あり（S1）。

## Reproduction

設計レビュー（バグではなく未確定の UX 仕様）:

1. `docs/contracts/hand-integration.md` § 5 / § 7 を参照（Phase 2.3 で seat 選択 UI が要る）。
2. 現 `gui/dashboard.py` には seat→player_id を選ぶ UI が無い（name のみ）。

## Root Cause

S2 core は `assign_seat(session_id, hand_id, seat_no, player_id)` の API だけを提供し、
operator がどの粒度でこの呼び出しをトリガするかは UI 層の決定。Phase 2.x 接続戦略（ADR-0008）が
write-through を採用したため、UI の seat 選択タイミングが整合性の前提条件になった。

## Fix

**E1+E2-core（#10, 2026-06-05）で土台は実装済**: `IntegrationThread` が `seat_player_map`（seat→player_id）を
DI で受け取り、hand 開始で `assign_seat`・確定時に `player_id` を埋め込む write-through が動く
（`config.session_layer.enabled` 既定 off で挙動不変）。**残るのは「seat→player_id を選ぶ UX」**＝本 issue 本体:

### Decision（E3, 2026-06-07 — user 確認済）

6 つの open question への確定回答:

1. **初回 seating の入力 UX** → モーダルダイアログ（`gui/seat_selection.py:SeatSelectionDialog`）。
   席ごとに `CTkOptionMenu` で `PlayerRepository.list_players()` から選ぶ。dashboard の「座席設定」
   ボタン／起動時 promote で開く（別 Toplevel、Player Registry 画面の前例に倣う）。
2. **carry-forward** → **採用**。直前に確定した map をデフォルト表示し、毎ハンドの確認は出さない。
   設定は session 中 carry-forward される（cadence = 開始時に一度＋必要時のみ編集）。
3. **seat change の入力方法** → 「変更がある時だけ『座席設定』ボタンで再オープンして編集」。
   常時全 seat 再確認は採らない（UX 軽量・誤入力は client 側 unique 検証で防ぐ）。
4. **sitting_out** → **skip**（空席は `assign_seat` しない）。explicit `status=sitting_out` は将来
   スコープ（`SeatAssignment.status` は既に additive 対応）。
5. **未登録 player** → **その場作成**を許す（ダイアログ内 `create_player`、Empty/Duplicate は inline 表示）。
6. **dashboard との両立** → dashboard 上に「座席設定」ボタン（session レイヤ接続時のみ）→ 別 Toplevel
   モーダル。既存 hand logger UI（name 表示）は不変。

### 実装（E3, 2026-06-07）

- `gui/seat_selection.py`（新規, モーダル + 純ロジック関数）/ `gui/dashboard.py`（ボタン + 起動時 promote）/
  `integration/engine.py:set_seat_player_map`（map 更新 + `_session_layer_active` 再評価）/
  `main.py run_gui`（`session_layer.enabled` で repo/session 構築・UUID4 session_id・`session_repo` DI）。
- 既定 off では従来動作（ボタン非表示・timestamp session_id・PHH 不変, rollback）。
- carry-forward / 空席 skip は client 側純関数で表現し、サブ contract（schema 追加）は不要だった。

## Regression Test

- `tests/test_seat_selection.py`: ダイアログの純ロジック（customtkinter 非依存）—
  carry-forward 解決 / 同一 player 二重割当の検出 / 空席除外の map 構築。
- `tests/test_engine_session_setter.py`: `set_seat_player_map` で `_session_layer_active` が
  再評価され、setter 経由でも assign_seat 永続 + `player_id` 埋め込みが成立 / 空 map・repo 無しで無効。
- `tests/test_phase_e_session_integration.py`: 接続/非接続の write-through 振る舞い（E1+E2-core から継続）。
- GUI 本体（customtkinter Toplevel）は CI 非導入のため自動テスト対象外。手動 QA は worklog に記載。

## Affected Files

- `gui/dashboard.py`（hand logger 画面, 改修候補）
- `gui/player_registry.py`（既存。new widget の参照モデル）
- `core/session_repository.py`（API は変更しない）
- `docs/contracts/hand-integration.md`（仕様の住み処）

## Related Worklog

- `docs/worklog/2026-06-03-s2.x-hand-integration-planning.md`

## Related ADRs

- `docs/adr/0008-hand-logger-session-integration-strategy.md`（write-through を採用）
- `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md`
- `docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md`

## Related Issues

- `docs/issues/0013-session-viewer-data-source-and-enhancements.md` — WS2-α の read-only viewer は
  hand 間の seat→player 差分を **表示** できる土台になる。本 issue で seat change の入力 UX が確定
  したら、viewer 側の差分ハイライト（seat change の可視化）要件と整合させる。

## Related Commits

- 本 issue と同じコミット（S2.x integration planning）

## Notes

UX 仕様であり、本実装には GUI（WS2）の着手が必要。最終決定は Phase 2.3 開始時に user とすり合わせる。
mobile（WS3）側の seat 選択画面も同 contract に従う前提。
