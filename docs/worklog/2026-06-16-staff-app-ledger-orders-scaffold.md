# Worklog: staff iPad アプリ — 会計/注文タブの scaffold（WS4, ADR-0037）

## Date

2026-06-16

## Scope / Task

ADR-0037 の最優先 2 機能（会計 / 注文）について、新規 `staff/`（Expo/RN）アプリを mock 先行・
contract-first で scaffold する（WS4 の第一歩）。staff API（`/api/staff/...`, ADR-0021）が実装済の
範囲で動かす。

## Goal

- player 用 `mobile/` とは別アプリの `staff/` を作り、Login → SessionList → TableView（会計/注文タブ）が
  mock で動く。
- `StaffRepository` interface + `MockStaffRepository`（fixtures）/ `HttpStaffRepository`（staff API）を
  注入で差し替えられる（`EXPO_PUBLIC_API_URL` 切替）。
- typecheck + mock 契約テスト + web export が green。

## Changed Files

- `staff/package.json` / `tsconfig.json` / `app.json` / `index.ts` / `.gitignore` / `AGENTS.md` /
  `README.md` — Expo/RN プロジェクト雛形（`mobile/` のバージョン・規約を踏襲）。
- `staff/App.tsx` — 最小 useState スタック（login → sessions → table）+ repository 注入。
- `staff/src/api/types.ts` — contract / staff endpoints からの型転記（Player / LedgerEntry /
  SessionSettlement / OrderRequest / MenuItem / StaffSession / StaffApiError）。
- `staff/src/api/repository.ts` — `StaffRepository` interface（会計/注文 scope + token）。
- `staff/src/api/mockRepository.ts` — `MockStaffRepository`（settlement 計算 / commit / payment /
  order confirm-reject を core 近似で再現、error code 一致）。
- `staff/src/api/httpRepository.ts` — `HttpStaffRepository`（staff API を fetch、Bearer token。
  `listSessions` は ADR-0038 §B 未実装で `not_implemented`）。
- `staff/src/api/mockRepository.test.ts` — 7 件の契約テスト（node:test）。
- `staff/src/mocks/fixtures.ts` — fixtures（open/closed session, players, ledger, orders, menu, presets）。
- `staff/src/hooks/useAsync.ts` — loading/error(code)/data + reload。
- `staff/src/screens/common.tsx` — 共有 UI（Button / Field / Chip / payment バッジ / yen 整形 / 色）。
- `staff/src/screens/{LoginScreen,SessionListScreen,TableViewScreen,LedgerTab,OrdersTab}.tsx` — 画面。
- `docs/adr/0035-...md`（Validation 更新）/ `CLAUDE.md`（WS4 + 実装状況）/ `CHANGELOG.md` 更新。

## Expected Behavior

- staff token でログイン（mock=`demo-staff-token`、HTTP=`viewer_api.staff_token`）。token 不正は
  unauthorized で弾く。
- session を選び、会計タブで entry 追加 / プリセット / 中間集計 / 精算確定・支払、注文タブで pending の
  確定（order entry を起こす）/ 却下ができる。
- 業務ルール（entry_fee は cash only / closed のみ commit / 確定は一度 / 注文確定で order_total 反映 /
  closed session への注文確定は session_closed）を mock が core 近似で enforce、error は code で表示分岐。

## Implemented Behavior

- 上記すべて実装。会計タブは settlement を speculative compute で表示し、closed session で commit →
  確定行（paid/unpaid トグル + 受領額記録）を local state で保持。注文確定後にタブ再マウントで会計が
  最新化（条件レンダリング）。注文 pending 件数はタブのバッジに表示。
- `HttpStaffRepository` は会計/注文の staff endpoint に 1:1 対応。`listSessions` のみ未実装
  （`GET /api/staff/sessions` = ADR-0038 §B）で `not_implemented`、`listPlayers` は当面 `GET /api/players`
  を流用。

## Test Results

- `cd staff && npm install --legacy-peer-deps` — OK（peer 競合は `mobile/` と同じ react/react-native-web
  バージョン由来。legacy-peer-deps で解消）。
- `npm test`（tsx --test）— **7 passed / 0 fail**（認可 / 中間集計 / entry 追加 / commit + 支払遷移 /
  注文 confirm-reject + error code）。
- `npm run typecheck`（tsc --noEmit）— **エラーなし**。
- `npm run export:web`（expo export --platform web）— **green**（`dist/` 生成）。
- Python 側テストへの影響なし（`staff/` は独立、既存コード不変）。

## Mismatches Found During Testing

- `npm install` が ERESOLVE（react 19 ↔ react-native-web peer）で失敗。`mobile/` と同条件のため
  `--legacy-peer-deps` で install する運用とした（README に記載）。挙動上の問題なし。

## Fixes Applied

- install は `--legacy-peer-deps` を使用（`mobile/` と同じ依存ツリー）。コード修正は不要。

## Remaining Gaps / Out-of-Scope

- [ ] 座席タブ / ハンドタブ（不足 staff API = ADR-0038 §B/§C 実装後）。
- [ ] HTTP `listSessions`（`GET /api/staff/sessions`, ADR-0038 §B）。現状 mock のみ。
- [ ] 確定済 settlement の再取得 API が無く、commit 後の支払状態は画面 local state 保持
      （画面離脱で消える）。GET committed settlements は ADR-0038 で検討。
- [ ] `ViewerApiClient`（Python）への staff 会計/注文メソッドは既存（ADR-0021）。staff app 専用の
      round-trip 実機テストは API 拡張時に。
- [ ] open question（hand logger 遠隔制御 / session 結線 / token 運用 / 並行・オフライン）= ISSUE-0020。

## Related ADRs

- `docs/adr/0037-staff-ipad-app-touch-frontend.md` — staff iPad アプリ（本 scaffold の設計）
- `docs/adr/0038-staff-api-session-seat-handlogger-expansion.md` — 不足 staff API（後続）

## Related Issues

- `docs/issues/0020-staff-ipad-app-open-questions.md` — open question / risk register

## Related Commits

- 本 worklog と同じ commit
