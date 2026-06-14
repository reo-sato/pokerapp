# Poker Hand Logger — CLAUDE.md

## プロジェクト概要

ライブポーカートーナメントのハンド履歴を自動記録する Python アプリケーション。
ディーラー口元マイク（faster-whisper 音声認識）と RFID NFC（**ESP32-S3 + PN5180**, USB CCID で PC/SC 公開, ADR-0015）の 2 ソースを統合し、
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
│   ├── contracts/                 ← contract-first 基盤 (shared IDs / schemas / fixtures; **全 model schema `1.0` frozen** — player/hand/action + session/seat/hand_ref(S2) + ledger/point/settlement(S3) + order_request/player_session_summary(viewer), ADR-0019。残 draft は interface/sync(S5) のみ)
│   ├── installation.md            ← エンドユーザー: インストール手順 (Phase I)
│   ├── usage.md                   ← エンドユーザー: 使い方・読み上げ語彙・設定 (Phase I)
│   ├── troubleshooting.md         ← エンドユーザー: 困りごと対処 (Phase I)
│   └── decision-log.md            ← ADR / 主要 issue の索引
├── sprc_v4.docx                   ← 仕様書（要件定義）
├── claude_v4.docx                 ← 旧仕様書（参考）
├── main.py                        ← エントリーポイント (--cli / GUI / --players / --sessions / --ledger / --export-ledger / --viewer-api)
├── config_default.json            ← デフォルト設定テンプレート
├── rfid_cards.json                ← tag_id → card_code マスタ
├── players.json                   ← player registry 永続ファイル (.gitignore, S1)
├── sessions.json                  ← session + hand-based seating 永続ファイル (.gitignore, S2)
├── ledger.json                    ← session ledger + point ledger 永続ファイル (.gitignore, S3)
├── order_requests.json            ← 注文リクエスト永続ファイル (.gitignore, M5)
├── player_credentials.json        ← player PIN credential 永続ファイル (.gitignore, node-local, L1 PIN ADR-0027)
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
│   ├── player_repository.py       ← PlayerRepository (player CRUD + merge/canonical + JSON 永続化, S1/ADR-0030)
│   ├── session.py                 ← Session / SeatAssignment / HandRef データクラス (S2)
│   ├── session_repository.py      ← SessionRepository (session + hand-based seating + JSON 永続化, S2)
│   ├── ledger.py                  ← LedgerEntry / PointLedgerEntry / SessionSettlement データクラス (S3)
│   ├── ledger_repository.py       ← LedgerRepository (ledger + point + settlement + JSON 永続化, S3)
│   ├── order_request.py           ← OrderRequest データクラス (M5)
│   ├── order_request_repository.py ← OrderRequestRepository (注文リクエスト, thread-safe + reload-on-read, M5)
│   ├── menu.py                    ← MenuMaster (menu.json ロード・検索, M5)
│   ├── sync.py                    ← state-based merge 純粋関数 + snapshot I/O (S5 双方向 sync, ADR-0022/0024)
│   ├── auth_token.py              ← player principal の stateless 署名トークン (L1 PIN, ADR-0027)
│   └── player_credential_repository.py ← PlayerCredentialRepository (PIN ハッシュ PBKDF2 + lockout, node-local, ADR-0027)
│
├── audio/
│   ├── recorder.py                ← AudioThread (PyAudio + faster-whisper)
│   └── recognizer.py              ← parse_action(), parse_amount(), apply_corrections()
│
├── rfid/
│   ├── reader_thread.py           ← RFIDThread (pyscard PC/SC, canonical: PN5180+ESP32-S3 を USB CCID で公開, ADR-0015)
│   ├── http_receiver.py           ← RFIDHTTPReceiver (HTTP POST 受信, optional secondary: debug/remote 用, ADR-0015)
│   ├── bridge.py                  ← RFID ブリッジユーティリティ
│   └── card_master.py             ← CardMaster (rfid_cards.json ロード・検索)
│
├── integration/
│   └── engine.py                  ← IntegrationThread, calc_confidence()
│
├── output/
│   ├── json_writer.py             ← セッション JSON ログ書き込み
│   ├── phh_exporter.py            ← PHHExporter (PHH 形式エクスポート)
│   └── ledger_csv_exporter.py     ← LedgerCsvExporter (settlement / cashflow CSV, S3.3)
│
├── api/
│   ├── read_models.py             ← viewer read model (seat_assignment 起点 join + settlement 由来 summary, fastapi 非依存, M1)
│   ├── server.py                  ← viewer API server (FastAPI app factory + uvicorn, M1, ADR-0017)
│   └── client.py                  ← ViewerApiClient (viewer API の Python client = local↔API 分離点, S5, ADR-0020)
│
├── gui/
│   ├── dashboard.py               ← GUIDashboard (hand logger 画面, customtkinter; E3 で座席設定ボタン追加)
│   ├── player_registry.py         ← PlayerRegistryWindow (player registry 画面, S1, dashboard とは別画面)
│   ├── seat_selection.py          ← SeatSelectionDialog (seat→player_id 選択モーダル, S2.x E3)
│   ├── session_viewer.py          ← SessionViewerWindow (session/seating read-only inspection 画面, WS2-α, 別画面)
│   └── ledger_view.py             ← LedgerViewWindow (ledger viewer/editor + 精算確定/paid-unpaid + 注文確定/却下 + buy-in 金額プリセット, S3.2 + S4 + M5 + ADR-0026, dashboard とは別画面)
│
├── mobile/                        ← Poker Hand Viewer (Expo/RN, M2, ADR-0017。mock/HTTP repository 切替, web export 配布)
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
| RFID reader IC | **PN5180** | ISO 15693 (UID 8B) + 14443 A/B 対応。ADR-0015 で採用 |
| RFID MCU | **ESP32-S3** | native USB で **USB CCID class** を実装し PN5180 ×N を PC/SC multi-slot として公開。ADR-0015 |
| RFID (PC/SC, **canonical**) | pyscard ≥ 2.0.7 | OS 標準 PC/SC スタック越しに pyscard が reader_name で列挙。第一系統（ADR-0015） |
| RFID (HTTP, **optional secondary**) | 標準 http.server | debug / remote / 分散設置の限定用途で残置（ADR-0015） |
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

- player 削除、`display_name` 以外の属性、hand logger との自動接続。（player merge は **実装済 =
  ADR-0030**, alias/tombstone + read-time canonicalization）
- session / seat_assignment / point ledger / settlement / cross-app sync は後続 Phase。

---

## Session & Seating（S2, core 実装済）

player registry の上に重なる **session レイヤ + hand-based seating** の core 最小実装
（CLAUDE.md § Future Scope の S2 を昇格）。契約は `docs/contracts/session-seating.md`（draft）/
ADR-0006、実装上の判断は ADR-0007。**hand logger とは config フラグ `session_layer.enabled`（既定 off）で
write-through 接続可**（E1+E2-core, ADR-0008 Pattern A: `IntegrationThread` に `session_repo`/`seat_player_map`
を DI し、hand 開始で `assign_seat`・確定時に `HandSummary.players[i].player_id` を additive 埋め込み）。
**off では従来どおり独立**（別ストア・別 namespace、player_id キーも付けない）。seat 選択 GUI
（`gui/seat_selection.py`）と main.py 結線は **E3 で実装済**（ISSUE-0006 Resolved）。

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
- settlement（S4）、desktop / mobile UI、API / sync（S5）。
- session / seat の削除・merge、advanced seat history UI、schema `1.0` freeze（ISSUE-0005 残項目）。

---

## Ledger / Points / Settlement（S3, 実装済）

session world（S2）の上に重なる **ledger（金銭イベント）/ points（店内ポイント残高）/ settlement
（session 締めの精算）** の実装（CLAUDE.md § Future Scope の S3 を昇格）。狙いは「home game host が
混乱しないレベルの帳簿」で **複式簿記ではない**。契約は `docs/contracts/ledger-overview.md` /
`ledger-schema.md`（draft）、設計判断は ADR-0016（point 残高 = fold は ADR-0013 を踏襲・統合）。
**hand logger とは未接続**（別ストア・別 namespace、PHH は read-only）。

### スコープ（現時点）

- **ledger entry** の add / reverse / list（`buy_in` / `rebuy` / `add_on` / `order` / `entry_fee` /
  `adjustment`、cash + point 併用）。`order` のみ明細 `{item_name, unit_amount, quantity}`。
- **point** の grant / 残高照会（残高 = `point_ledger_entry` の fold が source of truth, ISSUE-0001）/
  spend（buy-in 等への充当、不足は cash 補完 = 不足分を呼び出し側が cash で渡す）。
- **settlement** を session ごとに導出（中間集計 = speculative）し、closed session で確定（commit）、
  paid/unpaid を操作（常に player→店、partial なし）。
- **desktop viewer / editor**（S3.2）と **settlement / cashflow CSV export**（S3.3）。
- 金額は **整数円**（chips とは別単位・自動換算なし）、point は整数点。
- 永続化はプロジェクト直下 `ledger.json`（アトミックリネーム、`.gitignore`）。**append-only**。

### 構成

| 要素 | ファイル | 役割 |
|------|---------|------|
| ドメイン | `core/ledger.py` | `LedgerEntry` / `PointLedgerEntry` / `SessionSettlement`（to_dict / from_dict） |
| リポジトリ | `core/ledger_repository.py` | `LedgerRepository`: ledger/point/settlement の CRUD + 業務ルール + JSON 永続化 |
| 画面 (S3.2) | `gui/ledger_view.py` | `LedgerViewWindow`（entry 追加/取消・point 付与・中間集計 speculative 表示, dashboard とは独立） |
| 起動 (S3.2) | `main.py --ledger` | hand logger とは別に ledger 画面を開く |
| export (S3.3) | `output/ledger_csv_exporter.py` | `LedgerCsvExporter`: settlement / cashflow を CSV 出力（utf-8-sig, `main.py --export-ledger`） |

### Validation / errors（`LedgerRepository` が source of truth）

- entry fee に point 充当 → `EntryFeeRequiresCashError`（`entry_fee_requires_cash`）。
- spend が残高超過 → `InsufficientPointsError`（`insufficient_points`）。不足分は cash 補完。
- 金額・符号・order 明細・reversal 要求が不正 → `InvalidAmountError`（`invalid_amount`）。
- 同一 idempotency_key の grant 重複 → `DuplicateGrantError`（`duplicate_grant`）。
- open session の確定 → `SessionNotClosedError`（`session_not_closed`）、確定済再確定 →
  `AlreadySettledError`（`already_settled`）。
- unknown player → `UnknownPlayerError`（`unknown_player`）、unknown entry/session/settlement →
  `LedgerNotFoundError`（`not_found`）。enum 外の kind/reason/status は `ValueError`。
- error code は `docs/contracts/error-shapes.md` の ledger セクションと 1:1 対応。

### 不変条件（ADR-0016）

- **append-only**：entry は mutate/delete せず、訂正は reversal（`reverses_entry_id`）で表す。
- **point 残高 = fold**、残高は負にならない、**ledger↔point 整合**（`point_amount!=0` の entry に
  `delta=-point_amount` の point entry 1 件）。
- settlement は derived・**player→店の 1 方向**・closed session のみ確定。

### Out of scope（現時点）

- **buy-in 金額プリセット**（ADR-0026, スタッフが `--ledger` 画面のボタン or staff API
  `GET /api/staff/buyin-presets` で店設定の整数円プリセットを選び、kind=buy_in の cash を prefill）
  と settlement の GUI からの確定 commit + paid/unpaid + partial-paid 切替は **実装済**
  （`gui/ledger_view.py` の精算/buy-in パネル）。**partial-paid は実装済**（`paid_amount`
  additive + `payment_status` 導出, ADR-0023）。
- hand 結果からの **自動 ledger 生成**（chip→円換算を伴うもの）は **作らない**（chips は別単位・
  自動換算なし, ADR-0016/0026）。hand logger（`HandSummary`）との自動接続、chip↔円換算、rake/fee。
- settlement schema の `1.0` freeze（S4）、cross-app sync（S5）。

---

## Viewer API / mobile / 注文リクエスト（M1/M2/M5, 実装済）

player が自分のスマホ（mobile, M2）から自分の session / ハンド履歴 / 会計を参照し、ドリンク注文
（M5）まで行える **player 向け front-end** と、それを支える **読み取り専用 viewer API**（M1,
ADR-0017）。**player 向け desktop viewer は作らない**（desktop = スタッフ操作専用）。

### viewer API（M1, `api/`, ADR-0017）

- 起動は 2 形態（要 `pip install ".[api]"` = fastapi/uvicorn）:
  - `python main.py --viewer-api` = **read-only**（注文 POST は 503 `orders_unavailable`）。
  - `python main.py --ledger`（`viewer_api.enabled=true`）= 会計画面に **in-process 組み込み**で
    注文 write が有効（単一プロセス所有, ADR-0018）。
- endpoints: `/api/health`, `/api/players`, `/api/players/{id}`, `/api/players/{id}/sessions`,
  `.../sessions/{sid}/hands`, `/api/sessions/{sid}/hands/{hid}`, `.../sessions/{sid}/ledger`,
  `/api/menu`, `.../sessions/{sid}/order-requests`（GET/POST）。契約は
  `docs/contracts/viewer-api.md` + `player_session_summary` / `order_request` schema（`1.0` frozen, ADR-0019）。
- **read model**: 「player のハンド」は `sessions.json` の seat_assignment 起点で hand log を
  `(session_id, hand_id)` join。ledger summary は verify-v1 ledger の `compute_settlement` を
  当該 player に絞った settlement 由来（`cash_in_total / order_total / entry_fee /
  point_spent_total / point_credited_total / net_due_to_store`, ADR-0016）。
- error は `{"code", "message"}`（`error-shapes.md` 準拠）。config: `viewer_api.bind_host` 既定
  `127.0.0.1`（無認証。LAN 参照は明示変更, ISSUE-0019）/ `bind_port` 既定 8788。

### staff write API（S5, `api/server.py` の `/api/staff/...`, ADR-0021）

別端末のスタッフが会計をリモート操作するための **staff 専用 write API**。**staff shared token**
（config `viewer_api.staff_token` を `Authorization: Bearer <token>` で送る）で認可する
（player read / 注文 POST は無認証のまま）。

- endpoints（すべて token 必須）: `GET /api/staff/sessions/{sid}/settlement`（中間集計）/
  `GET .../order-requests?status=`（全 player の注文 queue）/ `POST .../ledger-entries`（ledger 追加）/
  `POST .../settlement/commit` / `PUT .../players/{pid}/payment-status` /
  `POST /api/staff/order-requests/{rid}/confirm` / `POST .../{rid}/reject`。契約は
  `docs/contracts/viewer-api.md` の staff write 節。
- **認可**: token 未設定 → 403 `staff_writes_disabled` / token 不一致 → 401 `unauthorized`。
  write 系は **write 所有プロセス（`--ledger`, viewer_api.enabled）のみ**、単独 `--viewer-api` は
  503 `orders_unavailable`（単一書き手, ADR-0020）。staff read は token があれば read-only でも可。
- ledger / settlement / order の実 error code は `error-shapes.md` を再利用。`LedgerRepository` は
  RLock で thread-safe（`--ledger` の GUI スレッド × API スレッドの同時 mutate を保護）。Python client は
  `api/client.py:ViewerApiClient(staff_token=...)` の staff メソッド群。

### 注文リクエスト（M5, `core/order_request*.py` / `core/menu.py`, ADR-0018）

- player はスマホから **order_request**（pending）を POST する。**ledger には書かれず**、
  スタッフが `--ledger` 画面の確定/却下パネルで**確定**したときに `ledger_entry`（kind=order,
  cash_amount=unit×qty, `order={item_name, unit_amount, quantity}`）が作られ `ledger_entry_id` が
  リンクされる（staff-in-the-loop）。
- **menu master**: `menu.json`（コミット済みサンプル、店側で編集）。menu 外は `unknown_item`、
  確定時の単価は menu から prefill（スタッフ上書き可）。
- **単一プロセス所有**: `order_requests.json` の write は viewer API を in-process で抱えた
  `--ledger` プロセスのみ。単独 `--viewer-api` は read-only（POST 503）。
  `OrderRequestRepository` は thread-safe（lock）+ reload-on-read。
- **closed session への確定**は `OrderRequestRepository.confirm_request` が `session_closed`（409）で
  弾く（verify-v1 ledger は closed を拒否しないため、注文確定の closed 不変条件はこの層が担う）。

### Out of scope（現時点）

- **player** read の per-player アクセス制御（v1 = name-pick で確定, ISSUE-0019 Fixed）。本人確認の
  進化方針は **ADR-0025**（L0 name-pick → L1 PIN → L2 外部 IdP（LINE/Google OIDC））。実装は後続。
  スタッフ会計 write の認可は **staff shared token で解決済み**（ADR-0021, 上の staff write API 節）。
- player からの ledger への直接 write（注文も staff 確定が必須）。order_request schema の `1.0` freeze。

---

## Session / Seating Viewer（WS2-α, read-only, 実装済）

S2 core に蓄積された session / hand-based seating を **人間が確認するための read-only
inspection UI**（desktop, WS2 の最初の一歩 = WS2-α）。hand logger dashboard
（`gui/dashboard.py`）/ player registry（`gui/player_registry.py`）とは **完全に別画面**。

### スコープ（現時点）

- session 一覧（session_id / started_at / status / label / hand 数 / assignment 数の要約）を見られる。
- session を選ぶと **概要 / current seating / hand ごとの seat assignments** を見られる。
- `player_id` を `PlayerRepository` で `display_name` に解決して表示する。解決不能（registry 非実在）
  なら `(unknown)` を表示し、`player_id` 自体はそのまま残す。
- **再読込（refresh）** ボタンで `SessionRepository` / `PlayerRepository` をディスクから読み直す
  （別プロセスの hand logger が `sessions.json` を更新した場合に最新化）。live auto-refresh は持たない。
- empty state（session 無し）/ no-data state（seating / hand 無し）を明示表示する。

### 構成

| 要素 | ファイル | 役割 |
|------|---------|------|
| 画面 | `gui/session_viewer.py` | `SessionViewerWindow`（customtkinter, dashboard / registry とは独立した read-only 画面） |
| 起動 | `main.py --sessions` | hand logger / registry とは別に viewer 画面を開く |
| read API | `core/session_repository.py` | `list_sessions` / `get_session` / `current_seating` / `list_hand_ids` / `list_seat_assignments` / `reload`（list_hand_ids・reload は WS2-α で additive 追加した read API） |
| name 解決 | `core/player_repository.py` | `get` / `list_players` / `reload`（reload は additive 追加） |

### read-only の原則

- **編集系操作を一切持たない**（session 作成 / close / seat 割り当て / player rename / 削除 なし）。
  許容される書き込み相当は **refresh（再読込）のみ**。画面タイトルにも「読み取り専用 / inspection」を明示。
- 業務ルール（validation・seating 導出）は **core が source of truth**。viewer は read API を呼んで
  `player_id`→`display_name` 解決と表示整形だけを行い、business logic を複製しない。

### Out of scope（WS2-α 時点）

- session / seat の作成・編集・削除、hand logger からの live push 更新（手動 refresh で代替）。
- filters / search / CSV export 等の高度機能、mobile（WS3）側 viewer、ledger / point / settlement。
- legacy log reconciliation tool（ISSUE-0007）。
- **注**: hand logger × session の write-through 接続は **E1〜E3 で実装済**（`session_layer.enabled`,
  § Session & Seating 参照）。有効時は live の hand logger が書いた seating も viewer で確認できる。
  viewer の data source 依存と拡張は ISSUE-0013 参照。

---

## 実装状況（現時点）

| 機能 | 状態 | 備考 |
|------|------|------|
| 音声認識 (Whisper) | ✅ 実装済 | `audio/recognizer.py` |
| RFID PC/SC 受信 | ✅ 実装済 (canonical) | `rfid/reader_thread.py`（ESP32-S3 USB CCID 経由で PN5180 公開, ADR-0015） |
| RFID HTTP 受信 | ✅ 実装済 (optional secondary) | `rfid/http_receiver.py`（debug/remote 用, ADR-0015） |
| ESP32-S3 USB CCID firmware ↔ Python 契約固定 | 🔲 planned | ISSUE-0007（USB descriptor / reader_name / ATR / 8B UID 等） |
| RFID カード照合 | ✅ 実装済 | `rfid/card_master.py` |
| ストリート自動遷移 (RFID) | ✅ 実装済 | board 枚数 3/4/5 で遷移 |
| Confidence 算出 | ✅ 実装済 | センサー組み合わせ行列 |
| JSON ログ出力 | ✅ 実装済 | `output/json_writer.py` |
| PHH エクスポート | ✅ 実装済 | `output/phh_exporter.py` |
| GUI ダッシュボード | 🔨 部分実装 | `gui/dashboard.py` |
| **player registry (S1)** | ✅ 実装済 | `core/player.py`, `core/player_repository.py`, `gui/player_registry.py` |
| **session + hand-based seating (S2) core** | ✅ 実装済 | `core/session.py`, `core/session_repository.py`（§ Session & Seating 参照） |
| **session / seating viewer (WS2-α, read-only)** | ✅ 実装済 | `gui/session_viewer.py`（`main.py --sessions`, § Session / Seating Viewer 参照） |
| **ledger / point / settlement core (S3.1)** | ✅ 実装済 | `core/ledger.py`, `core/ledger_repository.py`（§ Ledger / Points / Settlement 参照, ADR-0016, ISSUE-0001 Resolved） |
| **ledger desktop viewer (S3.2)** | ✅ 実装済 | `gui/ledger_view.py`（`main.py --ledger`, 別画面, dashboard 不可侵） |
| **settlement 確定 GUI (S4)** | ✅ 実装済 | `gui/ledger_view.py` 精算パネル: closed session の `commit_settlement` + 確定済 settlement の paid/unpaid 切替（`set_payment_status`）+ **受領額入力で partial-paid 記録**（`record_payment`, ADR-0023） |
| **settlement partial-paid (S4)** | ✅ 実装済 | `SessionSettlement.paid_amount`（additive, 既定 0）+ `payment_status` 導出（paid/unpaid/partial, ADR-0023）。core `record_payment` / staff API `PUT .../payment` / `ViewerApiClient.record_payment` / GUI 受領額入力 / mobile 一部支払い表示。schema `1.0`→`1.1`（optional field + enum 値, additive） |
| **settlement / cashflow CSV export (S3.3)** | ✅ 実装済 | `output/ledger_csv_exporter.py`（`main.py --export-ledger`, utf-8-sig） |
| **viewer API (M1)** | ✅ 実装済 | `api/read_models.py`, `api/server.py`（`main.py --viewer-api`, read-only GET, `[api]` extra, ADR-0017。ledger summary は `compute_settlement` 由来 = ADR-0016） |
| **mobile viewer (M2)** | ✅ 実装済 | `mobile/`（Expo/RN。PlayerSelect→MySessions→MyHands→HandDetail + 会計（**精算状況: 確定/未確定・支払済み/未払い** 表示, S4）+ 注文画面。`ViewerRepository` に mock/HTTP 注入, `EXPO_PUBLIC_API_URL` 切替, ADR-0017） |
| **注文リクエスト write path (M5)** | ✅ 実装済 | `core/order_request*.py` / `core/menu.py` + viewer API `/menu`・`/order-requests`（GET/POST）+ `gui/ledger_view.py` の確定/却下パネル（§ 注文リクエスト参照, ADR-0018。staff-in-the-loop / in-process API / name-pick = ISSUE-0019 Fixed） |
| **hand logger × session 統合 (S2.x E1+E2-core)** | ✅ 実装済 | `integration/engine.py`（`session_repo`/`seat_player_map` DI、`assign_seat` write-through + `player_id` additive 埋め込み、`session_layer.enabled` 既定 off で挙動不変, ADR-0008） |
| **seat→player 選択 GUI + live 有効化 (S2.x E3)** | ✅ 実装済 | `gui/seat_selection.py`（`SeatSelectionDialog`: モーダル, 席ごと割当 / 未登録その場 create / 空席 skip / carry-forward）+ `gui/dashboard.py`「座席設定」ボタン + `integration/engine.py:set_seat_player_map` + `main.py` 結線（UUID4 session_id）。既定 off で挙動不変, ISSUE-0006 Resolved |
| **event 記録 sidecar (R1)** | ✅ 実装済 | `output/event_recorder.py`（opt-in `recording.enabled`, 挙動不変, ADR-0010, `reconstruction_event` schema） |
| **pokerkit game-state backend (R2) + live 既定切替 (G)** | ✅ 実装済 | `core/poker_engine.py`（`engine.backend`, ADR-0009/0012。actor/合法手/side-pot 権威）。**Phase G で live 既定を `pokerkit` に切替**（`config_default.json`、`requirements.txt` で `pokerkit>=0.7,<0.8` pin）。`legacy` は config で rollback 可。実機 E2E は Phase H |
| **rules-aware ライブ結線 + silent-fold 合成 (R3 D1/D2a/D2b)** | ✅ 実装済 (preview) | `audio/recognizer.py:apply_corrections`（合法手射影）+ `integration/engine.py:_handle_rules_aware_action`/`_resolve_actor`（合法手射影・actor 推定[RFID>明示席]・`fold_through` で silent-fold 合成 cap=2/atomic・合成 fold 記録）。legacy 既定は不変。派生 confidence(D3) は後続 |
| **決定的 replay harness + golden fixtures (R4 F1/F3a)** | ✅ 実装済 | `integration/replay.py` + `tools/replay_hand.py`（clock 注入で決定的、ADR-0011）。golden fixtures: `tests/fixtures/reconstruction/`（**green 5: 射影 2 + 合成 2 + side-pot 1**、DoD #2 達成）。round-trip 決定性 = `tests/test_reconstruction.py` |
| **派生 confidence + side-pot (R3 D3 / R5 F3a)** | ✅ 実装済 (preview) | `integration/engine.py:derive_confidence`（3 因子 L/A/Q、rules-aware 経路のみ。legacy 固定表は不変）+ needs_review 5 条件。`HandSummary.pots`（main/side、legacy は `[]`）。**Phase D 完了** |
| **hand/action schema freeze (R5 F3b)** | ✅ 実装済 | `docs/contracts/schemas/{hand,action}.schema.json`（`1.0`, additionalProperties:true, ISSUE-0011 Fixed）+ `_MODELS` 登録 + code↔contract + golden→schema テスト |
| **PHH call/check (F3c)** | ✅ 確認済（変更不要） | PHH 標準では check/call は同一トークン `cc`（check-or-call）。区別は非標準で pokerkit が parse 不能になるため統一が正。check/call の別は JSON ログ側で保持（`output/phh_exporter.py` にコメント） |
| Vosk 代替バックエンド | ❌ 未実装 | future phase |
| 音声正規化 / 数値正規化 | ❌ 未実装 | 設計提案 R0: `apply_corrections()`（合法手制約, ADR-0009） |
| ディーラーボタン自動回転 / SB/BB 自動 post | ❌ 未実装 | future phase |
| schema `1.0` freeze (S4) | ✅ 実装済 | 全 model（session/seat/hand_ref・ledger/point・settlement・order_request/player_session_summary）を `1.0` freeze（ADR-0019, ISSUE-0005 Resolved）。code↔contract test 全 model カバー |
| 実機 E2E (Phase H) / PN5180 firmware 契約 (ISSUE-0014/0015) | 🔲 planned | クリーン環境の通し確認 + USB CCID firmware↔Python 契約凍結（§ ロードマップ 残作業） |
| **cross-app boundary (S5 read)** | ✅ 実装済 | repository interface frozen（ADR-0020）+ `api/client.py:ViewerApiClient`（Python の local↔API 分離点）+ round-trip test。read boundary を二言語で実証（mobile + Python） |
| **staff 会計 write API (S5 write)** | ✅ 実装済 | `api/server.py` の `/api/staff/...`（ledger 追加 / settlement 確定 / paid-unpaid / 注文確定・却下 + staff read）を **staff shared token**（`Authorization: Bearer <viewer_api.staff_token>`）で公開（ADR-0021）。`LedgerRepository` を RLock で thread-safe 化。単一書き手維持（read-only は 503）。`ViewerApiClient(staff_token=...)` の staff メソッド + `tests/test_viewer_api_staff.py` |
| **双方向 sync (S5 — state-based merge)** | ✅ 実装済 | `core/sync.py`（純粋マージ: UUID union + 単調解決で可換・結合・冪等 ⇒ 収束, ADR-0022。settlement は **paid_amount monotonic max** で partial-paid 対応, ADR-0024）+ `GET/POST /api/staff/sync/{snapshot,merge}`（staff-token gate, write 所有のみ merge 受理）+ `ViewerApiClient.{pull_sync_snapshot,push_sync_merge,sync_bidirectional}`。全 repo に `path` property、`LedgerRepository`/`OrderRequestRepository` に `reload()` を additive。ADR-0020 の単一書き手前提を更新（複数書き手 + 収束マージ）。`tests/test_sync.py` / `tests/test_viewer_api_sync.py` |
| **player 本人認証 L1 PIN** | ✅ 実装済 | `core/auth_token.py`（stateless 署名トークン）+ `core/player_credential_repository.py`（PBKDF2 + lockout、node-local `player_credentials.json`、read API / sync 非対象）+ `api/server.py` の principal レイヤ（`_resolve_player_principal`/`_require_player`）+ `POST /api/auth/login`・`/api/players/{id}/pin`。config `viewer_api.player_auth`（off/optional/required, 既定 **off で後方互換**）。`ViewerApiClient.{login,set_pin}`。staff token と直交（ADR-0027）。`tests/test_auth_token.py` / `test_player_credential_repository.py` / `test_viewer_api_auth.py` |
| **player 本人認証 L2 外部 IdP** | 🔲 planned (設計済) | 詳細設計 = ADR-0028（LINE/Google OIDC、`auth_identity`、hosted モード）+ 運用設計 = ADR-0029（会場 source-of-truth + cloud は player ミラー / マネージド PaaS / LINE+Google / PII 最小 APPI）。前提の player merge は **実装済（ADR-0030）**。OIDC コード本体が未着手 |
| **player merge** | ✅ 実装済 | ADR-0030: `merged_into` の alias/tombstone（履歴 rewrite なし、append-only/収束 sync/player_id 不変を保つ・可逆）。`PlayerRepository.merge_players`/`resolve_canonical`/`equivalence_class`/`unmerge` + 全 player-keyed read（settlement 集計 / point / entry / order / viewer / login principal）を canonicalize + `core/sync.py:_resolve_player`（monotonic + tiebreak）+ staff API `POST /api/staff/players/merge` + registry GUI。schema `player` `1.0`→`1.1`。`tests/test_player_merge.py` 他 |
| cross-app sync 拡張 (S5 後続) | 🔲 planned | player rename 伝播（`updated_at` additive）/ hand log の file-level union / 定期 auto-trigger |

---

# Future Scope

> **状態（verify-v1 統合後）**: S 系（会計）・M 系（player 向け参照）・R 系（hand core）の 3 トラックは
> **大半が実装済**で 1 リポジトリに合流済み（§ 実装状況 / 下の「ロードマップ」表）。
> 本セクションは (1) 既実装機能の **契約上の定義**（settlement / cross-app から参照される model 定義）と、
> (2) **残作業**（schema `1.0` freeze・cross-app sync・実機 E2E 等）を分けて記述する。
> 「未実装」と明記された項目のみが未着手で、それ以外は上のセクションへ昇格済み。

## Product scope / future architecture

現状のプロジェクトは **hand logger 単機能** だが、今後は同一プロジェクト内に以下の
**session / accounting レイヤ** を planned scope として追加する。

| レイヤ | 目的 | 状態 |
|--------|------|------|
| hand logger | ハンドごとのアクション履歴を JSON/PHH に出力 | ✅ 実装済 |
| **player registry** | アプリ内で player を新規作成・管理 | ✅ 実装済 (S1, § Player Registry 参照) |
| **session + hand-based seating** | session 管理と hand ごとの seat→player スナップショット (`seat_assignment` / `hand_ref`) | ✅ core 実装済 (S2, § Session & Seating 参照) |
| **session ledger** | session 単位の buy-in / rebuy / add-on / order / adjustment / entry_fee を ledger entry として記録 | ✅ core 実装済 (S3, § Ledger / Points / Settlement 参照) |
| **point ledger** | prize point の grant / spend を記録、buy-in 等に充当可能 | ✅ core 実装済 (S3, § Ledger / Points / Settlement 参照) |
| **session settlement** | session 終了時に player ごとの「店への net 支払額」と paid/unpaid を確定 | ✅ core 実装済 (S3.1: compute/commit/paid-unpaid)・CSV export (S3.3)・schema `1.0` frozen (ADR-0019) |
| **cross-app boundary** | hand logger と ledger app の相互参照契約 (player_id / session_id / hand_id) | 🟡 read-only API は M1 で前倒し実現（`api/`, ADR-0017）。write/sync は planned (S5) |

hand logger と ledger app は **将来別画面・別アプリ** になることを前提に設計する。
両者は共通 ID で相互参照する（§ Cross-app boundary 参照）。

## Domain model（契約定義）

以下の概念モデルは **S1〜S3 で core 実装済**、schema は **`1.0` frozen（ADR-0019）**。本節は
settlement / cross-app / viewer から参照される **契約上の定義** として残す（各実装の詳細は上のセクション）。

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
- 種別: `buy_in` / `rebuy` / `add_on` / `order` / `adjustment` / `entry_fee`（ADR-0016 で additive 追加）
- 共通属性: `entry_id`, `session_id`, `player_id`, `kind`, `occurred_at`, `cash_amount`, `point_amount`, `note`
- `order` 種別のみ `item_name`, `unit_amount`, `quantity` 等の明細サブ構造を持つ
- **S3 で core 実装済**（§ Ledger / Points / Settlement 参照）。本節は settlement / cross-app から見た
  契約上の定義として残す。

### `point_ledger_entry`

- point の増減 1 件
- 増加理由: `manual_grant` / `result_credit` / `campaign_grant`
- 減少理由: `spend_on_buyin` / `spend_on_rebuy` / `spend_on_addon` / `spend_on_order` / `adjustment`
- 共通属性: `entry_id`, `player_id`, `delta_points`, `reason`, `occurred_at`, `related_ledger_entry_id?`, `idempotency_key?`
- 残高 = entry 列の fold（ADR-0016, ISSUE-0001 決着）
- **S3 で core 実装済**（§ Ledger / Points / Settlement 参照）。

### `session_settlement`

- session 終了時に player ごと 1 行確定する
- 属性: `session_id`, `player_id`, `cash_in_total`, `point_spent_total`, `order_total`, `entry_fee`, `point_credited_total`, `net_due_to_store`, `payment_status` (`paid` | `unpaid`), `settled_at`
- 「player 間の精算」は **扱わない**。settlement は **常に「player → 店」の 1 方向**

## Business rules

今後の実装でも守るべき業務ルール。**ルール 1〜8 はいずれも S3 core で enforce 済**
（`core/ledger_repository.py` の ledger / point / settlement、§ Ledger / Points / Settlement 参照）。
schema は `1.0` frozen（ADR-0019）。partial-paid（ルール 4）も **実装済**（schema `1.1`, ADR-0023）。

1. **entry fee は cash only**。point では支払えない。
2. **buy-in / rebuy / add-on / order** は cash + point の **併用可**。
   - 1 件の ledger_entry は `cash_amount + point_amount` の両方を持ち得る。
3. **point 不足分は cash で補完**。point 残高 < 必要点数の場合、不足分は cash として ledger に記録する。
4. **paid/unpaid/partial** は店への支払い状況を表す。**partial-paid 対応済**（ADR-0023）: 累計受領額
   `paid_amount`（>=0, additive）を真実とし、`payment_status` を `net_due_to_store` から導出する
   （`net<=0`→paid / `paid<=0`→unpaid / `paid>=net`→paid / `0<paid<net`→partial。過払いは paid に丸め）。
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

## ロードマップ（3 トラック統合後）

verify-v1 統合（2026-06-13, merge commit）で、会計トラック（S）と player 向け参照トラック（M）が
合流した。**M4（serene の cash-only ledger）は破棄**し S3 の cash+point ledger に一本化、
**M6（point）/ M7（settlement）は S3 で実装済**に合流、採番は serene ADR-0013/0015→**ADR-0017/0018**、
ISSUE-0013→**ISSUE-0019** に振り替え済み（§ decision-log）。

### 会計トラック S（session / ledger / settlement）

| Phase | スコープ | 状態 |
|-------|---------|------|
| **S0** | spec expansion（CLAUDE.md / ADR-0003 / issues） | ✅ |
| **S1** | player registry（`core/player*.py`, `gui/player_registry.py`） | ✅ 実装済 |
| **S2** | session + hand-based seating（`core/session*.py`, ADR-0006/0007） | ✅ core 実装済 + **schema `1.0` frozen**（ADR-0019, ISSUE-0005 Resolved） |
| **S3** | ledger + point ledger + settlement core + desktop viewer + CSV export（`core/ledger*.py`, `gui/ledger_view.py`, `output/ledger_csv_exporter.py`, ADR-0016, ISSUE-0001 Resolved） | ✅ 実装済 + **schema `1.0` frozen**（ADR-0019） |
| **S4** | schema `1.0` freeze（session/seat/hand_ref + ledger/point + settlement + viewer/order model）+ settlement partial-paid | ✅ **実装済**（ADR-0019, ISSUE-0005 Resolved）。**partial-paid 実装済**（ADR-0023, settlement schema `1.1`）。auto ledger = buy-in 金額プリセット（ADR-0026） |
| **S5** | cross-app contract / sync boundary（local↔API client 分離） | 🟡 **read + staff write boundary 実装済**（ADR-0020: repository interface frozen + viewer API（M1）+ Python `ViewerApiClient` + mobile mock/HTTP。ADR-0021: スタッフ会計 write を `/api/staff/...` に staff shared token で公開 + `LedgerRepository` thread-safe 化。on-demand pull / 単一書き手）。ADR-0022: 双方向 sync = state-based merge（`core/sync.py` 可換・冪等の UUID union + 単調解決）+ `/api/staff/sync/{snapshot,merge}` で **複数書き手 + 収束マージ**に拡張。**残**: player rename 伝播・hand log の file-level union・auto-trigger, player PIN（後続 ADR） |

### player 向け参照トラック M（viewer API / mobile / 注文）

| Phase | スコープ | 状態 |
|-------|---------|------|
| **M1** | read-only viewer API（`api/`, ADR-0017） | ✅ 実装済 |
| **M2** | mobile viewer（`mobile/`, Expo/RN, ADR-0017） | ✅ 実装済 |
| **M3** | seat 選択 GUI 結線（= S2.x E3, ISSUE-0006 Resolved） | ✅ 実装済（verify-v1 の `gui/seat_selection.py`） |
| ~~M4~~ | ~~cash-only ledger 先行~~ | ❌ **破棄**（S3 に一本化, 統合時） |
| ~~M6 / M7~~ | ~~point 連携 / settlement~~ | ✅ **S3 で実装済に合流** |

### hand core トラック R（rules-aware reconstruction, S/M と直交）

| Phase | スコープ | 状態 |
|-------|---------|------|
| **R0–R5 + G** | pokerkit を live ルール権威に / actor 推定 + silent-fold 合成 / `apply_corrections` / 決定的 record/replay + golden fixtures + hand/action schema freeze + live 既定切替（ADR-0009/0010/0011/0012） | ✅ 実装済（R1→R2→R3(D1/D2a/D2b/D3)→R4→R5→G 完了） |

### 残作業（次にやること）

1. ~~**schema `1.0` freeze（S4）**~~ → **✅ 完了（ADR-0019, ISSUE-0005 Resolved）**: 全 model を `1.0` freeze。
   次の最優先は下の #2（実機 E2E）。
2. **実機 E2E（Phase H / 最優先）**: クリーン環境で 音声→JSON/PHH の 1 ハンド通し + PN5180 RFID 実機 +
   `--ledger`（viewer_api.enabled）+ スマホ注文の通し確認。
3. **PN5180 / ESP32-S3 firmware ↔ Python 契約固定**（ISSUE-0014 / 0015）: USB descriptor / reader_name /
   ATR / 8B UID の凍結。
4. ~~**S5 cross-app boundary（read + staff write + 双方向 sync）**~~ → **✅ 完了（ADR-0020 / 0021 / 0022）**:
   read = repository interface frozen + `api/client.py:ViewerApiClient` + round-trip test。
   write = スタッフ会計 write を `/api/staff/...` に staff shared token で公開 +
   `LedgerRepository` thread-safe 化（ADR-0021）。双方向 sync = state-based merge（`core/sync.py`,
   可換・冪等の UUID union + 単調解決）+ `/api/staff/sync/{snapshot,merge}`（ADR-0022, 複数書き手 +
   収束マージ）。**残**: player rename 伝播（`updated_at` additive）・hand log の file-level union・
   定期 auto-trigger、player per-player アクセス制御（ISSUE-0019 PIN 再評価、別 ADR）。
5. **settlement / ledger 拡張**（**実装済**）: partial-paid（ADR-0023, `record_payment` / settlement
   schema `1.1`）、確定 commit + paid/unpaid/partial 切替（S4 GUI 精算パネル）、buy-in 金額プリセット
   （ADR-0026, `config.ledger.buyin_presets` + `--ledger` ボタン + staff API）。
6. **R 系の後続**: 派生 confidence の重み較正（golden fixtures 由来）、camera 源の統合。
7. **player 本人確認の進化（ADR-0025 方針 / ADR-0027・0028）**: player_id を内部不変キーに保ち、
   認証を additive レイヤで重ねる — L0 name-pick（済）→ **L1 per-player PIN = ✅ 実装済（ADR-0027）**:
   node-local `player_credentials.json`（PBKDF2 + lockout、read API / sync 非対象）+ `core/auth_token.py` の
   stateless 署名トークン + player principal 解決レイヤ。config `viewer_api.player_auth` 既定 off で後方互換。
   → **L2 外部 IdP = 🔲 設計済・未実装（ADR-0028 詳細設計 + ADR-0029 運用設計）**: LINE / Google OIDC、
   `(provider, subject)→player_id` の `auth_identity`（多対一・player_id は外部 sub から導出しない）。運用は
   **会場 source-of-truth + cloud は player ミラー**（cloud は会計を originate せず signup/閲覧/注文+sync のみ
   公開）/ マネージド PaaS / LINE+Google / secret は PaaS env / PII 最小（APPI, sub のみ）/ cloud は会計 write
   無効・CORS 絞り（ADR-0029）。前提の **player merge は実装済（ADR-0030, alias/tombstone + read-time
   canonicalization）**。将来プレイヤーが LINE/Google でサインアップできる土台。L2 実装の残: env override /
   cloud モード config / レート制限 / OIDC コード本体（ADR-0028）。
8. **未実装の単機能**: Vosk 代替 ASR、ディーラーボタン自動回転 / SB-BB 自動 post。

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

> 注: 現時点で実装済なのは hand logger core / S1 player registry / **S2 session + seating core** /
> **S3 ledger + point ledger core**（`core/session*.py` / `core/ledger*.py`）。本節の S4 以降・
> desktop ledger 画面・mobile・sync はすべて **planned / future scope**。S2/S3 schema の
> `1.0` freeze も未了（ISSUE-0005 が上流 blocker）。

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
   **draft 済 + core 実装済（ADR-0016, `core/ledger*.py`）**: 残高 = point ledger の fold、
   `entry_fee` kind 追加、`ledger.json` 永続形・`entry_id` 採番（UUID4 hex）は core について確定。
   schema `1.0` freeze は上流 session schema freeze（ISSUE-0005）後。
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
  - WS2: desktop の session/seating 別画面。**read-only viewer 実装済（WS2-α）**
    （`gui/session_viewer.py`, `main.py --sessions`, § Session / Seating Viewer）。編集系 UI は未着手。
  - WS3: mobile の session 画面。**実装済**（`mobile/` の MySessions/MyHands = M2、viewer API 経由 read-only）。
- **Blockers**: seat_assignment を hand-based にする設計確定（**ADR-0006 済**）。hand_id の cross-app 形
  （**ADR-0006 で `(session_id, hand_id)` 複合キーに確定**）。`session_id` 採番・永続形は
  **ADR-0007 で core について確定**。残: hand logger 接続・seat change UI 要件（ISSUE-0005）。
- **Done criteria**: session 開始/終了と hand 単位 seat snapshot が core で確定（**達成: WS1 core**）。
  desktop は read-only viewer まで実装（WS2-α、編集系 desktop UI は未着手）、mobile は M2 で実装済。
  schema `1.0` freeze は ISSUE-0005 残項目後。
- **Phase 2.x（hand logger 接続, planning 済 / 実装 planned）**: 既存 hand logger world
  （`HandSummary` / `JsonWriter` / `PHHExporter` / `IntegrationThread`）と S2 core を **段階接続**する。
  方針は **Pattern A（write-through, additive）**：hand logger が `SessionRepository` に依存し、
  hand 開始時に `assign_seat` バッチを呼ぶ。`HandSummary.players[i]` に `player_id` を additive 追加、
  `session_id` を session レイヤの UUID4 hex に切替、PHH は無改変、`hand_ref` は session レイヤ側に住む。
  詳細は **ADR-0008** / `docs/contracts/hand-integration.md`。UX は **ISSUE-0006（E3 で Resolved）**、
  legacy log 取り込みは **ISSUE-0007**。Phase 細分:
  - 2.1: schema sketch + `config.session_layer.enabled` フラグ planned。
  - 2.2: `main.py` session_id 切替 + `assign_seat` 連携 + `HandSummary.player_id` additive。**E1+E2-core 実装済**。
  - 2.3: seat 選択 GUI（registry 連動）。**実装済（E3, ISSUE-0006 Resolved）**: `gui/seat_selection.py` +
    dashboard「座席設定」ボタン + `set_seat_player_map` + `main.py` 結線（UUID4 session_id）。
  - 2.4: legacy log reconciler（任意, ISSUE-0007）。

### Phase 3 — ledger and points

- **Goal**: `ledger_entry`（buy_in/rebuy/add_on/order/adjustment/entry_fee）と
  `point_ledger_entry` を扱う。
- **Prerequisites**: session 契約（S2）+ ledger/point schema 凍結 + ISSUE-0001（point 残高の
  source of truth）の決着。
- **契約状況**: draft 整備済（ADR-0016, `docs/contracts/ledger-overview.md`、2 schema v0.1、
  fixtures、`_MODELS` 登録）。`1.0` freeze は上流 session schema freeze（ISSUE-0005）後。
- **Parallel tasks**:
  - WS0: ledger_entry / point_ledger_entry schema（cash+point 併用、order 明細）。**draft 済**。
  - WS1: ledger / point ledger repository/service + 残高計算。**core 実装済**
    （`core/ledger.py` / `core/ledger_repository.py`, ADR-0016、
    `tests/test_ledger_repository.py`、point/settlement 含む）。
  - WS2: desktop の ledger 入力・中間集計（buy-in 合計 / 注文合計）別画面。**実装済**
    （`gui/ledger_view.py`, `main.py --ledger`, S3.2）。
  - WS3: mobile の ledger 画面。**実装済**（`mobile/` の会計画面 = M2、viewer API 経由 read-only）。
- **Blockers**: ~~ISSUE-0001（残高 source of truth）~~ → **ADR-0016 で決着**
  （残高 = point ledger の fold、計算者は core のみ、grant 冪等性は idempotency_key）。
- **Done criteria**: cash+point 併用・point 不足の cash 補完・entry fee cash only が core で
  enforced（**達成: WS1 core**）。両 front-end の中間集計表示も実装済（desktop=S3.2 / mobile=M2）。**達成**。

### Phase 4 — settlement（core + partial-paid 実装済 / schema `1.1`）

- **Goal**: session 終了時に player ごとの `session_settlement`（net due to store / paid-unpaid）を確定。
- **状態**: settlement core（compute / commit / paid-unpaid）と CSV export は **S3.1/S3.3 で実装済**
  （`core/ledger_repository.py:compute_settlement`/`commit_settlement`/`set_payment_status`,
  `output/ledger_csv_exporter.py`, `main.py --export-ledger`）。mobile/API では `compute_settlement`
  由来の中間集計を read 表示（M1/M2）。
- **実装済**:
  - WS0: session_settlement schema `1.0` 凍結（ADR-0019）→ **partial-paid で `1.1`**（ADR-0023,
    optional `paid_amount` + enum `partial` 追加 = additive）。
  - WS1: **partial-paid core**（`paid_amount` additive・`payment_status` 単一導出・`record_payment`・
    `from_dict` 後方互換推定, ADR-0023）。
  - WS2: settlement の **GUI からの確定（commit）+ paid/unpaid/partial 切替**（`gui/ledger_view.py`
    精算パネル: paid/unpaid トグル + 受領額入力 `record_payment`,
    `tests/test_ledger_view_gui.py::TestSettlement`）。staff API `PUT .../payment` +
    `ViewerApiClient.record_payment`。
  - WS3: mobile が自分の精算状況（確定/未確定・支払済み/一部支払い/未払い）を表示（`MyLedgerScreen`、
    viewer API の player ledger summary に `settled`/`payment_status`/`settled_at`/`paid_amount` を additive）。
  - **buy-in 金額プリセット（ADR-0026）**: スタッフが buy-in 記帳時に店設定の整数円プリセットから
    金額を選んで prefill（`config.ledger.buyin_presets` / GUI ボタン / staff API
    `GET /api/staff/buyin-presets`）。schema / 業務ルール変更なし・additive。これが「auto ledger
    生成」の最終形（hand 結果からの chip→円換算は ADR-0016/0026 で **作らない**）。
- **残**: なし（auto ledger 生成は ADR-0026 で buy-in プリセットとして決着）。

### Phase 5 — sync / cross-app contract hardening

- **Goal**: 同一プロセス前提から、別プロセス / 別アプリ + API/sync へ移行できる boundary を切り出す。
- **read boundary 実装済（ADR-0020）**:
  - WS0: repository / service interface 契約を **frozen**（freeze order #6, `repository-interfaces.md`）。
    同期方式は **on-demand pull**（push/event/双方向 auto-sync は持たない）、衝突は **単一書き手 +
    reload-on-read** で回避。ID は app 内採番 UUID で backend 非依存に安定。
  - WS1: viewer API の **Python client を分離**（`api/client.py:ViewerApiClient` = mobile `HttpRepository`
    の Python 版）。API↔client の round-trip 契約 test（`tests/test_viewer_api_client.py`）で drift 検知。
  - WS3: mobile は `ViewerRepository` に mock/HTTP を注入して UI 無改修で切替済（M2）。
  - **Done criteria 達成**（read）: front-end が interface のみに依存したまま local↔API を切替できる
    （mobile = TS、Python = `ViewerApiClient`）。ID が backend を跨いで安定。
- **残（後続 ADR）**: write/sync 拡張（注文以外の write を HTTP に出す / 双方向同期）。認証
  （ISSUE-0019 の PIN 再評価）・衝突解決方針が前提。desktop の API client 化は任意（現状 local 直結で十分）。

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
- ESP32 停止・WiFi 切断時（HTTP, optional secondary）/ ESP32-S3 USB 切断・USB CCID 再列挙時
  （PC/SC, canonical 移行後）→ `rfid.enabled=false` で RFID なしモード継続動作（詳細挙動は ISSUE-0015）
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
pip install ".[api]"                         # viewer API を使う場合のみ (M1)
python main.py --cli                         # CLI モード (hand logger)
python main.py                               # GUI モード (hand logger)
python main.py --players                     # Player Registry 画面 (S1, 別画面)
python main.py --sessions                    # Session / Seating Viewer (WS2-α, read-only, 別画面)
python main.py --ledger                      # Ledger Viewer/Editor + 注文確定 画面 (S3.2/M5, 別画面)
python main.py --export-ledger logs/ledger_export  # settlement / cashflow CSV 出力 (S3.3)
python main.py --viewer-api                  # player 向け読み取り専用 viewer API (M1, ADR-0017)
pytest tests/ -v --ignore=tests/test_vision.py   # CI と同じ（vision レガシー除外）
python tools/replay_hand.py tests/fixtures/reconstruction/silent-fold  # 決定的 replay (F1)
python main.py --export-phh logs/session_xxx.json
# ローカル QA（実機なし。docs/manual-qa-checklist.md 参照）
printf 'ハンド開始\nチェック\nシート1 ウィナー\n' | python tools/play_hand_text.py - --seats 3  # mic 不要のテキスト駆動再構築
python tools/simulate_rfid.py register-demo && python tools/simulate_rfid.py board Ah Kd Qs  # 実機なし RFID 注入
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
