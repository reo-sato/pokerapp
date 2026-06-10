# Worklog: M1 — player 向け読み取り専用 viewer API（API-first 着手）

## Date

2026-06-10

## Scope / Task

プレイヤー向け参照アプリ（最終形: ハンドログ + 会計 + ドリンク注文の Expo モバイル）の第 1 フェーズ。
S5 の read-only サブセットを前倒しし、viewer API 契約 + `api/` 実装を行う（ADR-0013）。
M2（Expo scaffold, `mobile/`）は同ブランチの後続 commit。

## Goal

- 「desktop 別画面から着手すべきか」への決着: **player 向け desktop viewer は作らず API-first**
  （ADR-0013）。WS3 技術選定を Expo（web export LAN+QR 先行）に確定。
- 読み取り専用 viewer API（契約 + FastAPI server + テスト）が CI 同等で緑、手動 curl で契約どおり
  動作すること。

## Changed Files

- `docs/adr/0013-player-facing-viewer-api-first-architecture.md` — 新規 ADR（API-first / FastAPI `[api]` / Expo 確定 / read model 規則）
- `docs/issues/0013-player-viewer-privacy-model.md` — プライバシーモデル open question（M5 前に決着）
- `docs/decision-log.md` — ADR-0013 / ISSUE-0013 を索引に追加
- `docs/contracts/viewer-api.md` — viewer API 契約（draft 0.x, endpoints / error 形 / read model 規則）
- `docs/contracts/schemas/player_session_summary.schema.json` — read model envelope schema（0.1）
- `docs/contracts/fixtures/player_session_summary/*.json` — canonical / valid-minimal / invalid-* 3 種
- `docs/contracts/repository-interfaces.md` — `list_hand_ids` を session interface に additive 追記
- `docs/contracts/README.md` — viewer-api.md をディレクトリ構造に追記
- `tests/test_contracts.py` — `_MODELS` に `player_session_summary` を登録
- `config_default.json` — `viewer_api` セクション（enabled=false, 127.0.0.1:8788）
- `pyproject.toml` — `[api]` extra（fastapi/uvicorn）、`dev` に httpx、packages に `api`
- `requirements-dev.txt` — fastapi / httpx 追加（CI skip 0 維持）
- `core/session_repository.py` — `list_hand_ids(session_id)` additive 追加
- `api/__init__.py` / `api/read_models.py` / `api/server.py` — 新規パッケージ（read model + FastAPI app factory）
- `main.py` — `--viewer-api` フラグ（fastapi 未導入は友好的メッセージで exit 1）
- `tests/test_viewer_read_models.py` — read model 単体（join / gracefully-empty / legacy）
- `tests/test_viewer_api.py` — TestClient で endpoint / error shape / schema 適合（importorskip）
- `CLAUDE.md` — Viewer API セクション昇格、実装状況 / 構成 / 技術スタック / WS3 / Phase 5 / コマンド更新
- `CHANGELOG.md` — Unreleased に M1 追記

## Expected Behavior

- `python main.py --viewer-api` で 127.0.0.1:8788 に GET-only API が立つ。
- player のハンドは seat_assignment 起点で hand log を join（E3 前は空、エラーにしない）。
- 404 は `{"code": "not_found", "message": ...}`。応答は既存 schema（player 1.0 / hand 1.0 /
  player_session_summary 0.x）に適合。
- CI 同等のテストが skip 0 で緑のまま。

## Implemented Behavior

Expected どおり。補足:

- `/api/sessions/{sid}/hands/{hid}` は session レイヤ未登録の legacy session_id（timestamp 形式）
  でも hand log があれば返す（契約 doc に備考として明記）。
- `viewer_api.enabled` は M1 では未参照（将来の in-process 起動用 placeholder。`--viewer-api` は
  明示起動なので config フラグに依らない）。CLAUDE.md Out of scope に明記。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **351 passed**（既存 330 + 新規 21、skip 0）
- 手動: `python main.py --viewer-api` 起動 → `curl /api/health` = `{"status":"ok",...}`、
  `/api/players` = `{"players":[]}`（root に players.json なし）、
  `/api/players/ffff...` = 404 `{"code":"not_found","message":"player_id=... は存在しません。"}`

## Mismatches Found During Testing

None observed.

## Fixes Applied

—

## Remaining Gaps / Out-of-Scope

- [ ] M2: Expo scaffold（`mobile/`、mock repository → API client 差し替え）— 同ブランチ後続
- [ ] M3 (= E3, ISSUE-0006): seat 選択 GUI + main.py 結線。これまで viewer の実運用データは空
- [ ] ISSUE-0013: プライバシーモデル（M5 の注文 write path 前に決着）
- [ ] viewer API 契約の freeze（session 系 schema の ISSUE-0005 gate に従う）
- [ ] starlette の `httpx` deprecation warning（テスト時のみ。将来 httpx2 移行を検討）

## Related ADRs

- `docs/adr/0013-player-facing-viewer-api-first-architecture.md`

## Related Issues

- `docs/issues/0013-player-viewer-privacy-model.md` / `docs/issues/0006-seat-selection-ux-at-hand-start.md`

## Related Commits

- （本 commit）
