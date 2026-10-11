# ADR-0064: スタッフ用 iPad 画面を店舗 PC の API が `/staff/` で配信する

## Status

Accepted（2026-10-11, 実装済。店舗 PC での確認は未）

## Date

2026-10-11

## Context

製品化のフェーズ 1「店舗内の仕上げ」（ADR-0063 D8）の最初の作業。スタッフ用 iPad アプリ（`staff/`, ADR-0037）は
実装済みだが、店舗で iPad から開く手段が無かった（ADR-0057 Stage 2 の未着手項目）:

- `staff/README.md` の配布手順は「`EXPO_PUBLIC_API_URL=http://<PC の IP>:8788 npm run export:web` で
  `dist/` を作り、任意の静的サーバーで配信」= 店舗 PC か別の PC に Node と静的サーバーが要る。
- API の URL をビルドに埋め込むため、PC の IP が変わると作り直しになる。
- お客さん向け画面は ADR-0059 で「ビルドをリポジトリに含め、API と同じポートの `/` で配信」して解決済み。
- ログイン画面に開発用の案内（「mock モードでは demo-staff-token でログインできます」）が実接続でも出ていた。

他店へ配る製品（ADR-0063）では、店舗に Node を入れたり手でビルドしたりする手順は成り立たない。

## Decision

### D1. ADR-0059 と同じ方式で、スタッフ用画面を `/staff/` に置く

- `scripts/build_staff_web.py` が `staff/` を web ビルドし、`api/static/staff/` に置く。**ビルドはリポジトリに
  含める**（店舗 PC に Node 不要）。`staff/` のソースを変えて作り直し忘れると `tests/test_staff_web.py` が CI で落とす
  （ソースとビルド設定の hash, `build-info.json`）。
- `create_app(staff_web_dir=STAFF_WEB_DIR)` が `/staff` に mount する（`/` のお客さん向け画面より先。`/staff` は
  `/staff/` へ転送）。`--ledger`（`viewer_api.enabled=true`）と `--viewer-api` の両方が配信する。
  入口は `Cache-Control: no-cache`、hash 入りの `_expo/` はキャッシュ可（ADR-0059 と同じ）。
- 起動時に「スタッフ用画面（iPad）: `http://<この PC の IP>:8788/staff/`」を出す。

### D2. ビルドの設定

- `EXPO_PUBLIC_API_URL=/`（画面と同じ origin の API を読む = PC の IP が変わっても作り直し不要）。
- 置き場所は Expo の `experiments.baseUrl=/staff`。**ビルドのときだけ**効かせる: `staff/app.config.js` が環境変数
  `STAFF_WEB_BASE_URL` を見る（開発の `expo start` と e2e の `dist/`（`/` で配信）は従来どおり）。
- **スタッフトークンは埋め込まない**（ログイン画面で入力。`/api/staff/...` の認可は ADR-0021 のまま）。開発者の
  `EXPO_PUBLIC_*` と `staff/.env*` は読ませない（`EXPO_NO_DOTENV=1`）。
- Web の表示名を「店舗スタッフ」・言語を `ja` に（`staff/app.json` の `web`）。

### D3. ログイン画面の demo トークンの案内は模擬データのときだけ出す

- `LoginScreen` の `showDemoHint`（`App.tsx` が `EXPO_PUBLIC_API_URL` 未設定 = mock repository のときだけ true）。

## Alternatives Considered

- **別ポートで配信する（例 8789）** — mount の順序を気にしなくてよい。
  - Why rejected: ファイアウォールの許可（`docs/installation.md` §0）とショートカットが増える。API と別 origin になり CORS
    と API の URL 埋め込みが要る。
- **`staff/app.json` に `experiments.baseUrl` を常に書く** — 設定ファイルを増やさない。
  - Why rejected: 開発サーバーと e2e（`/` で配信）まで `/staff` 配下になり壊れる。
- **お客さん向けとスタッフ用を 1 つのアプリに統合する** — ビルドが 1 つ。
  - Why rejected: ADR-0037 で別アプリと決めている（お客さんの画面にスタッフ操作の導線を一切含めない）。

## Consequences

- Positive: iPad は Safari で `http://<PC の IP>:8788/staff/` を開くだけ。店舗 PC に Node も別サーバーも要らない。
  お客さん向け画面と同じポート・同じファイアウォール規則で済む。
- Negative / trade-offs: リポジトリにビルド（約 0.5 MB）が増える。`staff/` を変えるたびにビルドし直してコミットが要る。
- Neutral: スタッフの write は従来どおり `--ledger`（write 所有プロセス）でのみ有効。`--viewer-api` 単独で開いた
  スタッフ画面は読み取りだけ（write は 503 `orders_unavailable`）。

## Validation / Follow-up

- [x] `tests/test_staff_web.py`（ビルドの揃い方・置き場所・トークン非埋め込み・`/staff/` の配信・`/` と `/api/` が不変）
- [ ] 店舗 PC + iPad Safari での手動確認（ログイン → 会計・注文・座席・ハンドのタブ）
- [ ] フェーズ 1 の残り: 自動起動 / リリース版での更新 / 同期の常駐（ADR-0063 D8）

## Related Files

- `scripts/build_staff_web.py`, `api/static/staff/`, `api/server.py`（`STAFF_WEB_DIR` / `staff_web_dir`）, `main.py`
- `staff/app.config.js`, `staff/app.json`, `staff/App.tsx`, `staff/src/screens/LoginScreen.tsx`

## Related Tests

- `tests/test_staff_web.py`

## Related Commits

- 本 ADR と同じ commit

## Supersedes / Superseded by

- Supersedes: —（ADR-0057 Stage 2 の「iPad 画面の API 配信」を実現。方式は ADR-0059 に揃える）
- Superseded by: —
