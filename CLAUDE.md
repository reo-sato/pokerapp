# Poker Hand Logger — CLAUDE.md

## 応答言語

- **ユーザーへの返答（チャットのメッセージ）は常に日本語で書く**（オーナーの指示, 2026-09-25。英語で返したときに
  2 度「日本語で」と指摘あり。2026-10-01 にも長い作業のあとの返答を英語で書いて 2 度「日本語でね　常に」と指摘あり。
  同日の夜にも、作業の途中経過の一言・裏で回した処理の通知への返事・完了の報告を英語で書いて「日本語でね」と指摘あり
  — ツールの結果を受けた直後・途中経過・作業の報告・短い返事も含め、例外なく日本語）。コマンドは従来どおりコピペでそのまま実行できる 1 行の形で示す。

## 店舗ログのメモ

- 店舗のログ（pack_logs の zip）を受け取ったら、**真のアクションの各ハンドのメモ（`<sid>.ground_truth.json` の
  `notes` = 入力画面の「気づいたこと」）を毎回すべて読み、要望・仕様として扱う**（オーナーの指示, 2026-09-29。
  メモに書かれた要望を読み落とし、あとで直接頼まれたことがある）。`python tools/eval_store.py <zip>` がセッションごとの
  先頭に全部出す。

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
│   ├── ui-feature-inventory.md    ← UI 実運用機能の棚卸し台帳（◎/○/△ 優先度 + 着手状態。◎○ は 2026-07-12 全消化、残は △ 群）
│   ├── estimator.md               ← 推定器（事後推定, ADR-0056）のいまの設計・監査の推奨ごとの対応・文書の地図
│   ├── installation.md            ← エンドユーザー: インストール手順 (Phase I)
│   ├── cloud-setup.md             ← クラウドの準備（Google Cloud・Supabase の登録 → 置く → 店舗 PC をつなぐ, ADR-0067。進み具合に合わせて書き足す）
│   ├── usage.md                   ← エンドユーザー: 使い方・読み上げ語彙・設定 (Phase I)
│   ├── troubleshooting.md         ← エンドユーザー: 困りごと対処 (Phase I)
│   ├── plan-board.md              ← 並行している改善計画の台帳（トラック・ブランチ・オーナー待ちの決めごと・ぶつかり。状態が変わったら更新）
│   └── decision-log.md            ← ADR / 主要 issue の索引
├── sprc_v4.docx                   ← 仕様書（要件定義）
├── claude_v4.docx                 ← 旧仕様書（参考）
├── install.cmd / update.cmd / uninstall.cmd   ← Windows ワンステップインストーラの入口 (ダブルクリック, ADR-0057)
├── start_logger.cmd / start_monitor.cmd / start_viewer.cmd / start_ledger.cmd / rfid_check.cmd ← 店舗 PC 用ランチャ (venv 経由。デスクトップのショートカットが指す。ASCII のみ。start_viewer = お客さん向けハンド閲覧, ADR-0059)
├── start_truth.cmd / start_script.cmd / start_cards.cmd ← 真のアクション入力（`tools/ground_truth_ui.py --host 0.0.0.0 --port 8791`, ADR-0043 追記）/ 台本のハンド 声だけ（`main.py --cli --log-file --script voice`）/ 札の確認（`--script cards`）
├── pack_logs.cmd / pack_logs_audio.cmd ← ログをまとめる（`tools/pack_logs.py` → デスクトップの `pokerlogs_<日時>.zip`）/ 音声付き（`--audio`, 25 MB ごとに `_1of3.zip`… に分ける）
├── server_autostart.cmd / start_server.cmd ← 店舗サーバー（`--serve`）の自動起動の登録（`installer/store_server_task.ps1`, `remove` で消す）/ 手で起動して確かめる (ADR-0065)
├── installer/
│   ├── install.ps1                ← 本体: Python 3.12 導入 → venv → pip install -e .[pcsc,api]（+ 第 2 の耳 .[ear] は失敗しても続ける）→ config → モデル先読み → ショートカット 11 個 → 動作確認 (-Update / -Uninstall / -DryRun)
│   ├── bootstrap.ps1              ← 1 行インストール (irm … | iex): zip を C:\PokerHandLogger に展開して install.ps1 を実行
│   ├── store_server_task.ps1      ← 店舗サーバーをタスク PokerStoreServer に登録（サインイン時 + 5 分ごと、pythonw、管理者不要、-Remove で消す, ADR-0065）
│   └── rfid_relay_task.ps1        ← RFID の中継をタスク PokerRFIDRelay に登録（SYSTEM・PC の起動時、管理者で実行、更新後はもう一度、-Remove で消す, ADR-0070）
├── main.py                        ← エントリーポイント (--cli / GUI / --players / --sessions / --ledger / --export-ledger / --viewer-api / --serve / --version)
├── config_default.json            ← デフォルト設定テンプレート
├── rfid_cards.json                ← tag_id → card_code マスタ
├── players.json                   ← player registry 永続ファイル (.gitignore, S1)
├── sessions.json                  ← session + hand-based seating 永続ファイル (.gitignore, S2)
├── ledger.json                    ← session ledger + point ledger 永続ファイル (.gitignore, S3)
├── order_requests.json            ← 注文リクエスト永続ファイル (.gitignore, M5)
├── player_credentials.json        ← player PIN credential 永続ファイル (.gitignore, node-local, L1 PIN ADR-0027)
├── auth_identity.json             ← 外部 IdP ↔ player_id バインディング (.gitignore, node-local, L2 ADR-0031)
├── menu.json                      ← 注文メニューマスタ (コミット済みサンプル, 店側で編集, M5)
├── pyproject.toml                 ← パッケージ定義 (core / [pcsc] / [vision] / [api] / [ear] = 第 2 の耳 / [dev] / [cloud], entry: pokerapp, H1)。pytest の設定（印 `slow`・1 件 20 秒の打ち切り）
├── requirements.txt               ← core runtime 同期コピー (vision 除外)
├── requirements-dev.txt           ← テスト依存 (numpy/pokerkit/jsonschema/fastapi/httpx/pytest/pytest-xdist/pytest-timeout/ruff/psycopg, CI が使用)
├── .github/workflows/ci.yml       ← CI: 2 ジョブを同時に = 速い組（ruff + `pytest -m "not slow" -n auto`、Postgres 16 つき）/ 遅い組（`pytest -m slow -n auto`）。skip 0, vision 除外, junit を残す
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
│   ├── sync.py                    ← state-based merge 純粋関数 + snapshot I/O (S5 双方向 sync, ADR-0022/0024/0032)
│   ├── sync_scheduler.py          ← SyncScheduler (sync 定期 auto-trigger, clock 注入, ADR-0032)
│   ├── auth_token.py              ← player principal の stateless 署名トークン (L1 PIN, ADR-0027)
│   ├── player_credential_repository.py ← PlayerCredentialRepository (PIN ハッシュ PBKDF2 + lockout, node-local, ADR-0027)
│   ├── auth_identity.py           ← AuthIdentity ((provider,sub)→player_id, L2, ADR-0031)
│   ├── auth_identity_repository.py ← AuthIdentityRepository (node-local, sync 非対象, L2, ADR-0031)
│   ├── oidc.py                    ← OidcProvider 抽象 + FakeOidcProvider + resolve_player_for_claim (L2, ADR-0031)
│   ├── cloud_sync.py              ← 店舗 PC → クラウドの送り手 `CloudSyncer`（変わったレコードだけ・`cloud_sync_state.json`・失敗で間を空ける・最初にクラウドの番号に合わせる。店舗サーバーの中で動く, ADR-0067 2b）
│   ├── cloud_payload.py           ← 店舗 PC → クラウドの同期で送る形（種類・キー・送る前に落とす項目・指紋, ADR-0067。送り手と受け手で共通）
│   ├── atomic_io.py               ← atomic_write_json(fsync) + read_json_file(破損退避) (B2/B7)
│   ├── version.py                 ← いま動いている版（`installer/installed.json` か git → branch / commit / release）。`/api/health`・`main.py --version`・店舗サーバーのログ (ADR-0066)
│   ├── store_lock.py              ← StoreWriterLock: 会計の書き手を 1 プロセスに限る OS のファイルロック（`--serve` / `--ledger`, `store_writer.lock`, ADR-0065）
│   ├── backup.py                  ← データ JSON のバックアップ (B2)
│   ├── positions.py               ← ディーラーボタン / ポジション名の純粋ロジック (FR-05b, ISSUE-0032)
│   ├── table_state.py             ← TableState (RFID だけから導く 卓状態: カード/有効席/ストリート, ADR-0056 D5)
│   ├── showdown.py                ← ショーダウンの役判定 + pot ごとの配分（RFID の手札とボード, pokerkit, ADR-0062）
│   ├── bet_sizing.py              ← 賭けの額の事前の重み（ポットに対する大きさ。engine の額の選び方と推定器で共通, 2026-10-01）
│   ├── hand_estimate.py           ← 推定（`logs/<sid>.estimate.json`）をライブの記録に重ねる（読む側 = viewer API・真のアクション入力・PHH, ADR-0056 D1）
│   ├── ground_truth.py / ground_truth_repository.py ← 真のアクション（ground truth, LWW per-session, ADR-0043）
│   ├── hand_correction.py         ← HandCorrection + apply_hand_corrections オーバーレイ (B4, ADR-0036)
│   └── hand_correction_repository.py ← ハンド訂正 append-only ストア (B4, ADR-0036)
│
├── audio/
│   ├── recorder.py                ← AudioThread (PyAudio + faster-whisper + Silero VAD。マイクが開けない・切れたら開き直し続ける)
│   ├── recognizer.py              ← parse_action(s), parse_amount(), apply_corrections()
│   ├── phonetic.py                ← 音の近さでアクションの語を読む（辞書に無い書き起こしゆれ, `fuzzy_keyword`）
│   ├── devices.py                 ← マイクを名前で選ぶ（`audio.device_name`）+ RDP の音声の行き先の判定と案内
│   ├── second_ear.py              ← 第 2 の耳（ReazonSpeech k2 v2 の ONNX 版）: 自由に聞いた文 + 候補ごとの確からしさ (ADR-0069)
│   └── short_words.py             ← 短い語の分類器（第 2 の耳の点数 → 雑談 / コール / チェック / フォールド / 額。重み short_word_model.json。推定器だけが使う, ADR-0069)
│
├── rfid/
│   ├── reader_thread.py           ← RFIDThread (pyscard PC/SC, canonical: PN5180+ESP32-S3 を USB CCID で公開, ADR-0015。使えるリーダーの見張り・再起動, ADR-0070)
│   ├── relay.py                   ← RFID の中継（SYSTEM のタスクで読んだ札を 127.0.0.1:8792 で渡す・ロガーは中継があれば中継から, ADR-0070）
│   ├── http_receiver.py           ← RFIDHTTPReceiver (HTTP POST 受信, optional secondary: debug/remote 用, ADR-0015)
│   ├── bridge.py                  ← RFID ブリッジユーティリティ
│   └── card_master.py             ← CardMaster (rfid_cards.json ロード・検索)
│
├── integration/
│   ├── engine.py                  ← IntegrationThread, calc_confidence()（ライブの解釈 = 暫定）
│   ├── replay.py                  ← 記録（events.jsonl）の決定的 replay (ADR-0011)
│   ├── world_replay.py            ← 生の観測（札・在否・発話）からハンドを組み直す再生器（推定器 v1 の入力, ADR-0056）
│   ├── estimator.py               ← 推定器 v1（直しを重ねたビーム探索・log 事後確率・要確認・`content_hash`, ADR-0056 / `docs/estimator.md`）
│   └── control_consumer.py        ← 遠隔制御 queue の消費 (ADR-0039)
│
├── output/
│   ├── json_writer.py             ← セッション JSON ログ書き込み
│   ├── phh_exporter.py            ← PHHExporter (PHH 形式エクスポート)
│   ├── ledger_csv_exporter.py     ← LedgerCsvExporter (settlement / cashflow CSV, S3.3)
│   └── table_state_writer.py      ← TableStateWriter (卓状態 snapshot + 履歴 sidecar, ADR-0056 D5)
│
├── api/
│   ├── read_models.py             ← viewer read model (seat_assignment 起点 join + settlement 由来 summary, fastapi 非依存, M1)
│   ├── server.py                  ← viewer API server (FastAPI app factory + uvicorn, M1, ADR-0017。`/` でお客さん向け画面も配信, ADR-0059)
│   ├── client.py                  ← ViewerApiClient (viewer API の Python client = local↔API 分離点, S5, ADR-0020)
│   ├── store_server.py            ← 店舗サーバー（`main.py --serve`, 画面なし）: 会計の書き手の API（`create_store_app` = `--ledger` と共通）+ 卓モニタ（+ 任意で真のアクション入力）を 1 プロセスで、落ちたら起動し直す (ADR-0065)
│   ├── static/player/             ← お客さん向け画面 (mobile/ の web 版) のビルド。コミット済 = 店舗 PC に Node 不要。scripts/build_player_web.py が作り、tests/test_player_web_build.py が mobile/ との drift を CI 検知 (ADR-0059)
│   └── static/staff/              ← スタッフ用 iPad 画面 (staff/ の web 版) のビルド。`/staff/` で配信。scripts/build_staff_web.py が作り、tests/test_staff_web.py が staff/ との drift を CI 検知 (ADR-0064)
│
├── gui/
│   ├── dashboard.py               ← GUIDashboard (hand logger 画面, customtkinter; E3 で座席設定ボタン追加)
│   ├── player_registry.py         ← PlayerRegistryWindow (player registry 画面, S1, dashboard とは別画面)
│   ├── seat_selection.py          ← SeatSelectionDialog (seat→player_id 選択モーダル, S2.x E3)
│   ├── session_viewer.py          ← SessionViewerWindow (session/seating read-only inspection 画面, WS2-α, 別画面)
│   └── ledger_view.py             ← LedgerViewWindow (ledger viewer/editor + 精算確定/paid-unpaid + 注文確定/却下 + buy-in 金額プリセット, S3.2 + S4 + M5 + ADR-0026, dashboard とは別画面)
│
├── cloud/                         ← クラウド（Cloud Run + Supabase の Postgres, ADR-0067。店舗 PC には不要）: `app.py` = 許可した受け口だけのアプリ `create_cloud_app`（health / 店舗 PC からの push・status / 運営の店舗・端末トークン / 閲覧リンクの発行・ログイン / お客さんの自分だけの読み取り）/ `views.py` = 店舗ごとの読み取りの写し（店内と同じ `read_models` を使う）/ `static/player/` = お客さん向け画面のクラウド用ビルド（`scripts/build_player_web.py`）/ `ingest.py` = push の検査と保存 / `records.py` = 種類ごとの検査と merge（`core/sync.py` の解決関数・append-only・送り手の番号）/ `storage.py` = 保存の約束 + メモリ版 / `postgres.py` = Postgres 版 / `migrations/` / `tokens.py` / `python -m cloud` で起動（2a 実装済。閲覧・送り手・置き方は 2b〜2e）
│
├── mobile/                        ← Poker Hand Viewer (Expo/RN, M2, ADR-0017。mock/HTTP repository 切替, web export 配布。L1 PIN / L2 サインアップ UI = AuthScreen, ADR-0027/0031。HandDetail はストリート単位リプレイ表示, ADR-0044)
│
├── shared/
│   └── hand_replay/               ← ハンドリプレイ共有 RN コンポーネントの正本 (ADR-0044。scripts/sync_shared_ui.py で mobile/staff の src/shared/ へコピー配布、drift は tests/test_shared_ui_sync.py が CI 検知)
│
├── firmware/                      ← デバイス firmware。`esp32s3-pn5180-ccid/` = ESP32-S3+PN5180 を USB CCID smart card reader として公開する ESP-IDF (本番 RFID, 契約 rfid-usb-ccid.md v1.11 / rfid-ccid-firmware-checklist.md 準拠。部品の版は `main/idf_component.yml` で固定 = esp_tinyusb 1.7.6・tinyusb 0.19、店舗 PC のビルド手順は README。実機ビルド/検証は host の tools/probe_pcsc.py)
│
├── tools/                         ← 店舗 PC と開発側の道具（主なもの）
│   ├── probe_pcsc.py              ← RFID の診断（list = 台数・使えるリーダーの一覧 / check / watch / raw / restart, 契約 §3-8・§6）
│   ├── rfid_relay.py              ← RFID の中継（serve = タスクが使う / status / watch / restart, ADR-0070）
│   ├── register_cards.py          ← 実カードの UID 登録（run / list / unregister / swap-suits）
│   ├── audio_check.py             ← マイクの確認（list / level / listen / bench = Whisper のスレッド数 / gain = 音量をそろえて聞き直す, ADR-0060）
│   ├── table_monitor.py           ← 卓状態モニタ（:8790, ADR-0056 D5）。analyze_table_state.py = その実測
│   ├── ground_truth_ui.py         ← 真のアクション入力の画面（:8791。進行中のハンド・評価のモード・読み上げ集 /corpus・台本 /script, ADR-0043 追記）
│   ├── read_corpus.py / test_script.py ← 読み上げ集（正解つきの句）/ 台本のハンド（声だけ・札の確認）
│   ├── pack_logs.py               ← ログを 1 つの zip にまとめる（店舗 → 開発側）
│   ├── eval_store.py              ← 店舗のログをいまのコードで再生して評価（真のアクションのメモ・一致率・音声の改善を終える目安・`--timeline` / `--export-fixture`）
│   ├── rescore_audio.py / second_ear.py / fixture_ears.py ← 保存した発話を Whisper で採点し直す / 第 2 の耳で聞き直す / 開発データに耳の結果を足す
│   ├── short_words.py             ← 短い語の分類器を学ぶ・確かめる（eval / train / labels, ADR-0069）
│   ├── estimate.py / estimate_logs.py / bench_hands.py / forward_ledger.py / compare_versions.py / sweep_estimator.py / simulate.py ← 推定器: 読みの選択肢 / ログの推定（`--shadow`）/ 開発データの物差し（前の記録 `tests/fixtures/store/bench_record.json`）/ 前向きの成績表（`first_look.json`）/ 前の版と比べる / 値の掃引 / 聞き取りのシミュレーション（`docs/estimator.md`）
│   ├── llm_hand_prompt.py / llm_candidates.py / bench_llm.py ← 実験: LLM の提案を推定器の候補に足す（`HandWindow.seeds`、キャッシュ `logs/llm_cache/`、事前登録 `docs/dogfood/llm-proposer-preregistration.md`。本番・店舗 PC では使わない, `docs/estimator.md` §9）
│   ├── review_page.py             ← 開発データのハンドを入力画面と同じ形の HTML 1 枚に（店舗 PC を触れないときの見直し）
│   ├── voice_style.py             ← 宣言の声と雑談の声を比べる（調べただけ・推定器には使わない）
│   ├── replay_hand.py / play_hand_text.py / simulate_rfid.py / calibrate_confidence.py / measure_capture_accuracy.py ← replay・テキスト駆動・RFID 注入・confidence 較正・捕捉精度
│   └── set_config.py / backup_data.py / cloud_link.py / release.py ← config の 1 項目 / データのバックアップ / クラウドにつなぐ (ADR-0067) / リリース版 (ADR-0066)
│
├── tests/                         ← pytest テストスイート（`fixtures/store/` = 店舗の開発データ = 真のアクションつきのセッション）
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
（`gui/seat_selection.py`）と main.py 結線は **E3 で実装済**（ISSUE-0006 Resolved）。**`--cli` も同じく記録する**
（ADR-0059, ISSUE-0036）: 起動時に session を作り、入力した名前を `PlayerRepository.find_or_create`（前後空白を
除いた完全一致 = 同じ名前は同じ人）で player に結び付ける（空 Enter の `PlayerN` 席は結び付けない）。
`name <席> <名前>` / `name <席> -` で**次のハンドから**席替え（`seat` にしないのは `seat 3 call` が読み上げ文として
通るため。名前の変更は `rename_seat` を queue 経由で
integration スレッドへ。ハンド途中なら次のハンド開始で反映）。`q` で session を close。

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
  `127.0.0.1`（無認証。LAN 参照は明示変更, ISSUE-0019）/ `bind_port` 既定 8788。`--viewer-api --host / --port`
  が config より優先（店舗 PC のランチャ `start_viewer.cmd` = `--host 0.0.0.0 --port 8788`）。
- **お客さん向け画面も配信する**（ADR-0059）: `create_app(player_web_dir=PLAYER_WEB_DIR)` が `/` に
  `api/static/player/`（`mobile/` の web 版、API = 同じ origin の `/api/`、PIN / LINE・Google と staff 訂正の
  導線なし）を mount する（全 API ルートの後。未知の `/api/...` は JSON 404 のまま）。入口は
  `Cache-Control: no-cache`、hash 入りの `_expo/` はキャッシュ可。`--viewer-api` と `--ledger` の両方が配信する。
- **店舗サーバー**（ADR-0065）: `python main.py --serve` = 画面を持たない会計の書き手（`api/store_server.py:create_store_app`。
  `--ledger` の組み込み API も同じ関数）+ 卓モニタ。待ち受けは `--host`/`--port` > `store_server.bind_host`/`bind_port` >
  `viewer_api.bind_port` > `0.0.0.0:8788`。店舗 PC のサインイン時（通電 → 自動サインイン）にタスク `PokerStoreServer`（`server_autostart.cmd`）で動かす。
  **会計を書くのは 1 プロセスだけ**: `--serve` と `--ledger` は `core/store_lock.py` のロックを取り、取れなければ起動しない。
- **スタッフ用 iPad 画面も配信する**（ADR-0064）: `create_app(staff_web_dir=STAFF_WEB_DIR)` が `/staff` に
  `api/static/staff/`（`staff/` の web 版、API = 同じ origin、置き場所 `/staff` = Expo `experiments.baseUrl` を
  `staff/app.config.js` がビルドのときだけ足す）を mount する（`/` より先。`/staff` は `/staff/` へ転送）。スタッフ
  トークンは埋め込まない（ログイン画面で入力、認可は ADR-0021 のまま）。write は `--ledger` のときだけ有効。
- **別プロセスの hand logger が書いた `sessions.json` / `players.json` を読み直す**（ADR-0059）: `/api/` の要求
  ごとに `SessionRepository.reload_if_changed()`（player も連鎖）が (mtime_ns, size) を比べ、変わっていれば
  読み直す（読めない瞬間は今の内容を保つ）。Windows の共有違反は `core/atomic_io.py` が短く再試行。
- **ホールカードの公開範囲**（ADR-0067 D6, 実装済）: `all`（記録した手札を全員分 = 既定）/ `own_and_shown`（自分の手札 +
  ショーダウンで見せた手札だけ）。店の設定 `viewer_api.hole_cards`、セッションごとの上書き（session schema `1.1` の
  `hole_card_visibility`, スタッフ API `PUT /api/staff/sessions/{sid}/hole-card-visibility` + スタッフ用画面の卓の上）。
  適用は `api/read_models.py:hide_hole_cards` の 1 か所（店内の viewer API とクラウド。スタッフの読み取りには効かない）。
  店内の `/api/sessions/{sid}/hands/{hid}` は `?player_id=`（選んだ人）で自分の手札を出す。

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

- player はスマホから **order_request**（pending）を POST し、**pending のうちは本人が
  キャンセルできる**（status=cancelled, ledger 影響なし, schema `1.0`→`1.1`, ADR-0045）。**ledger には書かれず**、
  スタッフが `--ledger` 画面の確定/却下パネルで**確定**したときに `ledger_entry`（kind=order,
  cash_amount=unit×qty, `order={item_name, unit_amount, quantity}`）が作られ `ledger_entry_id` が
  リンクされる（staff-in-the-loop）。
- **menu master**: `menu.json`。**staff アプリの「メニュー管理」画面から編集**（価格改定・品切れ・
  追加/削除 = `PUT /api/staff/menu` 全量置換, ADR-0046。`MenuMaster` は thread-safe +
  reload-on-read）。menu 外は `unknown_item`、**品切れ（`sold_out`）は `item_sold_out`** で
  注文を弾く。確定時の単価は menu から prefill（スタッフ上書き可）。
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

## ディーラーボタン / ポジション名（P0b, 実装済）

**ハンドごとにディーラーボタンを 1 つ回す**（仕様 FR-05b）。ターン順はアクター推定の最優先の
証拠（FR-26）なので、ボタンが回らないと 2 人超の卓では (n-1)/n のハンドで prior が誤る
（ISSUE-0032 = 収集済みデータを壊していた欠陥）。ADR-0056 の P0b。

### スコープ（現時点）

- ボタンは席番号の昇順に 1 ハンド 1 つ進み、1 周で全席を訪れる。**参加している席の中で回る**（休み
  `name <席> -` / スタック 0 の席はハンドに入らず飛ばす = 仕様 FR-05b、前のボタンの席が抜けたらその次の席。
  2026-09-26）。手動移動は `--cli` の `button <席>` / control `set_button`（仕様 FR-05g。通常の進み方と違えば
  知らせる = 仕様 §9。**ハンドの途中で打つとそのハンドのボタンを直して記録を組み直す**、ハンドの外なら次のハンド）。
  **配った順からの自動の救済**（`engine.button_from_deal`, 既定 true, ADR-0068）: 手札を最初に読んだ時刻（1・2 枚目）が
  別の席をボタンとした配り方に 1 か所の食い違いもなく合い、いまのボタンとは 2 か所以上食い違うときだけ直す（要確認、
  信号 `deal_order`）。**pokerkit backend のみ**
  （legacy は `button_seat=None` / `position_map()={}` = 単純ラウンドロビンのまま、rollback path 不変）。
- `position_map`（seat → BTN/SB/BB/UTG/UTG+1/UTG+2/MP/HJ/CO, 仕様 §6.1）はボタンから決定的に
  導出する。**heads-up はボタンが SB**（`["BB", "BTN"]`。pokerkit の post 位置と実測で一致）。
- ディーラーの **ポジション名の読み上げ**（「BTN、コール」, 仕様 §7）を席番号と同じ明示証拠として
  使う。両方あれば**席番号が優先**。卓に無いポジション名は**無視**する（手番は動かない）。
- 記録は additive: `HandSummary.button_seat` / `position_map`、`ActionRecord.position`
  （schema `hand` `1.3` / `action` `1.3`。`ActionRecord.actor_source` は `spoken_seat` / `spoken_position` /
  `engine_prior` / `unresolved`）。`AudioEvent.position` は sidecar 記録・replay でも往復する
  （記録しないと replay が別 actor を選ぶ）。
- 1 ハンド目のボタン席は `--cli` / GUI のセッション設定で訊く（空 Enter = 最大の席番号 =
  **ボタン導入前と同じ並び**）。卓状態モニタにボタンとポジション名を表示する。

### 構成

| 要素 | ファイル | 役割 |
|------|---------|------|
| 純粋ロジック | `core/positions.py` | 並び（`seat_order_from_button`）/ 回転（`next_button`）/ 名前（`position_names`, `position_map`）/ 別名パース（`parse_position`, `POSITION_ALIASES`） |
| engine | `core/poker_engine.py` | `new_hand` でボタンを進め、ボタンの次の席を先頭に `create_state`。`button_seat` / `position_map()` |
| 明示証拠の解決 | `integration/engine.py` | `_sensed_seat`（席番号 → ポジション名の順）。RFID の検出は**証拠にしない**（ISSUE-0033） |
| 読み上げ | `audio/recognizer.py` | `parse_action` が正準ポジション名を `AudioEvent.position` に載せる（席への解決はしない） |

### 設計上の注意

- **初期ボタン = 最大の席番号**。ボタンの次が SB になるので 1 ハンド目の並びが `sorted(seats)` と
  一致し、ボタン導入前の挙動（既存 golden fixtures）が変わらない。回転はハンド 2 から現れる。
- ポジション名の**席への解決は engine 側**。recognizer はゲーム状態を持たない規約のため、
  正準名を持ち回るだけにする。

### Out of scope（現時点）

- GUI（`gui/dashboard.py`）でのボタン表示・手動指定（CLI / control には有る）。`turn_order` の明示記録
  （`position_map` から導ける）。起動時に決めた席以外への着席（席は操作で決める = オーナー, 2026-09-26）。

---

## 実装状況（現時点）

> **記録の正本は「事後・確率的推定」に移す（ADR-0056, 2026-09-12 決定 / 推定器 v1 は実装済で影で運用・正本への切り替えは評価のあと）**
> 仕様 `sprc_v4.docx`（FR-26/27・§5.4・FR-25/35）は当初からアクター推定を**尤度ベース**と規定しており、
> 現在の**決定的なライブ確定**は ISSUE-0009 の暫定が定着した drift（**ISSUE-0031**）。今後は
> **ライブ経路 = 暫定表示**、**正本 = 事後推定**（pokerkit の合法手列に対する制約付きビーム探索 +
> 観測尤度、決定的）とし、読み取りは **live ⊕ estimate ⊕ staff corrections** で重ねる。
> 実施順序は追記 1 で S0〜S5 に引き直した（P1〜P8 を置き換え）。推定器 v1・生の観測の再生器・物差し・読む側の
> 重ね方は実装済だが、推定はまだ影のファイルに書く（記録の本体にするのは事前登録した評価に通ってから。評価は
> 音声入力の改善のあと = 2026-10-01 オーナー決定、追記 3）。**いまの設計・監査の推奨ごとの対応・文書の地図は
> `docs/estimator.md`**。下の「実装状況」表は **現時点で動いているもの**の記述。
> **P0a 済**: ISSUE-0033（RFID の appear を actor 証拠に使っており配っただけで誤 fold）→ **Fixed**。
> **P0b 済**: ISSUE-0032（ディーラーボタンが回らずターン順 prior が (n-1)/n のハンドで誤る）→
> **Fixed**（§ ディーラーボタン / ポジション名）。
> **2026-09-22 verify-v1 へマージ**: 復元の正当性修正バッチ（ADR-0047〜0050）と合流。**RFID の
> 検出を actor 証拠にしない（P0a）を優先**し、ADR-0049 G3 の該当部分を supersede した。
> schema は `hand 1.3` / `action 1.3` / `reconstruction_event 0.3` に統合。**いまは `hand 1.7` / `action 1.5` /
> `reconstruction_event 0.18`**（どれも additive。経緯は各 schema の description と `docs/contracts/event-replay.md` §3:
> action 1.4 = `raw_text`（ADR-0060）・1.5 = `end_hand` / hand 1.4 = `winner_source`・`showdown`（ADR-0062）・1.5 = `announced_hand`・
> 1.6 = board の `??` と `winner_source=undetermined`・1.7 = `showdown[].announced`（ADR-0068）/ reconstruction_event 0.4 = rfid の
> `replaces`（ADR-0058）〜 0.18 = 札の離脱・配布・ショーダウンの信号と聞き取りの印（parse_flags）の追加）。
> **2026-09-25〜10-08 の店舗テスト**の決まり（卓の進行 = ADR-0068 / 第 2 の耳 = ADR-0069 / RFID の中継と再起動 = ADR-0070 /
> 真のアクション入力の画面 = ADR-0043 追記）。不一致の索引は ISSUE-0039、10/04〜10/08 の直しが開発版に入っていなかった件は
> ISSUE-0040（2026-10-13 のマージ `d3e9071` で解消）。


| 機能 | 状態 | 備考 |
|------|------|------|
| 音声認識 (Whisper) | ✅ 実装済 | `audio/recognizer.py` |
| **音声テストの見える化 (ADR-0060)** | ✅ 実装済（店舗 PC 未確認） | 聞き取った文を**すべて**報告（`AudioThread` の `on_transcript` + INFO ログ。アクションとして読めなかった発話も）→ `--cli` が `[聞き取り] 「…」→ raise 600 席3` と表示。モデルの読み込み中・所要秒数・マイクの状態（番号・名前 / 開けなかった理由）も CLI に出す。**モデル取得の失敗で落ちない**（`WhisperTranscriber.ready` / `load_error`、音声なしで続行）。ハンドの記録に `ActionRecord.raw_text`（そのアクションになった発話。rules-aware 経路と unresolved のみ、合成 fold には無い, action schema `1.4`）。スマホ画面の URL に **`?test`** を付けた端末だけ、各アクションの下に聞き取った文と補正の内容・理由（日本語）を出す（`shared/hand_replay` の `showHeard` / `heardDetails`）。マイク確認 `tools/audio_check.py`（`list` = 番号・方式・16 kHz の可否 / `level` = 入力レベル / `listen` = 本番と同じ経路で聞き取り / `bench` = 保存した発話で Whisper のスレッド数・ビーム幅ごとの時間（config `audio.cpu_threads`）/ `gain` = 保存した発話を音量をそろえて聞き直す）。**記録の完全化**: 聞き取った発話はすべて `logs/<sid>.transcripts.jsonl`（読めなかった・雑音・声ではない音も）、`audio.save_audio=true` で発話の WAV を `logs/audio/<sid>/`（既定 off = 卓の会話も入る）。声ではない音は Whisper にかけない（Silero VAD, `audio.vad_threshold` 既定 0.5）。**マイクを名前で固定**（`audio.device_name`、「\|」区切りの優先順。どれも見つからなければ開かない = 別のマイクで録らない, `audio/devices.py`）。60 秒声が入らなければ電池・受信機を確かめる知らせ。RDP の音声が接続元に回っている（WDM-KS しか見えない）ときは原因と直し方を出す。RDP の音声設定・マイクのプライバシー設定は `docs/troubleshooting.md` |
| **音声の実測への対応 (ADR-0061)** | ✅ 実装済（店舗 PC 再確認は未） | 店舗の `audio_check listen` の実測（ISSUE-0037）と「実運用では席番号を言わない」「遅れても最終的に解釈できればよい」を受けて: **続けて言った複数のアクションを言った順に分ける**（`audio/recognizer.py:parse_actions`。席番号・ポジション名の直前 → 最後の区切り文字の直後 → 次のキーワードの直前の順で切る。9 個以上は繰り返しの聞き違いとみなし 1 件 + `too_many_actions`。「チェックレイズ」は 1 つのレイズ（ただし運用ではチェックレイズも「レイズ N」とだけ言う = 「チェック、レイズ」と区切られると 2 人と区別できない）。音声・CLI の打鍵・`play_hand_text` 共通）/ 「ベッド」= bet・「ホールド」= fold（店舗の実測の書き起こしゆれ）/ **発話を捨てない**（推論待ちの上限なし、`AudioThread.backlog()`）/ **打った入力は認識待ちの発話を追い越さない**（`--cli` は打つ前に話し始めた発話が反映されるまで待ってから積む。聞き取りが 60 秒進まなければ先に積む。待つ間は残りの件数を 10 秒ごとに出す。`q` も）/ 認識に回す有音の下限 0.3 → **0.15 秒**（config `audio.min_speech_sec`、捨てた短い音は `audio_check listen` に表示）。モデルは medium のまま（small は金額を聞き違えた）。席番号を言わない運用では手番の順でアクターを決める。**コール・チェックは毎回言う**（2026-09-29 から = ADR-0068。前の「連続する席の同じアクションは 2 回目以降を言わない」運用は廃止）: 言われなかったチェック / コールを補った行は**いつも要確認**、「チェック」「コール」のあとのベット / レイズは次の手番の人のもの（`reconstruction_event` 0.7 の `reinterpret` は旧記録の再生のためだけに残る）。フォールドは札の離脱（下の「札の離脱でフォールド」）。**店の言い方の読み**（2026-09-25〜10-07）: 数字だけの発話 = ベットかレイズ（いまのベット以下・最小ベット未満・ベッティングの外は記録せず `●` で理由）/ レイズの額は常にトータル / 「チェックアラウンド」= まだ動いていない全員のチェック（ストリートの札の直後 3 秒内なら前のストリートの言い直し）/ 「ヘッズアップ」「スリープレイヤーズ」「3ウェイ」= 残りの人数（ベットに向き合っている人のコールを補う、人数が合わなければ要確認）/ 確認型の疑問形（「コールですか？」）はアクションにしない / 言い直し（同じ額・同じ語・8 秒以内のオールイン）と額の言い直し（`amount_restated`）を次の人にしない / 仮名・崩れた額（「2千500」「7千ロッピャク」「2センテン」、意味の無い単発の語は第 2 の耳と音の近さで額に = `phonetic_amount`、使えない額は使える額に寄せる = `legal_amount`、桁のあとの意味の通らない語 = `garbled_digits`）/ 書き起こしゆれは辞書 + 音の近さ（`audio/phonetic.py`, `fuzzy_keyword`）/ 「失礼しました」= 訂正の合図・「これで終わりです」「コールド」= 聞き違い・「Nヒット」= 役名（ADR-0068）。勝者は自動（ADR-0062。決まらないときだけ `w <席>`、分けたら `w <席> <席>`） |
| **手札を配ったらハンド開始・勝者の自動判定 (ADR-0062)** | ✅ 実装済（店舗 PC 未確認） | 店舗の通しテストで `n` /「ハンド開始」を言わない運用のためハンドが一度も始まらなかった（ISSUE-0038）。**2 席以上に手札が置かれたら新しいハンド**（`engine.auto_new_hand`, 既定 true。2 席以上に 2 枚ずつが `DEAL_STABLE_SEC`=1.5 秒載り続け、ボードのリーダーが空のときだけ = シャッフル・ウォッシュ・ショーダウンのあと席に残った札では始まらない。15 秒窓で片付け中の 1 枚を除く・卓で使っていない席は数えない・集めた札を手札に入れる・前のハンドが未確定なら先に確定・**配る前に話し始めた発話だけを先に反映**（`speech_backlog`。配ってから話し始めた発話は待たない、聞き取りが 60 秒進まなければ先に始める。遅れて始めたときは待つ間に札が離れた席を先に入れる）・アクション前の「ハンド開始」/ `n` は無視・何も起きていないハンドへの札は配り直し = ADR-0058）。ボードは手札が配られてから読み、音声はプレー中（配布〜確定）だけ認識に回す（全員フォールドで確定しても 2 席以上に札が残れば最長 30 秒は書き起こしだけ残す）。ハンドを始めて 5 秒たっても卓で使っていない席に手札があれば「席の設定が違うかもしれません」と知らせる。**スタック 0 の席**は `r` で買い足すまで配られない（配られたら最初の持ち点に戻して要確認）。ボタンの救済（途中の `button <席>`・配った順からの自動 = `engine.button_from_deal`）は § ディーラーボタン。**勝者の自動判定**（`engine.auto_winner`, 既定 true, pokerkit のみ）: ほかが全員フォールド → 即確定（`winner_source=fold`）。ショーダウン（2 人以上残りベッティング終了）はすぐ決めない。**ショーダウンの決まり**（オーナー, 2026-09-26〜10-07 = ADR-0068）: アウトオブポジションから見せ、ディーラーは見せるたびに**役名を必ず言う**（「キングヒット」「エースキングフル」「クイーンジャック」「トリップス」「ナナポケ」など店の言い方。2 人分をつないだ言い方も）→ **役名 = 次に見せる人が見せた**（`showdown[].announced`）。**「フォールド」= 見せずにマック**: 見せる前はアウトオブポジションから順、**見せたあとの「フォールド」= まだ見せていない次の人のマック**（席を言えばその席。見せずにマックした人は手札が強くても負け = 手札ではマックした側が強ければ `mucked_stronger_hand` で要確認、3 人以上の順番推定は `muck_order_assumed`。記録は fold / street=showdown）。残った全員が見せたら、または役名もマックも **8 秒**（`SHOWDOWN_MUCK_SEC`、信号 `showdown_end`）無ければ、ほかに「ハンド終了」・席の無い「ウィナー」・`n`・次の配布のときも **RFID の手札とボードで判定**（`core/showdown.py`、pot ごとに対象の席の最強 = side pot の勝者が別でも `end_hand_awards` で正しく配る、同点は等分・端数はボタンの次から。`winner_source=cards` + `showdown` + 2 人以上なら `pot_awards`）。役名は判定と突き合わせる（違えば要確認、役名だけで決まる勝者が判定と違えばその席 = `winner_source=announced`）。**ショーダウンは必ず「ショウダウン」と言う**（声が無ければ全員降りて終わった = 推定器の証拠。ライブは変えない）。**読めない札ではチップを動かさない**: 読めていない札のありうるすべてで勝者が同じならその勝者（要確認）、変わるなら各席が出した額を返して確定（`winner_source=undetermined`・`winner_seat` null・board の読めない位置は `??`、スマホは「勝者なし」）。それまでに `w <席>` を打てばその席。ショーダウンでないのに決まらないときだけ仮（`estimated`, 要確認）。終了（`q`）では確定していないハンドを同じ規則で閉じて保存する（`session_end`）。確定後の `w` は同じ席ならお知らせ、違えば `winner_after_hand_end` の保留。CLI は `● …` でお知らせ（`on_notice`）。聞き取り: 「ベト」= bet・「ゴール」= call・「ハンド終了」= `end_hand`・**語の直後の仮名の数**（ベトナナ = 7、ロッピャク = 600）。schema `hand` 1.4（〜1.7 = `announced_hand`・`??`・`undetermined`・`showdown[].announced`）/ `action` 1.5 |
| **札の離脱でフォールド (ADR-0068)** | ✅ 実装済（店舗で運用中） | オーナー決定（2026-09-25）「札が保持されたまま口頭でフォールドされることは基本的にない」。**フォールドは RFID で決める**（`engine.rfid_folds`, 既定 true。false = 従来の音声のフォールド）: 席の札が `engine.fold_absent_sec`=3 秒離れたまま戻らない / 卓の中央（ボードのリーダー）を通過 → その席のフォールド（CLI `← 札が離れた` / `← 札が中央を通過`）。**「フォールド」の語と札の離脱は 1 対 1 に組む**（札が離れかけている席があればその席、1 つの離脱が 2 つの語を吸わない、1 つの席に「フォールド」は 1 つ）。語のあと札が残ったまま次の手番のアクション・次のストリートの札・別の席の離脱が来たらその席のフォールド（`spoken_fold`）、札が残ったまま次のストリートの札が置かれたら降ろさずチェック / コール（要確認）。**手番でない席の札が離れた** = 前の人のアクションが聞き取れなかったとみて間をチェック / コールで補う（要確認）。**札が戻ったら**入れたフォールドを取り消して組み直す（3 秒未満の離脱 = 持ち上げ・のぞき見は数えない）。札の離れたあとの「コール」は次の人のもの（`late_departure_retracted_call`）。配る前からの不在・一度も読めていない席は数えない。最後の 1 人を残すフォールドは `FOLDOUT_CONFIRM_SEC`=10 秒待って確定（札が戻る・「ショーダウン」で取り消し）。**ボードの札でストリートを進める**: 札が置かれた時点で閉じていないラウンドは、札が離れた人はフォールド・残る人はチェック / コールで閉じる（要確認）。リバーでベットが無いときの離脱はショーダウンに向けて前に出した（チェック）、ショーダウン中の離脱はマックにしない（見せずにマックは「フォールド」で決める）。**ストリートの札より前に言ったアクション**は前のストリートのもの（札の 3 秒以上前に話した語を組み直す、前のラウンドを閉じてから 6 秒内の語だけ。札の時刻を使うのはフロップ 3 枚が 8 秒内に読めたハンドだけ）。**ターンの札をフロップに入れない**（一番右のボードのリーダーだけで読んだ札はターン以降、フロップの最初の札から 10 秒より後もターン以降 = `rfid.flop_window_sec`、ボードは位置どおり・読めない位置は `??`）。ボードの片付け（10 秒に新しい札 4 枚）での入れ替えは取り消す。札の離脱・戻り・確定・配布・ストリートは `events.jsonl` の rfid の `kind`（`leave` / `return` / `muck` / `confirm` / `spoken_fold` / `deal` / `hand_start` / `street` …）で replay も同じ。口頭の訂正「**失礼しました**」= 直前のアクションかストリートの最初からの言い直しかを続くアクションとの辻褄で選ぶ（要確認） |
| RFID PC/SC 受信 | ✅ 実装済 (canonical) | `rfid/reader_thread.py`（ESP32-S3 USB CCID 経由で PN5180 公開, ADR-0015）。**reader 1 台に複数枚（席 = hole card 2 枚 / board = 1 台に 1〜3 枚）**: Get UID の連結応答（8B×k, 16/24/32B）を `rfid/bridge.py:split_uid_response` で分割し、**UID 単位の集合デバウンス**で増えた UID ごとに 1 event。**board reader は全台で 1 つの論理ボードを共有**し、`board_index`（1..5）は **全台を通した検出順** = 配った順で決まる（どの台が読んだかに依存しない。flop を「左 1 枚 + 真ん中 2 枚」で置いても 1,2,3、turn/river がどの台でも 4/5。5 枚超過は WARN + 位置なし。**1 枚 = 1 位置**（既に位置を持つ UID には常に同じ位置を返す。隣接リーダーの磁界が重なると 1 枚を 2 台が読んでデバウンスが台ごとに発火するため, ISSUE-0025）。**ハンド内は append-only**（一度与えた位置はカードが盤上から消えても解放しない）＋ **解放は「新ハンド」と「明示のミスディール訂正」だけ**（`RFIDThread.reset_board_positions` を `IntegrationThread(on_new_hand=...)` から engine の `_board_positions` クリアと同じ場所で呼ぶ。1 位置だけの解放は `forget_board_position` / 席は `forget_seat_cards` = § ミスディール訂正, ADR-0054）。ポーカーではハンド中にボードの札は減らず engine の board も縮まないので両者を同じ規則に揃える — 解放すると一瞬の読み落ちや札の入れ替えで空きスロットを別の札が奪い、同じ札が 2 か所に並んで枚数が水増しされる（ISSUE-0026）。**卓の流れに合わせた解釈（ADR-0058, 本番の起動経路で既定有効）**: そのハンドで**席に記録した札はボードの札にしない**（フォールドした手札は卓の中央 = ボードのリーダーの上を通る。その席の「マック」として記録）/ **ボードの札は `commit_sec`=2 秒載り続けてから確定**（一瞬の通過を除く。時刻は最初に見えた時刻。確定前の札は 3 秒までの途切れを許す = リーダーの境目・重ね置きで途切れながら読める札。見え始め・読み直しは INFO ログ）/ **配り直しは入力なしで反映**: 前の札が `release_sec`=6 秒以上見えず新しい札が載り続けたら差し替え。ボードは **1 枚だけの差し直し**（前の札が見えなくなった近くへ置かれた札。前の札が `redeal_confirm_sec`=3 秒見えなければ差し替え、戻れば読み落ち = 次の位置。flop の札は**前の札を読んでいたリーダー** + 消えてから `redeal_window_sec`=30 秒以内、**最後に配った turn / river** は**同じか隣のリーダー**・時間は問わない（位置がリーダーの境目にある）。**そのハンドで一度でも読めなくなって戻った札は差し直しの対象にしない**（読みにくい位置の札は載ったまま長く読めなくなる。確定前の途切れも数える）。**差し替えた札が戻ってきて、差し替えた札（最後に配った札）も載っていれば差し替えを取り消す**（戻った札を元の位置へ、差し替えた札を次の位置へ。戻った札が前と同じか隣のリーダーで読めたときだけ）。別のリーダーの読み落ち中に来た次のストリートの札は次の位置のまま）/ **flop 全体の配り直し**（flop が全部消えたら時間によらず）/ **5 枚埋まったボードの差し直し**（5 枚のうち 1 枚だけ `release_sec` 消えていれば、新しい札を**その位置に入れる** = river のあとで turn を差し直しても turn と river が逆にならない。消えたあとに空き位置へ入れた札があれば、それを見逃した差し直しとしてその位置へ移して詰め、新しい札を 5 枚目に = 読みにくい札を本当に差し直したときもここで直る。消えた札がまだ `release_sec` に達していなければ待つ）。ボードの札を空き位置に入れるたびに INFO ログ「N 枚目にしました（左から K 台目）— 見えていない札: …（読み直し N 回）」。同じハンドで別の席に記録した札は元の席から消えたら移す。差し替えは `RFIDEvent.replaces` で engine へ、そのハンドは `needs_review`（ADR-0058 + 追記 1〜5 / 契約 v1.10 §4）。`probe_pcsc` と既存テストは従来の解釈（最初に見えた瞬間に確定）。**ボードに同じカードが 2 枚以上見えたら WARN + needs_review**（1 組のデッキではあり得ない = `rfid_cards.json` 重複登録か誤読みのサイン）。board ログには `tag=`（UID）も出す。config の `index`/`cards` は廃止 = 残っていても無視 + 起動 WARN + lint 指摘。board reader は左から右の順に並べて書く。ADR-0053 / 契約 v1.3 §4, ISSUE-0024）。**物理リーダーは Get UID の P2（config `reader`, 既定 0）で選ぶ**（Windows は CCID slot を 1 つしか公開しないため reader 名は 1 個。`(name, reader)` が一意、範囲外は `6A 86` で WARN、台数は `FF CA 00 FF 00`）。**接続は reader 名ごとに 1 本持続**し 1 接続に N 個の APDU（ADR-0052, 契約 v1.2）。**つながらなくても止まらず 5 秒ごとに試し直す**（中継があとから動いても・USB を挿し直しても起動し直さずに使い始める。CLI につながり具合）。**読み取り装置の見張りと再起動（契約 v1.11, ADR-0070）**: 使えるリーダーの一覧（`FF CA 00 FE 00` → 2 byte LE + `90 00`、初期化中は `69 85`、古い firmware は `6A 86`）で使えないリーダーを `●` で知らせ、ボードのリーダーが 2 ハンド続けて 1 枚も読まなければ知らせる（一覧に出ない不調）。**再起動の命令**（`FF CA 00 FD 00` → `90 00` を返してから `esp_restart`）を CLI の **`rr`**（卓に札が無い状態が 2 秒続いたら送る）と、使えないリーダーがあれば**卓に札が無い状態が 10 秒続いたときだけ自動**で送る（`rfid.auto_restart`, 既定 true、1 つの不調につき 2 回まで・60 秒空ける）。**卓の ESP32 は給電アダプタで動く**ので、PC との USB の抜き挿しでは再起動しない（リーダーの準備は起動時だけ = 途中でコードが抜けた・挿し替えたリーダーは、再起動の命令か給電アダプタの抜き挿しまで読まない。案内の文もそう出す, 2026-10-06）。手札を最初に読んだ時刻を札ごとに覚える（ボタンの救済の `deal_order`）。engine は無改修。**実機 2026-09-11（10 台 ready）**: `probe_pcsc list`（`physical readers: 11`, 11 件 matched）/ `check` 11 PASS / `watch` で席 8 台 × 2 枚 + board の 22 タッチが config どおりの `seat N [rK]` に対応（ISSUE-0022 Fixed 確認） |
| RFID HTTP 受信 | ✅ 実装済 (optional secondary) | `rfid/http_receiver.py`（debug/remote 用, ADR-0015） |
| ESP32-S3 USB CCID firmware ↔ Python 契約固定 | ✅ 実装済 (契約 freeze + 実機確定値追記 + v1.11) | `docs/contracts/rfid-usb-ccid.md` **v1.11**（ADR-0034 で freeze / ADR-0052 で v1.2 / ADR-0053 で v1.3 / ADR-0054 で v1.4 / ADR-0058 で v1.5〜v1.10 = 卓の流れに合わせた解釈 / ADR-0070 で v1.11 = 使えるリーダーの一覧 `FF CA 00 FE 00`・再起動 `FF CA 00 FD 00`、P2 の 0xFD〜0xFF は予約, ISSUE-0015 Fixed / ISSUE-0022 Fixed / ISSUE-0024 Fixed）: USB descriptor / reader_name 安定規約 / **CCID slot は 1 つ固定・物理リーダーは Get UID の P2（`FF CA 00 <k> 00`、範囲外 `6A 86`、台数 `FF CA 00 FF 00`）** / 役割は host config が source of truth / ATR-agnostic / UID 4-7-8B 正規化 / hot-plug / reader 名ごと 1 接続持続。config `pcsc_readers`(list, 要素に `reader`) 分離。firmware 実装者向け MUST チェックリスト = `docs/rfid-ccid-firmware-checklist.md`（各項目を `probe_pcsc` で受け入れ確認）。**実機確定値（2026-06-22）**: VID=0x303A PID=0x8B5D、manufacturer="PokerRFID"、product="PN5180-CCID"、Windows reader_name = `PokerRFID PN5180-CCID 0` |
| ESP32-S3 USB CCID firmware 実装 | ✅ 実機 1 slot で契約 §5–§8 通し確認（`watch` 再発火含む） | `firmware/esp32s3-pn5180-ccid/`（ESP-IDF）: USB CCID 記述子（class 0x0B, bulk IN/OUT のみ）+ CCID メッセージ処理（ATR / Get UID `FF CA 00 00 00`→UID+90 00 / Parameters T=1 7B）+ TinyUSB カスタムクラス登録（`ccid_force_link()`）+ PN5180 読取り（`jef-sure/pn5180`, ISO15693 のみ, UID **MSB-first**, presence debounce）。**2026-09-10 実機**: MUX scan で通電 ch を自動選択 → `reader 0 ready` → `🎴 UID=E0:04:…(8B)` → `probe_pcsc raw` で `PRESENT` + カード無し `SW=6A81` / 置いて `SW=9000`+UID ＝ 契約 §5/§6/§7/§8 を host 側で確認。**設計確定（ADR-0051）**: slot は仮想カード常時挿入、カード有無は Get UID の SW のみ（Windows usbccid は interrupt 通知を無視・polling せず bind 時 IccPowerOn しか送らないため）。`probe_pcsc watch` で `seat 1 UID=…(8B)` が置く→離す→置くで 2 回発火（本番 `RFIDThread` 経路）。**複数 reader 化の準備は実装済（実機未検証）**: RF 時分割（inventory 直後に `pn5180_setRF_off`）/ 未通電 reader の skip + 配線チェック / poll 周期の計測ログ / **CCID slot は 1 固定（`CCID_SLOT_COUNT=1`, `bMaxSlotIndex=0`, `bcdDevice=0x0201`）+ 物理 reader 台数は `PN5180_READER_COUNT`（既定 11）で、Get UID の P2 = reader index（`FF CA 00 <k> 00`、範囲外 `6A 86`、台数 `FF CA 00 FF 00`、契約 v1.2 / ADR-0052。台数を変えても USB 記述子は不変）** + `bMaxCCIDBusySlots=1` / 高速 inventory + mask DFS anti-collision + Get UID 連結（重ね置き = 契約 v1.1 §6、ISSUE-0021。**実機: 1 台 15 ms、2 枚/3 枚重ね OK・再発火なし**。coll_pos DFS + 確認間引きで 11 台 1 周 ≤ 300 ms を狙う（実機未検証）。coll_pos の安全弁 = 採用分割で 3 ラウンド連続 0 枚なら 1 bit DFS に固定）。**init の耐性（ISSUE-0023）**: 配線表 13 本の NSS を init 前に全 High 固定、`pn5180_init` 失敗は共有 SPI を作り直して 1 回再試行 → それでも失敗した reader だけ skip（全台停止しない）。**2026-09-11 実機（`076b844`）: 10 台接続で `PN5180 ready: 9/11 reader`（未通電 #4/#11 を skip）、9 台すべてでカード検出、1 周 124〜139 ms（1 台 ≈ 14 ms）**。配線表 `PN5180_READERS` は実機の挿し方（コネクタ #4=ch3 の BUSY 不通を飛ばして #1,#2,#3,#5,…,#12 の若い順）に振替済み。振替後の起動（`0e93de4`）は `PN5180 ready: 10/11 reader`、host 側の `list/check/watch` 通し OK。skip した reader は起動時に **BUSY 非依存の SPI で chip 生存確認**し「BUSY 線のみ不通 / 電源・chip 不良」を WARN で切り分ける（`405f218` 実機: ch10 は `FW=00 04` = chip 生存・**BUSY 1 本のみ不通**）。**10 台の poll 実測**: カード無し 1 周 138 ms（1 台 ≈ 13.8 ms）/ **満載（8 席 × 2 枚 + board）1 周 538 ms**（目標 ≤ 300 ms 未達）。原因は **`RX_COLL_POS` が実機では常に使えない**こと（衝突は UID 先頭バイトで起き受信 1 byte のみ = prefix を作れず 1 bit DFS の空枝が RX timeout を舐める, ISSUE-0021）。対策として **定常状態は「前回 UID を `mask_len=32` の完全一致 inventory で狙い撃ち」**（`PN5180_FAST_TARGETED_PROBE`, 既定 on）に変更し、衝突しない経路に。**応答待ちの上限はフレーム長に連動**（26.48 kbps, mask を伸ばすと送信が伸びるため固定値では応答前に打ち切る）。実機 `047ee1e` で **538 → 433 ms**（命中 340/342、`coll_pos fallback` 0、probe 最大 3、`応答待ち最長 7.4 ms`）。さらに **簡略サイクル**（狙い撃ちが当たる reader は `PN5180_FAST_CONFIRM_EVERY` 回に (N-1) 回、Stay Quiet と root probe を省いて狙い撃ちだけで終える。空の reader は対象外なので席 1 枚目/flop 1 枚目/turn/river は毎 poll 検出）+ `CARD_POLL_INTERVAL_MS` 100→50 を追加。**配線振替で 11/11 ready 達成後の満載実測（`081c9bb`）: 1 周 min/avg/max = 238/324/**495** ms**（簡略 180/297, 狙い撃ち 533/540 命中, `coll_pos fallback` 0）。max が突出した原因は **簡略サイクルが全 reader で lockstep**（旧実装は完全確認のたびにカウンタを 0 に畳むため、ハンドの切れ目で全 reader が 0 枚になると位相が揃う）。対策として **完全確認を `(poll 周回 + reader index) % N == 0` の reader だけに限定**（位相源 = poll 1 周の末尾で +1 する自由走行カウンタ。0 枚を跨いでも崩れず、周期はどの reader も厳密に N、1 周に完全確認するのは `ceil(11/N)` 台）+ **`PN5180_FAST_CONFIRM_EVERY` 3→6**（max が 300 ms 未満になる最小の N。遅れの上限 ≈ 1.7 s、対象は「既に札がある reader に増えた札」= 席 2 枚目 / flop 2・3 枚目のみ）。**11 台満載の実測（`83ad762`）: 1 周 min/avg/max = 272/297〜301/**331** ms**（min/max の差 36〜59 ms = 1〜2 台ぶん、`簡略 266/319` = 完全確認 1.83 台/周 = 11/6 ちょうど、`coll_pos fallback` 0、狙い撃ち 97〜98% 命中）＝ **目標 ≤ 300 ms を平均で達成 / hard bound 0.5 s に 34% の余裕（ISSUE-0021 Fixed）**。札を置いた瞬間だけ過渡で max 720 ms・probe 8（root DFS で衝突を解く）。**配線表**は BUSY 不通の コネクタ #4(ch3)/#11(ch10) を予備に降格し board2=#12(ch11) / board3=#13(ch12)（修理せず振替, ISSUE-0023）。**`PN5180_SPI_HZ` は 1 MHz 据え置きで確定**（5 MHz の効果は 3〜7% に対しハーネスに BUSY 導通不良の実績があり、間欠 SPI 化けの方が高リスク, ISSUE-0021 OQ5）。**v1.11（2026-10-06, ADR-0070）**: 使えるリーダーの一覧（起動時に初期化できなかった・途中で SPI / BUSY が答えなくなったリーダーを外す）と再起動の命令を firmware に追加（店舗 PC は書き換えが 1 回要る。部品の版は `main/idf_component.yml` で固定, 2026-10-07）。**残**: CCID/ドライバのログ水準を DEBUG に、reader 0 の `RST診断 during_rst=0`（ISSUE-0023 Open）。経緯: `docs/worklog/2026-09-10-rfid-ccid-end-to-end-bringup.md` / `docs/worklog/2026-09-10-multi-reader-firmware-prep.md` / `docs/worklog/2026-09-10-pn5180-reader-index-p2-firmware.md` / `docs/worklog/2026-09-11-pn5180-init-resilience.md` |
| RFID カード照合 | ✅ 実装済 | `rfid/card_master.py` |
| ストリート自動遷移 (RFID) | ✅ 実装済 | board 枚数 3/4/5 で遷移 |
| **卓状態モニタ (ADR-0056 D5)** | ✅ 実装済 | **RFID だけから導く卓状態**（カード / 有効席 / ストリート）を publish する経路。**アクション推定に依存しない**ので、音声なし・アクションがダミーでも実プレイ環境で検証できる。`core/table_state.py`（純粋な導出）+ `output/table_state_writer.py`（`logs/{session}.table_state.json` を atomic 上書き + 実質変化時だけ `.jsonl` に append）+ `tools/table_monitor.py`（LAN のブラウザ / iPad 向けページ。1 秒自動更新 + **反映遅延を画面表示**）。config `table_state.enabled` 既定 **true**。**有効席は 1 つの真偽値に潰さない**（ADR-0056 D4）: `dealt_in`（配られた）/ `present`（いま載っている）/ `away_sec`（離席秒数）/ `likely_folded`（`DEFAULT_FOLD_HINT_SEC=20` 秒超の**表示上の推測**）。**ボタン席とポジション名**も表示する（回転を卓の脇から目視確認するため, ISSUE-0032）。手札が卓の中央（ボードのリーダーの上）を通過した席は **「マック」**と出す（ADR-0058。判定ではなく表示）。ボードに記録した札がいま読めていなければ **「外れた N s」**と薄く出し、読めているがまだ数えていない札は **「確認中 N s」/「差し直し確認中」**と点線で出す（`RFIDThread.board_presence()` の `absent` / `pending` → `TableState.board_away_sec` / `board_pending`。記録は変えない = 表示だけ, ADR-0058 追記 2・3。置いた札が「確認中」にすら出なければ、どのリーダーにも読めていない）。ストリートは **RFID 由来**（ボード枚数）と engine のものを**並べて表示**（pokerkit では `advance_street` が no-op なので両者は食い違い得る = それ自体が見たい情報）。カードが**外れても** RFIDEvent は出ないため、engine が `TABLE_STATE_INTERVAL=1.0` 秒ごとに定期 publish する |
| **観測の時刻精度 (ADR-0055)** | ✅ 実装済 | アクション履歴は**音声録音の時系列と突き合わせて再生**する。実時刻が要るのは **fold の時刻**と**ターン/リバーの配布時刻**の 2 つ（カード内容は事後で可・フロップ内順序は無意味）。(1) **合成 silent-fold の時刻** = RFID の「席のカードが消えたまま戻っていない時刻」（`RFIDThread.seat_cards_absent_since` → engine `_synth_fold_timestamp`）。**不在は fold の判定に使わない**（プレイヤーがカードを持ち上げるため。判定は従来どおり合法手・actor 推定、**時刻だけ**差し替え）。ハンド範囲外は棄却、新ハンド/訂正で観測を捨てる、`source.rfid` で出所を示す、confidence は据え置き。(2) **`HandSummary.board_timeline`** = `[{index, card, dealt_at}]`、時刻は**最初の検出**で固定（再発火で上書きしない）。ターン=4 枚目 / リバー=5 枚目がラウンドの区切り、フロップは 3 枚の最小値が開始。`hand` schema `1.1`（additive）|
| **ミスディール訂正 (ADR-0054)** | ✅ 実装済 | 一度読ませた札を外して正しい札を読ませ直す操作。**`cb <位置>`** = ボード N 枚目の記録を取り消す（他の位置は不動 = 順序が壊れない）/ **`cs <席>`** = その席のホールカードを捨てて読み直す（2 枚上限に引っかからない）。`--cli` のコマンド + staff API control（`correct_board`(`index`) / `correct_seat`(`seat`) = iPad も同経路, ADR-0039）。engine が記録を取り消し、`on_card_correction` フックで RFID 側の割り当て・デバウンスも落とす。**absence からの自動判定はしない**（一瞬の読み落ちと区別できず ISSUE-0025/0026 の壊れ方が再発する）。訂正は「記録を捨てて物理を読み直す」= **冪等**（外す前に打っても打ち直せば収束）。訂正したハンドは `needs_review`、ストリートは戻さない。**音声語彙には追加しない**（「訂正」は一般語で誤爆が正しい記録を消す方向に働く）。ハンドごとのやり直しは従来どおり `n`。**配り直しは ADR-0058 で入力なしでも反映**される（前の札が 6 秒消えて新しい札が載り続けたとき）ので、明示コマンドは急ぐとき・1 枚だけ確実に直すときの手段 |
| Confidence 算出 | ✅ 実装済 | センサー組み合わせ行列 |
| JSON ログ出力 | ✅ 実装済 | `output/json_writer.py` |
| PHH エクスポート | ✅ 実装済 | `output/phh_exporter.py` |
| GUI ダッシュボード | 🔨 部分実装 | `gui/dashboard.py`（ヘッダーに**録音系の死活表示** = マイク入力レベル / RFID リーダー接続数。`AudioThread.health` / `RFIDThread.health` を 100ms poll） |
| **player registry (S1)** | ✅ 実装済 | `core/player.py`, `core/player_repository.py`, `gui/player_registry.py` |
| **session + hand-based seating (S2) core** | ✅ 実装済 | `core/session.py`, `core/session_repository.py`（§ Session & Seating 参照） |
| **session / seating viewer (WS2-α, read-only)** | ✅ 実装済 | `gui/session_viewer.py`（`main.py --sessions`, § Session / Seating Viewer 参照） |
| **ledger / point / settlement core (S3.1)** | ✅ 実装済 | `core/ledger.py`, `core/ledger_repository.py`（§ Ledger / Points / Settlement 参照, ADR-0016, ISSUE-0001 Resolved） |
| **ledger desktop viewer (S3.2)** | ✅ 実装済 | `gui/ledger_view.py`（`main.py --ledger`, 別画面, dashboard 不可侵） |
| **settlement 確定 GUI (S4)** | ✅ 実装済 | `gui/ledger_view.py` 精算パネル: closed session の `commit_settlement` + 確定済 settlement の paid/unpaid 切替（`set_payment_status`）+ **受領額入力で partial-paid 記録**（`record_payment`, ADR-0023） |
| **settlement partial-paid (S4)** | ✅ 実装済 | `SessionSettlement.paid_amount`（additive, 既定 0）+ `payment_status` 導出（paid/unpaid/partial, ADR-0023）。core `record_payment` / staff API `PUT .../payment` / `ViewerApiClient.record_payment` / GUI 受領額入力 / mobile 一部支払い表示。schema `1.0`→`1.1`（optional field + enum 値, additive） |
| **settlement / cashflow CSV export (S3.3)** | ✅ 実装済 | `output/ledger_csv_exporter.py`（`main.py --export-ledger`, utf-8-sig） |
| **viewer API (M1)** | ✅ 実装済 | `api/read_models.py`, `api/server.py`（`main.py --viewer-api`, read-only GET, `[api]` extra, ADR-0017。ledger summary は `compute_settlement` 由来 = ADR-0016。`/` でお客さん向け画面を配信 + 要求ごとに `sessions.json` / `players.json` を変わっていれば読み直す + `--host` / `--port`, ADR-0059） |
| **お客さん向けハンド閲覧の店舗運用 (ADR-0059)** | ✅ 実装済（店舗 PC 未確認） | `--cli` が席とお客さんを記録（`session_layer.enabled=true`。起動時の名前 → player、`name <席> <名前>` / `name <席> -` で次のハンドから席替え、`q` で session close）→ `start_viewer.cmd`（`--viewer-api --host 0.0.0.0 --port 8788`, ショートカット「お客さん用 ハンド履歴 (スマホ)」）→ お客さんは `http://<PC の IP>:8788/` で名前を選ぶだけ。画面のビルドは `api/static/player/` にコミット（`scripts/build_player_web.py`、API = `/`、`EXPO_PUBLIC_PLAYER_AUTH=off`、staff token なし、drift は CI）。記録したホールカードの公開範囲は店の設定 + セッションごと（ADR-0067 D6）。`tools/set_config.py` で config の 1 項目を変更（BOM なし）。ISSUE-0036 |
| **mobile viewer (M2)** | ✅ 実装済 | `mobile/`（Expo/RN。PlayerSelect→MySessions→MyHands→HandDetail + 会計（**精算状況: 確定/未確定・支払済み/未払い** 表示, S4）+ 注文画面。`ViewerRepository` に mock/HTTP 注入, `EXPO_PUBLIC_API_URL` 切替（`/` = 画面と同じ origin の API, ADR-0059）, ADR-0017。`EXPO_PUBLIC_STAFF_TOKEN` が無ければハンド訂正、`EXPO_PUBLIC_PLAYER_AUTH=off` なら PIN / サインアップの導線を出さない（ADR-0059）。HandDetail は**ストリート単位リプレイ**表示 = ADR-0044（URL に `?test` を付けた端末は聞き取った文と補正の理由も出す = ADR-0060）。全画面に手動再読込（↻）+ エラー再試行、選択 player / ログイントークンを永続化 = web localStorage・native は in-memory fallback, `src/storage.ts`。注文の**本人キャンセル**（ADR-0045）+ **品切れ表示/注文不可**（ADR-0046）+ ハンドの**テキスト共有/コピー**（`shared/hand_replay/handReplayText.ts`, Phase B「書き出し」導線）+ **PIN の自己設定/変更**（AuthScreen, `setPin` = ADR-0027 D6。初回 = pin_self_enroll 会場・変更 = 現 PIN 必須）） |
| **ハンドリプレイ UI (ADR-0044)** | ✅ 実装済 | GG 風ストリート単位リプレイ（board スライス 3/4/5・4 色スート・ポット推移・needs_review/訂正済バッジ・記録がある席は全員分ホールカード・main/side pot）。正本 `shared/hand_replay/`（`handReplayModel.ts` 純関数 + `HandReplay.tsx` + TS tests 8）を `scripts/sync_shared_ui.py` で mobile/staff 両アプリへコピー配布（monorepo 化しない）、drift は `tests/test_shared_ui_sync.py`（CI）。mobile HandDetail + staff ハンドタブ履歴 drill-in に搭載。staff hands read = `GET /api/staff/sessions/{sid}/hands`（訂正適用済, `list_session_hands` + `ViewerApiClient.list_session_hands`）。BTN/ポジション推定は scope 外（M1 の spot_config_builder） |
| **ハンド訂正 (B4, ADR-0036)** | ✅ 実装済 | 音声自動記録の誤認識を append-only オーバーレイで訂正。`core/hand_correction*.py`（store + `apply_hand_corrections`: 元値 `_original` 保持・`corrected`・needs_review 解除・監査痕、元 hand log は不変）+ viewer get_hand/list_player_hands に適用 + staff API `POST .../hands/{hid}/corrections` + `ViewerApiClient.add_hand_correction` + mobile `addHandCorrection` + **iPad 訂正画面 `mobile/src/screens/CorrectionScreen.tsx`**（HandDetail から導線、`EXPO_PUBLIC_STAFF_TOKEN` で staff write。typecheck + web export green）+ **staff アプリ内の訂正パネル**（`staff/src/screens/HandCorrectionPanel.tsx`, ハンドタブのリプレイ詳細から action/amount/winner_seat を訂正）。**残**: PHH overlay / 訂正取消 / board・hole 訂正 |
| **Phase A ground truth 入力 UX (ADR-0043)** | ✅ 実装済 | `core/ground_truth*.py`（LWW per-session）+ staff API (`GET /measurement-rows`, `PUT/GET /ground-truth/{hid}`, staff token + write 所有) + `staff/src/screens/MeasurementTab.tsx`（一覧 triage + 「✓ 流す」/ 「✏ 修正」+ 一括 + 5 秒 polling）。**C-2 ガード**: `review_required` or 任意 `action.needs_review=True` を含むハンドは「✓ 流す」を 400 で reject（強制 drill-in）。訂正適用は `get_hand()` 経由（ADR-0036）。tests 32 件（core 13 + API 12 + mock 7）。measurement-plan §2.3 と整合 |
| **mobile 本人認証 UI (L1/L2)** | ✅ 実装済 (preview) | `mobile/src/screens/AuthScreen.tsx`（PIN ログイン = L1/ADR-0027、LINE/Google サインアップ = L2/ADR-0031）+ `ViewerRepository.{login,oidcExchange,currentPrincipal,clearAuth}`（mock/HTTP 両実装）+ 注文 POST に Bearer トークン付与。PlayerSelect に「PIN でログイン」「サインアップ」導線。既定 name-pick は不変。実 IdP の認可コード取得（SDK/redirect）は実環境タスク。typecheck + 11 mock tests + web export green |
| **注文リクエスト write path (M5)** | ✅ 実装済 | `core/order_request*.py` / `core/menu.py` + viewer API `/menu`・`/order-requests`（GET/POST）+ `gui/ledger_view.py` の確定/却下パネル（§ 注文リクエスト参照, ADR-0018。staff-in-the-loop / in-process API / name-pick = ISSUE-0019 Fixed） |
| **hand logger × session 統合 (S2.x E1+E2-core)** | ✅ 実装済 | `integration/engine.py`（`session_repo`/`seat_player_map` DI、`assign_seat` write-through + `player_id` additive 埋め込み、`session_layer.enabled` 既定 off で挙動不変, ADR-0008） |
| **seat→player 選択 GUI + live 有効化 (S2.x E3)** | ✅ 実装済 | `gui/seat_selection.py`（`SeatSelectionDialog`: モーダル, 席ごと割当 / 未登録その場 create / 空席 skip / carry-forward）+ `gui/dashboard.py`「座席設定」ボタン + `integration/engine.py:set_seat_player_map` + `main.py` 結線（UUID4 session_id）。既定 off で挙動不変, ISSUE-0006 Resolved。**`--cli` も記録**（起動時の名前 + `name` コマンド, ADR-0059） |
| **event 記録 sidecar (R1)** | ✅ 実装済 | `output/event_recorder.py`（`recording.enabled` **既定 on**（2026-09-27, ADR-0056 追記 1 = 再生・評価・事後推定の入力。off にすると `tools/eval_store.py` で再生できない）, 記録の挙動は不変, ADR-0010, `reconstruction_event` schema `0.18`）。札の離脱・配布・ストリート・ショーダウンなど engine の判断も信号として記録し、replay は記録の順（= 店舗で処理した順）に流して live と同じ記録になる。ログの末尾が読めない行（電源断の NUL）でも残りを使う |
| **pokerkit game-state backend (R2) + live 既定切替 (G)** | ✅ 実装済 | `core/poker_engine.py`（`engine.backend`, ADR-0009/0012。actor/合法手/side-pot 権威）。**Phase G で live 既定を `pokerkit` に切替**（`config_default.json`、`requirements.txt` で `pokerkit>=0.7,<0.8` pin）。`legacy` は config で rollback 可。実機 E2E は Phase H |
| **rules-aware ライブ結線 + silent-fold 合成 (R3 D1/D2a/D2b)** | ✅ 実装済 (preview) | `audio/recognizer.py:apply_corrections`（合法手射影）+ `integration/engine.py:_handle_rules_aware_action`/`_resolve_actor`（合法手射影・actor 推定・`fold_through` で silent-fold 合成 cap=2/atomic・合成 fold 記録）。**actor の証拠は明示発話のみ**（席番号「シート3」→ ポジション名「BTN」の順で解決 = `_sensed_seat`, ISSUE-0032。ISSUE-0033 で RFID の検出を証拠から外した = カードの**存在**は**行動**ではなく、配布と区別できないため。RFID は同席の裏付けとしてのみ効く）。legacy 既定は不変 |
| **復元の正当性修正バッチ (ADR-0047〜0050)** | ✅ 実装済 | 「静かな誤り」経路の全廃: 金額パース拡充+曖昧 flag（2千/N百/小数万/「4万2」= `ambiguous_amount`）/ 席表現 strip・抽出の統一（空白・全角・漢数字）/ raise to-by 曖昧性・snap review 同次元化（gap>=bb）・bb 丸め / fold 合成後の stale legal_ctx 再取得 / ハンド外イベントの unresolved レコード化（無音消失全廃, live↔replay 同一例外セマンティクス）/ winner fallback 連鎖 + 空 summary 抑止 / `result` のブラインドずれ修正（stack_start = post 前）/ `pot_total` を engine 権威に / **監査フィールド実配線**（actor_source/corrected_from/reason/asr_confidence/apply_ok, action schema `1.3`）/ 計測のシーケンスアライメント（G6, 誤合成 fold 1 件で崩壊しない）/ whisper 欠測=0.5（P9）/ **制御語ガード**（config `engine.control_conf_threshold` 既定 0=off + mid-hand new_hand 異常確定）/ RFID の actor 証拠化は**廃止**（ADR-0049 G3 の「fold 済み席のみ除外」は配布直後を防げないため ISSUE-0033 / ADR-0056 が supersede。同席の裏付けのみ） / 発話区間ベース照合窓（`utterance_start_ts`, envelope `0.3`, 旧記録後方互換）/ **recorder 再構築**（推論 worker 分離・有音ゲート・自然文プロンプト）/ **split pot**（チョップ→`end_hand_split` 等分 + `HandSummary.pot_awards`, hand schema `1.3`）。tests: `tests/test_reconstruction_hardening.py` |
| **決定的 replay harness + golden fixtures (R4 F1/F3a)** | ✅ 実装済 | `integration/replay.py` + `tools/replay_hand.py`（clock 注入で決定的、ADR-0011）。golden fixtures: `tests/fixtures/reconstruction/`（**green 13**: 射影 2 + 合成 1 + `rfid-appear-is-not-an-action`（RFID は actor を動かさない, ISSUE-0033）+ side-pot 1 + ADR-0047 で 8 追加 = postflop 遷移 / 6max / multi-hand / 明示席>RFID（`rfid-vs-spoken-seat-conflict`） / camera / 低信頼 / cap 負例 / chop）。round-trip 決定性 = `tests/test_reconstruction.py`。**pin は手計算検証必須**（event-replay.md §6.5） |
| **派生 confidence + side-pot (R3 D3 / R5 F3a)** | ✅ 実装済 (preview) | `integration/engine.py:derive_confidence`（3 因子 L/A/Q、rules-aware 経路のみ。legacy 固定表は不変）+ needs_review 条件（パース曖昧 flag 含む）。`HandSummary.pots`（main/side、legacy は `[]`。未回収 bet は残差合成で `sum(pots)=実ポット`）。whisper 欠測は `MISSING_WHISPER_CONF=0.5`（満点補完廃止, ADR-0033 追記） |
| **派生 confidence 重み較正 (R5/F2)** | ✅ 実装済 | ADR-0033: 重み（暫定）を golden fixtures archetype + 境界グリッド由来の較正プロパティ **P1〜P9**（順序単調性 / 閾値分離 / 合法性ゲート / synth-fold / 欠測保守性）で正当化・回帰ロック。数値据え置き。`tools/calibrate_confidence.py`（ハーネス）+ `tests/test_confidence_calibration.py`。「暫定」表記を解除 |
| **推定器 v1（事後推定, ADR-0056）** | 🟡 影で運用（記録の本体にはまだしない） | `integration/estimator.py`（ハンドごとのビーム探索・log 事後確率・要確認）+ `integration/world_replay.py`（生の観測の再生）+ `tools/estimate.py`（発話の読みの選択肢、v0）+ `core/hand_estimate.py`（読む側の重ね方 = viewer API・真のアクションの入力画面・PHH）+ `tools/bench_hands.py`（全部正しいハンドの割合）・`tools/estimate_logs.py`（`--shadow` / `--write`）。統計の監査 2 回・作業計画の監査・固定の前の監査 3 回目（Fable 5.1）を受け、10/03 に 3 回目の推奨をすべて採った（追記 5: 合格の条件 1 = 要確認なしで悪くなった 0 + 悪くなった 40 ハンドで 1 以下 / 値の直し 3 つ / 終わったあとの勝者の指定の食い違いを要確認に / 推定器まわりのファイルの内容の指紋 `content_hash` / 物差しの「数えないハンド」/ 前の版と比べる `tools/compare_versions.py`）。評価の事前登録は草稿（2026-10-01 オーナー: 評価の前に音声入力を改善 → 10/02 に合格の条件と評価に入るまでの手順を決定 = 追記 4。音声の改善を終える目安は `tools/eval_store.py` の最後の行 = 直近 2 セッションの合算、評価のセッションは真のアクションの入力画面を評価のモード `ground_truth.evaluation` に。入力画面は保存のときに真のアクションの不備を確かめる）。10/07 までに推定器だけの規則を足した（全発話の第 2 の耳と短い語の分類器の読みを選択肢に = ADR-0069 / 決まり文句の発話を「雑談 / アクション」の 2 状態に（r = 0.27 = オーナーが聞いた 51 発話）/ ショーダウンの声の有無を両側の証拠に / 額の別の候補・賭けの大きさの重み（`core/bet_sizing.py`）/ 札より前のアクション）。**テスト方針 = ADR-0056 追記 6**（2026-10-08, オーナー承認）: 精度は**前向きの成績表**（`tools/forward_ledger.py` = 各セッションを初めて見たときの版 `tests/fixtures/store/first_look.json` で採点。10/06〜10/07 の 81 ハンドで 43/81）で見る、開発データの物差しは前の記録（`tests/fixtures/store/bench_record.json`）と比べて新しい要確認なしの誤りを止める。音声の改善は 10/17 まで → 5〜6 人卓 2 セッション（10/20〜10/24）で**準備の確認**（誤り 6 以下・要確認なしの誤り 0 なら固定して評価）→ 届かなければ合格の条件を変えるか推定器を道具にとどめるかを **11 月初めまで**にオーナーが選ぶ。真のアクションは毎回評価のモードで入れる。設計・監査の推奨ごとの対応は `docs/estimator.md`。**LLM の提案を候補に足す実験（10/23〜10/24, ⏹ 打ち切り = オーナー 10/24。開発 A で提案つき 34〜35/49 対 今 35/49・LLM 単独 Opus 28/49。誤りの中心は入力に発話が無いこと → 音声の取りこぼし対策へ）**: 推定器に外から直しの組を足す口 `HandWindow.seeds`（既定は空 = 結果は不変）+ `tools/llm_candidates.py` / `tools/bench_llm.py`（Opus 5.5 と Haiku 5.5、事前登録の草稿 `docs/dogfood/llm-proposer-preregistration.md`、`docs/estimator.md` §9）。本番の経路・店舗 PC では使わない |
| **第 2 の耳と短い語の分類器 (ADR-0069)** | ✅ 実装済（ライブは聞き直しだけ・分類器は推定器だけ） | **第 2 の耳** = 日本語専用の音声認識 ReazonSpeech k2 v2 の ONNX 版（`audio/second_ear.py`, `.[ear]`, モデル約 170 MB を `models/`、config `audio.second_ear.enabled` 既定 true・項目が無くても使う）。自由に聞いた文と、決まった候補（アクションの語・額・その組み合わせ）ごとの確からしさを出す。**ライブ**: Whisper がアクションとして読めなかった発話（幻聴・読めない文）と額を読んだ発話だけ聞き直し、耳の文が候補と一致したときだけ使う（要確認の印 `second_ear`。額だけ崩れたベット・レイズの額・下の桁 `fill_lower_digits`・自信 0.15 未満の Whisper の読みの捨て）。**全発話**にも耳をかけて書き起こしの記録（`transcripts.jsonl` の `ear`・額ごとの点数 `ear.amounts`・短い語 42 語の確からしさ）に残す（`ear_wanted` が偽なら記録だけ = ライブの読みは変えない。1 発話 +0.14 秒ほど）。**短い語の分類器**（`audio/short_words.py`、重み `audio/short_word_model.json`、学び方 `tools/short_words.py` = 開発データの真のアクションとオーナーの聞き取りのラベル `tests/fixtures/store/*/utterance_labels.json`）: 耳の点数から発話が雑談 / コール / チェック / フォールド / 額のどれかの確率を出す。**推定器だけが使う**（読みは既定の読みより log で 1 までしか高くしない）。測る道具 `tools/second_ear.py`（保存した発話を聞き直して `<sid>.ear.jsonl`、`--whisper` で Whisper の別のやり方）/ `tools/fixture_ears.py`（開発データに耳の結果を足す） |
| **真のアクション入力の画面 (ADR-0043 追記)** | ✅ 実装済（店舗で運用中） | `tools/ground_truth_ui.py`（店舗 PC の `start_truth.cmd` / ショートカット「真のアクション入力 (iPad から)」= `http://<PC の IP>:8791/`。店舗サーバーの中でも動かせる = `store_server.ground_truth_ui`）: 記録したハンドの**実際のアクション列**を入れて `logs/<sid>.ground_truth.json`（Phase A 計測の形式）に書く。入れるのは席・アクション・ベット / レイズの額（トータル）だけ（手番・ストリート・コールの額は pokerkit で補う）、ボードは記録の札で自動（進行中は 3 秒ごとに読み直す）、手札はタップ、**ボタンを選べる**（ディーラーが動かし忘れたハンド）、ショーダウンで見せずに降りた行（street=showdown）・勝者の手札判定・「＋ 行を追加」・行ごとの「自信なし」・入力の時間・**メモ（気づいたこと = 要望・仕様として毎回読む, § 店舗ログのメモ）**・「評価から外す」。**全部のハンドを記録を見ずに入れる**→ 保存すると記録と並べて違う行を黄色で（最初の内容は残る）。**評価のモード**（`ground_truth.evaluation` = true / `--evaluation`）では保存するまで「記録を見る」「記録どおり」を使えない（サーバーも拒む、保存に `evaluation`、画面を開き直すだけで効く）。保存のときに降りた席のアクション・反映できない行を示して確かめる（`lint_confirmed`）。発話と札の流れを手札の配布からの秒数で並べ、▶ で発話の音声を聞ける。**進行中のハンド**: ロガーが手札を配った時点から `logs/<sid>.live_hand.json` を書き（終われば消す）、一覧のいちばん上に「進行中」で出て途中まで保存できる。セッションの一覧も 5 秒ごとに読み直し、新しいセッションに移る。同じサーバーが**読み上げ集**（`/corpus`, `tools/read_corpus.py`）と**台本のハンド**（`/script`, `tools/test_script.py`）の画面も出す。入力中は画面を作り直さない（キーボード・ロールダウンが消えない） |
| **評価の道具（店舗のログ → 開発データ）** | ✅ 実装済 | **ログをまとめる** `tools/pack_logs.py`（`pack_logs.cmd` / `pack_logs_audio.cmd`: 直近 12 時間のセッションの記録・events・transcripts・卓状態・真のアクション・発話の音声 + その時間帯の `pokerapp.log`（二分探索で切り出し・同じ行は畳む）+ トークンを伏せた `config.json` + `manifest.json` をデスクトップの zip に）→ **`tools/eval_store.py <zip…>`**（いまのコードで再生して評価: 真のアクションのメモをセッションの先頭と各ハンドに必ず出す・記録 / 再生 / 読み直し / 採点し直した文の一致率・要確認の精度と再現率・ブラインド入力との差・**音声の改善を終える目安**（最後の行 = 直近 2 セッションの合算、アクションの発話が読めた割合 90% 以上・決まり文句 5% 以下）・`--timeline`・`--export-fixture tests/fixtures/store` = 回帰テスト `tests/test_store_fixtures.py`）。**`tools/rescore_audio.py`**（店舗 PC: 保存した発話を Whisper のビーム上位 5 と候補の文で採点し直して `<sid>.rescored.jsonl`）。**正解が先に分かるデータ**: 台本のハンド（`start_script.cmd` = `main.py --cli --log-file --script voice`、6 人の仮想の卓・30 ハンド、画面の「ハンド N を始める」で台本のボタンと持ち点から、勝者は画面がロガーに送る、RFID は使わない）/ 札の確認（`start_cards.cmd` = `--script cards`、配った順・ボタンの置き忘れ・持ち上げ・マック・フロップ 3 枚同時・ターンの置き直しの 6 手順）/ 読み上げ集（`tools/read_corpus.py`: 店の言い方の 135 句を読み、句ごとの音声と正解を `logs/corpus/` に。`eval` / `transcribe` / `serve`（:8793））。**見直し**: `tools/review_page.py`（開発データのハンドを入力画面と同じ形の HTML 1 枚に、発話に音声の番号、直した内容は `load_reviews` で同じ検査をして読み戻す）。**精度**: `tools/forward_ledger.py`（前向きの成績表）/ `tools/bench_hands.py`（開発データの全部正しいハンド、`--update-record` で前の記録 `tests/fixtures/store/bench_record.json` を書き直す、新しい要確認なしの誤りは終了コード 1）/ `tools/compare_versions.py`（前の版と今の版でハンドごとに）。`tools/voice_style.py`（宣言の声と雑談の声の比較, AUC 0.69 = 推定器には使わない） |
| **RFID の中継 (ADR-0070)** | ✅ 実装済（店舗で運用中） | 店舗 PC は RDP で操作し、RDP のセッションの中のアプリからはリーダー（PC/SC）が見えない（`0x8010001D`、スマートカードの転送を止めても同じ, 2026-09-30）。**中継** `rfid/relay.py` + `tools/rfid_relay.py serve` を SYSTEM の権限のスケジュールタスク `PokerRFIDRelay`（PC の起動時、`installer/rfid_relay_task.ps1` を管理者で 1 回・更新後ももう一度、`-Remove` で消す）で動かし、読んだ札と使えるリーダーの一覧を PC の中（`127.0.0.1:8792`, config `rfid.relay_port`）だけに渡す。ロガー（CLI・GUI）・`probe_pcsc list / check / watch`・`register_cards run` は中継が動いていれば中継から（無ければ直接）、札の扱いは同じ。`config.json` のリーダーを変えたら中継も読み直す。再起動の命令も中継から送ってつなぎ直す（`rr` / 自動 / `tools/rfid_relay.py restart`）。確認は `tools/rfid_relay.py status`（使えないリーダーは ✗、ただし ✓ は「札なし」と答えているだけでリーダー本体が動いている証拠にならない）/ `watch` |
| **hand/action schema freeze (R5 F3b)** | ✅ 実装済 | `docs/contracts/schemas/{hand,action}.schema.json`（hand `1.7` / action `1.5`, additionalProperties:true, ISSUE-0011 Fixed。additive 追加: board_timeline=ADR-0055 / button_seat・position_map・position=ISSUE-0032 / 監査フィールド actor_source・corrected_from・reason・asr_confidence・apply_ok=ADR-0047 G2 / pot_awards=ADR-0050 / raw_text=ADR-0060 / winner_source・showdown・action の end_hand=ADR-0062 / announced_hand・board の `??`・winner_source の announced / undetermined・showdown[].announced=ADR-0068。`reconstruction_event` は `0.18` = utterance_start_ts・parse_flags（ADR-0047/0048）+ position + rfid の replaces（ADR-0058）+ hand_name・rfid の kind（leave / muck / return / confirm / street / showdown / spoken_fold / deal / hand_start / deal_order / showdown_end …）・amount_options / amount_scores・hit_rank・parse_flags の追加（2026-09-26〜10-13）。開発データの記録がすべて schema に通ること、読み取りが付ける印がすべて表にあることもテストで確かめる）+ `_MODELS` 登録 + code↔contract + golden→schema テスト。**`action.street` は「そのアクションが行われたストリート」**（適用後ではない。rules-aware backend はラウンドを閉じたアクションで次ストリートへ自動進行するため, ISSUE-0029） |
| **PHH call/check (F3c)** | ✅ 確認済（変更不要） | PHH 標準では check/call は同一トークン `cc`（check-or-call）。区別は非標準で pokerkit が parse 不能になるため統一が正。check/call の別は JSON ログ側で保持（`output/phh_exporter.py` にコメント） |
| Vosk 代替バックエンド | ❌ 未実装 | future phase |
| 音声正規化 / 数値正規化 | ❌ 未実装 | 設計提案 R0: `apply_corrections()`（合法手制約, ADR-0009） |
| **ディーラーボタン回転 + ポジション名 (P0b)** | ✅ 実装済 | `core/positions.py`（純粋ロジック）+ `PokerkitGameState` がハンドごとにボタンを 1 つ回す（§ ディーラーボタン / ポジション名, ADR-0056 P0b, ISSUE-0032 Fixed）。SB/BB の自動 post は pokerkit backend が元から行う。**legacy backend はボタンを持たない**（単純ラウンドロビンのまま = rollback path 不変） |
| schema `1.0` freeze (S4) | ✅ 実装済 | 全 model（session/seat/hand_ref・ledger/point・settlement・order_request/player_session_summary）を `1.0` freeze（ADR-0019, ISSUE-0005 Resolved）。code↔contract test 全 model カバー |
| 実機 E2E (Phase H) | 🟡 RFID 実機 + `--cli` 1 ハンド通し 済（JSON 検証まで）+ 店舗 PC 導入済 | **2026-09-24 店舗 PC（GEEKOM A8 / Windows 11 Pro / HDMI ダミープラグで画面なし・RDP 操作）**: 1 行インストール（ADR-0057）→ RFID（卓の ESP32-S3 を USB 直結）→ `--cli` + 卓モニタを起動し、iPhone から卓モニタの反映まで確認。RDP 運用に要る PC 側設定（スマートカード転送の停止 / Wi-Fi をプライベート + 8788・8790 を LAN 限定で許可）は `docs/installation.md` §0。**2026-09-12 実機（11 reader / pokerkit backend / `audio.enabled=false`）**: `n` → 席 1・2 のホールカード各 2 枚 → flop(pos 1,2,3) → turn(pos 4) → river(pos 5) → キーボードのアクション（`チェック` / `ベット５` / `コール` / `ベット`）→ `w 1` → JSON 保存まで通り、**保存された JSON を検証済**: `street` が実際の進行と一致（preflop×2 / flop×3 / turn×3 / river×2, ISSUE-0029 修正確認）、`pots` に side-pot 情報あり（`{"amount":44,"eligible_seats":[1,2]}`）、`confidence` は派生値（keyboard 投入 = 合法 + audio のみ・裏付けなしで一律 0.575。legacy 固定 0.5 ではない）、金額を言わない `ベット` は最小ベットに snap + `needs_review`。**board 位置は安定**（同じ札の再発火が同じ位置に収まり付け替え無し = ISSUE-0025/0026 の修正確認。ただし 1 台で同じ札の再検出が繰り返し出る = 結合が弱い台の間欠読み。位置が固定なので記録は無害）。残りはクリーン環境の通し確認（§ ロードマップ 残作業）。**PN5180 firmware 契約は凍結済（ADR-0034, ISSUE-0015 Fixed）**= `docs/contracts/rfid-usb-ccid.md`（いまは **v1.11**。ADR-0051 で §2/§5/§8 を additive 追記、ADR-0052 で §3/§4/§6/§8 = 1 slot + P2 選択, ISSUE-0022、以降は上の契約の行）。**実機 RFID の bring-up 診断ツール + 手順は実装済**: `tools/probe_pcsc.py`（list/check/watch/**raw**/restart, 契約 §3-8 を production の `PCSCBridge`/`RFIDThread` で検査。list は物理リーダー台数も表示、raw は pyscard 直叩きで OS の slot 状態と connect の hresult を表示）+ `docs/hardware-qa-checklist.md`。**2026-09-10**: 1 台で `raw`（`SW=9000`+UID）と `watch`（再発火 2 件）まで実機確認。**残**: `rfid_cards.json` の実カード登録は **1 デッキ 52 枚 済**（ジョーカー 2 枚のみ未登録）。**新ハンドは手札を配ると始まる**（ADR-0062。`n` は手札の無い構成・急ぐときの手段。ハンドの外のアクションは記録されず WARN が出る, ISSUE-0028）。**2026-09-25〜10-07 の店舗テスト**: マイク有り・RFID 11 台・RDP 運用で通しのプレー（真のアクション入力つきのセッションを `tests/fixtures/store/` に開発データとして蓄積、不一致の索引は ISSUE-0039）。残りは `--export-phh`、`--ledger`（`viewer_api.enabled`）+ スマホ注文の通し。**`config.json` に `engine.backend="pokerkit"` を明示**すること（既存 config に `engine` セクションが無いと legacy にフォールバックし side-pot / 派生 confidence / actor 推定が無効 = `pots: []`・`confidence: 0.5` 固定がその状態） |
| **Windows ワンステップインストーラ (ADR-0057)** | ✅ 実装済 (Stage 1) | 店舗 PC 向け。`install.cmd`（ダブルクリック）→ `installer/install.ps1`: **Python 3.12** を `py` → winget（`--source winget` 固定。msstore の証明書エラー 0x8a15005e で止まるため）→ 入らなければ python.org の順で確保 → フォルダ内 `venv` → `pip install -e ".[pcsc,api]"`（データが `Path(__file__).parent.parent` 直下にある前提なので **フォルダ in-place + editable**。site-packages に入れない）+ 第 2 の耳の部品 `.[ear]`（入らなくても止めない。モデル約 170 MB は更新で `models/` に取得）→ `config.json` 生成（既存は不変）→ Whisper モデル先読み（任意）→ デスクトップにショートカット **11 個**（ハンドロガー / 卓モニタ / お客さん用 ハンド履歴 / 真のアクション入力 / 台本のハンド (声だけ) / 札の確認 (RFID) / ログをまとめる / ログをまとめる (音声付き) / 会計 + スマホ注文 API / 店舗サーバー 自動起動の登録 / RFID リーダー チェック = `start_*.cmd` / `pack_logs*.cmd` / `server_autostart.cmd` / `rfid_check.cmd`）→ import と `main.py --help` で動作確認 → `install.log`。`update.cmd` = GitHub の zip を robocopy で上書き（`.git` があれば `git pull --ff-only`）したあと、**更新後のインストーラで続きを実行し直す**（取得元のブランチは `installer\branch.txt` に覚え、`-Branch` 無しの更新はそれを使う）、**`$PreservedFiles` = `core/backup.py` のデータ一覧 + config/rfid_cards/menu、`$PreservedDirs` = venv/logs/backups/models** は保持（`tests/test_installer.py` が整合を固定）。`uninstall.cmd` = ショートカットと venv のみ削除（RFID の中継のタスクも消す。管理者の権限が無ければ消し方を出して止まる）。1 行版 `installer/bootstrap.ps1`（`irm … \| iex`、既導入なら更新）。`install.ps1` は UTF-8 BOM（PowerShell 5.1 の `-File`）、`bootstrap.ps1` は **BOM 無し + `exit` 無し + `& { }` 包み**（`irm \| iex` でユーザーの対話コンソールの中で走る。exit はウィンドウを閉じ、BOM は irm の戻り値に残る）、.cmd は ASCII + CRLF（CP932 の cmd.exe）。CI は pwsh で構文解析 + `-DryRun` 通し。**Stage 2**: iPad 画面の API 配信は **ADR-0064 で実装済**（`/staff/` = `api/static/staff/`。お客さん向け画面は ADR-0059 = `api/static/player/`）。**自動起動は ADR-0065 で実装済**（店舗サーバー `--serve` をタスク `PokerStoreServer` でサインイン時に。録音は対象外）。**リリース版は ADR-0066 で実装済**（`stable` ブランチ = 店で確かめた版だけ、`tools/release.py promote` で前に進めるだけ + `release-YYYYMMDD` タグ。店舗 PC は `update.cmd -Branch stable` を 1 回。更新では先にコミットを確かめてその zip を取り `installer/installed.json` に記録 → `/api/health`・`--version`）。未着手 = データフォルダ分離 / release zip / PyInstaller + Inno Setup の exe |
| **cross-app boundary (S5 read)** | ✅ 実装済 | repository interface frozen（ADR-0020）+ `api/client.py:ViewerApiClient`（Python の local↔API 分離点）+ round-trip test。read boundary を二言語で実証（mobile + Python） |
| **staff 会計 write API (S5 write)** | ✅ 実装済 | `api/server.py` の `/api/staff/...`（ledger 追加 / settlement 確定 / paid-unpaid / 注文確定・却下 + staff read）を **staff shared token**（`Authorization: Bearer <viewer_api.staff_token>`）で公開（ADR-0021）。`LedgerRepository` を RLock で thread-safe 化。単一書き手維持（read-only は 503）。`ViewerApiClient(staff_token=...)` の staff メソッド + `tests/test_viewer_api_staff.py` |
| **staff iPad app（会計/注文/座席/ハンド, WS4）** | 🟡 一部実装済 (preview) | `staff/`（Expo/RN, player `mobile/` とは別アプリ, ADR-0037）。**会計タブ**（エントリ追加 / buy-in プリセット / ポイント付与 / 中間集計 / 精算確定・paid-unpaid-partial / エントリ一覧 + 取消(reversal)）+ **注文タブ**（pending 確定→order entry / 却下）+ **座席タブ**（現在 seating / 次 hand への seat→player 割当 / その場 player 作成）+ **ハンドタブ**（hand logger 遠隔制御 = 新ハンド/ウィナー/リバイ, ADR-0039）+ **SessionList**（session 作成・close）を staff API（`/api/staff/...`, ADR-0021/0038/0039）上に実装。`StaffRepository`（mock/HTTP）。**ハンドタブにハンド履歴一覧 + ストリート単位リプレイ drill-in + アクション/勝者席の訂正パネル**（ADR-0044 + B4/ADR-0036 の staff アプリ内導線 = `HandCorrectionPanel`, 訂正→hands read 再読込で即反映）。**注文 pending バッジ・ハンド履歴・座席は open 卓で 5 秒 polling 自動更新**（useAsync は再取得中 stale data 保持でちらつかない）。**SessionList に営業日サマリ**（当日全卓の中間集計をクライアント合算 = 締め目安）+ **プレイヤー管理画面**（作成/リネーム/merge = `PlayersScreen`, `StaffRepository.mergePlayers`） + **メニュー管理画面**（価格改定/品切れ/追加削除 = `MenuScreen`, ADR-0046）。座席タブは**「現在の座席をコピー」→ 行単位で外して次 hand へ**（退席の明示反映）。typecheck + 38 mock tests + web export + **Playwright E2E 8 件**（`staff/e2e/`, headless Chromium。browser 取得はネットワーク要）。**残**: 実機タッチの手動 QA、open question = ISSUE-0020 |
| **hand logger 遠隔制御 (WS4 §C)** | ✅ 実装済 | `core/control_queue.py`（append-only `logs/{session_id}.control.jsonl`）+ `integration/control_consumer.py`（`ControlConsumerThread`: 末尾シーク + command_id 重複排除で新規のみ `AudioEvent` に翻訳 → IntegrationThread, ISSUE-0012 遵守）+ `POST /api/staff/sessions/{sid}/control` + `ViewerApiClient.send_control` + `main.py:run_gui` 結線。config `hand_control.enabled` 既定 off（+ GUI + `session_layer.enabled` 前提）で挙動不変。録音は PC 常駐・iPad は制御送信のみ（ADR-0039）。コマンド種別は `new_hand` / `winner` / `rebuy` / **`correct_board`(`index`) / `correct_seat`(`seat`)**（ミスディール訂正, ADR-0054）。`tests/test_control_queue.py` |
| **双方向 sync (S5 — state-based merge)** | ✅ 実装済 | `core/sync.py`（純粋マージ: UUID union + 単調解決で可換・結合・冪等 ⇒ 収束, ADR-0022。settlement は **paid_amount monotonic max** で partial-paid 対応, ADR-0024）+ `GET/POST /api/staff/sync/{snapshot,merge}`（staff-token gate, write 所有のみ merge 受理）+ `ViewerApiClient.{pull_sync_snapshot,push_sync_merge,sync_bidirectional}`。全 repo に `path` property、`LedgerRepository`/`OrderRequestRepository` に `reload()` を additive。ADR-0020 の単一書き手前提を更新（複数書き手 + 収束マージ）。`tests/test_sync.py` / `tests/test_viewer_api_sync.py` |
| **player 本人認証 L1 PIN** | ✅ 実装済 | `core/auth_token.py`（stateless 署名トークン）+ `core/player_credential_repository.py`（PBKDF2 + lockout、node-local `player_credentials.json`、read API / sync 非対象）+ `api/server.py` の principal レイヤ（`_resolve_player_principal`/`_require_player`）+ `POST /api/auth/login`・`/api/players/{id}/pin`。config `viewer_api.player_auth`（off/optional/required, 既定 **off で後方互換**）。`ViewerApiClient.{login,set_pin}`。staff token と直交（ADR-0027）。`tests/test_auth_token.py` / `test_player_credential_repository.py` / `test_viewer_api_auth.py` |
| **player 本人認証 L2 外部 IdP（コア）** | 🟡 一部実装済 | 詳細設計 ADR-0028 / 運用 ADR-0029 / 前提 merge ADR-0030（実装済）。**実 IdP 非依存コアは実装済（ADR-0031）**: `core/auth_identity*.py`（node-local, sync 非対象）+ `core/oidc.py`（provider 抽象 + `FakeOidcProvider` + `resolve_player_for_claim`）+ `POST /api/auth/{provider}/exchange`（app-driven, 既定 off）+ `ViewerApiClient.oidc_exchange`。principal は ADR-0027 トークンで L1 と合流、merge 後は canonical。`tests/test_{auth_identity_repository,oidc,viewer_api_oidc}.py`。**残**: 実 LINE/Google HTTP（token/JWKS）+ hosted（ADR-0029）= 実環境タスク |
| **player merge** | ✅ 実装済 | ADR-0030: `merged_into` の alias/tombstone（履歴 rewrite なし、append-only/収束 sync/player_id 不変を保つ・可逆）。`PlayerRepository.merge_players`/`resolve_canonical`/`equivalence_class`/`unmerge` + 全 player-keyed read（settlement 集計 / point / entry / order / viewer / login principal）を canonicalize + `core/sync.py:_resolve_player`（monotonic + tiebreak）+ staff API `POST /api/staff/players/merge` + registry GUI。schema `player` `1.0`→`1.1`。`tests/test_player_merge.py` 他 |
| **cross-app sync 拡張 (S5 後続)** | ✅ 実装済 | ADR-0032: player rename 伝播（`updated_at` LWW, schema `1.2`）/ hand log の file-level union（`merge_hand_logs` + snapshot `log_dir`）/ 定期 auto-trigger（`core/sync_scheduler.py`, config `sync_auto_interval_sec` 既定 0）。`_resolve_player` は display=LWW × `merged_into`=monotonic。`tests/test_sync*.py`。**残**: auto-trigger の常駐スレッド結線（運用タスク, ADR-0029 会場主導） |

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
   `--ledger`（viewer_api.enabled）+ スマホ注文の通し確認。**RFID 実機の bring-up は診断ツール
   `tools/probe_pcsc.py`（list/check/watch）+ 手順 `docs/hardware-qa-checklist.md` を用意済**
   （契約 §3-8 を production と同じ `PCSCBridge`/`RFIDThread` で検査）。残は実機を繋いだ通し確認。
3. ~~**PN5180 / ESP32-S3 firmware ↔ Python 契約固定**（ISSUE-0015）~~ → **✅ 完了（ADR-0034, ISSUE-0015
   Fixed）**: `docs/contracts/rfid-usb-ccid.md` v1.0 で USB descriptor / reader_name 規約 / ATR / Get UID /
   UID 4-7-8B を凍結。**残（実環境）**: firmware の VID/PID・実 reader_name を確定して契約 §2/§4 に追記。
4. ~~**S5 cross-app boundary（read + staff write + 双方向 sync）**~~ → **✅ 完了（ADR-0020 / 0021 / 0022）**:
   read = repository interface frozen + `api/client.py:ViewerApiClient` + round-trip test。
   write = スタッフ会計 write を `/api/staff/...` に staff shared token で公開 +
   `LedgerRepository` thread-safe 化（ADR-0021）。双方向 sync = state-based merge（`core/sync.py`,
   可換・冪等の UUID union + 単調解決）+ `/api/staff/sync/{snapshot,merge}`（ADR-0022, 複数書き手 +
   収束マージ）。sync 後続も **実装済（ADR-0032）**: player rename 伝播（`updated_at` LWW）・hand log の
   file-level union・定期 auto-trigger（`SyncScheduler`）。player per-player アクセス制御は **L1 PIN 実装済
   （ADR-0027）**。**残**: auto-trigger の常駐スレッド結線（運用タスク）。
5. **settlement / ledger 拡張**（**実装済**）: partial-paid（ADR-0023, `record_payment` / settlement
   schema `1.1`）、確定 commit + paid/unpaid/partial 切替（S4 GUI 精算パネル）、buy-in 金額プリセット
   （ADR-0026, `config.ledger.buyin_presets` + `--ledger` ボタン + staff API）。
6. **R 系の後続**: 派生 confidence の重み較正は **実装済（ADR-0033, property-based + 回帰ロック）**。
   **復元の正当性修正バッチも実装済（ADR-0047〜0050, 2026-08-19）**: 静かな誤り経路（金額/席パース・
   stale ctx・イベント消失・result/pot_total）・監査配線・制御語ガード・時刻整合・recorder 再構築・
   split pot・計測アライメント・fixtures 5→13。
   残: 実運用 review ログが貯まってからの数値較正（+ `engine.control_conf_threshold` の実運用値決め）、
   camera 源の統合、宣言した勝者の side pot 個別配分（手札で判定したときの side pot 個別勝者は **ADR-0062 で実装済**。V2 複数アクション発話は **ADR-0061 で実装済** = 店舗の実測に基づく分割）
   （ADR-0050 future scope）。
7. **player 本人確認の進化（ADR-0025 方針 / ADR-0027・0028）**: player_id を内部不変キーに保ち、
   認証を additive レイヤで重ねる — L0 name-pick（済）→ **L1 per-player PIN = ✅ 実装済（ADR-0027）**:
   node-local `player_credentials.json`（PBKDF2 + lockout、read API / sync 非対象）+ `core/auth_token.py` の
   stateless 署名トークン + player principal 解決レイヤ。config `viewer_api.player_auth` 既定 off で後方互換。
   → **L2 外部 IdP = 🔲 設計済・未実装（ADR-0028 詳細設計 + ADR-0029 運用設計）**: LINE / Google OIDC、
   `(provider, subject)→player_id` の `auth_identity`（多対一・player_id は外部 sub から導出しない）。運用は
   **会場 source-of-truth + cloud は player ミラー**（cloud は会計を originate せず signup/閲覧/注文+sync のみ
   公開）/ マネージド PaaS / LINE+Google / secret は PaaS env / PII 最小（APPI, sub のみ）/ cloud は会計 write
   無効・CORS 絞り（ADR-0029）。前提の **player merge は実装済（ADR-0030）**。**L2 の実 IdP 非依存コアも
   実装済（ADR-0031, `core/auth_identity*.py` + `core/oidc.py` + `POST /api/auth/{provider}/exchange`,
   既定 off）**。将来プレイヤーが LINE/Google でサインアップできる土台。**残（実環境タスク）**: 実
   LINE/Google provider の HTTP（token 交換 / JWKS）、hosted デプロイ（env override / cloud モード config /
   CORS 絞り / レート制限）、web redirect 変種。
8. **未実装の単機能**: Vosk 代替 ASR。（ディーラーボタン回転 / SB-BB 自動 post は **pokerkit
   backend で実装済** = ADR-0056 P0b / ISSUE-0032 Fixed。legacy backend は持たない。）
9. **UI 実運用機能の棚卸し台帳**: `docs/ui-feature-inventory.md`（1 卓 dogfood 前提で全 UI を
   ◎/○/△ に優先度づけ。**◎・○ は実装済**（ADR-0044/0045/0046 ほか, 2026-07-12）。残は △ 群 =
   プッシュ通知・通算成績・player 検索・オフラインキュー・多言語等で、ネイティブ配布・外販期に
   優先度を再評価する。新しい機能候補は本台帳に追記して管理する）。
10. **製品化（他店への販売, ADR-0063 / planned）**: 店舗 PC が正本のまま、クラウド（Cloud Run 東京 + Postgres 東京）で
   お客さんの店外閲覧・LINE / Google ログイン・注文受付と、運営の店舗管理を行う。新しい概念は店舗（store）のみ
   （店舗 PC の JSON と契約 schema は変えず、クラウドが同期受信時に端末トークンから店舗 ID を付ける）。会員は店舗を
   またいで同一人物、手札の公開範囲は設定で切り替え、プレイヤー課金なし。順番は フェーズ 1 店舗内の仕上げ
   （staff 画面の店舗 PC 配信 = ADR-0064 で実装済 / 自動起動 = ADR-0065 で実装済（店舗サーバー `--serve`）/ リリース版での更新 = ADR-0066 で実装済（`stable`）/ `SyncScheduler` の常駐結線）→
   2 クラウド最小（**設計済 = ADR-0067**: Cloud Run 東京 + **Supabase** Postgres 東京 / 店舗 PC → クラウドの一方向で変わった分だけ送る
   （`SyncScheduler` の常駐もここ）/ 主キーは店舗 ID + 元の ID / 許可リストのアプリ `cloud/app.py`（スタッフ・会計の受け口なし）/
   店外閲覧は店で渡す閲覧リンク / 公開範囲 `all`・`own_and_shown`。**2a クラウドの骨組み = 実装済**（`cloud/`、受け口・保存・push・端末トークン・管理）/ **2b 店舗 PC の送り手 = 実装済**（`core/cloud_sync.py` を店舗サーバーの中で常駐 = `SyncScheduler` の常駐結線の代わり、`tools/cloud_link.py`、`/api/health` の `cloud`）/ **2c 閲覧リンク = 実装済**（スタッフ画面「プレイヤー管理」の「閲覧リンク (QR)」→ お客さんは店外から自分の記録だけ。`mobile/` のクラウド用ビルド `EXPO_PUBLIC_CLOUD=1`）/ **2d 公開範囲 = 実装済**（§ viewer API の「ホールカードの公開範囲」）、2e は planned、登録手順 `docs/cloud-setup.md`）→
   3 ログインと会員 → 4 外販の準備。製品化の作業は推論・音声（`integration/`・`audio/`）に触れない。

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
| **WS4** | **staff iPad app（会計/注文/座席/ハンド 実装済）** | 店舗操作（会計/注文/座席/ハンド）を **スタッフ専用タッチ front-end** に統合（player `mobile/` とは別アプリ・Expo/RN・iPad/web）。staff API 上に実装 | `staff/*`（4 タブ実装済）, `StaffRepository`(mock/HTTP), staff API（ADR-0038/0039） | WS0（staff API 契約）。会計/注文/座席/ハンド + ハンド履歴リプレイ（ADR-0044）とも実装済。残: 実機 QA |

> **WS4 = 店舗用 staff iPad アプリ（ADR-0037/0038, ISSUE-0020）**: 現状の店舗操作は desktop customtkinter
> （`gui/` の 5 画面・`main.py` の別プロセス起動）+ staff write API（`/api/staff/...`, ADR-0021）に分散。
> これを **1 つの Expo/RN staff アプリ**（`staff/`, 卓単位タブ統合 = 会計✅ / 注文✅ / 座席✅ /
> ハンド✅）に畳む。認可は staff shared token、**録音（音声/RFID）は PC 常駐のまま・iPad は操作**
> （ADR-0037 §4）。**会計/注文/座席/ハンドタブ + session 作成/close は実装済**（`staff/`, mock/HTTP
> repository 注入・Login → SessionList → TableView、typecheck + 34 mock tests + web export +
> Playwright E2E 8 件）。staff API は **ADR-0038 §A/§B 実装済**（reversal/grant + session/座席/player
> ライフサイクル）+ **ADR-0039 §C 実装済**（hand logger 遠隔制御 = control queue, `core/control_queue.py`
> + `integration/control_consumer.py` + `POST .../control`, 既定 off, 634 passed）。会計タブは entry
> 一覧 + 取消(reversal) まで実装済。ハンド履歴 read + リプレイは **ADR-0044 で実装済**。**残**: 実機タッチの
> 手動 QA。desktop GUI は当面並存（破壊しない）。未決事項は ISSUE-0020。

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
  （PC/SC, canonical 移行後）→ `rfid.enabled=false` で RFID なしモード継続動作（詳細挙動は ISSUE-0015）。
  PC/SC のリーダー（または中継）につながらなくても**ロガーは止まらず 5 秒ごとに試し直す**（ADR-0070）
- **読み取り装置（ESP32）の再起動は卓に札が無いときだけ送る**（再起動のあいだは全リーダーが読めない）。`rr` は
  卓が 2 秒空になってから、自動（`rfid.auto_restart`）は使えないリーダーがあり卓が 10 秒空のときだけ・1 つの不調に
  つき 2 回まで（ADR-0070）。札が載っていれば片付くのを待つ
- **マイクを開けない・切れたときも止まらない**: 起動時に開けなくても、録音の途中で切れたとき（読み取りが 3 回続けて
  失敗）と同じく、待ちを倍々に延ばしながら（1〜10 秒）名前で探し直して開き直し続け、開けたら「● マイクを開けました」と
  出して聞き取りを始める（ロガーを起動し直さない。警告は 1 回だけ = ログを埋めない）。`audio.device_name` のどの名前も
  見つからなければ別のマイクでは録らない
- pokerkit 未導入で `engine.backend="pokerkit"`（既定）→ warning を出して legacy backend に
  自動フォールバック（起動は落とさない。rules-aware 機能は無効）
- **進行中のハンドが無いときのアクション / winner は状態に適用せず「unresolved レコード」にする**
  （traceback にしない, ISSUE-0028 / ADR-0047 B2）。`PokerEngine.is_hand_active()`（legacy は常に
  `True` = 挙動不変）で判定し、`actor_source="unresolved"` / `apply_ok=false` / `reason="no_active_hand"`
  の needs_review レコードを **on_action にだけ**流す（HandSummary.actions には積まない = ゲーム状態も
  記録も変えない。CLI は `[未適用]` と表示）。ログは「先に新ハンド（CLI の `n`）を」と案内する。
  winner はハンドが無ければ確定しない（確定済みハンドへの再宣言によるポット二重加算を防ぐ）。
  席の無い winner は ADR-0047 B5 の連鎖（唯一の active 席 → 最後のアグレッサー + review →
  手番席 + review）で補う（`engine.auto_winner` ではショーダウンなら先に手札で判定, ADR-0062）。
  ベッティングが終わったあと（ショーダウン待ち）の check / call 等は `reason="betting_over"`。
- **ショーダウンでは手札より「見せずにマック」を優先する**（ADR-0062 / ADR-0068）。ベッティングが終わった時点で手札から
  勝者を決めてはならない（見せずにマックした人は手札が強くてもポットを失う）。手札で判定するのは残った全員が見せた
  とき・役名もマックも 8 秒無いとき（「ハンド終了」/ 次の配布も）だけ。
- **読めない札ではチップを動かさない**（ADR-0068）。ショーダウンで読めていない札しだいで勝者が変わるなら、仮の勝者に
  せず各席が出した額を返して確定する（`winner_source=undetermined`, 要確認）。読めなかったボードの位置は `??` のまま
  にし、あとの札（ターン）をフロップの空きに詰めない。
- **配布の検出（RFID）は音声より先に届く**ので、配る前に話し始めた発話（認識待ち）を反映してから新しいハンドを
  始める（`speech_backlog`, ADR-0062）。前のハンドの最後のアクションを新しいハンドに入れない。配ってから話し始めた
  発話は待たない（話し声が続いてハンドが始まらなくならないように）。
- **ストリートの札より前に言ったアクションはそのストリートのものにしない**（ADR-0068）。ターンの札の配布・チェック
  アラウンドの宣言より前にターンのアクションが言われることはない。札の 3 秒以上前に話した語は前のストリートの言い直し
  （チェック・コール）か前のストリートの賭けとして組み直し、直せなければ要確認にする。
- **「フォールド」の語と札の離脱は 1 対 1 に組む**（`engine.rfid_folds`, ADR-0068）。札が席に残っているのに語だけで
  次の手番の人を降ろさない・1 つの離脱に 2 つの語を付けない。札が戻ったらそのフォールドを取り消して組み直す。
  離脱の判定・取り消しは記録（events.jsonl）に残し、replay も同じ判断にする。
- ゲーム状態の変更は IntegrationThread に一元化する。GUI/CLI の操作（新ハンド/ウィナー/リバイ/
  ミスディール訂正/ボタン/ブラインド/席の参加・休み）は `AudioEvent` として queue に積む（`GameStateManager` はロックを持たないため
  直接変更禁止, ISSUE-0012）
- **カードの物理的な載せ替え（ミスディール）は「消えたこと」だけで判断しない**（ADR-0054 / ADR-0058）。
  「カードが見えなくなった」ことだけを載せ替えと解釈してはならない（一瞬の読み落ちと区別できず、
  同じ札が 2 か所に出る / 枚数が水増しされる = ISSUE-0025/0026 の壊れ方が再発する）。配り直しは
  「前の札が `release_sec` 以上見えず、**新しい札が載り続けた**」ときだけ自動で反映する（ボードの 1 枚だけの
  差し直しは、前の札が消えた近く（flop = 同じリーダー、最後に配った turn / river = 同じか隣のリーダー）へ置かれ、
  前の札が `redeal_confirm_sec` 見えないときだけ。そのハンドで一度でも読めなくなった札は対象にしない。差し替えた
  札が戻り、差し替えた札も載っていれば取り消す。flop は全部消えたとき、5 枚埋まったボードは消えた札がちょうど 1 枚のとき = その位置に入れる）。明示コマンド（`cb` / `cs`）も使える。そのハンドで席に記録した札はボードの札にしない
- RFID HTTP 受信は `bind_host` 既定 `127.0.0.1`（無認証のため。LAN 受信は明示的に変更）+
  `Content-Length` 上限 16KB（413）

---

## よく使うコマンド

```bash
pip install -r requirements.txt              # core runtime（または pip install .）
pip install -r requirements-dev.txt          # テスト依存（CI と同じ。skip 0）
pip install ".[pcsc]"                         # RFID PC/SC を使う場合のみ
pip install ".[api]"                         # viewer API を使う場合のみ (M1)
install.cmd                                  # Windows 店舗 PC: ダブルクリックで Python 3.12 導入 → venv → 依存 → config → ショートカット (ADR-0057)
update.cmd / uninstall.cmd                   # 更新（config / データ / rfid_cards.json は保持）/ ショートカットと venv の削除
#   リリース版にする: update.cmd -Branch stable（1 回。以後覚える, ADR-0066）。いまの版: python main.py --version
python tools/release.py status               # リリース版（stable）と開発版（verify-v1）の差 (ADR-0066)
python tools/release.py promote --dry-run     # 開発版の先頭をリリース版にする（--dry-run を外すと stable を進めてタグを push。前に進めるだけ）
#   1 行版: irm https://raw.githubusercontent.com/reo-sato/pokerapp/verify-v1/installer/bootstrap.ps1 | iex
#   検証: pwsh -File installer/install.ps1 -DryRun -NonInteractive -SkipModel（何も変更せず全手順を表示）
python main.py --cli                         # CLI モード (hand logger)。マイク無しで回すには config の audio.enabled=false
#   --cli の入力: q=終了（確定していないハンドも閉じて保存）/ n=新ハンド / w <席>=ウィナー（分けたら w <席> <席>）/
#   r <席> <金額>=リバイ（スタック 0 の席はこれで買い足すまで配られない）。
#   RFID があれば手札を配るとハンドが始まり（n 不要）、フォールドは札の離脱、勝者は自動（全員フォールド / ショーダウンは
#   役名 = 見せた・「フォールド」= 次の人のマック、全員見せたか 8 秒で手札から判定。決まらないときだけ w）。お知らせは「● …」(ADR-0062/0068)
#   ミスディール訂正: cb <位置>=ボード N 枚目を取り消し / cs <席>=その席の札を読み直し (ADR-0054)
#   blinds <SB> <BB>=次のハンドからブラインドを変える / button <席>=ボタンを直す（ハンドの途中ならそのハンドを組み直す）
#   rr=読み取り装置（ESP32）を再起動（卓に札が無いときに送る, ADR-0070）/ 「失礼しました」=口頭の訂正（打っても可, ADR-0068）
#   席替え（session_layer.enabled=true）: name <席> <名前>=次のハンドからその席の人 / name <席> -=空席（次のハンドから配られない） (ADR-0059)
#   コマンドは全角でも可（`ｎ` / `ｗ　１`。照合前に半角へ寄せる, ISSUE-0030）。`w1` のように
#   空白を打ち損ねても通る（w/r のみ, ISSUE-0034）
python main.py --cli --log-file           # ログを端末に出さない（実機テスト推奨。卓は table_monitor で見る）
#   それ以外の行は読み上げ文として parse_actions に通る（例: チェック / シート3 コール / ベット 500。
#   「フォールド フォールド コール」は順に 3 アクション, ADR-0061。数字だけの `600` = ベットかレイズ）。音声が有効なら、
#   打った行は打つ前に話し始めた発話の反映を待ってから入る（前のハンドの発話を追い越さない）。
#   席の代わりにポジション名でも可（例: BTN コール / ビッグブラインド チェック, ISSUE-0032）
#   ひらがなも可（照合前にカタカナへ正規化, ISSUE-0027。ASR の書き起こし揺れにも効く）
#   起動時の「席」は人数（`6` = 1〜6 番）か使う席番号の並び（`2 4 5 8`。空いている席を飛ばして座る卓）。
#   「1ハンド目のボタン席」も訊く（使う席のどれか。空 Enter = 最大の席番号。以降ハンドごとに回る）
python main.py --cli --log-file --script voice   # 台本のハンド（声だけ, start_script.cmd。画面は :8791/script = 真のアクション入力のサーバー）
python main.py --cli --log-file --script cards   # 札の確認（RFID, start_cards.cmd）
python tools/ground_truth_ui.py --host 0.0.0.0 --port 8791   # 真のアクション入力（start_truth.cmd。--evaluation か config ground_truth.evaluation=true で評価のモード）
python main.py                               # GUI モード (hand logger)
python main.py --players                     # Player Registry 画面 (S1, 別画面)
python main.py --sessions                    # Session / Seating Viewer (WS2-α, read-only, 別画面)
python main.py --ledger                      # Ledger Viewer/Editor + 注文確定 画面 (S3.2/M5, 別画面)
python main.py --export-ledger logs/ledger_export  # settlement / cashflow CSV 出力 (S3.3)
python main.py --viewer-api                  # player 向け読み取り専用 viewer API (M1, ADR-0017)
python main.py --serve                       # 店舗サーバー（画面なし）: 会計の書き手の API + お客さん / スタッフ画面 + 卓モニタ (ADR-0065。自動起動は server_autostart.cmd)
python main.py --viewer-api --host 0.0.0.0 --port 8788   # 店の Wi-Fi のスマホから http://<PCのIP>:8788/ (画面 + API, ADR-0059)
python scripts/build_player_web.py            # mobile/ を変えたらお客さん向け画面を作り直す (店舗用 api/static/player/ + クラウド用 cloud/static/player/, 要 Node, ADR-0059/0067)
python scripts/build_staff_web.py             # staff/ を変えたらスタッフ用画面を作り直す (api/static/staff/ = http://<PCのIP>:8788/staff/, 要 Node, ADR-0064)
python tools/set_config.py session_layer.enabled true   # config.json の 1 項目を変える (BOM なし。値を省くと表示)
pytest -m "not slow" -n auto                 # CI の速い組（変更ごとに回す。vision 除外・印・打ち切りは pyproject.toml / tests/conftest.py）
pytest -m slow -n auto                       # CI の遅い組（推定器の店舗のハンドなど。CI は 2 つを別のジョブで同時に + ruff check .）
#   クラウドの保存の試験は POKERAPP_TEST_DATABASE_URL（Postgres）があるときだけ（CI はサービスの Postgres 16）
CLOUD_DEV_MEMORY=1 CLOUD_ADMIN_TOKEN=<32 文字以上> python -m cloud   # クラウドのアプリを手元で（メモリに保存, ADR-0067。本番は DATABASE_URL）
python tools/cloud_link.py link --url https://<クラウド> --name "店名"   # 店舗 PC をクラウドにつなぐ（管理トークンを伏せ字で入力。status / push / reissue / unlink も, ADR-0067 2b）
python tools/replay_hand.py tests/fixtures/reconstruction/silent-fold  # 決定的 replay (F1)
python tools/calibrate_confidence.py            # 派生 confidence 較正サーフェス + P1〜P8 検証 (ADR-0033)
python tools/bench_hands.py --twice --search-check --workers 3   # 推定器の物差し: 開発データの全部正しいハンド (docs/estimator.md)
python tools/estimate_logs.py logs --latest --shadow --workers 1  # いちばん新しいセッションを推定して影のファイルに (ハンドごとの時間)
python tools/bench_hands.py --update-record     # 開発データの物差しの前の記録（tests/fixtures/store/bench_record.json）を書き直す（新しい要確認なしの誤りは終了コード 1）
TZ=Asia/Tokyo python tools/llm_candidates.py --split A --dry-run   # 実験（打ち切り済）: LLM に聞くハンドの数と費用の目安（API を呼ばない）
TZ=Asia/Tokyo python tools/llm_candidates.py --split A --limit 10 --model claude-haiku-5-5   # 実験: LLM に聞いてキャッシュ（要 ".[llm]" と POKERAPP_ANTHROPIC_API_KEY。クラウドのセッションでは ANTHROPIC_API_KEY が外される）
TZ=Asia/Tokyo python tools/bench_llm.py --split A --model claude-haiku-5-5   # 実験: 今の推定器 / 提案つき / LLM が選ぶ / LLM 単独を比べる（キャッシュだけ）
TZ=Asia/Tokyo python tools/forward_ledger.py     # 前向きの成績表: 各セッションを初めて見たときの版で採点（精度はこれで見る, ADR-0056 追記 6。約 30 分）
TZ=Asia/Tokyo python tools/compare_versions.py b2f1e5a <zip>   # 前の版と今の版でハンドごとに比べる（固定の前の前向きの確認, ADR-0056 追記 5）
python tools/pack_logs.py --audio            # 店舗 PC: ログ（+ 発話の音声）をデスクトップの zip に（pack_logs_audio.cmd。--hours / --session / --text）
python tools/eval_store.py pokerlogs_<日時>.zip   # 店舗のログをいまのコードで再生して評価（真のアクションのメモ・一致率・音声の改善を終える目安。分けた zip はまとめて渡す）
python tools/eval_store.py logs --session <sid> --timeline   # ハンドごとに発話・札・信号の時系列と行の比較（--export-fixture tests/fixtures/store で開発データに）
python tools/rescore_audio.py --session <sid>   # 店舗 PC: 保存した発話を Whisper で採点し直す（<sid>.rescored.jsonl, 営業のあとに）
python tools/second_ear.py --session <sid>      # 保存した発話を第 2 の耳で聞き直す（<sid>.ear.jsonl、--whisper で Whisper の別のやり方も, ADR-0069）
python tools/short_words.py eval                 # 短い語の分類器をセッションを 1 つずつ抜いて確かめる（train で audio/short_word_model.json に, ADR-0069）
python tools/review_page.py tests/fixtures/store/<日付-sid>:<ハンド> --out review.html   # 開発データのハンドを入力画面と同じ形の HTML 1 枚に（--audio / --audio-out で WAV）
python tools/read_corpus.py eval <logs か zip>    # 読み上げ集の句ごとの正解と聞き取りを比べる（serve = :8793/corpus の画面だけ）
python tools/measure_capture_accuracy.py --session logs/<sid>.json --ground-truth logs/<sid>.ground_truth.json  # Phase A 捕捉精度計測 (docs/dogfood/measurement-plan.md)
python main.py --export-phh logs/session_xxx.json
# ローカル QA（実機なし。docs/manual-qa-checklist.md 参照）
printf 'ハンド開始\nチェック\nシート1 ウィナー\n' | python tools/play_hand_text.py - --seats 3  # mic 不要のテキスト駆動再構築
python tools/simulate_rfid.py register-demo && python tools/simulate_rfid.py board Ah Kd Qs  # 実機なし RFID 注入 (HTTP)
# 実機 RFID QA（PC/SC canonical。docs/hardware-qa-checklist.md 参照, 要 pip install ".[pcsc]"）
# 卓状態モニタ（実プレイ環境の検証。音声なし・アクションはダミーで可, ADR-0056 D5）
python tools/table_monitor.py --host 0.0.0.0 --port 8790   # iPad/スマホから http://<PCのIP>:8790/
python tools/table_monitor.py --once                        # 端末に 1 回表示
python tools/analyze_table_state.py                         # 反映遅延 / 不在時間 / ストリート遷移の実測 (D7)
# マイク / 音声認識の確認（音声テストの前に, ADR-0060。docs/troubleshooting.md）
python tools/audio_check.py list                # 録音できるデバイス（番号・方式・16 kHz の可否、config の audio.device_id に印）
python tools/audio_check.py level --seconds 15  # 入力レベル（発話として拾えるか）
python tools/audio_check.py listen --seconds 120  # 本番と同じ経路で聞き取り →「何と聞こえた → どのアクション」（初回はモデル取得。配る人が替わるたびに）
python tools/audio_check.py bench               # 保存した発話で Whisper のスレッド数・ビーム幅ごとの時間を測り、速い設定の config コマンドを出す（マイク不要）
python tools/audio_check.py gain --session <sid>  # 保存した発話を、そのままと音量をそろえた音で聞き直して比べる（音量が原因か）
python tools/probe_pcsc.py list    # reader_name 列挙 + 物理リーダー台数 + 使えるリーダーの一覧 + config 突き合わせ (契約 §3-4・§6 v1.11)
python tools/probe_pcsc.py check   # config lint + connect 検査 (§5, カード不要)
python tools/probe_pcsc.py watch --seconds 30  # 実 RFIDThread でタップ確認 (UID/役割/hot-plug, §6-8)
python tools/probe_pcsc.py raw --seconds 20    # pyscard 直叩き: OS のスロット状態 + connect/Get UID の例外(hresult) を表示 (watch 0 件の切り分け)
python tools/probe_pcsc.py restart             # 読み取り装置（ESP32）を再起動して使えるリーダーの一覧を出す（卓に札が無いときに, v1.11 §6）
#   RDP の中からは PC/SC が見えないので、店舗 PC では上の list / check / watch も中継を通す（中継が動いていれば自動, ADR-0070）
python tools/rfid_relay.py status               # 中継が動いているか・リーダーごとの読み取り（使えないリーダーは ✗）
python tools/rfid_relay.py watch --seconds 60   # 札を置く・外すたびに 1 行（中継を通した読み取りの確認）
python tools/rfid_relay.py restart              # 中継から読み取り装置を再起動してつなぎ直す（卓に札が無いときに。直らなければ ESP32 の給電アダプタを抜き挿し）
#   中継の登録（管理者の PowerShell）: Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File C:\PokerHandLogger\installer\rfid_relay_task.ps1'
python tools/register_cards.py run --deck 1    # 実カード UID を rfid_cards.json に登録 (タップ駆動, 2 デッキ目は --deck 2, 中断/再開可, list/unregister あり)
python tools/register_cards.py swap-suits d c  # 登録済みの札の 2 つのスートを入れ替える（取り違えたとき。もう一度で元に戻る、前の登録はバックアップに）
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

### 7b. 店舗テスト中の軽い運用（オーナー決定, 2026-09-25）

- 店舗での実機テストを受けた修正を繰り返している間は、文書は **CHANGELOG と worklog だけ**を短く更新する。
  ADR / ISSUE / CLAUDE.md / usage / troubleshooting / decision-log は、動きが固まった時点かオーナーの依頼で
  まとめて更新する。
- 手元で変更部分のテストと全体テスト（1 回）が通ったらプッシュし、**CI の完了を待たずに**更新コマンドを渡す。
  CI はあとで確認し、失敗したときだけ知らせる。
- 2026-09-25〜10-08 のあいだ後回しにした文書（CLAUDE.md・ADR-0068〜0070・ADR-0043 追記・ISSUE-0039 / 0040 など）は
  2026-10-13 にまとめて追いついた（作業ログ `docs/worklog/2026-10-13-docs-catch-up.md`）。

### 8. 完了条件

- code / tests / docs（CLAUDE.md / ADR / worklog / issue / decision-log / CHANGELOG の該当箇所）が
  全部揃って初めてタスク完了とする。
- 完了報告には changed files / 期待挙動 / 実装挙動 / mismatches / fixes / tests / docs / 残課題 を含める。
