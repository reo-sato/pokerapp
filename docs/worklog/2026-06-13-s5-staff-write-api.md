# Worklog: S5 write 拡張 — スタッフ会計 write API（staff shared token 認証）

## Date

2026-06-13

## Scope / Task

S5 write boundary の第一歩。スタッフ会計 write（ledger 追加 / settlement 確定 / paid-unpaid /
注文確定・却下）+ それを駆動する staff read を **viewer API に staff shared token 認証付きで公開**し、
別端末のスタッフがリモートで会計運用できるようにする（ADR-0021）。

## Goal

- スタッフ会計 write を `/api/staff/...` で HTTP 公開し、staff shared token で認可する。
- 単一書き手（ADR-0020）を維持しつつ、`--ledger` プロセスの GUI × API 同時 mutate を thread-safe に。
- player read / 注文 POST は無認証のまま（ISSUE-0019）。

## Changed Files

- `core/ledger_repository.py` — `threading.RLock` + module-level `_locked` デコレータを追加し、
  読み書き公開メソッド（add_entry / reverse_entry / grant_points / point_balance /
  list_point_entries / list_entries / compute_settlement / commit_settlement / list_settlements /
  all_settlements / set_payment_status）を囲んだ。業務ロジックは不変。
- `api/server.py` — `create_app(..., staff_token=None)`。`_staff_guard`（403/401/503）+
  `_map_ledger_error`（LedgerError → error-shape）+ 7 個の `/api/staff/...` エンドポイント。
  `run_server` も staff_token を config から渡す。
- `api/client.py` — `ViewerApiClient(..., staff_token=None)` + `_request(headers=...)` +
  staff メソッド群（compute_settlement / list_session_order_requests / add_ledger_entry /
  commit_settlement / set_payment_status / confirm_order / reject_order）。
- `main.py` — `run_ledger_view` の in-process `create_app` に `staff_token` を結線。
- `config_default.json` — `viewer_api.staff_token`（既定空 = 無効）+ コメント。
- `tests/test_viewer_api_staff.py` — 新規。happy path（write 一通り）+ 認可失敗 + error code。
- docs: ADR-0021 / decision-log / viewer-api.md / error-shapes.md / repository-interfaces.md /
  ISSUE-0019 / CLAUDE.md / CHANGELOG.md。

## Expected Behavior

- staff token 設定時のみ `/api/staff/...` が有効。`Authorization: Bearer <token>` 必須。
- token 未設定 → 403 `staff_writes_disabled`、不一致 → 401 `unauthorized`。
- write 系は orders_writable=True（write 所有プロセス）でのみ。read-only は 503 `orders_unavailable`。
- ledger / settlement / order の実 error は既存 error-shape code を再利用。
- player read / 注文 POST は無認証で従来どおり。

## Implemented Behavior

期待どおり。`_staff_guard(request, need_write=...)` を各 staff endpoint の先頭で呼び、
`JSONResponse | None` を返す（None なら本処理）。staff read（settlement / 注文 queue）は need_write=False
なので read-only プロセスでも token があれば通る。ledger error は `_map_ledger_error` で集約マップ
（具体例外を先に並べて isinstance 判定）。order 確定・却下の error（not_found / already_resolved /
session_closed）は既存の exception handler / `_ORDER_ERROR_MAP` が拾う。

## Test Results

- `python -m pytest tests/ --ignore=tests/test_vision.py -q` → **491 passed, 0 skipped**
  （baseline 482 + 新規 staff test 9）。
- `python -m pytest tests/test_ledger_repository.py -q` → 23 passed（lock 追加後も緑）。
- `ruff check .` → All checks passed!。

## Mismatches Found During Testing

None observed.

## Fixes Applied

なし（一発で緑）。

## Remaining Gaps / Out-of-Scope

- [ ] 双方向 sync / 複数書き手の衝突解決（単一書き手では不要。後続 ADR）。
- [ ] player per-player アクセス制御（PIN, ISSUE-0019。staff write は token で解決済み）。
- [ ] desktop GUI を staff API client backed にする（現状 local 直結で十分）。
- [ ] 並行性 stress test（RLock の正しさは構造で担保。負荷テストは追加していない）。

## Related ADRs

- `docs/adr/0021-s5-staff-write-api-token-auth.md` — 本作業の判断。
- `docs/adr/0020-s5-cross-app-boundary-repository-interface-and-api-client.md` — read boundary（前提）。
- `docs/adr/0018-m5-order-request-write-path.md` — 単一書き手 / staff-confirm。

## Related Issues

- `docs/issues/0019-player-viewer-privacy-model.md` — staff write 認可を token で解決（player PIN は将来）。

## Related Commits

- 本 worklog と同じ commit（feat(s5): staff accounting write API …）。
