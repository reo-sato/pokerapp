# pokerapp-staff — 店舗用 staff iPad アプリ (WS4)

スタッフが iPad（/ web）から店舗オペレーション（会計・注文・座席・ハンド）を操作するための
**スタッフ専用 front-end**（ADR-0035）。player 用 `mobile/`（Poker Hand Viewer）とは **別アプリ**。
Expo（React Native, TypeScript）。当面は **web export を運営 PC から LAN 配信**してタブレットの
ブラウザで開く運用（`mobile/` と同形）。同一コードでネイティブビルドにも移行できる。

## 現状スコープ（このスキャフォルド）

ADR-0035 の最優先 2 機能 = **会計（Ledger/精算）+ 注文リクエスト捌き**。これらは staff write API
（`/api/staff/...`, ADR-0021）が実装済みのため先行できる。

```
Login（staff token）→ SessionList → TableView(session)
                                      ├─ 会計タブ … エントリ追加 / buy-in プリセット / 中間集計 / 精算確定・支払
                                      └─ 注文タブ … pending 確定（→ order entry）/ 却下
```

**未実装（後続）**: 座席タブ・ハンドタブ、および HTTP の `listSessions`（`GET /api/staff/sessions`）は
**ADR-0036** の staff API 追加が前提（現状 `not_implemented` で reject）。mock では全機能が動く。
open question は `docs/issues/0020-staff-ipad-app-open-questions.md`。

## repository 注入（contract-first, ADR-0035 §6）

UI は `src/api/repository.ts` の `StaffRepository` interface のみに依存する:

| 実装 | 用途 |
|------|------|
| `MockStaffRepository` | 契約 fixtures 相当の in-memory（既定。API なしで画面開発。token = `demo-staff-token`） |
| `HttpStaffRepository` | staff API（`python main.py --ledger` + `viewer_api.enabled` + `staff_token`）を fetch |

`EXPO_PUBLIC_API_URL` を設定すると `HttpStaffRepository` に切り替わる。staff token はログイン画面で
入力し、全 write に `Authorization: Bearer <token>` で付与する（ADR-0021）。

## 開発

```bash
npm install
npm run typecheck            # tsc --noEmit
npm test                     # MockStaffRepository の契約挙動テスト (node:test)
npm start                    # Expo dev server (mock データ、token=demo-staff-token)
EXPO_PUBLIC_API_URL=http://<運営PCのLAN IP>:8788 npm start   # 実 staff API 接続
```

運営 PC 側では会計 write を所有するプロセスを起動しておく（単独 `--viewer-api` は read-only で
write が 503）:

```bash
# config viewer_api.enabled=true / staff_token="..." を設定のうえ
python main.py --ledger
```

## LAN 配布（web export）

```bash
EXPO_PUBLIC_API_URL=http://<運営PCのLAN IP>:8788 npm run export:web   # dist/ に静的ビルド
# dist/ を任意の静的サーバーで LAN 配信（例: python -m http.server -d dist 8080）
```

**無認証ではなく staff token 認可だが、LAN 限定前提**（ADR-0021）。信頼できる店内ネットワークでのみ
使用すること。

## 型と契約

`src/api/types.ts` は `docs/contracts/`（player / ledger_entry / order_request / error-shapes）と
`api/server.py` の staff endpoints からの転記。validation / 業務ルールは複製しない（core が
source of truth）。契約が変わったら同タスクで本ディレクトリも更新する。
