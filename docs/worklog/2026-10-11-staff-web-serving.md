# Worklog: スタッフ用 iPad 画面を店舗 PC から `/staff/` で配信する

## Date

2026-10-11

## Scope / Task

製品化フェーズ 1（ADR-0063 D8）の最初の作業。ADR-0057 Stage 2 の「iPad 画面の API 配信」を ADR-0059 と同じ方式で
実現する（ADR-0064）。

## Goal

- iPad は Safari で `http://<PC の IP>:8788/staff/` を開き、スタッフトークンでログインするだけで使える。
- 店舗 PC に Node・別の静的サーバーを入れない。PC の IP が変わってもビルドし直さない。
- スタッフトークンを画面に埋め込まない。お客さん向け画面（`/`）と API（`/api/`）の動きは変えない。

## Changed Files

- `scripts/build_staff_web.py` — 新規。`staff/` を web ビルドして `api/static/staff/` に置く（`--check` で drift 検査）
- `api/static/staff/` — 新規。ビルド（約 0.5 MB, `build-info.json` に設定とソースの hash）
- `staff/app.config.js` — 新規。`STAFF_WEB_BASE_URL` のときだけ Expo `experiments.baseUrl` を足す
- `staff/app.json` — Web の表示名「店舗スタッフ」・`lang: ja`・themeColor
- `staff/App.tsx` / `staff/src/screens/LoginScreen.tsx` — demo トークンの案内を mock のときだけ（`showDemoHint`）
- `api/server.py` — `STAFF_WEB_DIR` / `STAFF_WEB_PATH`、`create_app(staff_web_dir=)` で `/staff` に mount（`/` より先、`/staff` は
  `/staff/` へ転送）、`log_web_addresses()`
- `main.py` — `--ledger` の組み込み API にも `staff_web_dir` を渡し、開く URL を表示
- `tests/test_staff_web.py` — 新規 14 件
- 文書: `docs/adr/0064-…`、`docs/decision-log.md`、`CHANGELOG.md`、`CLAUDE.md`、`docs/usage.md`（新しい節）、
  `docs/installation.md`（ショートカット表）、`staff/README.md`（配布の節）

## Expected Behavior

- `GET /staff/` → スタッフ用画面（`<title>店舗スタッフ</title>`）、JS は `/staff/_expo/...`。`GET /staff` → `/staff/` へ 307。
- `GET /` → 従来どおりお客さん向け画面。`/api/...` は従来どおり JSON（未知のパスは JSON の 404）。
- `/api/staff/...` はトークンなしで 401、正しいトークンで 200（画面の配信で認可は変わらない）。
- 開発（`npm start`）と e2e（`npm run export:web` → `/` で配信）は従来どおり。

## Implemented Behavior

- 期待どおり。実 API（`create_app` + uvicorn、staff_token 設定）に headless Chromium で `/staff/` を開き、
  誤ったトークン →「トークンが無効です」、正しいトークン → セッション一覧の表示まで確認した（demo トークンの案内は出ない）。
- `npm run export:web` の `dist/index.html` は `/_expo/...` を指す（置き場所は配信版のビルドだけに効く）。

## Test Results

- `tests/test_staff_web.py` 14 passed。関連（`test_player_web_build` / `test_viewer_api_store` / `test_installer`）含め 81 passed。
- 全体: 2821 passed, 5 skipped, **1 failed** = `tests/test_reconstruction.py::test_golden_replay_matches_expected[postflop-street-transition]`。
  変更前の `verify-v1`（c912187）でも同じく落ちる = 今回の変更とは無関係（推論側の既存の問題。Python 3.13 / pokerkit 0.7.6 の環境）。
- `ruff check .` 通過。staff: `tsc --noEmit` 通過、`npm test` 46 passed、Playwright e2e 8 passed。

## Mismatches / Fixes

- 最初のビルドは `<title>staff</title>` / `lang="en"` だった → `staff/app.json` の `web` を設定。
- ログイン画面に開発用の demo トークンの案内が実接続でも出ていた → mock のときだけに。

## Remaining Gaps

- 店舗 PC + iPad Safari での手動確認。
- `--ledger` には `--host` が無いので、iPad から使うには `viewer_api.bind_host=0.0.0.0` の設定が要る（`docs/usage.md` に手順）。
  フェーズ 1 の「自動起動」で起動の形をまとめて見直す。
- 既存の golden replay の失敗（上記）は推論側の作業で確認してもらう。

## Related Commits

- 本 worklog と同じ commit
