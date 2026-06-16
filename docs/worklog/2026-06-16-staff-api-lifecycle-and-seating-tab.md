# Worklog: staff API §A/§B 実装 + staff アプリ座席タブ（WS4, ADR-0036）

## Date

2026-06-16

## Scope / Task

ADR-0036 §A（会計の reversal / point grant）と §B（session / 座席 / player ライフサイクル）の
staff API を実装し、staff アプリ（`staff/`）に **座席タブ** と **session 作成/close** を足して有効化する。
§C（hand logger 遠隔制御）は ISSUE-0020 の open question として未着手。

## Goal

- staff token 認可・単一書き手を維持したまま、staff が iPad だけで session 作成 → 座席割当 →
  会計（buy-in/注文/精算/reversal/grant）まで回せる API + UI を用意する。
- error は `error-shapes.md`（ledger / session / player）を 1:1 再利用（新規 code を増やさない）。
- Python 全テスト + staff アプリ typecheck/test/web export が green。

## Changed Files

- `api/server.py` — §A/§B エンドポイント追加。session/player の error map（`_map_session_error` /
  `_map_player_error`）+ `DuplicateGrantError`→`duplicate_grant` を ledger map に追加。CORS の
  `allow_methods` を GET/POST/PUT に拡張（web write 用）。imports 追加。
- `api/client.py` — `ViewerApiClient` に reverse_entry / grant_points / list_sessions /
  create_session / close_session / get_seating / assign_seats / staff_list_players /
  create_player / rename_player を additive 追加。
- `tests/test_viewer_api_staff_lifecycle.py` — §A/§B の round-trip + error code + 認可テスト（新規）。
- `staff/src/api/types.ts` — `SeatAssignment` / `StaffSeating` / `SeatAssignInput` 追加。
- `staff/src/api/repository.ts` — `StaffRepository` に session/player/座席/reverse/grant メソッド追加。
- `staff/src/api/mockRepository.ts` — 上記の mock 実装（seating 計算・validation・error code）。
- `staff/src/api/httpRepository.ts` — 実装済 API に接続（`listSessions` の `not_implemented` 解消）。
- `staff/src/mocks/fixtures.ts` — `seatAssignments` fixture 追加。
- `staff/src/screens/SeatingTab.tsx` — 座席タブ（新規）。
- `staff/src/screens/TableViewScreen.tsx` — 座席タブを統合（タブ追加）。
- `staff/src/screens/SessionListScreen.tsx` — session 作成 / close を追加。
- `staff/src/screens/LedgerTab.tsx` — ポイント付与コントロール追加。
- `staff/src/api/mockRepository.test.ts` — §A/§B の mock 契約テスト追加（7→12 件）。
- docs: `docs/adr/0036-...md`（Validation 更新）/ `docs/contracts/viewer-api.md`（staff 節に A/B 追記）/
  `CLAUDE.md`（WS4 + 実装状況）/ `CHANGELOG.md`。

## Expected Behavior

- §A: 任意 entry を reversal で取消（append-only、settlement に反映）/ point を manual_grant で付与。
- §B: session を作成（UUID4）・close（already_closed）・一覧、player 作成/rename（empty/duplicate）、
  指定 hand に seat→player を batch 割当（seat_taken / player_already_seated / unknown_player /
  invalid_seat / session_closed）、現在 seating を read。
- 認可: token 無し→401、token 未設定→403、read-only プロセスの write→503。
- staff アプリ: SessionList で session 作成/close、TableView の座席タブで現在 seating 表示・次 hand 割当・
  その場 player 作成、会計タブで point 付与。

## Implemented Behavior

- 上記すべて実装。seat batch PUT は指定 hand への **append**（conflict は error）。完全な
  idempotent-replace（hand seating クリア）は core メソッドが無いため後続（ISSUE-0020）。
- ledger reversal は API/client/test まで実装したが、staff アプリ UI は「session の entry 一覧」read
  API が無いため未提供（grant は UI 提供済）。entry 一覧 read API は後続。

## Test Results

- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **629 passed**（回帰なし。新規
  `test_viewer_api_staff_lifecycle.py` 含む）。
- `cd staff && npm run typecheck` — エラーなし。
- `npm test` — **12 passed / 0 fail**（§A/§B 追加分含む）。
- `npm run export:web` — green（`dist/`）。

## Mismatches Found During Testing

- 当初 `tests/...lifecycle.py` の helper が `tmp_path/"ro"/logs` を `mkdir(exist_ok=True)`（parents なし）で
  作ろうとして FileNotFoundError。`parents=True` に修正して解消。

## Fixes Applied

- test helper の `log_dir.mkdir(parents=True, exist_ok=True)`。

## Remaining Gaps / Out-of-Scope

- [ ] §C hand logger 遠隔制御（別プロセス境界）= ISSUE-0020 Q1。staff アプリのハンドタブは未提供。
- [ ] seat batch の idempotent-replace（core の hand seating クリア method 後）。
- [ ] ledger reversal の staff アプリ UI（session の entry 一覧 read API が前提）。
- [ ] 確定済 settlement の GET（commit 後の支払状態の再取得。現状は画面 local state 保持）。

## Related ADRs

- `docs/adr/0036-staff-api-session-seat-handlogger-expansion.md`（§A/§B 実装）
- `docs/adr/0035-staff-ipad-app-touch-frontend.md`（staff アプリ）

## Related Issues

- `docs/issues/0020-staff-ipad-app-open-questions.md`（§C / open question）

## Related Commits

- 本 worklog と同じ commit
