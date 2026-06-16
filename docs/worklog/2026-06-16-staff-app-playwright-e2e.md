# Worklog: staff アプリ ブラウザ E2E（Playwright, WS4）

## Date

2026-06-16

## Scope / Task

staff iPad アプリの UI テストとして **ブラウザ E2E（Playwright）** を整備する。web export +
MockRepository をヘッドレス Chromium で開き、主要フローをタップ駆動で検証する。iPad の実配布形態
（web export を Safari で開く）に最も近い自動テスト。

## Goal

- API なし（MockRepository, token=`demo-staff-token`）で、ログイン→会計（追加/取消）→注文確定→
  座席割当→セッション作成の実フローが回ることを自動検証する。
- `npm run e2e` で export:web → 静的配信 → playwright test を一括実行できる。

## Changed Files

- `staff/package.json` — devDep `@playwright/test`、scripts `e2e` / `e2e:install`。
- `staff/playwright.config.ts` — testDir e2e、iPad 相当 viewport + touch、webServer（`serve.mjs`）。
- `staff/e2e/serve.mjs` — 依存なしの静的サーバー（`dist/` 配信 + SPA fallback）。
- `staff/e2e/staff.spec.ts` — E2E 5 件（invalid token / ledger add+reverse / orders confirm /
  seating assign / session create）。
- `staff/src/screens/LedgerTab.tsx` / `SeatingTab.tsx` — E2E 用に入力欄 placeholder を追補
  （cash(円) / point(任意) / 付与pt / 席1-9）。UX 的にも自然な追加。
- `staff/.gitignore` — Playwright 成果物（test-results/ 等）を除外。
- docs: `docs/adr/0035-...md`（Validation に E2E）/ `CLAUDE.md` / `CHANGELOG.md`。

## Expected Behavior

- `npm run e2e` が dist を作って配信し、5 件の E2E が headless Chromium で pass する。
- selector は表示テキスト（exact でチップ一意化）+ placeholder（testID 非依存）。

## Implemented Behavior

- 上記の harness を実装。RN Web の Pressable=div / TextInput=input を前提に、テキスト/placeholder で
  操作・アサート。各テストは page reload で MockRepository を初期化（独立）。

## Test Results

- `npm run typecheck`（e2e spec + playwright.config 含む）— エラーなし。
- `npm test`（mock 契約）— 12 passed。
- `npm run export:web` — green。`node e2e/serve.mjs` → `curl /` = HTTP 200 + `<div id="root">`、
  SPA fallback OK。
- `npx playwright test --list` — 5 tests を検出（config + spec 解析 OK）。
- **browser 実行は本環境のネットワークポリシーで未実施**: `npx playwright install chromium` が
  ダウンロード失敗（download code=1）。ブラウザ取得が可能な環境（開発機 / CI）で `npm run e2e:install`
  → `npm run e2e` を実行する。

## Mismatches Found During Testing

- Playwright の Chromium ダウンロードがオフライン環境で失敗。harness の配線（typecheck / 静的配信 /
  test 検出）は検証済みだが、ブラウザ実行はネットワーク前提のため別環境で行う。

## Fixes Applied

- なし（環境制約。README / 本 worklog に手順と前提を明記）。

## Remaining Gaps / Out-of-Scope

- [ ] CI への E2E 組み込み（browser キャッシュ or `playwright install` を許可する runner が前提）。
- [ ] 実機 iPad のタッチ/レイアウト/キーボードは手動 QA（別途。ハンドタブ実装後にまとめても良い）。
- [ ] 実 staff API（`--ledger`）相手の結合 E2E（EXPO_PUBLIC_API_URL を build に渡す変種）は後続。
- [ ] ハンドタブ（ADR-0036 §C / ISSUE-0020）実装時に E2E を追補。

## Related ADRs

- `docs/adr/0035-staff-ipad-app-touch-frontend.md`（UI テスト = E2E）

## Related Issues

- `docs/issues/0020-staff-ipad-app-open-questions.md`

## Related Commits

- 本 worklog と同じ commit
