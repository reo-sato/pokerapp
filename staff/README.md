# pokerapp-staff — 店舗用 staff iPad アプリ (WS4)

スタッフが iPad（/ web）から店舗オペレーション（会計・注文・座席・ハンド）を操作するための
**スタッフ専用 front-end**（ADR-0037）。player 用 `mobile/`（Poker Hand Viewer）とは **別アプリ**。
Expo（React Native, TypeScript）。当面は **web export を運営 PC から LAN 配信**してタブレットの
ブラウザで開く運用（`mobile/` と同形）。同一コードでネイティブビルドにも移行できる。

## 現状スコープ（このスキャフォルド）

ADR-0037 の最優先 2 機能 = **会計（Ledger/精算）+ 注文リクエスト捌き**。これらは staff write API
（`/api/staff/...`, ADR-0021）が実装済みのため先行できる。

```
Login（staff token）→ SessionList → TableView(session)
                                      ├─ 会計タブ … エントリ追加 / buy-in プリセット / 中間集計 / 精算確定・支払
                                      └─ 注文タブ … pending 確定（→ order entry）/ 却下
```

**未実装（後続）**: 座席タブ・ハンドタブ、および HTTP の `listSessions`（`GET /api/staff/sessions`）は
**ADR-0038** の staff API 追加が前提（現状 `not_implemented` で reject）。mock では全機能が動く。
open question は `docs/issues/0020-staff-ipad-app-open-questions.md`。

## repository 注入（contract-first, ADR-0037 §6）

UI は `src/api/repository.ts` の `StaffRepository` interface のみに依存する:

| 実装 | 用途 |
|------|------|
| `MockStaffRepository` | 契約 fixtures 相当の in-memory（既定。API なしで画面開発。token = `demo-staff-token`） |
| `HttpStaffRepository` | staff API（`python main.py --ledger` + `viewer_api.enabled` + `staff_token`）を fetch |

`EXPO_PUBLIC_API_URL` を設定すると `HttpStaffRepository` に切り替わる。staff token はログイン画面で
入力し、全 write に `Authorization: Bearer <token>` で付与する（ADR-0021）。

## 開発

```bash
npm install                  # peer 競合があれば: npm install --legacy-peer-deps
npm run typecheck            # tsc --noEmit
npm test                     # MockStaffRepository の契約挙動テスト (node:test)
npm start                    # Expo dev server (mock データ、token=demo-staff-token)
EXPO_PUBLIC_API_URL=http://<運営PCのLAN IP>:8788 npm start   # 実 staff API 接続
```

### E2E（ブラウザ, Playwright）

web export（`dist/`）+ MockRepository をヘッドレス Chromium で開き、**ログイン→会計（追加/取消）→
注文確定→座席割当→セッション作成**の実フローをタップ駆動で検証する（`e2e/staff.spec.ts`）。staff アプリの
配布形態（web export を iPad Safari で開く）に最も近い自動テスト。

```bash
npm run e2e:install          # 初回のみ: Chromium を取得（要ネットワーク）
npm run e2e                  # export:web → 静的配信(e2e/serve.mjs) → playwright test
```

- API は不要（MockRepository, token=`demo-staff-token`）。`playwright.config.ts` の webServer が
  `dist/` を 4173 で配信する。
- 実機タッチ/レイアウト/ソフトキーボードの最終確認は**手動 QA**（iPad Safari に web export を LAN 配信、
  または Expo Go）で行う（Linux CI に iOS シミュレータは無いため）。

運営 PC 側では会計 write を所有するプロセスを起動しておく（単独 `--viewer-api` は read-only で
write が 503）:

```bash
# config viewer_api.enabled=true / staff_token="..." を設定のうえ
python main.py --ledger
```

## 店舗での配布（店舗 PC が `/staff/` で配信, ADR-0064）

店舗 PC の API（`python main.py --ledger`）が、リポジトリに含めたビルド（`api/static/staff/`）を
`http://<PC の IP>:8788/staff/` で配信する。iPad は Safari で開いてスタッフトークンでログインするだけ
（Node・別サーバー不要）。`staff/` を変えたらリポジトリの直下で作り直してコミットする:

```bash
python scripts/build_staff_web.py          # api/static/staff/ を作り直す（要 staff/node_modules）
python scripts/build_staff_web.py --check  # ソースとビルドが揃っているか（CI は tests/test_staff_web.py）
```

ビルドは API の URL を `/`（画面と同じ origin）、置き場所を `/staff`（`app.config.js` の
`STAFF_WEB_BASE_URL`）にし、トークンは埋め込まない。開発用の別配信が要るときは従来どおり:

```bash
EXPO_PUBLIC_API_URL=http://<運営PCのLAN IP>:8788 npm run export:web   # dist/ に静的ビルド（/ で配信）
```

**無認証ではなく staff token 認可だが、LAN 限定前提**（ADR-0021）。信頼できる店内ネットワークでのみ
使用すること。

## 型と契約

`src/api/types.ts` は `docs/contracts/`（player / ledger_entry / order_request / error-shapes）と
`api/server.py` の staff endpoints からの転記。validation / 業務ルールは複製しない（core が
source of truth）。契約が変わったら同タスクで本ディレクトリも更新する。
