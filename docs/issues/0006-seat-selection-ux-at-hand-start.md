# Issue 0006: Seat 選択 UX（hand 開始時の seat → player_id 確定方法）が未確定

## Date

2026-06-03

## Status

Open

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

未対応（Phase 2.3 着手時に確定）。決まったら:

- `gui/dashboard.py` または別 window の widget 仕様を `docs/contracts/hand-integration.md` に追記。
- carry-forward と sitting_out の振る舞いを `validation-rules.md`（または本 doc）に明記。
- 必要なら seat-change を表すサブ contract（追加 schema or note）を起こす。

## Regression Test

- 現状ではテスト対象なし（UI 仕様）。確定後は GUI ロジックテストで:
  - 初回 seating → assign_seat バッチが正しい引数で呼ばれる
  - carry-forward モードで差分のみ assign される
  - unknown_player / seat_taken エラーが UI に正しく表示される
  を追加する。

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

- `docs/issues/0008-session-viewer-data-source-and-enhancements.md` — WS2-α の read-only viewer は
  hand 間の seat→player 差分を **表示** できる土台になる。本 issue で seat change の入力 UX が確定
  したら、viewer 側の差分ハイライト（seat change の可視化）要件と整合させる。

## Related Commits

- 本 issue と同じコミット（S2.x integration planning）

## Notes

UX 仕様であり、本実装には GUI（WS2）の着手が必要。最終決定は Phase 2.3 開始時に user とすり合わせる。
mobile（WS3）側の seat 選択画面も同 contract に従う前提。
