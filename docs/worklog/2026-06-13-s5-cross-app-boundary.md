# Worklog: S5 — cross-app boundary（repository interface 凍結 + Python API client）

## Date

2026-06-13

## Scope / Task

ロードマップ S5（cross-app contract / sync boundary）の read boundary 確立（ADR-0020）。実機が
不要な範囲で、contract-first の WS0（interface 契約凍結 + sync 方式 ADR）と WS1（Python API client
分離）を実装する。write/sync 拡張は後続 ADR に分離。

## Goal

- repository / service interface 契約を frozen にする（freeze order #6）。
- viewer API の Python client（mobile `HttpRepository` の Python 版）を追加し、read boundary を
  二言語で実証 + API↔client の round-trip 契約 test で drift を検知。
- 同期方式・衝突回避・ID 不変性を ADR で確定。

## Changed Files

- `docs/adr/0020-s5-cross-app-boundary-repository-interface-and-api-client.md`（新規, Accepted）。
- `api/client.py`（新規）— `ViewerApiClient` + `ViewerApiError`。viewer API の read endpoints
  （+ 注文 GET/POST）を httpx で呼び、非 2xx を error-shape の code を持つ例外に変換。
- `tests/test_viewer_api_client.py`（新規, 4 件）— API↔client の round-trip（reads / typed error /
  注文 pending-only / read-only 503）。TestClient を transport として注入し in-process round-trip。
- `pyproject.toml` — `[api]` extra に `httpx` 追加（client 用、dev には既存）。
- `docs/contracts/repository-interfaces.md` — header を frozen に、stale な「schema draft 0.x」注記を
  `1.0 frozen` へ、viewer API client セクション + S5 残作業を追記。
- `docs/contracts/versioning-and-freeze.md` — freeze order #6 を frozen に。
- `docs/decision-log.md` — ADR-0020 追加。
- `CLAUDE.md` — api/client.py を tree に、実装状況に S5 read boundary 行、ロードマップ S5 行・残作業・
  Phase 5 詳細を read boundary 実装済に更新。
- `CHANGELOG.md` — S5 boundary の Unreleased 追記。

## Expected / Implemented Behavior

read boundary が二言語（mobile TS `ViewerRepository` mock/HTTP + Python `ViewerApiClient`）で確立。
interface 契約は frozen。同期 = on-demand pull、衝突 = 単一書き手 + reload-on-read、ID は backend
非依存に安定。write/sync 拡張は scope 外（後続 ADR）。

## Test Results

- `pytest tests/ --ignore=tests/test_vision.py -q` — **482 passed**（+4 = round-trip client）。
- `ruff check .` — clean。

## Mismatches Found During Testing

- `httpx.ASGITransport` は async 専用（`handle_async_request`）で sync `httpx.Client` から使えず
  `AttributeError`。→ FastAPI `TestClient`（sync httpx 互換で ASGI を in-process 処理）を
  `ViewerApiClient` に注入する形に修正。

## Fixes Applied

- round-trip test の transport を `TestClient(app)` 注入に変更（ソケットなしで in-process round-trip）。

## Remaining Gaps / Out-of-Scope

- write/sync 拡張（注文以外の write を HTTP に / 双方向同期）: 認証（ISSUE-0019 PIN 再評価）・
  衝突解決方針が前提、別 ADR。
- desktop GUI の API client 化は任意（現状 local 直結で十分）。
- 実機 E2E（Phase H）・PN5180 firmware 契約（ISSUE-0014/0015）は実機待ち。

## Related ADRs / Issues

- ADR-0020（本 boundary）/ ADR-0004（contract-first boundary）/ ADR-0017（viewer API）/
  ADR-0018（orders）/ ADR-0019（schema freeze）/ ISSUE-0019（プライバシー, write 認証の前提）

## Related Commits

- 本 commit（S5 read boundary）
