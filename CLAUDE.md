# Poker Hand Logger — CLAUDE.md

## プロジェクト概要

ライブポーカートーナメントのハンド履歴を自動記録する Python アプリケーション。
ディーラー口元マイク（faster-whisper 音声認識）と RFID NFC（ESP32 + PN532）の 2 ソースを統合し、
JSON/PHH 形式でハンドログを出力する。

- **対象**: 小規模クラブ・個人配信向け
- **仕様書**: `sprc_v4.docx`（本ファイルより詳細な要件定義）
- **カメラ入力**: `vision/` ディレクトリは廃止予定のレガシーコードであり、メイン処理では使用しない

本ドキュメントは **現時点で実装されている仕様** と、**今後のスコープ (future scope)** を明確に分離して記述する。
未実装機能はすべて「将来スコープ」「planned」「phase candidate」と明示し、既存実装と混在させない。

---

## ディレクトリ構成（現時点）

```
pokerapp/
├── CLAUDE.md                      ← このファイル（現状仕様 + future scope）
├── CHANGELOG.md                   ← ユーザー可視変更ログ
├── docs/
│   ├── adr/                       ← Architecture Decision Records
│   ├── issues/                    ← issue / mismatch log
│   ├── worklog/                   ← タスク単位の作業ログ
│   ├── templates/                 ← adr / issue / worklog テンプレート
│   └── decision-log.md            ← ADR / 主要 issue の索引
├── sprc_v4.docx                   ← 仕様書（要件定義）
├── claude_v4.docx                 ← 旧仕様書（参考）
├── main.py                        ← エントリーポイント (--cli / GUI / --players)
├── config_default.json            ← デフォルト設定テンプレート
├── rfid_cards.json                ← tag_id → card_code マスタ
├── players.json                   ← player registry 永続ファイル (.gitignore, S1)
├── requirements.txt
│
├── core/
│   ├── config.py                  ← config.json ロード・保存
│   ├── constants.py               ← ACTION_KEYWORDS, KANJI_DIGIT/UNIT, WHISPER_PROMPT_JA
│   ├── event_queue.py             ← EventQueue (スレッド間共有キュー)
│   ├── events.py                  ← AudioEvent, CameraEvent, RFIDEvent データクラス
│   ├── game_state.py              ← GameStateManager (スタック/ポット/ターン管理)
│   ├── hand_log.py                ← ActionRecord, HandSummary データクラス
│   ├── player.py                  ← Player データクラス (S1)
│   └── player_repository.py       ← PlayerRepository (player CRUD + JSON 永続化, S1)
│
├── audio/
│   ├── recorder.py                ← AudioThread (PyAudio + faster-whisper)
│   └── recognizer.py              ← parse_action(), parse_amount(), apply_corrections()
│
├── rfid/
│   ├── http_receiver.py           ← RFIDHTTPReceiver (ESP32 HTTP POST 受信)
│   ├── reader_thread.py           ← RFIDThread (pyscard PC/SC 直接読み取り)
│   ├── bridge.py                  ← RFID ブリッジユーティリティ
│   └── card_master.py             ← CardMaster (rfid_cards.json ロード・検索)
│
├── integration/
│   └── engine.py                  ← IntegrationThread, calc_confidence()
│
├── output/
│   ├── json_writer.py             ← セッション JSON ログ書き込み
│   └── phh_exporter.py            ← PHHExporter (PHH 形式エクスポート)
│
├── gui/
│   ├── dashboard.py               ← GUIDashboard (hand logger 画面, customtkinter)
│   └── player_registry.py         ← PlayerRegistryWindow (player registry 画面, S1, dashboard とは別画面)
│
├── tests/                         ← pytest テストスイート
└── vision/                        ← レガシー（未使用）
```

---

## 技術スタック

| 用途 | ライブラリ | 備考 |
|------|------------|------|
| 音声認識 | faster-whisper ≥ 1.0 | CPU int8 モード |
| マイク入力 | PyAudio ≥ 0.2.13 | |
| RFID (HTTP) | 標準 http.server | ESP32 から HTTP POST 受信 |
| RFID (PC/SC) | pyscard ≥ 2.0.7 | transport="pcsc" 時のみ |
| PHH 出力 | pokerkit ≥ 0.5 | |
| GUI | customtkinter ≥ 5.2 | |
| テスト | pytest ≥ 7.0 | |

---

## アーキテクチャ（現時点）

### スレッド構成

| スレッド | 役割 | キュー |
|---------|------|--------|
| AudioThread | マイク入力 → ASR → `parse_action()` → AudioEvent | → audio_queue |
| RFIDHTTPReceiver / RFIDThread | RFID 受信 → card_master 解決 → RFIDEvent | → rfid_queue |
| IntegrationThread | キュー消費 → ゲーム状態更新 → ActionRecord 生成 → JSON 書き込み | ← 全キュー |
| MainThread | GUI 描画のみ | |

### Confidence 行列

| センサー組み合わせ | confidence |
|------------------|-----------|
| RFID + audio + camera | 1.00 |
| RFID + audio | 0.95 |
| RFID + camera | 0.85 |
| RFID のみ | 0.70 |
| audio + camera | 0.80 |
| audio のみ | 0.50 |
| camera のみ | 0.30 |

---

## Player Registry（S1, 実装済）

hand logger とは **完全に別画面** の player 管理機能。session / ledger / settlement が
参照する `player_id` を発行する registry の最小実装（CLAUDE.md § Future Scope の S1 を昇格）。

### スコープ（現時点）

- player の **新規作成 / 一覧表示 / display_name のリネーム** ができる。
- player 属性は `player_id`（UUID hex, 永続・安定）+ `display_name` + `created_at` のみ。
- 永続化はプロジェクト直下 `players.json`（`{"players": [...]}`、アトミックリネーム書き込み）。
  アプリ再起動を跨いで `player_id` が安定する。
- **hand logger とは未接続**。registry は `GameStateManager` / `JsonWriter` 等に依存しない。

### 構成

| 要素 | ファイル | 役割 |
|------|---------|------|
| ドメイン | `core/player.py` | `Player` データクラス（to_dict / from_dict） |
| リポジトリ | `core/player_repository.py` | `PlayerRepository`: create / list / rename + JSON 永続化 + validation |
| 画面 | `gui/player_registry.py` | `PlayerRegistryWindow`（customtkinter, dashboard とは独立） |
| 起動 | `main.py --players` | hand logger とは別に registry 画面を開く |

### Validation（`PlayerRepository` が source of truth）

- 空文字・前後空白のみの `display_name` は不可（`EmptyDisplayNameError`）。
- 完全一致（前後空白除去後）の `display_name` 重複は不可（`DuplicateDisplayNameError`）。
- rename 時も同じ validation を適用。自分自身との一致は許容（no-op rename 可）。
- 大文字小文字・全半角の厳密同一視は **今回 scope 外**（将来検討、`docs/issues/0002` 参照）。

### Out of scope（S1 時点）

- player 削除 / merge、`display_name` 以外の属性、hand logger との自動接続。
- session / seat_assignment / point ledger / settlement / cross-app sync は後続 Phase。

---

## 実装状況（現時点）

| 機能 | 状態 | 備考 |
|------|------|------|
| 音声認識 (Whisper) | ✅ 実装済 | `audio/recognizer.py` |
| RFID HTTP 受信 | ✅ 実装済 | `rfid/http_receiver.py` |
| RFID PC/SC 受信 | ✅ 実装済 | `rfid/reader_thread.py` |
| RFID カード照合 | ✅ 実装済 | `rfid/card_master.py` |
| ストリート自動遷移 (RFID) | ✅ 実装済 | board 枚数 3/4/5 で遷移 |
| Confidence 算出 | ✅ 実装済 | センサー組み合わせ行列 |
| JSON ログ出力 | ✅ 実装済 | `output/json_writer.py` |
| PHH エクスポート | ✅ 実装済 | `output/phh_exporter.py` |
| GUI ダッシュボード | 🔨 部分実装 | `gui/dashboard.py` |
| **player registry (S1)** | ✅ 実装済 | `core/player.py`, `core/player_repository.py`, `gui/player_registry.py` |
| ベッティングステート / actor 推定 | ❌ 未実装 | future phase |
| Vosk 代替バックエンド | ❌ 未実装 | future phase |
| 音声正規化 / 数値正規化 | ❌ 未実装 | future phase |
| ディーラーボタン自動回転 / SB/BB 自動 post | ❌ 未実装 | future phase |
| session ledger / point ledger / store settlement | ❌ 未実装 | **future scope（本ファイル下部参照）** |

---

# Future Scope

ここから下は **すべて未実装** であり、現時点では設計検討・仕様確定段階。  
コード・テスト・データモデルは存在しない。Phase ごとに段階導入する。

## Product scope / future architecture

現状のプロジェクトは **hand logger 単機能** だが、今後は同一プロジェクト内に以下の
**session / accounting レイヤ** を planned scope として追加する。

| レイヤ | 目的 | 状態 |
|--------|------|------|
| hand logger | ハンドごとのアクション履歴を JSON/PHH に出力 | ✅ 実装済 |
| **player registry** | アプリ内で player を新規作成・管理 | ✅ 実装済 (S1, § Player Registry 参照) |
| **session ledger** | session 単位の buy-in / rebuy / add-on / order / adjustment を ledger entry として記録 | 🔲 planned (S3) |
| **point ledger** | prize point の grant / spend を記録、buy-in 等に充当可能 | 🔲 planned (S3) |
| **session settlement** | session 終了時に player ごとの「店への net 支払額」と paid/unpaid を確定 | 🔲 planned (S4) |
| **cross-app boundary** | hand logger と ledger app の相互参照契約 (player_id / session_id / hand_id) | 🔲 planned (S5) |

hand logger と ledger app は **将来別画面・別アプリ** になることを前提に設計する。
両者は共通 ID で相互参照する（§ Cross-app boundary 参照）。

## Domain model（future scope）

未実装。以下の概念モデルを今後 Phase 単位で実装する。

### `player`

- アプリ内で新規作成する
- 属性: `player_id` (内部 ID), `display_name`
- 現時点ではそれ以外の属性は持たない（連絡先・実名等は scope 外）
- **S1 で実装済**（§ Player Registry 参照）。本節は session / ledger / settlement から
  見た契約上の定義として残す。

### `session`

- 1 卓 1 回の運営単位
- 属性: `session_id`, `started_at`, `ended_at`, `blinds`（参考値）, `status`
- session 終了時に `session_settlement` を確定する

### `seat_assignment`

- **hand-based**: hand ごとに `seat → player_id` のスナップショットを取る
- session 中の seat change はこの hand-based スナップショットの差分として現れる
- 「現在この席に誰が座っているか」は最新 hand の seat_assignment から導出する

### `hand_ref`

- hand logger 側の hand_id を ledger 側から参照するための軽量参照
- 最低限 `hand_id`, `session_id`, `started_at`, `seat_assignments` を含む
- hand logger と ledger app が別アプリ化されたあとも安定する不変参照

### `ledger_entry`

- session 中の金銭イベント 1 件
- 種別: `buy_in` / `rebuy` / `add_on` / `order` / `adjustment`
- 共通属性: `entry_id`, `session_id`, `player_id`, `kind`, `occurred_at`, `cash_amount`, `point_amount`, `note`
- `order` 種別のみ `item_name`, `unit_amount`, `quantity` 等の明細サブ構造を持つ

### `point_ledger_entry`

- point の増減 1 件
- 増加理由: `manual_grant` / `result_credit` / `campaign_grant`
- 減少理由: `spend_on_buyin` / `spend_on_rebuy` / `spend_on_addon` / `spend_on_order` / `adjustment`
- 共通属性: `entry_id`, `player_id`, `delta_points`, `reason`, `occurred_at`, `related_ledger_entry_id?`

### `session_settlement`

- session 終了時に player ごと 1 行確定する
- 属性: `session_id`, `player_id`, `cash_in_total`, `point_spent_total`, `order_total`, `entry_fee`, `point_credited_total`, `net_due_to_store`, `payment_status` (`paid` | `unpaid`), `settled_at`
- 「player 間の精算」は **扱わない**。settlement は **常に「player → 店」の 1 方向**

## Business rules（future scope）

未実装。今後の実装で守るべき業務ルール。

1. **entry fee は cash only**。point では支払えない。
2. **buy-in / rebuy / add-on / order** は cash + point の **併用可**。
   - 1 件の ledger_entry は `cash_amount + point_amount` の両方を持ち得る。
3. **point 不足分は cash で補完**。point 残高 < 必要点数の場合、不足分は cash として ledger に記録する。
4. **paid/unpaid** は「店への支払いが完了したか」だけを表す状態。partial paid は現時点では扱わない（将来検討）。
5. **player-to-player settlement は扱わない**。session settlement は常に「player → 店」のみ。
6. **point 増加経路** は `manual_grant` / `result_credit` / `campaign_grant` の 3 種類を想定する。
7. **session 中間集計** では player ごとに「buy-in 合計」「注文合計」を表示できる必要がある（確定値ではない／途中スナップショット）。
8. **session 確定** は session 終了時の `session_settlement` の生成をもって行う。

## Cross-app boundary（future scope）

hand logger と ledger app は **将来別アプリ化** することを前提に、以下の契約を planned とする。

- 共通 ID: `player_id`, `session_id`, `hand_id` の 3 つを両アプリ間で安定キーとして共有する。
- 参照は **双方向**:
  - ledger app は `hand_ref` を介して hand logger の hand 一覧を読み込む。
  - hand logger は session 開始時に ledger app から `seat → player_id` の seat_assignment を取得する。
- いずれの ID 採番もアプリ内で完結すること（外部システム前提を作らない）。
- 物理的な配置（同一プロセス / 別プロセス / 別アプリ）は phase ごとに段階移行する。S2〜S4 では同一プロセス、S5 で boundary を切り出す。

## Phase candidates（future scope）

| Phase | スコープ | 主な成果物 |
|-------|---------|-----------|
| **S0** | spec expansion | CLAUDE.md / ADR-0003 / issues / worklog |
| **S1** | player registry ✅ 実装済 | `player` データモデル、CRUD、display_name のみ、別画面 |
| **S2** | session + hand-based seating | `session`, `seat_assignment`, `hand_ref`、hand 開始ごとのスナップショット |
| **S3** | ledger entries + point ledger | `ledger_entry`, `point_ledger_entry`、cash+point 併用ルール |
| **S4** | session settlement + paid/unpaid | `session_settlement`、net due to store、paid/unpaid 操作 |
| **S5** | cross-app contract / sync boundary | hand logger ↔ ledger app の参照契約、ID 安定性、別プロセス化準備 |

各 Phase の着手前に対応する ADR / issue を起こすこと（traceability rules を参照）。

---

# Parallel development plan

本プロジェクトは今後 **hand logger core** / **desktop 別画面 (registry / ledger UI)** /
**iOS・Android 別アプリ** を **並行的** に育てる。並行作業を成立させるため、依存関係を整理し、
parallelizable なタスクと逐次でしかできないタスクを分離する。

本計画は **contract-first**（先に契約を凍結し、各 front-end / core はそれに対して独立実装する）
を原則とする。`player_id` / `session_id` / `hand_id` は全 workstream 共有の安定キーであり、
これらと各モデルの schema を最初に凍結する（§ Cross-app boundary も参照）。

> 注: 現時点で実装済なのは hand logger core と S1 player registry のみ。本節の S2 以降・
> mobile・sync はすべて **planned / future scope**。production code の大規模実装はまだ開始しない。

## Workstreams

| WS | 名称 | 責務 | 主要成果物 | 依存 |
|----|------|------|-----------|------|
| **WS0** | contract / spec / schema | 共有 ID・各 domain model の schema・validation 契約・error 形を凍結 | `docs/contracts/*`（planned）, ADR, schema fixtures | なし（全 WS の上流） |
| **WS1** | core domain / repository / services | `core/` のドメイン・repository・service。永続化と業務ルールの source of truth | `core/*.py`, repository, service, tests | WS0（該当 model の契約凍結後） |
| **WS2** | desktop separate screen | registry / ledger / settlement の **別画面** UI（既存 hand logger UI は汚さない） | `gui/*.py`（別 window）, GUI ロジックテスト | WS1（同 phase の repository/service） |
| **WS3** | mobile scaffold (iOS/Android) | 将来の別 front-end。**最初は mock repository** で UI を先行させる | mobile プロジェクト雛形, screen skeleton, mock repo | WS0 のみ（contract）。WS1 完成を待たない |

責務分離の原則:

- **core (WS1) が業務ルールの唯一の source of truth**。desktop / mobile はそれを呼ぶだけで、
  validation・残高計算・settlement 確定ロジックを各 front-end に複製しない。
- **desktop (WS2) と mobile (WS3) は対等な front-end**。mobile は hand logger の置き換えではなく
  「registry / ledger 等を扱う将来の別 front-end」。どちらも同じ contract に対して実装する。
- **hand logger 既存 UI (`gui/dashboard.py`) は触らない**。registry / ledger は常に別画面。

## 何が parallelizable で、何が blocker か

- **Blocker（逐次・上流）**: WS0 の契約凍結。ある model（player / session / ledger ...）の
  schema・ID・validation・error 形が凍るまで、その model を扱う WS1/WS2/WS3 は本実装に入れない。
- **Parallelizable（契約凍結後）**: 同一 model について WS1（core）/ WS2（desktop）/ WS3（mobile mock）
  は **同時並行** で進められる。front-end は mock / 実 repository を contract 越しに差し替えるだけ。
- **Cross-phase parallel**: phase N の WS0 契約が凍れば、phase N の実装中に phase N+1 の WS0
  契約凍結作業を先行できる（契約 WS が常に 1 phase 先行する形）。
- **Sequencing 制約**: session(S2) は player(S1) を、ledger(S3) は session を、settlement(S4) は
  ledger/point を参照するため、**契約レベルの依存順序**（S1→S2→S3→S4）は維持する。ただし
  これは「契約凍結の順序」であって「実装の並行性」を妨げない。

## 先に凍結すべき contract（freeze order）

1. **共有 ID 契約**（最優先・全 phase 共通）: `player_id` / `session_id` / `hand_id` は
   アプリ内採番・文字列・不変。採番責任の所在を WS0 で確定する。
2. **player schema**（S1, 凍結済に近い）: `player_id` + `display_name` + `created_at`、
   validation（空文字 / 前後空白 / 完全一致重複）。
3. **session / seat_assignment / hand_ref schema**（S2）: seat_assignment は hand-based。
4. **ledger_entry / point_ledger_entry schema**（S3）: cash+point 併用、order 明細。
5. **session_settlement schema**（S4）: net due to store / paid-unpaid。
6. **repository / service interface 契約**: 各 front-end が呼ぶ抽象 API（mock 差し替え可能な形）。
   これを凍結することで mobile が mock で先行できる。

## mobile が mock で先行できる範囲

- repository interface（WS0 契約）に対する **in-memory / fixture mock** で、player list / add /
  rename などの画面遷移・状態管理・validation 表示を **WS1 完成前に** 作り込める。
- mock は WS0 の schema fixtures（サンプル JSON）を読むだけにし、実 persistence を持たない。
- 後で実 repository（ローカル or API）に差し替えても UI 層が壊れない境界を最初から引く。

## Mobile scaffold 提案（WS3, planned）

- **技術選定案**: **React Native**（iOS / Android 両対応のたたき台）。Expo 起点で
  プラットフォーム分岐を最小化する。最終決定は Phase 1 着手時に別 worklog で確定する。
  - 代替案: Flutter（Dart 統一・高描画性能）/ ネイティブ 2 本（最大の自由度・最大コスト）。
    React Native を初期案とするのは、将来 desktop と TypeScript 系の契約型を共有しやすいため。
- **最初の screen skeleton 範囲**: player registry のみに限定する。
  - `PlayerListScreen`（一覧）/ `AddPlayerForm`（新規作成）/ `RenamePlayerForm`（リネーム）。
  - validation 表示（空文字 / 重複）は **core と同じ contract** に従い、UI 側で再実装しない。
- **repository は mock 先行**: `PlayerRepository` interface（WS0 契約）に対する in-memory mock を
  使い、WS1 完成を待たずに画面遷移・状態管理・validation 表示を作る。実 repository（local or
  API）への差し替えで UI が壊れない境界を最初から引く。
- **scope 外（たたき台時点）**: 永続化の本実装、hand logger 機能、session / ledger / settlement、
  実 API / sync。これらは後続 phase。

## 将来 API / sync を入れても壊れにくい境界

- front-end は **repository interface にのみ依存**し、`core/*` の具象 / API client / mock を
  注入で差し替える。UI は「どこにデータがあるか」を知らない。
- 採番は常にアプリ内で完結（外部システム前提を作らない）。sync 導入時も ID は不変キーとして残る。
- 物理配置の移行（同一プロセス → 別プロセス → 別アプリ + API/sync）は S5 で boundary を切り出す。
  S2〜S4 は同一プロセス前提で進めてよい。

## Phase 別計画

各 phase は WS0（契約）→ WS1/WS2/WS3（並行実装）の順。S1 は実装済、S2 以降は planned。

### Phase 0 — contract freeze

- **Goal**: 共有 ID 契約と「契約の置き場所・凍結プロセス」を確定し、以降の全 phase が
  contract-first で動ける土台を作る。
- **Prerequisites**: なし（最上流）。
- **Parallel tasks**:
  - WS0: 共有 ID 契約（player_id / session_id / hand_id）と error 形・schema 表現方法
    （`docs/contracts/` + サンプル fixtures）を定義。
  - WS0: repository / service interface の契約テンプレートを定義。
- **Blockers**: なし。これ自体が他 phase の blocker。
- **Done criteria**: 共有 ID 契約が ADR 化され、schema/fixtures の置き場所と更新手順が決定。
  各 front-end が「契約だけ見て」mock を書ける状態。

### Phase 1 — player registry core + desktop + mobile mock

- **Goal**: player を core / desktop / mobile(mock) の 3 面で扱えるようにする。
- **Prerequisites**: Phase 0 の共有 ID 契約 + player schema 凍結。
- **Parallel tasks**:
  - WS1: `core/player.py` + `core/player_repository.py`（**実装済 S1**）。
  - WS2: `gui/player_registry.py` 別画面（**実装済 S1**）。
  - WS3: mobile player registry screen skeleton + mock repository（**planned**、WS1 を待たない）。
- **Blockers**: player schema / repository interface 契約（Phase 0 / S1 で凍結済）。
- **Done criteria**: core/desktop は S1 完了済。mobile は mock repo で list/add/rename 画面が
  動く skeleton ができ、後で実 repo に差し替え可能な境界を持つ。

### Phase 2 — session and seating

- **Goal**: `session` と hand-based `seat_assignment` / `hand_ref` を扱う。
- **Prerequisites**: player 契約（S1）+ Phase 2 の session/seat schema 凍結。
- **Parallel tasks**:
  - WS0: session / seat_assignment / hand_ref schema 凍結（seat_assignment は hand-based）。
  - WS1: session 管理・seat snapshot の repository/service。
  - WS2: desktop の session/seating 別画面。
  - WS3: mobile の session 画面（mock）。
- **Blockers**: seat_assignment を hand-based にする設計確定（ADR）。hand logger 側 hand_id を
  `hand_ref` から参照する契約。
- **Done criteria**: session 開始/終了と hand 単位 seat snapshot が core で確定し、両 front-end が
  契約越しに表示できる。

### Phase 3 — ledger and points

- **Goal**: `ledger_entry`（buy_in/rebuy/add_on/order/adjustment）と `point_ledger_entry` を扱う。
- **Prerequisites**: session 契約（S2）+ ledger/point schema 凍結 + ISSUE-0001（point 残高の
  source of truth）の決着。
- **Parallel tasks**:
  - WS0: ledger_entry / point_ledger_entry schema 凍結（cash+point 併用、order 明細）。
  - WS1: ledger / point ledger repository/service + 残高計算。
  - WS2: desktop の ledger 入力・中間集計（buy-in 合計 / 注文合計）別画面。
  - WS3: mobile の ledger 画面（mock）。
- **Blockers**: **ISSUE-0001**（残高 source of truth）。これが決まらないと残高計算の API 契約が
  凍結できず、front-end の中間集計表示が宙に浮く。
- **Done criteria**: cash+point 併用・point 不足の cash 補完・entry fee cash only が core で
  enforced、両 front-end が中間集計を表示できる。

### Phase 4 — settlement

- **Goal**: session 終了時に player ごとの `session_settlement`（net due to store / paid-unpaid）を確定。
- **Prerequisites**: ledger/point 契約（S3）+ settlement schema 凍結。
- **Parallel tasks**:
  - WS0: session_settlement schema 凍結（player→店の 1 方向のみ）。
  - WS1: settlement 確定 service + paid/unpaid 操作。
  - WS2: desktop の settlement 画面。
  - WS3: mobile の settlement 表示（mock）。
- **Blockers**: paid/unpaid の状態遷移と partial paid の要否確定。player-to-player を扱わない前提の固定。
- **Done criteria**: session 終了で settlement 1 行/ player が確定し、paid/unpaid を操作できる。

### Phase 5 — sync / cross-app contract hardening

- **Goal**: 同一プロセス前提から、別プロセス / 別アプリ + API/sync へ移行できる boundary を切り出す。
- **Prerequisites**: S1〜S4 の schema が安定し、repository interface が front-end から実証済。
- **Parallel tasks**:
  - WS0: cross-app 参照同期方式（pull / push / event）と API contract を確定。
  - WS1: repository を local 実装と API client 実装に分離（interface は不変）。
  - WS2 / WS3: front-end を API-backed repository に差し替え（UI 層は無改修が目標）。
- **Blockers**: 参照同期方式の ADR。ID 不変性の保証。衝突解決方針。
- **Done criteria**: front-end が repository interface のみに依存したまま、local↔API backend を
  切り替えられる。ID が backend を跨いで安定。

---

## コーディング規約

- 型ヒント必須（`from __future__ import annotations` 使用）
- データクラスは `@dataclass` を使用
- スレッド間通信は `queue.Queue` のみ（共有変数の直接参照禁止）
- GUI スレッドからビジネスロジックを呼ばない
- 定数は `core/constants.py` に集約
- ログは `logging` モジュール使用（`print` 禁止 — ただし `main.py` の CLI 出力は除く）
- コメントは「なぜ」が自明でない場合のみ記述
- 正規表現はすべて raw string (`r"..."`) で記述

---

## エラーハンドリング方針

- 認識エラーでクラッシュしない → `try/except` で捕捉し `needs_review=True` を付与
- ハンド完了ごとにディスクへ書き込む（バッファリングしない）
- ログファイルは追記モード（既存セッションデータを上書きしない）
- ESP32 停止・WiFi 切断時 → `rfid.enabled=false` で RFID なしモード継続動作

---

## よく使うコマンド

```bash
pip install -r requirements.txt
python main.py --cli                         # CLI モード (hand logger)
python main.py                               # GUI モード (hand logger)
python main.py --players                     # Player Registry 画面 (S1, 別画面)
pytest tests/ -v --ignore=tests/test_vision.py
python main.py --export-phh logs/session_xxx.json
```

---

## Documentation and Traceability Rules

実装作業では **コードだけでなく docs-as-code の更新も必須** とする。以下のルールは
今後の全タスクに適用される恒常ルール。本 CLAUDE.md は現時点の正仕様を保ち、
履歴・経緯は ADR / worklog / issue へ分離する。

### 1. 基本原則

- ドキュメント更新は実装タスクの一部であり、optional follow-up ではない。
- コード・テスト・ドキュメントが揃って初めてタスク完了とする。
- `CLAUDE.md` は **現時点の正仕様** のみを記述する。歴史的経緯・廃止された理由付け・
  Phase ごとの進捗は worklog / ADR / issue に分離する。
- 未実装の機能を「実装済」として書かない。`planned` / `future scope` / `phase candidate` を明示する。

### 2. `CLAUDE.md` 更新ルール

- 振る舞い・スコープ・UI・API・所有境界・実装状況が変わったら同タスク内で `CLAUDE.md` を更新する。
- 履歴は `CLAUDE.md` から削ぎ落とし、worklog / ADR / issue に置く。
- `Future Scope` セクション以下は未実装専用。実装が進んだら該当項目を上のセクションへ昇格する。

### 3. worklog ルール (`docs/worklog/`)

- 非自明な変更には 1 タスク 1 ファイルで worklog を残す。
- 命名: `YYYY-MM-DD-<short-slug>.md`。テンプレートは `docs/templates/worklog-template.md`。
- 最低限: goal / changed files / expected vs implemented / test results / mismatches / fixes / remaining gaps / related commits。

### 4. ADR ルール (`docs/adr/`)

- アーキテクチャ的に重要な判断は ADR として残す。命名: `NNNN-<short-title>.md`（ID 昇順）。
- 旧 ADR は書き換えず、方針が変わった場合は新 ADR を起こし旧 ADR を `Superseded` に更新する。
- 新規 ADR は `docs/decision-log.md` の ADR Index に登録する。

### 5. issue / mismatch log ルール (`docs/issues/`)

- 期待挙動と実挙動の乖離、未確定の open question を `NNNN-<slug>.md` で残す。
- 最低限: expected / actual / reproduction / root cause / fix / regression test。
- ステータスが Open のままでも構わない（risk register として機能する）。

### 6. decision log ルール (`docs/decision-log.md`)

- ADR と主要 issue の **索引** に徹する。詳細は各ファイル本体に置く。
- 新規 ADR / 主要 issue を追加したら必ず 1 行追記する。

### 7. changelog ルール (`CHANGELOG.md`)

- ユーザー可視の挙動変更、および仕様 / docs の重要更新は `Unreleased` セクションに追記する。

### 8. 完了条件

- code / tests / docs（CLAUDE.md / ADR / worklog / issue / decision-log / CHANGELOG の該当箇所）が
  全部揃って初めてタスク完了とする。
- 完了報告には changed files / 期待挙動 / 実装挙動 / mismatches / fixes / tests / docs / 残課題 を含める。
