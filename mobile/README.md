# pokerapp-mobile — Poker Hand Viewer (M2)

プレイヤーが自分のスマホからハンド履歴を参照するための front-end（WS3, ADR-0013）。
Expo（React Native, TypeScript）。当面は **web export を運営 PC から LAN 配信** して
スマホのブラウザで開く運用（QR コード）。同一コードでネイティブビルドにも移行できる。

## 画面

PlayerSelect（自分の名前を選ぶ）→ MySessions → MyHands → HandDetail
（board / 自分のホールカード / アクション列 / 収支）

## repository 注入（contract-first）

UI は `src/api/repository.ts` の `ViewerRepository` interface のみに依存する:

| 実装 | 用途 |
|------|------|
| `MockRepository` | 契約 fixtures 相当の in-memory データ（既定。API なしで画面開発） |
| `HttpRepository` | M1 viewer API（`python main.py --viewer-api`）を fetch |

`EXPO_PUBLIC_API_URL` を設定すると `HttpRepository` に切り替わる。

## 開発

```bash
npm install
npm run typecheck            # tsc --noEmit
npm test                     # MockRepository の契約挙動テスト (node:test)
npm start                    # Expo dev server (mock データ)
EXPO_PUBLIC_API_URL=http://<運営PCのLAN IP>:8788 npm start   # 実 API 接続
```

## LAN 配布（web export）

```bash
EXPO_PUBLIC_API_URL=http://<運営PCのLAN IP>:8788 npm run export:web   # dist/ に静的ビルド
# dist/ を任意の静的サーバーで LAN 配信（例: python -m http.server -d dist 8080）
# 運営 PC 側では viewer API を LAN bind で起動しておく（config viewer_api.bind_host）
```

無認証のため信頼できる店内ネットワークのみで使用すること（ISSUE-0013 参照）。

## 型と契約

`src/api/types.ts` は `docs/contracts/`（player 1.0 / hand・action 1.0 /
player_session_summary 0.x / error-shapes）からの転記。validation ロジックは複製しない
（core が source of truth）。契約が変わったら同タスクで本ディレクトリも更新する。
