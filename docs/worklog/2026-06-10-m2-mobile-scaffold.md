# Worklog: M2 — Poker Hand Viewer mobile scaffold（Expo, mock→API 注入切替）

## Date

2026-06-10

## Scope / Task

プレイヤー向け参照アプリ第 1 フェーズの後半（ADR-0013, M1 viewer API の続き）。
WS3 mobile scaffold を `mobile/` に作成し、mock repository 先行 + HTTP repository 差し替えの
境界を実装する。

## Goal

- Expo (TypeScript) プロジェクトが `mobile/` に存在し、PlayerSelect → MySessions → MyHands →
  HandDetail の参照フローが `ViewerRepository` interface 越しに動く。
- mock（fixtures 相当）⇔ HTTP（M1 viewer API）を UI 無改修で切替できる。
- typecheck / repository テスト / web export（LAN 配布形態）が通る。

## Changed Files

- `mobile/`（新規, `npx create-expo-app --template blank-typescript` 起点。
  テンプレート同梱の `.claude/` / `CLAUDE.md` / `LICENSE`（Expo 名義 MIT）は削除）
  - `App.tsx` — repository 注入（`EXPO_PUBLIC_API_URL` で mock/HTTP 切替）+ 最小 stack navigation
  - `src/api/types.ts` — contracts（player 1.0 / hand・action 1.0 / player_session_summary 0.x /
    error-shapes）からの型転記 + `ViewerApiError`
  - `src/api/repository.ts` — `ViewerRepository` interface（viewer-api.md 対応）
  - `src/api/httpRepository.ts` — fetch 実装（非 2xx は `{code,message}` を ViewerApiError に）
  - `src/api/mockRepository.ts` / `src/mocks/fixtures.ts` — fixtures 相当の in-memory 実装
    （ID は contract fixtures の canonical 値を再利用）
  - `src/api/mockRepository.test.ts` — 契約挙動テスト（node:test, 6 件）
  - `src/hooks/useAsync.ts` / `src/screens/common.tsx` — loading / error(code 分岐) 共通部
  - `src/screens/{PlayerSelect,MySessions,MyHands,HandDetail}Screen.tsx` — 4 画面
  - `package.json` — name=pokerapp-mobile, scripts: typecheck / test / export:web。
    web 用 react-dom / react-native-web は `expo install` が api.expo.dev 不達のため
    `expo/bundledNativeModules.json` の同梱版数（19.2.3 / ~0.21.0）で直接 npm install
  - `tsconfig.json` — strict + `types: ["node", "react"]`（node:test の型解決）
  - `README.md` — 開発 / LAN 配布手順
- `CLAUDE.md` — ディレクトリ構成 / 実装状況 / WS3 / Mobile scaffold 節を M2 実装済に更新
- `CHANGELOG.md` — Unreleased に M2 追記

## Expected Behavior

- mock データで 4 画面の参照フローが成立し、`EXPO_PUBLIC_API_URL` を与えるだけで
  M1 viewer API に切り替わる（UI 層無改修）。
- not_found / 通信失敗は error code で UI 分岐（error-shapes.md と同じ分岐キー）。
- `expo export --platform web` で LAN 配布用静的ビルドが生成できる。

## Implemented Behavior

Expected どおり。補足:

- navigation は react-navigation 等を入れず useState の最小 stack（M2 で依存を増やさない判断。
  画面数が増える M4 以降で導入を再検討）。
- 当初案（CLAUDE.md 旧記述）の「最初の skeleton = player registry 画面」は viewer 画面に
  差し替え（ADR-0013 の API-first 方針に整合）。registry 編集画面は後続。
- hand.players の「自分の行」は player_id（additive, E3 前は absent）→ display_name の順で
  fallback 解決（`findOwnRow`）。

## Test Results

- `npm run typecheck` — エラーなし
- `npm test` — **6 pass / 0 fail**（MockRepository 契約挙動）
- `npm run export:web` — `dist/` 生成成功（index.html + 433KB bundle）
- Python 側回帰: `pytest tests/ -q --ignore=tests/test_vision.py` — 351 passed（M1 時点から不変）
- 手動（ブラウザ/エミュレータ無し環境のため未実施）: 画面遷移の実機確認は次回
  `npm start` で行う（plan の verification 節どおり worklog に明記）

## Mismatches Found During Testing

- `npx expo install react-dom react-native-web` が api.expo.dev 不達で失敗（実行環境の
  ネットワーク制約）。→ Fixes 参照。
- `node:test` / `node:assert` の型が expo/tsconfig.base の既定で解決されず typecheck 失敗。

## Fixes Applied

- web 依存は `expo/bundledNativeModules.json` の SDK 56 同梱版数を読んで npm install 直叩き
  （`expo install` と同じ結果になる版数固定）。
- `tsconfig.json` に `"types": ["node", "react"]` を追加し @types/node を devDependencies に導入。

## Remaining Gaps / Out-of-Scope

- [ ] 実機/ブラウザでの画面遷移スモーク（`npm start` / web 配信）— 運営 PC 環境で実施
- [ ] M3 (= E3, ISSUE-0006): seat 選択 GUI + main.py 結線（これまで実データは流れない）
- [ ] M4 以降: ledger（cash-only 先行凍結）→ ドリンク注文 write path（ISSUE-0013 決着が前提）
- [ ] player registry 編集画面（list/add/rename）の mobile 版
- [ ] navigation ライブラリ導入の再検討（画面数増加時）

## Related ADRs

- `docs/adr/0013-player-facing-viewer-api-first-architecture.md`

## Related Issues

- `docs/issues/0013-player-viewer-privacy-model.md` / `docs/issues/0006-seat-selection-ux-at-hand-start.md`

## Related Commits

- （本 commit。M1 は直前 commit `b5eadc8`）
