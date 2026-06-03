# Issue 0006: Seat 選択 UX（hand 開始時の seat → player_id 確定方法）が未確定

## Date

2026-06-03

## Status

Partially Resolved（Phase 2.3 で最小 seat selection UX を実装。高度 UX は引き続き Open）

## Severity / Priority

- Severity: Low（最小 UX は実装済。残るのは sitting_out / 未登録 player 追加 / seat change 履歴 UI 等の拡張）
- Priority: P3

## Phase 2.3 実装で解決した部分（2026-06-03）

最小 seat selection UX を desktop hand logger に実装した（`gui/seat_assignment.py` /
`gui/dashboard.py`、ADR-0008 Pattern A の seating 入力経路）:

- **手動 seat selection ダイアログ**: `SeatAssignmentDialog`（モーダル）。seat ごとに player
  選択コンボボックス（候補 = `PlayerRepository.list_players()` の `display_name`、内部値 =
  `player_id`）。「（空席）」選択 = その seat を seating に入れない。
- **carry-forward**: ダイアログ初期値に `IntegrationThread.get_seating()`（直前 hand の seating）を
  使う。通常は無変更で OK、入れ替え時だけ変更。
- **最小フロー**: 「新ハンド」ボタンが session レイヤ有効時にダイアログを開き、OK で
  `IntegrationThread.update_seating()` → 続けて new_hand を流す。「席割り当て」ボタンで hand を
  開始せず seating だけ編集も可能。
- **config 連携**: `session_layer.enabled == False` では seat UI ボタンを出さず、「新ハンド」は
  従来どおり直接 new_hand を流す（UX 不変）。
- player 候補は **ダイアログを開くたびに registry から最新取得**（起動時キャッシュしない）。

## まだ Open な部分（Phase 2.3 scope 外 → 将来）

- **sitting_out / late entry / temporary leave** 等の seat 状態（現状は「空席」= 割り当てなしの
  単純 2 値のみ。`SeatAssignment.status` の活用は未実装）。
- **未登録 player のその場追加**（ダイアログから `create_player` への遷移）。現状は事前に Player
  Registry 画面で登録が必要。
- **seat change の履歴 UI**（hand 間差分の可視化）。記録は hand-based snapshot で取れているが
  閲覧 UI は無い。
- **重複 player のガード**: 同一 hand で同じ player を 2 席に選ぶと 2 つ目の `assign_seat` が
  `player_already_seated` で degraded（warning）になる。UI 側の事前バリデーションは未実装。
- **mobile（WS3）との UX 一貫性**: 同 contract に従う前提だが mobile 実装自体が未着手。
- **auto-accept / キーボード操作** 等の操作効率化。

## Phase 2.2 時点の暫定対応（2026-06-03, 履歴）

write-through 接続（ADR-0008）は Phase 2.2 で実装済だが、seat 選択 UI は未着手のため:

- `IntegrationThread` は `seating`（`seat_no -> player_id`）を **DI で受け取るだけ**にし、
  「既知の player_id をそのまま割り当てる」最小形にした（seat 選択ロジックは持たない）。
- `main.py` の `_init_session_layer()` は flag on でも `seating` を **空 dict** で渡す。よって
  実運用（`main.py` 経由）では session 作成・session_id 切替・空 assign_seat バッチのみ効き、
  `HandSummary.players[i].player_id` は実際には付かない。
- `seating` を与えたときの write-through 本体（assign_seat バッチ → player_id additive）は
  `tests/test_session_integration.py` で IntegrationThread 単体検証済。
- carry-forward は「同一 `seating` を毎 hand 再適用」する単純形（hand ごとに独立 snapshot を記録）。
  差分入力 / sitting_out / その場 player 登録は本 issue（Phase 2.3）で確定する。

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

## Related Commits

- 本 issue と同じコミット（S2.x integration planning）

## Notes

UX 仕様であり、本実装には GUI（WS2）の着手が必要。最終決定は Phase 2.3 開始時に user とすり合わせる。
mobile（WS3）側の seat 選択画面も同 contract に従う前提。
