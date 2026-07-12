# 2026-07-12 — 実運用 UI 補強（棚卸し ◎ 4 点）

## Goal

各 UI の実運用機能棚卸し（ユーザー確認済みの前提: 1 卓 + iPad 1 台 dogfood / mobile は
ネイティブ配布も視野 / desktop 維持最小）で「◎ = dogfood 開始前に欲しい」と整理した 4 点を実装する。

1. mobile の手動更新（useAsync reload + 全画面の再読込導線）
2. mobile の選択 player / ログイン状態の永続化
3. staff の注文 pending バッジの自動更新（5 秒 polling）
4. staff アプリ内のアクション訂正導線（B4/ADR-0036 のリプレイ詳細からの訂正）

## Changed files

- mobile: `src/hooks/useAsync.ts`（reload 追加）、`src/screens/common.tsx`（`ReloadLink` +
  `ErrorView.onRetry`）、`PlayerSelect/MySessions/MyHands/HandDetail/MyLedger/Order` 各画面、
  `src/storage.ts`（新規: web localStorage / native in-memory fallback）、
  `src/api/authStorage.ts`（新規: AuthSession 永続化・期限切れ破棄）、
  `src/api/{httpRepository,mockRepository}.ts`（constructor 復元 + login/oidc 保存 + clearAuth 破棄）、
  `App.tsx`（選択 player の保存・復元 = `phv.player`、戻るで破棄）、
  `src/storage.test.ts`（新規 4 件）、`src/api/mockRepository.test.ts`（storage リーク防止の beforeEach）
- staff: `src/hooks/useAsync.ts`（**再取得中 stale data 保持** — polling のちらつき解消。
  初回は data=null なので Loading 挙動は不変）、`src/screens/TableViewScreen.tsx`
  （open 卓のみ pending バッジ 5 秒 polling）、`src/api/types.ts`（HandCorrection 型）、
  `src/api/{repository,httpRepository,mockRepository}.ts`（`addHandCorrection`。mock は
  core `apply_hand_corrections` の意味論を近似: `_original` 保持 / corrected / needs_review 解除 /
  review_required 導出 / 計測 row 同期 = C-2 ガード解除）、
  `src/screens/HandCorrectionPanel.tsx`（新規）、`src/screens/HandTab.tsx`
  （選択を hand_id 化 = 訂正→reload で detail が訂正済みビューに更新 + パネル組み込み）、
  `src/api/mockRepository.test.ts`（+4）、`e2e/staff.spec.ts`（訂正フロー E2E +1）
- docs: `CLAUDE.md` / `CHANGELOG.md` / 本 worklog

## Expected vs implemented

期待どおり。設計上の要点:

- **API 変更なし**。訂正は既存 staff API（`POST .../hands/{hid}/corrections`）の再利用のみ。
  schema / 契約は不変。
- mobile の永続化はネイティブ配布を視野に **storage アダプタを 1 ファイルに隔離**
  （AsyncStorage への差し替えは `src/storage.ts` のみ変える）。オフラインキャッシュには使わない
  （source of truth は API / core のまま）。
- staff useAsync の stale-data 保持は既存画面の Loading 分岐（`loading ?` が先）を壊さない。
  `MeasurementTab` は既に `loading && rows.length === 0` で書かれており、従来 5 秒ごとに
  Loading にフォールバックしていたのが解消された（副次改善）。

## Tests

- mobile: `tsc --noEmit` green、`npm test` **25 passed**（mock 13 + storage 4 + replay 8）、
  `expo export --platform web` 成功。
- staff: `tsc --noEmit` green、`npm test` **34 passed**（+4 訂正）、web export 成功、
  Playwright E2E **8 passed**（+1 訂正フロー。preinstalled Chromium を executablePath 指定）。
- Python: `pytest` **717 passed**（shared UI drift 含む・変更なし）、`ruff check .` clean。
- 目視: staff リプレイ詳細の訂正パネル（要確認バッジ → 訂正編集）をスクリーンショット確認。

## Mismatches / fixes

- MockRepository の認証永続化が node:test のプロセス内でテスト間リークするため、
  `beforeEach(clearMemoryStorageForTest)` を mock テストに追加（fixtures 由来の挙動は不変）。

## Remaining gaps（棚卸しの ○ / △ は未着手のまま）

- ○: 注文キャンセル（要 API）、mobile ハンド共有/書き出し（Phase B KPI の観測対象）、
  営業日サマリの iPad 表示、menu 編集 UI、player リネーム/merge の iPad 導線、
  座席の明示解除、desktop の録音死活表示、mobile の PIN 自己設定導線。
- △: プッシュ通知（ネイティブ/PWA）、通算成績、player 検索、オフラインキュー、
  多言語、計測ランダム drill-in 等（棚卸し表参照）。
