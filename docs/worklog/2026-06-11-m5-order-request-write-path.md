# Worklog: M5 — スマホからのドリンク注文（order-request write path）

## Date

2026-06-11

## Scope / Task

プレイヤー向け参照アプリのロードマップ M5（ADR-0013）。viewer API 初の write 系 =
注文リクエスト。前提だった ISSUE-0013（プライバシーモデル）を user 決定（name-pick 維持）で
決着し、ADR-0015 として確定（staff-in-the-loop / in-process API / menu master）。

## Goal

- プレイヤーがスマホからメニューを見て注文リクエストを送れる。
- リクエストは pending で記録され、スタッフが `--ledger` 画面で確定したときのみ
  `ledger_entry`（kind=order）が作られる（却下も可）。台帳の append-only と検証点を維持。
- `order_requests.json` の書き込み競合を構成で排除する（単一プロセス所有）。

## Changed Files

- `docs/adr/0015-m5-order-request-write-path.md` — 4 決定（name-pick / staff-in-the-loop /
  単一プロセス所有 / menu master）+ 代替案
- `docs/issues/0013-player-viewer-privacy-model.md` — Fixed 化（v1 = name-pick、根拠と回帰テスト）
- `docs/contracts/schemas/order_request.schema.json`（0.1）+ `fixtures/order_request/` 5 件 +
  `_MODELS` 登録、`{ledger,viewer-api,error-shapes,repository-interfaces}.md` 追記
- `core/order_request.py` / `core/order_request_repository.py` — pending → confirmed | rejected、
  confirm で ledger 追記 → リンク（失敗時 pending 維持）、**thread-safe（RLock）**
  + **reload-on-read（mtime）**、`order_requests.json` 永続化
- `core/menu.py` + `menu.json` — メニューマスタ（コミット済みサンプル、店側編集）
- `api/server.py` — `GET /api/menu`、`GET/POST .../order-requests`、error→HTTP 対応
  （400 invalid_quantity・unknown_item / 409 session_closed・already_resolved /
  503 orders_unavailable）、`orders_writable` フラグ
- `main.py` — `--ledger` が `viewer_api.enabled=true` で uvicorn を背景スレッド起動
  （repo 共有 = 単一プロセス所有）。終了時 should_exit で停止
- `gui/ledger_entry.py` — 「注文リクエスト」欄（2 秒ポーリング、単価 prefill、確定/却下）
- `mobile/` — types / repository（getMenu / listOrderRequests / createOrderRequest）/
  HttpRepository POST / mock（in-memory）/ `OrderScreen`（メニュー・数量・送信・注文状況）/
  MyLedger から遷移
- `.gitignore`（order_requests.json）/ `config_default.json`（viewer_api.enabled の意味確定）/
  `CLAUDE.md` / `docs/usage.md` / `mobile/README.md` / `CHANGELOG.md`
- `tests/test_order_request_repository.py`（新規 16）/ `tests/test_viewer_api.py`（注文 7 件追加）

## Expected Behavior

- 注文 POST → pending 作成・ledger 不変。スタッフ確定 → ledger 記帳 + リンク。却下 → 記帳なし。
- menu 外 / 数量範囲外 / closed session / 解決済みの再操作は error code で reject。
- 単独 `--viewer-api` では注文 POST が 503。

## Implemented Behavior

Expected どおり。補足:

- 確定時の単価はスタッフが最終決定（menu prefill を上書き可）— 価格改竄をクライアント入力に
  依存させない。
- FastAPI の return 型 `JSONResponse | dict` が response model 生成と衝突 → `response_model=None`
  を明示（下記 Mismatches）。
- mobile の注文状況はポーリングなし（「状況を更新」ボタン。push は scope 外）。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **400 passed**（+22、skip 0）
- mobile: `npm run typecheck` クリーン / `npm test` **8 pass** / `expo export --platform web` 成功
- 手動: `python main.py --viewer-api`（read-only）→ `GET /api/menu` がマスタを返し、
  注文 POST が 503 `orders_unavailable` を返すことを確認
- `--ledger` + 組み込み API + スマホの E2E は実機スモーク待ち（headless 環境のため）

## Mismatches Found During Testing

- POST endpoint の return 型注釈 `JSONResponse | dict` で FastAPI が response model 生成に失敗
  （`FastAPIError: Invalid args for response field`）。

## Fixes Applied

- `@app.post(..., response_model=None)` を明示し型注釈は文字列リテラルに（応答 schema の自動生成を
  無効化。契約は viewer-api.md が source）。

## Remaining Gaps / Out-of-Scope

- [ ] 実機 E2E: `viewer_api.enabled=true` + `--ledger` + スマホからの注文 → 確定 → 会計反映
- [ ] player 側の注文キャンセル（現状はスタッフ却下で代替）/ 注文状況の自動更新（ポーリング/push）
- [ ] M6 (= S3b): ISSUE-0001 決着 → point 解放 → ledger / order_request schema の `1.0` freeze
- [ ] S4: settlement（paid/unpaid）
- [ ] PIN の再評価（なりすましが実害化した場合, ADR-0015）

## Related ADRs

- `docs/adr/0015-m5-order-request-write-path.md` / `docs/adr/0014-s3a-cash-only-ledger-first.md` /
  `docs/adr/0013-player-facing-viewer-api-first-architecture.md`

## Related Issues

- `docs/issues/0013-player-viewer-privacy-model.md`（Fixed）/
  `docs/issues/0001-point-balance-source-of-truth.md`（Open のまま — M6 gate）

## Related Commits

- （本 commit。M1 = `b5eadc8`, M2 = `ba0ad46`, M3 = `9f6661f`, M4 = `bf3beb2`）
