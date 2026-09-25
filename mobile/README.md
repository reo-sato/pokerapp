# pokerapp-mobile — Poker Hand Viewer (M2)

プレイヤーが自分のスマホからハンド履歴を参照するための front-end（WS3, ADR-0013）。
Expo（React Native, TypeScript）。当面は **web export を運営 PC から LAN 配信** して
スマホのブラウザで開く運用（QR コード）。同一コードでネイティブビルドにも移行できる。

## 画面

PlayerSelect（自分の名前を選ぶ）→ MySessions → MyHands → HandDetail
（board / 自分のホールカード / アクション列 / 収支）。
MyHands からは **会計**（MyLedger: バイイン・注文の明細と中間集計, M4/S3a, read-only）、
会計からは **ドリンク注文**（Order: メニュー選択 → リクエスト送信 → 注文状況, M5）に遷移できる。
注文はスタッフ確定（`--ledger` 画面）まで会計に載らない（ADR-0015 staff-in-the-loop）。
注文の受付には運営側が `viewer_api.enabled=true` で `--ledger` を起動している必要がある。

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

## 店舗での配布（viewer API が画面も出す, ADR-0059）

お客さん向けの web 版はリポジトリの **`api/static/player/`** に同梱し、viewer API（`python main.py --viewer-api`、
店舗 PC では `start_viewer.cmd`）が **`/` で配信**する。お客さんは `http://<PC の IP>:8788/` を開くだけ
（画面と API が同じ origin なので、ビルドに IP を埋め込まない）。店舗 PC に Node は要らない。

このディレクトリを変えたら、リポジトリのルートで作り直してコミットする（忘れると CI の
`tests/test_player_web_build.py` が落ちる）:

```bash
python scripts/build_player_web.py      # npm ci 済みの mobile/ を web export → api/static/player/
```

ビルドの設定は固定: `EXPO_PUBLIC_API_URL=/`（同じ origin）/ `EXPO_PUBLIC_PLAYER_AUTH=off`（PIN・サインアップの
導線を出さない）/ `EXPO_PUBLIC_STAFF_TOKEN` なし（ハンド訂正を出さない）/ `.env*` は読まない。

無認証のため信頼できる店内ネットワークのみで使用すること（ISSUE-0019 参照）。

別の静的サーバーで配る場合（開発用）:

```bash
EXPO_PUBLIC_API_URL=http://<運営PCのLAN IP>:8788 npm run export:web   # dist/ に静的ビルド
```

## 型と契約

`src/api/types.ts` は `docs/contracts/`（player 1.0 / hand・action 1.0 /
player_session_summary 0.x / error-shapes）からの転記。validation ロジックは複製しない
（core が source of truth）。契約が変わったら同タスクで本ディレクトリも更新する。
