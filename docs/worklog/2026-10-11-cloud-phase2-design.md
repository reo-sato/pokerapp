# Worklog: クラウド最小（フェーズ 2）の設計

## Date

2026-10-11

## Scope / Task

製品化フェーズ 2（ADR-0063 D8）の設計。ADR-0063 の Follow-up「フェーズ 2 着手時の ADR」（受け口・店舗 ID と端末トークン・
Postgres の事業者・公開範囲の形）に答える。ADR-0067。コードは含まない。

## Goal

- オーナーの決定（データベースは Supabase）を受けて、クラウド最小の形と実装の段（2a〜2e）を決める。
- オーナーが先にできる登録（Google Cloud・予算アラート・Supabase）の手順を用意する。

## Changed Files

- `docs/adr/0067-cloud-phase2-minimal-cloud.md` — 新規
- `docs/cloud-setup.md` — 新規（登録の手順。置く・つなぐは実装の段で書き足す）
- `docs/decision-log.md` / `CHANGELOG.md` / `CLAUDE.md`（ディレクトリ構成・残作業 #10）

## Expected Behavior

- 設計のみ。挙動の変更なし。

## Implemented Behavior

- 主な決定: 店舗 PC → クラウドの一方向（変わったレコードだけ・送る前に `raw_text` と監査項目を削る）/ クラウドは
  `core/sync.py` の解決関数で店舗ごとに merge / 店舗 ID は端末トークン（ハッシュ）から、主キーは店舗 ID + 元の ID /
  許可リストのアプリ `create_cloud_app` / 店外閲覧は QR の閲覧リンク（`#` の後ろの乱数）/ 公開範囲 `all`・`own_and_shown` /
  Supabase は Postgres だけ（`pokerapp` スキーマ・RLS・Supavisor transaction モード 6543・prepared statement 無効）/
  Cloud Run は `stable` から自動デプロイ / 公開前の確認 10 項目。

## Test Results

- コードの変更なし（テストは走らせていない）。

## Mismatches / Fixes

- ADR-0063 の範囲では「クラウド最小」に注文受付・サインアップを含むか曖昧だった → クラウドで生まれるデータが無いフェーズ 2 は
  一方向に限り、注文・サインアップはフェーズ 3 に回した（クラウドが乗っ取られても店舗 PC が変わらない形を先に作る）。
- 店外閲覧の本人確認は LINE（フェーズ 3）まで待たず、閲覧リンクで先に出す（PIN は店舗 PC にしか無く、ネットに出すと総当たりに弱い）。

## Remaining Gaps

- 2a〜2e の実装。オーナーの登録（Google Cloud・予算アラート・Supabase）は 2e までに。
- Supabase の Free は 1 週間使わないと止まる・バックアップなし → お客さんのデータの前に Pro。

## Related Commits

- 本 worklog と同じ commit
