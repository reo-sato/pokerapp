# Worklog: 製品化の方針（他店への販売・アプリ化・サーバー側の整備）

## Date

2026-10-11

## Scope / Task

推論エンジン・音声入力の精緻化と並行して進める「製品化（アプリ化・サーバー側の整備）」の全体設計と、
オーナーの決定の記録（ADR-0063）。コードの変更なし。

## Goal

- 現状（verify-v1）を読み、製品化で足りないものを洗い出す。
- 構成・技術選定・データ設計・アプリの形・お金の流れ・セキュリティ・開発の順番を 1 つの設計書にまとめる。
- 未決事項をオーナーに決めてもらい、ADR に残す。

## Changed Files

- `docs/adr/0063-productization-multi-store-edge-and-cloud.md` — 新規。製品化の方針（D1〜D8）
- `docs/adr/0029-l2-hosted-operational-design.md` — Superseded by に ADR-0063（D2 のみ）を追記
- `docs/decision-log.md` — ADR-0063 を索引に追加
- `CHANGELOG.md` — Unreleased に方針の追加を記載
- `CLAUDE.md` — 残作業 #10 に製品化（planned）を追加

## Expected Behavior

- 振る舞いの変更なし（文書のみ）。

## Implemented Behavior

- 振る舞いの変更なし。

## 設計書と決定

- 全体設計書は claude.ai の文書「pokerapp 製品化 全体設計書(アプリ・サーバー)」（2026-10-11）。
  構成図・技術選定表・クラウド DB のテーブル案・アプリ 3 種・お金の流れ・公開前のセキュリティ確認・ロードマップを含む。
- オーナーの回答（同日）: 外販する / 推奨のホスティングで進める（説明は後で）/ 店外閲覧は中核なので作る /
  会員は店舗またぎで同じ人 / LINE + Google / 手札の公開範囲は設定で / プレイヤー課金なし・店舗に導入費・
  利用料・卓数比例。
- 設計書の現状の記述はコードで確かめた: staff token は `==` の比較（`api/server.py`）/ CORS は `*` /
  `player_token_secret` が空なら起動ごとに `secrets.token_hex(32)` / `SyncScheduler` は tests 以外から使われていない /
  staff 画面の静的配信は無い / ISSUE-0018 で決済手段連携は scope 外。

## Test Results

- 文書のみのためテストの対象なし。

## Mismatches / Fixes

- ADR-0029 D2 はマネージド PaaS（Render / Railway / Fly.io）だったが、東京に置けることと待機中の課金が無いことから
  Cloud Run に置き換えた（ADR-0063 D2）。

## Remaining Gaps

- フェーズ 1 の実装（staff 画面の店舗 PC 配信 / 自動起動 / リリース版での更新 / 同期の常駐結線）。
- フェーズ 2 着手時の ADR（クラウドモード・店舗 ID と端末トークン・Postgres の事業者・公開範囲の設定）。
- ホスティングの説明（オーナーの依頼: 後で詳しく）。

## Related Commits

- 本 worklog と同じ commit
