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
├── README.md                      ← エンドユーザー向け概要・クイックスタート (Phase I)
├── CLAUDE.md                      ← このファイル（現状仕様 + future scope）
├── CHANGELOG.md                   ← ユーザー可視変更ログ
├── docs/
│   ├── adr/                       ← Architecture Decision Records
│   ├── issues/                    ← issue / mismatch log
│   ├── worklog/                   ← タスク単位の作業ログ
│   ├── templates/                 ← adr / issue / worklog テンプレート
│   ├── contracts/                 ← contract-first 基盤 (shared IDs / schemas / fixtures; player + hand/action freeze済, session/seat/hand_ref は S2 core 実装済・schema draft)
│   ├── installation.md            ← エンドユーザー: インストール手順 (Phase I)
│   ├── usage.md                   ← エンドユーザー: 使い方・読み上げ語彙・設定 (Phase I)
│   ├── troubleshooting.md         ← エンドユーザー: 困りごと対処 (Phase I)
│   └── decision-log.md            ← ADR / 主要 issue の索引
├── sprc_v4.docx                   ← 仕様書（要件定義）
├── claude_v4.docx                 ← 旧仕様書（参考）
├── main.py                        ← エントリーポイント (--cli / GUI / --players / --ledger / --viewer-api)
├── config_default.json            ← デフォルト設定テンプレート
├── rfid_cards.json                ← tag_id → card_code マスタ
├── players.json                   ← player registry 永続ファイル (.gitignore, S1)
├── sessions.json                  ← session + hand-based seating 永続ファイル (.gitignore, S2)
├── ledger.json                    ← ledger entry 永続ファイル (.gitignore, S3a/M4)
├── order_requests.json            ← 注文リクエスト永続ファイル (.gitignore, M5)
├── menu.json                      ← 注文メニューマスタ (コミット済みサンプル, 店側で編集, M5)
├── pyproject.toml                 ← パッケージ定義 (core / [pcsc] / [vision] / [api] / [dev], entry: pokerapp, H1)
├── requirements.txt               ← core runtime 同期コピー (vision 除外)
├── requirements-dev.txt           ← テスト依存 (numpy/pokerkit/jsonschema/fastapi/httpx/pytest, CI が使用)
├── .github/workflows/ci.yml       ← CI: pytest (skip 0, vision 除外, H4)
│
├── core/
│   ├── config.py                  ← config.json ロード・保存
│   ├── constants.py               ← ACTION_KEYWORDS, KANJI_DIGIT/UNIT, WHISPER_PROMPT_JA
│   ├── event_queue.py             ← EventQueue (スレッド間共有キュー)
│   ├── events.py                  ← AudioEvent, CameraEvent, RFIDEvent データクラス
│   ├── game_state.py              ← GameStateManager (スタック/ポット/ターン管理)
│   ├── hand_log.py                ← ActionRecord, HandSummary データクラス
│   ├── player.py                  ← Player データクラス (S1)
│   ├── player_repository.py       ← PlayerRepository (player CRUD + JSON 永続化, S1)
│   ├── session.py                 ← Session / SeatAssignment / HandRef データクラス (S2)
│   ├── session_repository.py      ← SessionRepository (session + hand-based seating + JSON 永続化, S2)
│   ├── ledger.py                  ← LedgerEntry / OrderDetail データクラス (S3a, M4)
│   ├── ledger_repository.py       ← LedgerRepository (cash-only ledger + 中間集計 + JSON 永続化, S3a)
│   ├── order_request.py           ← OrderRequest データクラス (M5)
│   ├── order_request_repository.py ← OrderRequestRepository (注文リクエスト, thread-safe + reload-on-read, M5)
│   └── menu.py                    ← MenuMaster (menu.json ロード・検索, M5)
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
├── api/
│   ├── read_models.py             ← viewer read model (seat_assignment 起点 join, fastapi 非依存, M1)
│   └── server.py                  ← viewer API server (FastAPI app factory + uvicorn, M1, ADR-0013)
│
├── gui/
│   ├── dashboard.py               ← GUIDashboard (hand logger 画面, customtkinter)
│   ├── player_registry.py         ← PlayerRegistryWindow (player registry 画面, S1, dashboard とは別画面)
│   └── ledger_entry.py            ← LedgerEntryWindow (スタッフ用 会計入力画面, S3a/M4, 別画面)
│
├── mobile/                        ← Poker Hand Viewer (Expo/RN, M2, ADR-0013。mock/HTTP repository 切替, web export 配布)
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
| viewer API | fastapi ≥ 0.110 + uvicorn ≥ 0.29 | optional extra `[api]`（M1, ADR-0013） |
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

## Session & Seating（S2, core 実装済）

player registry の上に重なる **session レイヤ + hand-based seating** の core 最小実装
（CLAUDE.md § Future Scope の S2 を昇格）。契約は `docs/contracts/session-seating.md`（draft）/
ADR-0006、実装上の判断は ADR-0007。**hand logger とは config フラグ `session_layer.enabled`（既定 off）で
write-through 接続可**（E1+E2-core, ADR-0008 Pattern A: `IntegrationThread` に `session_repo`/`seat_player_map`
を DI し、hand 開始で `assign_seat`・確定時に `HandSummary.players[i].player_id` を additive 埋め込み）。
**off では従来どおり独立**（別ストア・別 namespace、player_id キーも付けない）。**seat 選択 GUI /
main.py 結線は M3 (= E3) で実装済**（ISSUE-0006 Fixed）: on にするとセッション設定プロンプトで
registry から seat→player を選択（`n` でその場登録、空 Enter で割当なし）、session は UUID4 hex で
作成され（label/blinds 付き、終了時 close を y/N 確認）、dashboard「席設定」で mid-session の
seat change を差分入力できる（`seat_assign` queue イベント → 次ハンド開始時に帰属と表示名へ反映）。

### スコープ（現時点）

- `session` の **create / list / get / close** ができる。
- hand 単位で **seat→player 割り当て**（`assign_seat`）を記録し、`(session_id, hand_id, seat_no)`
  の競合・同一 hand での player 重複を防ぐ。
- あるハンドの **seat map / `hand_ref` スナップショット** を解決でき、最新 hand から
  **現在の seating を導出** できる。
- 永続化はプロジェクト直下 `sessions.json`（アトミックリネーム書き込み、`.gitignore`）。

### 構成

| 要素 | ファイル | 役割 |
|------|---------|------|
| ドメイン | `core/session.py` | `Session` / `SeatAssignment` / `HandRef` データクラス（to_dict / from_dict） |
| リポジトリ | `core/session_repository.py` | `SessionRepository`: session CRUD + hand-based seating + validation + JSON 永続化 |

### Validation / errors（`SessionRepository` が source of truth）

- unknown session → `SessionNotFoundError`（`not_found`）。
- closed session への close → `SessionAlreadyClosedError`（`already_closed`）、seat 割り当て →
  `SessionClosedError`（`session_closed`）。
- 同一 hand で seat 重複 → `SeatTakenError`（`seat_taken`）、player 重複 →
  `PlayerAlreadySeatedError`（`player_already_seated`）。
- unknown player（registry 非実在）→ `UnknownPlayerError`（`unknown_player`）。
- `seat_no` 範囲外（1..9 外）/ 不正 `hand_id` → `InvalidSeatError`（`invalid_seat`）。
- error code は `docs/contracts/error-shapes.md` の session セクションと 1:1 対応。

### 識別子・永続形（ADR-0007）

- `session_id` は session レイヤが UUID4 hex で採番（hand logger の timestamp session_id とは別系統）。
- seat_assignment は `sessions.json` 配下に hand 単位で入れ子保持（hand logger JSON は不変）。
- mid-session seat change は専用イベントを持たず、最新 hand との差分として導出（ADR-0006）。

### Out of scope（S2 core 時点）

- hand logger（`HandSummary`）との自動接続 / reconciliation、player_id 突き合わせ。
- ledger / point / settlement（S3〜S4）、desktop / mobile UI、API / sync（S5）。
- session / seat の削除・merge、advanced seat history UI、schema `1.0` freeze（ISSUE-0005 残項目）。

---

## Viewer API（M1, 実装済）

プレイヤーが自分のスマホ（将来は Expo client, M2）から自分の session / ハンド履歴を参照するための
**読み取り専用 HTTP API**（ADR-0013。S5 cross-app boundary の read-only サブセットの前倒し）。
**player 向け desktop viewer は作らない**（desktop = スタッフ操作用に限定）。

### スコープ（現時点）

- 起動は 2 形態（要 `pip install ".[api]"` = fastapi/uvicorn）:
  - `python main.py --viewer-api` = **read-only**（注文 POST は 503 `orders_unavailable`）。
  - `python main.py --ledger`（`viewer_api.enabled=true`）= 会計画面に **in-process 組み込み**で
    注文 write が有効（単一プロセス所有, M5/ADR-0015）。
- endpoints: `/api/health`, `/api/players`, `/api/players/{id}`, `/api/players/{id}/sessions`,
  `/api/players/{id}/sessions/{sid}/hands`, `/api/sessions/{sid}/hands/{hid}`,
  `/api/menu`（M5）, `/api/players/{id}/sessions/{sid}/order-requests`（GET/POST, M5）。
  契約は `docs/contracts/viewer-api.md`（draft 0.x）+ `player_session_summary` schema/fixtures。
- **read model 規則**: 「player のハンド」は `sessions.json` の seat_assignment 起点で
  hand log（`logs/{session_id}.json`）を `(session_id, hand_id)` join。hand log 側 `player_id` は
  best-effort。`session_layer.enabled` が off の間は gracefully-empty（M3 = E3 実装済のため、
  on にすれば実データが流れる）。
- error は `{"code": "not_found", "message": ...}`（`error-shapes.md` 準拠、404）。
- config: `viewer_api.bind_host` 既定 `127.0.0.1`（無認証。スマホからの LAN 参照は明示変更、
  ISSUE-0013 プライバシーモデル参照）/ `bind_port` 既定 8788。

### 構成

| 要素 | ファイル | 役割 |
|------|---------|------|
| read model | `api/read_models.py` | fastapi 非依存の純関数（join / summary 集計） |
| server | `api/server.py` | `create_app(player_repo, session_repo, log_dir)` DI + error handler + CORS(GET) |
| 起動 | `main.py --viewer-api` | config を読み foreground で uvicorn 起動 |

### Out of scope（viewer API 時点）

- ledger への直接 write（注文も order_request 経由でスタッフ確定が必須 — § Ledger 参照）。
- 認証 / per-player アクセス制御（**v1 = name-pick で確定**, ISSUE-0013 Fixed / ADR-0015。
  PIN は問題が顕在化した場合に additive に再評価）。
- Expo mobile client は **M2 で実装済**（`mobile/`、実装状況表参照）。

---

## Ledger（S3a, cash-only 実装済）

session 中の金銭イベント（buy-in / rebuy / add-on / 注文 / 調整）を記録する **cash-only ledger**
（ADR-0014。S3 を S3a（本実装）と S3b（point 連携, M6）に分割。S3a は **ISSUE-0001 に
ブロックされない**）。

### スコープ（現時点）

- `LedgerEntry`: `entry_id`（UUID4 hex）/ `session_id` / `player_id` / `kind`（5 種別）/
  `occurred_at` / `cash_amount` / `point_amount`（**S3a では常に 0**, 違反は
  `points_not_supported`）/ `note?` / `order?`（kind=order: item_name / unit_amount / quantity）。
  契約: `docs/contracts/ledger.md` + `ledger_entry.schema.json`（draft 0.x, freeze は M6 後）。
- validation は `core/ledger_repository.py` が source of truth（open session 必須 / kind 別金額
  規則 / append-only。詳細は `validation-rules.md`）。永続化は `ledger.json`（`.gitignore`）。
- **中間集計**: `session_player_summary` = `buy_in_total` / `order_total` / `adjustment_total` /
  `total_due`（確定値ではない。確定は S4 settlement）。
- **入力はスタッフ desktop のみ**: `python main.py --ledger`（`gui/ledger_entry.py`、
  hand logger とは別画面。open session 選択 → player/種別/金額（注文は品名・単価・数量から
  自動計算）→ 中間集計と履歴を表示）。
- **参照**: viewer API `GET /api/players/{id}/sessions/{sid}/ledger`（entries + summary）+
  mobile「会計」画面（MyHands から遷移, read-only）。

### 注文リクエスト（M5, 実装済 — ADR-0015）

- player はスマホから **order_request**（pending）を POST する。**ledger には書かれず**、
  スタッフが `--ledger` 画面の「注文リクエスト」欄で**確定**したときに `ledger_entry`
  （kind=order）が作られ `ledger_entry_id` がリンクされる（却下も可。staff-in-the-loop）。
- **menu master**: `menu.json`（コミット済みサンプル、店側で編集）。player はメニューから選択
  （menu 外は `unknown_item`）、確定時の単価は menu から prefill（スタッフ上書き可）。
- **単一プロセス所有**: `order_requests.json` の write は viewer API を in-process で抱えた
  `--ledger` プロセスのみ（`viewer_api.enabled=true` で組み込み起動）。単独 `--viewer-api` は
  read-only（POST 503）。`OrderRequestRepository` は thread-safe（lock）+ reload-on-read。
- 本人確認は **name-pick（v1 確定, ISSUE-0013 Fixed）**: なりすまし注文はスタッフ確定・
  提供時の対面で発覚できる。
- mobile: 会計画面 →「ドリンクを注文する」→ メニュー選択・数量・送信 + 注文状況一覧。

### Out of scope（S3a/M5 時点）

- point 払い・point ledger・残高（S3b/M6, ISSUE-0001 gate）、settlement / paid-unpaid（S4）。
- entry の編集・削除（訂正は adjustment）。注文リクエストの player 側キャンセル（スタッフ却下で代替）。

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
| **session + hand-based seating (S2) core** | ✅ 実装済 | `core/session.py`, `core/session_repository.py`（§ Session & Seating 参照） |
| **hand logger × session 統合 (S2.x E1+E2-core + M3=E3)** | ✅ 実装済 | `integration/engine.py`（`session_repo`/`seat_player_map` DI、`assign_seat` write-through + `player_id` additive 埋め込み, ADR-0008）+ **M3: main.py 結線**（seat→player 選択プロンプト・UUID session_id・終了時 close 確認）+ **dashboard「席設定」**（`seat_assign` queue イベントで mid-session seat change、次ハンド反映, ISSUE-0006 Fixed）。`session_layer.enabled` 既定 off は維持（実機 E2E = Phase H 後に再検討） |
| **event 記録 sidecar (R1)** | ✅ 実装済 | `output/event_recorder.py`（opt-in `recording.enabled`, 挙動不変, ADR-0010, `reconstruction_event` schema） |
| **pokerkit game-state backend (R2) + live 既定切替 (G)** | ✅ 実装済 | `core/poker_engine.py`（`engine.backend`, ADR-0009/0012。actor/合法手/side-pot 権威）。**Phase G で live 既定を `pokerkit` に切替**（`config_default.json`、`requirements.txt` で `pokerkit>=0.7,<0.8` pin）。`legacy` は config で rollback 可。実機 E2E は Phase H |
| **rules-aware ライブ結線 + silent-fold 合成 (R3 D1/D2a/D2b)** | ✅ 実装済 (preview) | `audio/recognizer.py:apply_corrections`（合法手射影）+ `integration/engine.py:_handle_rules_aware_action`/`_resolve_actor`（合法手射影・actor 推定[RFID>明示席]・`fold_through` で silent-fold 合成 cap=2/atomic・合成 fold 記録）。legacy 既定は不変。派生 confidence(D3) は後続 |
| **決定的 replay harness + golden fixtures (R4 F1/F3a)** | ✅ 実装済 | `integration/replay.py` + `tools/replay_hand.py`（clock 注入で決定的、ADR-0011）。golden fixtures: `tests/fixtures/reconstruction/`（**green 5: 射影 2 + 合成 2 + side-pot 1**、DoD #2 達成）。round-trip 決定性 = `tests/test_reconstruction.py` |
| **派生 confidence + side-pot (R3 D3 / R5 F3a)** | ✅ 実装済 (preview) | `integration/engine.py:derive_confidence`（3 因子 L/A/Q、rules-aware 経路のみ。legacy 固定表は不変）+ needs_review 5 条件。`HandSummary.pots`（main/side、legacy は `[]`）。**Phase D 完了** |
| **hand/action schema freeze (R5 F3b)** | ✅ 実装済 | `docs/contracts/schemas/{hand,action}.schema.json`（`1.0`, additionalProperties:true, ISSUE-0011 Fixed）+ `_MODELS` 登録 + code↔contract + golden→schema テスト |
| **PHH call/check (F3c)** | ✅ 確認済（変更不要） | PHH 標準では check/call は同一トークン `cc`（check-or-call）。区別は非標準で pokerkit が parse 不能になるため統一が正。check/call の別は JSON ログ側で保持（`output/phh_exporter.py` にコメント） |
| **viewer API (M1)** | ✅ 実装済 | `api/read_models.py`, `api/server.py`（§ Viewer API 参照, ADR-0013。読み取り専用、`[api]` extra） |
| **mobile viewer scaffold (M2)** | ✅ 実装済 | `mobile/`（Expo/RN + TypeScript。PlayerSelect→MySessions→MyHands→HandDetail（+ M4: 会計画面）、`ViewerRepository` interface に mock / HTTP 実装を注入、`EXPO_PUBLIC_API_URL` で切替。配布は web export を LAN 配信, ADR-0013）。実データは `session_layer.enabled=true` で流れる（M3 実装済） |
| **cash-only ledger (S3a/M4)** | ✅ 実装済 | `core/ledger.py` / `core/ledger_repository.py` / `gui/ledger_entry.py`（`--ledger`）+ viewer API `/ledger` endpoint + mobile 会計画面（§ Ledger 参照, ADR-0014。point は S3b/M6） |
| **注文リクエスト write path (M5)** | ✅ 実装済 | `core/order_request*.py` / `core/menu.py` + viewer API `/menu`・`/order-requests`（GET/POST）+ `--ledger` の「注文リクエスト」欄（確定/却下）+ mobile 注文画面（§ Ledger 注文リクエスト参照, ADR-0015。staff-in-the-loop / in-process API / name-pick 確定 = ISSUE-0013 Fixed） |
| Vosk 代替バックエンド | ❌ 未実装 | future phase |
| 音声正規化 / 数値正規化 | ❌ 未実装 | 設計提案 R0: `apply_corrections()`（合法手制約, ADR-0009） |
| ディーラーボタン自動回転 / SB/BB 自動 post | ❌ 未実装 | future phase |
| point ledger / store settlement | ❌ 未実装 | **future scope**（S3b/M6 = ISSUE-0001 gate, S4。session ledger は S3a で cash-only 実装済 ↑） |

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
| **session + hand-based seating** | session 管理と hand ごとの seat→player スナップショット (`seat_assignment` / `hand_ref`) | ✅ core 実装済 (S2, § Session & Seating 参照) |
| **session ledger** | session 単位の buy-in / rebuy / add-on / order / adjustment を ledger entry として記録 | ✅ cash-only 実装済 (S3a/M4, § Ledger 参照。point 併用は S3b) |
| **point ledger** | prize point の grant / spend を記録、buy-in 等に充当可能 | 🔲 planned (S3b/M6, ISSUE-0001 gate) |
| **session settlement** | session 終了時に player ごとの「店への net 支払額」と paid/unpaid を確定 | 🔲 planned (S4) |
| **cross-app boundary** | hand logger と ledger app の相互参照契約 (player_id / session_id / hand_id) | 🔲 planned (S5)。ただし **read-only viewer API は M1 で前倒し実装済**（ADR-0013, § Viewer API） |

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
| **S2** | session + hand-based seating ✅ core 実装済 | `session`, `seat_assignment`, `hand_ref`、hand 開始ごとのスナップショット（`core/session*.py`, ADR-0007。schema は draft のまま） |
| **S3** | ledger entries + point ledger | `ledger_entry`, `point_ledger_entry`、cash+point 併用ルール |
| **S4** | session settlement + paid/unpaid | `session_settlement`、net due to store、paid/unpaid 操作 |
| **S5** | cross-app contract / sync boundary | hand logger ↔ ledger app の参照契約、ID 安定性、別プロセス化準備 |
| **R0–R5 + G**（実装済） | rules-aware hand reconstruction（**hand core 改善トラック**, S 系列と直交） | pokerkit を live ルール権威に / actor 推定（手番 prior × sensor + silent-fold 合成）/ `apply_corrections`（合法手制約）/ 決定的 record/replay + golden fixtures + schema freeze + **live 既定切替**。ADR-0009/0010/0011/0012。**R1 record ✅ → R2 engine ✅ → R3 推定/訂正/融合(D1/D2a/D2b/D3) ✅ → R4 replay ✅ → R5 freeze+side-pot ✅ → G 既定=pokerkit ✅**。残: 実機 E2E（H）、重み較正、camera 源 |

各 Phase の着手前に対応する ADR / issue を起こすこと（traceability rules を参照）。

---

# Parallel development plan

本プロジェクトは今後 **hand logger core** / **desktop 別画面 (registry / ledger UI)** /
**iOS・Android 別アプリ** を **並行的** に育てる。並行作業を成立させるため、依存関係を整理し、
parallelizable なタスクと逐次でしかできないタスクを分離する。

本計画は **contract-first**（先に契約を凍結し、各 front-end / core はそれに対して独立実装する）
を原則とする。`player_id` / `session_id` / `hand_id` は全 workstream 共有の安定キーであり、
これらと各モデルの schema を最初に凍結する（§ Cross-app boundary も参照）。

契約の **単一 source は `docs/contracts/`**（Phase 0a で bootstrap 済）。shared ID 契約・
schema・fixtures・repository interface・error 形・validation・freeze/versioning ルールはすべて
そこに置く。詳細・凍結手順は `docs/contracts/README.md` と
`docs/contracts/versioning-and-freeze.md` を参照（ADR-0005）。

> 注: 現時点で実装済なのは hand logger core / S1 player registry / **S2 session + seating core**
> （`core/session*.py`, hand logger とは未接続）。本節の S3 以降・mobile・sync はすべて
> **planned / future scope**。S2 schema の `1.0` freeze も未了（ISSUE-0005 残項目）。

## Workstreams

| WS | 名称 | 責務 | 主要成果物 | 依存 |
|----|------|------|-----------|------|
| **WS0** | contract / spec / schema | 共有 ID・各 domain model の schema・validation 契約・error 形を凍結 | `docs/contracts/*`（planned）, ADR, schema fixtures | なし（全 WS の上流） |
| **WS1** | core domain / repository / services | `core/` のドメイン・repository・service。永続化と業務ルールの source of truth | `core/*.py`, repository, service, tests | WS0（該当 model の契約凍結後） |
| **WS2** | desktop separate screen | registry / ledger / settlement の **別画面** UI（既存 hand logger UI は汚さない） | `gui/*.py`（別 window）, GUI ロジックテスト | WS1（同 phase の repository/service） |
| **WS3** | mobile scaffold (iOS/Android) | 将来の別 front-end。**最初は mock repository** で UI を先行させる。技術は **Expo (React Native, web export 先行) に確定**（ADR-0013） | mobile プロジェクト雛形 (`mobile/`, **M2 実装済** = viewer 画面 + mock/HTTP repository), 後続: registry / ledger 画面 | WS0 のみ（contract）。WS1 完成を待たない |

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

> 置き場と凍結手順は `docs/contracts/`（Phase 0a で bootstrap 済）。以下は順序の要約。

1. **共有 ID 契約**（最優先・全 phase 共通）: `player_id` / `session_id` / `hand_id` は
   アプリ内採番・文字列・不変。採番責任の所在を WS0 で確定する
   （`docs/contracts/shared-ids.md`。`player_id` は S1 で確定、`session_id` / `hand_id` は S2）。
2. **player schema**（S1, 凍結済に近い）: `player_id` + `display_name` + `created_at`、
   validation（空文字 / 前後空白 / 完全一致重複）。
3. **session / seat_assignment / hand_ref schema**（S2）: seat_assignment は hand-based。
   **draft 済（Phase 0b, ADR-0006）+ core 実装済（ADR-0007, `core/session*.py`）**: `hand_id` は
   int 据え置き、cross-app は `(session_id, hand_id)` 複合キー。`session_id` 採番（UUID4 hex）・
   永続形（`sessions.json`）は core について確定。schema `1.0` freeze は ISSUE-0005 残項目決着後。
4. **ledger_entry / point_ledger_entry schema**（S3）: cash+point 併用、order 明細。
5. **session_settlement schema**（S4）: net due to store / paid-unpaid。
6. **repository / service interface 契約**: 各 front-end が呼ぶ抽象 API（mock 差し替え可能な形）。
   これを凍結することで mobile が mock で先行できる。

## mobile が mock で先行できる範囲

- repository interface（WS0 契約）に対する **in-memory / fixture mock** で、player list / add /
  rename などの画面遷移・状態管理・validation 表示を **WS1 完成前に** 作り込める。
- mock は WS0 の schema fixtures（サンプル JSON）を読むだけにし、実 persistence を持たない。
- 後で実 repository（ローカル or API）に差し替えても UI 層が壊れない境界を最初から引く。

## Mobile scaffold（WS3, M2 実装済）

- **技術選定（確定, ADR-0013）**: **Expo（React Native, TypeScript）**。配布は当面
  `expo export --platform web` の Web ビルドを運営 PC から LAN 配信（QR コード）し、
  App Store / Play Store 配布（ネイティブビルド）は同一コードベースの将来オプションとする。
  - 検討した代替案: Flutter / ネイティブ 2 本 / 素の React PWA（ADR-0013 参照）。
- **M2 実装済の範囲**（`mobile/`、詳細は `mobile/README.md`）: viewer 画面
  `PlayerSelect` → `MySessions` → `MyHands` → `HandDetail`。
  - UI は `ViewerRepository` interface（`viewer-api.md` 契約に対応）のみに依存し、
    `MockRepository`（fixtures 相当の in-memory）と `HttpRepository`（M1 viewer API）を
    `EXPO_PUBLIC_API_URL` で注入切替。型は contracts から転記（validation は複製しない）。
  - navigation は依存を増やさない最小 stack（useState）。テストは mock repository の契約挙動
    （`npm test` = node:test）+ `npm run typecheck`。
- **scope 外（M2 時点）**: player registry の編集画面（list/add/rename — 当初案。viewer を
  優先したため後続）、永続化、注文 / ledger / settlement 画面、push 通知、認証（ISSUE-0013）。

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
    （`docs/contracts/` + サンプル fixtures）を定義。**Phase 0a (bootstrap) で実施済**。
  - WS0: repository / service interface の契約テンプレートを定義。**Phase 0a で player を基準例に実施済**。
- **Blockers**: なし。これ自体が他 phase の blocker。
- **Done criteria**: 共有 ID 契約が ADR 化され（ADR-0005）、schema/fixtures の置き場所と更新手順が
  決定（`docs/contracts/`）。各 front-end が「契約だけ見て」mock を書ける状態。
  - **Phase 0a 済**: `docs/contracts/` bootstrap、shared IDs / player schema + fixtures、
    freeze/versioning/drift ルール、最小 contract test（`tests/test_contracts.py`）。
  - **Phase 0b 済（S2 planning）**: `session` / `seat_assignment` / `hand_ref` の **draft** schema +
    fixtures + `session-seating.md` + repository interface 草案を追加（version 0.x, 未 freeze）。
    `hand_id` の cross-app 形を **ADR-0006 で確定**（`(session_id, hand_id)` 複合キー、int 据え置き、
    ISSUE-0004 Resolved）。contract test の `_MODELS` に 3 model を登録。
  - **S2 core 実装済**: `core/session.py` / `core/session_repository.py`（ADR-0007）。`session_id`
    採番（UUID4 hex）・seat_assignment 永続形（`sessions.json`）を core について確定。code↔contract
    test 緑（`tests/test_session_repository.py`）。
  - **残（freeze 前）**: hand logger 接続・mid-session seat change UI 要件（**ISSUE-0005**）と
    schema `1.0` 昇格。

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
- **契約状況**: draft 整備済（Phase 0b, ADR-0006, `docs/contracts/session-seating.md`、3 schema v0.x、
  fixtures）。freeze は **ISSUE-0005** 決着が前提（未 freeze）。
- **Parallel tasks**:
  - WS0: session / seat_assignment / hand_ref schema 凍結（seat_assignment は hand-based）。
    **draft 済**、freeze は ISSUE-0005 残項目後。
  - WS1: session 管理・seat snapshot の repository/service。**core 実装済**
    （`core/session.py` / `core/session_repository.py`, ADR-0007、`tests/test_session_repository.py`）。
  - WS2: desktop の session/seating 別画面。**未着手**。
  - WS3: mobile の session 画面（mock）。**未着手**。
- **Blockers**: seat_assignment を hand-based にする設計確定（**ADR-0006 済**）。hand_id の cross-app 形
  （**ADR-0006 で `(session_id, hand_id)` 複合キーに確定**）。`session_id` 採番・永続形は
  **ADR-0007 で core について確定**。残: hand logger 接続・seat change UI 要件（ISSUE-0005）。
- **Done criteria**: session 開始/終了と hand 単位 seat snapshot が core で確定（**達成: WS1 core**）。
  両 front-end の契約越し表示（WS2/WS3）は未着手。schema `1.0` freeze は ISSUE-0005 残項目後。
- **Phase 2.x（hand logger 接続, 2.1〜2.3 実装済）**: 既存 hand logger world
  （`HandSummary` / `JsonWriter` / `PHHExporter` / `IntegrationThread`）と S2 core を **段階接続**する。
  方針は **Pattern A（write-through, additive）**：hand logger が `SessionRepository` に依存し、
  hand 開始時に `assign_seat` バッチを呼ぶ。`HandSummary.players[i]` に `player_id` を additive 追加、
  `session_id` を session レイヤの UUID4 hex に切替、PHH は無改変、`hand_ref` は session レイヤ側に住む。
  詳細は **ADR-0008** / `docs/contracts/hand-integration.md`。legacy log 取り込みは **ISSUE-0007**。
  Phase 細分:
  - 2.1: schema sketch + `config.session_layer.enabled` フラグ ✅（E1+E2-core）。
  - 2.2: `main.py` session_id 切替 + `assign_seat` 連携 + `HandSummary.player_id` additive
    ✅（engine = E1+E2-core、main.py 結線 = M3）。
  - 2.3: seat 選択 GUI（registry 連動）✅（M3 = E3, ISSUE-0006 Fixed。プロンプト選択 +
    dashboard「席設定」+ `seat_assign` queue イベントで次ハンド反映）。
  - 2.4: legacy log reconciler（任意, ISSUE-0007）— 未着手。

### Phase 3 — ledger and points（S3a 実装済 / S3b planned, ADR-0014 で分割）

- **Goal**: `ledger_entry`（buy_in/rebuy/add_on/order/adjustment）と `point_ledger_entry` を扱う。
- **S3a（cash-only, M4 実装済 — § Ledger 参照）**:
  - WS0: `ledger_entry` schema **draft 0.x** + fixtures + `ledger.md`（point_amount はフィールド
    のみ存在、validation で 0 強制 = `points_not_supported`）。
  - WS1: `core/ledger.py` / `core/ledger_repository.py`（kind 別金額規則・open session 必須・
    append-only・中間集計）。
  - WS2: `gui/ledger_entry.py`（`--ledger`, スタッフ入力 + 中間集計表示）。
  - WS3: mobile 会計画面（read-only, mock/HTTP 両実装）+ viewer API `/ledger` endpoint。
  - ISSUE-0001 に**依存しない**（point を使えない間、残高計算が存在しないため）。
- **S3b（point 連携, M6 planned）**:
  - **Blockers**: **ISSUE-0001**（残高 source of truth）。これが決まらないと残高計算の API 契約が
    凍結できず、point 充当 UI が宙に浮く。
  - WS0: `point_ledger_entry` schema + `ledger_entry` の point 解放 + **schema `1.0` freeze**。
  - WS1: point ledger repository + 残高計算 + cash+point 併用・不足分 cash 補完・
    entry fee cash only の enforce。
  - WS2/WS3: point 充当入力・残高表示。
- **Done criteria（S3 全体）**: cash+point 併用・point 不足の cash 補完・entry fee cash only が
  core で enforced、両 front-end が中間集計を表示できる（中間集計表示は S3a で達成済）。

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
- **前倒し済（M1, ADR-0013）**: read-only サブセット（viewer API, `api/`）は実装済（§ Viewer API）。
  本 phase の残りは write 系 API / sync / repository の API client 分離。
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
- pokerkit 未導入で `engine.backend="pokerkit"`（既定）→ warning を出して legacy backend に
  自動フォールバック（起動は落とさない。rules-aware 機能は無効）
- ゲーム状態の変更は IntegrationThread に一元化する。GUI/CLI の操作（新ハンド/ウィナー/リバイ）は
  `AudioEvent` として queue に積む（`GameStateManager` はロックを持たないため直接変更禁止, ISSUE-0012）
- RFID HTTP 受信は `bind_host` 既定 `127.0.0.1`（無認証のため。LAN 受信は明示的に変更）+
  `Content-Length` 上限 16KB（413）

---

## よく使うコマンド

```bash
pip install -r requirements.txt              # core runtime（または pip install .）
pip install -r requirements-dev.txt          # テスト依存（CI と同じ。skip 0）
pip install ".[pcsc]"                         # RFID PC/SC を使う場合のみ
pip install ".[api]"                          # viewer API を使う場合のみ (M1)
python main.py --cli                         # CLI モード (hand logger)
python main.py                               # GUI モード (hand logger)
python main.py --players                     # Player Registry 画面 (S1, 別画面)
python main.py --ledger                      # スタッフ用 会計入力画面 (S3a/M4, 別画面)
python main.py --viewer-api                  # player 向け読み取り専用 viewer API (M1, ADR-0013)
pytest tests/ -v --ignore=tests/test_vision.py   # CI と同じ（vision レガシー除外）
python tools/replay_hand.py tests/fixtures/reconstruction/silent-fold  # 決定的 replay (F1)
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
