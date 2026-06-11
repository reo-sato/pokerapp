# Issue 0006: Seat 選択 UX（hand 開始時の seat → player_id 確定方法）が未確定

## Date

2026-06-03

## Status

Fixed（M3 = E3, 2026-06-11。決定内容は § Fix 参照）

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
（`config.session_layer.enabled` 既定 off で挙動不変）。

**M3 (= E3, 2026-06-11) で UX を確定・実装**（open question への回答番号は § Actual Behavior 対応）:

1. **初回 seating**: セッション設定プロンプト（CLI/GUI 共通, `main.py:_prompt_session_config`）で
   席ごとに registry の番号選択。`n` = その場で `create_player`、空 Enter = 割当なし
   （自由入力名のみ・player_id なし）。選択 player の `display_name` が席の表示名になる。
2. **carry-forward**: 確定した seat map は次の変更まで **全 hand に自動適用**（毎 hand 確認なし。
   engine の `seat_player_map` がそのまま次 hand の `assign_seat` バッチに使われる）。
3. **seat change**: 「変更がある場合だけ差分入力」を採用。GUI dashboard の「席設定」ボタン →
   ダイアログで選び直し → 差分のみ `seat_assign` イベント（`AudioEvent(action="seat_assign",
   seat, raw_text=player_id)`）として queue 投入（ISSUE-0012 の rebuy と同じ一元化規約）。
   IntegrationThread が **次ハンド開始時** に map と表示名（`set_player_name`）へ反映する
   （mid-hand の帰属・名前の揺れを防ぐ）。CLI は初回 seating のみ（mid-session 変更は GUI）。
4. **sitting_out**: 割当解除（map から除外 = `raw_text=""`）のみ。explicit `status=sitting_out` は後続。
5. **未登録 player**: セッション開始前プロンプトでは新規登録可。mid-session ダイアログは既存
   player のみ（registry への書き込みはスレッド起動前に限定）。
6. **dashboard 統合**: 別 window ではなく dashboard 内ボタン + modal ダイアログ。

仕様の住み処: `docs/contracts/hand-integration.md` § seat 選択 UX（E3）。

## Regression Test

- `tests/test_phase_m3_seat_selection.py`:
  - `TestSeatAssignEvent` — seat_assign が次ハンドから map/表示名に反映・割当解除・
    空 map からの後追い有効化・session レイヤ off では無視
  - `TestPromptSeatPlayers` — registry 選択 / 新規登録 / 割当なし / 重複割当拒否
  - `TestViewerEndToEnd` — write-through 出力が viewer read model で読める（M3 の目的）
  - `TestMaybeCloseSession` — 終了時 close の y/N
- GUI ダイアログ本体（customtkinter）は手動スモーク（worklog 参照）。

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

## Related Commits

- 本 issue と同じコミット（S2.x integration planning）

## Notes

UX 仕様であり、本実装には GUI（WS2）の着手が必要。最終決定は Phase 2.3 開始時に user とすり合わせる。
mobile（WS3）側の seat 選択画面も同 contract に従う前提。
