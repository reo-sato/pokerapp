# Worklog: mobile での精算状況表示（S4, ADR-0016/0017）

## Date

2026-06-13

## Scope / Task

ロードマップ残作業「mobile の settlement 表示（read-only）」。プレイヤーが自分のスマホで、
自分の session 精算が確定済か・支払済みかを確認できるようにする。

## Goal

viewer API の player ledger summary に確定状態（settled / payment_status / settled_at）を additive
追加し、mobile `MyLedgerScreen` に精算状況を表示する。新規 endpoint は作らず既存 summary を拡張。

## Changed Files

- `api/read_models.py`: `get_player_session_ledger` の summary に `settled`/`payment_status`/`settled_at`
  を追加。確定判定は `list_settlements`（確定行のみ）で行う（speculative 行の `settled_at` は
  wall-clock で常に埋まるため compute_settlement では判定しない）。totals は committed があれば
  その値、無ければ speculative。
- `tests/test_viewer_api.py`: 既存 ledger summary assertion を settled=false 込みに更新 +
  `test_player_ledger_settled_status`（close→commit→paid で settled/paid を確認）。
- `mobile/src/api/types.ts`: `LedgerSummary` に settled / payment_status? / settled_at? 追加。
- `mobile/src/screens/MyLedgerScreen.tsx`: 「精算状況: 未確定（暫定）/ 確定済（支払済み・未払い）」表示、
  確定時はタイトルを「精算額（確定）」に。
- `mobile/src/api/mockRepository.ts` + `mockRepository.test.ts`: summary に settled=false 等を追加。
- docs: `viewer-api.md` / `repository-interfaces.md` の ledger summary、`CLAUDE.md`（M2 行・Phase 4
  WS3）、`CHANGELOG.md`。

## Expected / Implemented Behavior

未確定: settled=false、payment_status=null、UI は「未確定（暫定）」。
確定後（staff が commit + paid）: settled=true、payment_status=paid、UI は「確定済（支払済み）」（緑）。

## Test Results

- `pytest tests/ --ignore=tests/test_vision.py -q` — **518 passed, 0 skipped**。
- mobile: `npm run typecheck` clean / `npm test` 8 pass。
- `ruff check .` — clean。

## Mismatches Found During Testing

- compute_settlement の `settled_at` は speculative 行でも `_now_iso()` で埋まるため確定判定に使えない
  → `list_settlements`（確定行のみ）で判定する設計にした。
- summary に 3 フィールド追加で既存 deep-equal 系 assertion（python / mobile mock）が落ちた → 更新。

## Remaining Gaps / Out-of-Scope

- partial-paid、auto ledger 生成、per-player PIN、player rename 伝播。実機 E2E / PN5180 firmware。

## Related ADRs / Issues

- ADR-0016（settlement）/ ADR-0017（viewer API）/ ADR-0019（schema freeze）

## Related Commits

- 本 commit
