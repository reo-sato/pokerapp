# Issue 0008: Session / Seating Viewer の data source 依存と将来拡張が未確定

## Date

2026-06-03

## Status

Open

## Severity / Priority

- Severity: Low（read-only viewer 自体は core read API に対して完結して動作する。実運用での
  データ供給は Phase 2.2 の write-through 待ち）
- Priority: P3

## Area

desktop（WS2） / hand logger × session 接続 / GUI

## Expected Behavior

WS2-α で追加した read-only Session / Seating Viewer（`gui/session_viewer.py`,
`main.py --sessions`）が、**実運用で意味のある session / seating データ**を表示できる。すなわち
hand logger 運用中に作られた session と hand-based seat assignment が viewer に現れる。

## Actual Behavior

viewer は `SessionRepository` / `PlayerRepository` の read API に対して正しく動作する（一覧 /
選択 / current seating / hand assignments / name 解決 / refresh）。ただし **hand logger からの
write-through（Phase 2.2/2.3）が未実装**のため、`sessions.json` に session/seating を書き込む
本番経路がまだ無い。

調査時点の事実（読み取り専用調査）:

1. `main.py` / `gui/dashboard.py` / `integration/engine.py` に `SessionRepository` /
   `assign_seat` / `session_layer` への参照が無い。
2. `core/hand_log.py` の `HandSummary` に `player_id` フィールドが無い。
3. `docs/contracts/hand-integration.md` は「draft / 設計フェーズ」「コードは未変更」と明記。
4. 結論: Phase 2.x は ADR-0008 + hand-integration.md + ISSUE-0006/0007 の **planning のみ**で、
   write-through の実装は未着手。

したがって現状 viewer に出るのは、テストや将来の write-through が直接 `SessionRepository` に
書いたデータに限られる。

## Open Questions

1. **data source**: viewer に実データを供給するのは Phase 2.2（hand logger → `assign_seat`
   write-through, ADR-0008 Pattern A）でよいか。別途 legacy 取り込み（ISSUE-0007）も供給源にするか。
2. **live 更新**: 現状は手動 refresh のみ。hand logger 稼働中の自動更新（polling / push）を将来
   入れるか。入れる場合の更新間隔・スレッド安全境界（GUI スレッドから core を直接呼ばない原則）。
3. **seat change の可視化**: hand 間の seat→player 差分（着席/離席/移動）をハイライト表示するか
   （ISSUE-0006 の seat-change UX と表示要件が連動）。
4. **filters / search**: open/closed フィルタ、player でのフィルタ、session 検索を入れるか。
5. **closed session の扱い**: 概要での視覚的区別、終了済みハンドのアーカイブ表示。
6. **read API の安定性**: 本タスクで additive 追加した `SessionRepository.list_hand_ids` /
   `reload`、`PlayerRepository.reload` を repository interface 契約に正式に含めるか
   （現状 `repository-interfaces.md` に追記済・schema は未 freeze なので additive で問題なし）。

## Reproduction

1. `python main.py --sessions` で viewer を開く（クリーンな環境）。
2. hand logger（`python main.py`）でハンドを記録しても、write-through が無いため
   `sessions.json` は更新されず、viewer の一覧は空のまま。

## Root Cause

WS2-α は contract-first 方針に沿って「core の read API に対する read-only front-end」を先行実装した。
書き込み側（hand logger → SessionRepository）の接続は別 workstream（Phase 2.2/2.3, ADR-0008）であり、
本タスクのスコープ外。viewer の有用性が write-through 実装に依存するのは設計上の前後関係であって
viewer 自体の不具合ではない。

## Fix

未対応（Phase 2.2/2.3 実装時、および viewer 拡張の必要が出た時に確定）。決まったら:

- Phase 2.2 で write-through が入った後、viewer の end-to-end 動作（hand logger →
  `sessions.json` → viewer 表示）を確認するテスト/手順を追加。
- live 更新・filters・seat-change ハイライトを入れる場合は本 issue を分割し、それぞれに
  GUI ロジックテストを追加。

## Regression Test

- 現状の read-only ロジックは `tests/test_session_viewer_gui.py` でカバー済み。
- write-through 接続後は「hand logger 経由で書いた seat assignment が viewer の read model に
  現れる」結合テストを追加する。

## Affected Files

- `gui/session_viewer.py`（read-only viewer 本体）
- `core/session_repository.py` / `core/player_repository.py`（additive read API: list_hand_ids / reload）
- `main.py`（`--sessions` 起動）
- 将来: `integration/engine.py` / `main.py`（Phase 2.2 write-through）

## Related Worklog

- `docs/worklog/2026-06-03-session-seating-viewer.md`

## Related ADRs

- `docs/adr/0008-hand-logger-session-integration-strategy.md`（write-through 戦略, 実装 planned）
- `docs/adr/0006-...` / `docs/adr/0007-...`（S2 contract / 永続形）

## Related Issues

- `docs/issues/0006-seat-selection-ux-at-hand-start.md`（seat change UX = viewer の差分表示要件と連動）
- `docs/issues/0007-legacy-hand-log-migration-policy.md`（legacy を viewer の供給源にするか）
- `docs/issues/0005-s2-session-seating-freeze-blockers.md`（schema freeze 残項目）

## Related Commits

- 本 issue と同じコミット（WS2-α session/seating viewer）

## Notes

viewer は read-only として完成しており、本 issue は「いつ実データが流れ込むか」と「将来拡張」の
risk register。Phase 2.2 着手時に #1（data source）が自然に解消する見込み。
