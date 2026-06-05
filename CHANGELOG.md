# Changelog

本ファイルは [Keep a Changelog](https://keepachangelog.com/) の慣習に沿って、
ユーザー可視の挙動変更および仕様 / docs の重要更新を記録する。
詳細な経緯は `docs/adr/` / `docs/worklog/` / `docs/issues/` を参照。

## [Unreleased]

### Removed (camera / vision — sprc_v4.docx 準拠の廃止, ADR-0011)

- **カメラ入力経路を全面削除**し、RFID + 音声の 2 ソース構成に確定（sprc_v4.docx が
  「カメラ廃止」を明記。これまで `vision/` は legacy として残置されていた）。**音声 + RFID の
  挙動・JSON/PHH 出力は不変**（camera は実運用で未接続だったため挙動退行なし）。
  - 削除: `vision/`（`camera.py` / `motion_detector.py` / `calibration.py`）、`tests/test_vision.py`、
    `config_default.json` の `camera` ブロック、`requirements.txt` の `opencv-python` / `easyocr`、
    `main.py --calibrate`（ROI キャリブレーション）。
  - `core/events.py`: `CameraEvent` データクラスと numpy 依存を削除。`core/event_queue.py`:
    `make_camera_queue` と `EventItem` の `CameraEvent` を削除。
  - `integration/engine.py`: confidence 行列を 2 ソースに再構成（`calc_confidence(has_rfid, has_audio)`、
    RFID+audio=0.95 / RFID=0.70 / audio=0.50 / なし=0.00）。camera バッファ・`_drain_camera_queue` /
    `_pop_matching_camera_event` を削除し、`CAMERA_BUFFER_TTL` を `BUFFER_TTL` に改名（RFID バッファ
    失効に流用）。`ActionRecord.source` は `{"audio", "rfid"}`（sprc_v4.docx §6.1 と一致、`camera` キー廃止）。
  - `main.py` / `gui/dashboard.py`: camera スレッド起動・`camera_queue` DI・「カメラ」ソース表示を削除。
  - tests: `test_phase7.py` / `test_integration.py` を 2 ソースへ更新（camera 照合テストを除去、
    audio 単独 confidence を維持）。`test_gui` / `test_logger` / `test_phh_exporter` の source dict から
    `camera` キーを除去。全 199 テスト green。
  - **ADR-0011** (Accepted): sprc_v4.docx 準拠で camera/vision を削除する判断。

### Added (WS2-α — Session / Seating Viewer, desktop read-only)

- **read-only な Session / Seating Viewer を別ウィンドウで追加**（S2 core + Phase 2.2/2.3 で
  記録された session / seating を人間が覗くための inspection 専用 GUI。編集機能なし）。
  - `gui/session_viewer.py`（新規）: `SessionViewerWindow` + view model（`SessionDetail` /
    `SeatRow` / `HandAssignmentRow`）+ pure ヘルパ `resolve_display_name` / `build_session_detail` /
    `build_name_map`。session 一覧 → 選択 → 概要 / current seating / hand 別 seat assignments を表示。
    `player_id` を `display_name` に解決（未解決は `(unknown)` + player_id 併記）。手動 Refresh。
    empty state（session 0 件 / seating 無し）を明示。
  - `core/session_repository.py`: read-only helper `list_hand_ids(session_id)` を追加（viewer 用）。
  - `main.py`: `--sessions-viewer` で起動する `run_session_viewer()` を追加。
  - `gui/dashboard.py`: session レイヤ有効 + `session_repo` 指定時のみ「Session Viewer」ボタンを表示し
    Toplevel で開く（`session_repo` を optional DI 追加。flag off / 既存 UX は不変）。
  - `tests/test_session_viewer_gui.py`（新規）: pure ヘルパ / empty state / 自動選択 / seating 解決 /
    unknown player / refresh 再読込 / read-only 性。
  - Out of scope（ISSUE-0012）: 作成・編集・削除、filter/search/sort、live auto-refresh、export、
    mobile/web、ledger 連携。

### Added (Phase 2.3 — seat selection UX, desktop minimal)

- **desktop hand logger に最小 seat selection UX を追加**（ISSUE-0006 を部分解決、ADR-0008 Pattern A
  の seating 入力経路）。session レイヤ有効時のみ有効、**flag off では既存 UX を完全維持**。
  - `gui/seat_assignment.py`（新規）: `SeatAssignmentDialog`（モーダル）+ `build_seating_map`
    （seat→display_name → seat→player_id の純粋変換）+ `EMPTY_LABEL`（空席）。player 候補は
    `PlayerRepository.list_players()` の `display_name`（内部値は `player_id`）。
  - `gui/dashboard.py`: `GUIDashboard` に `player_repo` / `session_layer_enabled` を DI。session
    レイヤ有効時のみ「席割り当て」ボタンを表示。「新ハンド」は seat ダイアログ（carry-forward 初期値）
    → OK で `update_seating` → new_hand、キャンセルで hand 開始せず。
  - `integration/engine.py`: `IntegrationThread.update_seating()` / `get_seating()`（lock 保護の
    スレッド安全 API）。`_assign_seats_for_hand` / `_finalize_hand` は lock 越しに seating を読む。
  - `main.py`: `_init_session_layer()` が `PlayerRepository` も生成（session_repo と共有）し GUI に渡す。
    初期 seating は空のまま、実行時に GUI で確定する。
  - `tests/test_seat_assignment_gui.py`（新規）: dialog ロジック / dashboard seat フロー（flag on/off,
    OK/キャンセル）。`tests/test_session_integration.py`: `update_seating` / carry-forward / hand 間
    seat 変更 / flag off no-op を追加。
  - 残（ISSUE-0006）: sitting_out / 未登録 player 追加 / seat change 履歴 UI / 同一 hand 重複 player の
    事前バリデーション / mobile UX。

### Added (Phase 2.2 — session/seating integration, config-gated)

- **hand logger × S2 session/seating の最小統合**（ADR-0008 Pattern A, write-through）。
  `config.session_layer.enabled`（既定 false）で切替可能。**PHH はバイト不変、JSON は additive のみ**。
  - `config_default.json`: `session_layer.enabled`（default false）+ 説明コメントを追加。
  - `integration/engine.py`: `IntegrationThread` に `session_repo` / `session_id` / `seating` を DI。
    flag on で hand 開始時に `SessionRepository.assign_seat` バッチ、hand 確定時に
    `HandSummary.players[i].player_id` を additive 付与、`HandSummary.session_id` を session レイヤの
    UUID4 hex に切替。`assign_seat` 失敗は warning に留め hand logger を止めない。
  - `main.py`: `_init_session_layer()` を追加。flag on で `create_session()`（UUID4 採番）→
    JsonWriter / IntegrationThread に DI。flag off は従来の timestamp session_id（rollback path）。
  - `core/hand_log.py`: `HandSummary.session_id` / `players[i].player_id` の additive 意味を明記
    （構造変更なし）。
  - `tests/test_session_integration.py`（新規）: ON（assign_seat + session_id/player_id 反映）/
    OFF（SessionRepository 不使用・キー集合互換）/ assign_seat 失敗時の耐障害性。
  - seat 選択 UX（ISSUE-0006）は未実装のため `main.py` の seating は現状空。HandSummary schema の
    `1.0` freeze は引き続き保留（ISSUE-0005）。

### Added (Phase R2 — pokerkit game-state backend, preview / default-off)

- **pokerkit を live ルール権威にした game-state backend**（ADR-0009, **default-off の preview**）:
  ノイジー入力からの正確な再構築のため、actor 順（ポジション順）/ 合法手集合 / amount_to_call / min-raise /
  **side-pot** を pokerkit に委ねる backend を追加。**既定 `legacy` で挙動不変**、`config.engine.backend=pokerkit`
  で opt-in。
  - `core/poker_engine.py`（新規）: `PokerEngine` Protocol（legacy/pokerkit 共通 I/F）＋ `PokerkitGameState`
    ＋ `create_game_state` factory。pokerkit は **遅延 import**（未導入でも legacy は動く）。announced winner を
    手動 push（pokerkit auto-showdown はダミーカードのため無効化）、side-pot スナップショット、seat↔index 固定。
  - `main.py`: `_make_game_state(cfg, ...)` で backend 選択（CLI/GUI 両経路）。`config_default.json` に
    `engine.backend: "legacy"` を追加。
  - **ISSUE-0008（Fixed）**: pokerkit 0.7.4 で必要 API（actor / 合法手 / min-raise / amount_to_call / side-pot の
    incremental 露出、不正額の `ValueError`、`HOLE_DEALING` でカード不要駆動）を spike で実機確認。ADR-0009 の
    gate 解除。**ADR-0009 を Accepted**（R2 engine 実装済 / R3 は planned）。
  - tests: `tests/test_poker_engine.py`（11: actor 順 / legal_context / street 自動進行 / 不正・非手番拒否 /
    side-pot / winner award / rebuy / **allin ショートスタック call-all-in**）。**全 198 passed**。
  - review fix: `apply_action("allin")` を「raise 可なら max へ raise、不可だが call 可なら call-all-in」に
    分離（レイズ不可なショートスタックの「オールイン」での pokerkit state desync を防止）。
  - 既知の差（legacy より正確側・preview）: ブラインド自動 post、合法手のみ受理（raw ASR の射影は R3）、
    street は betting 完了で自動進行。**live 既定動作（legacy）は不変**。

### Added (Phase R1 — event recording sidecar)

- **生センサーイベントの append-only sidecar 記録**（ADR-0010, record-only 先行実装）:
  `IntegrationThread` が**解釈する前**に各 `AudioEvent` / `RFIDEvent` / `CameraEvent` を
  `reconstruction_event` envelope（camera frame 除外）として `logs/{session_id}.events.jsonl` へ 1 行追記する。
  再構築ロジックは不変で、**recorder 未指定（既定）なら挙動完全不変**。
  - `output/event_recorder.py`（新規）: `EventRecorder` ＋ `event_to_envelope()`。append-only・スレッド安全・
    I/O 失敗で再構築を止めない。
  - `integration/engine.py`: `IntegrationThread(event_recorder=...)` を additive 追加。3 つの dequeue 点
    （audio get / camera drain / rfid drain）で解釈前に `_record()`。default None = 従来動作。
  - `main.py`: `config.recording.enabled`（既定 false, opt-in）で `EventRecorder` を構築し CLI / GUI 両経路で注入。
    `config_default.json` に `recording.enabled: false` を追加。
  - `docs/contracts/schemas/reconstruction_event.schema.json`（v0.1, `additionalProperties:false`）＋
    `fixtures/reconstruction_event/`（canonical / valid-* / invalid-*）。`tests/test_contracts.py` の `_MODELS` に登録。
  - tests: `tests/test_event_recorder.py`（envelope / JSONL / code↔contract）、
    `tests/test_integration_recording.py`（engine→recorder e2e / recorder 未指定で sidecar 無し）。
    **全 187 passed**（`pytest tests/ -q --ignore=tests/test_vision.py`）。
  - **ADR-0010** を Accepted に更新（R1 実装済。R4/R5 = hand/action freeze・replayer は planned）。

### Docs / Planning (Phase R0 — rules-aware reconstruction & contract-first hand core, 設計提案)

- **ハンド再構築エンジンと contract-first hand core の設計提案**（**docs-only, `.py` / schema / fixtures は
  未変更**）: 目的（ノイジーな ASR＋RFID からの正確な再構築）と思想（contract-first / fixtures-as-oracle）の
  両面のギャップに対し、再構築を「ルール制約付き状態推定」として捉え直し、既存依存 pokerkit を live
  ルール権威に据える方針を提案。
  - **ADR-0009** (Proposed): `pokerkit.State` を live ルール権威として採用し、その合法手制約で再構築する
    （ルール制約付き状態推定）。現 `GameStateManager` の安定 I/F 背後で `engine.backend` フラグ選択、raw ASR を
    直接流さない「境界での推定」、出力は additive。actor 推定（手番 prior × sensor ＋ silent-fold 自動合成）、
    `apply_corrections()`（合法手制約・call/check の状態一意化・amount スナップ）、派生 confidence（8 行固定
    テーブルの置換）と `needs_review` 条件の明文化。`pokerkit>=0.5.0` は宣言済みだが**未 import** である事実を
    明記（`game_state.py` の Phase 3 TODO の具体化）。当初の engine / algorithm 2 案を 1 ADR に統合。
  - **ADR-0010** (Proposed): hand core の contract 化（`hand`/`action`/`reconstruction_event`）と決定的
    record/replay（append-only event sidecar、注入クロック、golden fixtures を core の oracle に）。ADR-0008
    と整合し hand-logger immutability を維持。
  - `docs/contracts/hand-reconstruction.md`（新規 draft）: `PokerEngine` interface 草案 / actor 推定 /
    `apply_corrections` 修復表 / 派生 confidence / `hand`・`action` の inline schema sketch（freeze せず）。
  - `docs/contracts/event-replay.md`（新規 draft）: record/replay harness / 決定性条件 / `reconstruction_event`
    envelope sketch / golden-fixture レイアウトとテスト計画。
  - **ISSUE-0008**（Open）pokerkit online API 実現性（ADR-0009 の gate）/ **ISSUE-0009**（Open）actor 競合・
    silent-fold ポリシー / **ISSUE-0010**（Open）replay 決定性の記録境界 / **ISSUE-0011**（Open）hand/action
    schema freeze blockers（ISSUE-0005 の hand core 版）。
  - **decision-log.md** に ADR-0009/0010 と ISSUE-0008..0011 を登録。**CLAUDE.md** Future Scope に
    rules-aware reconstruction の planned/proposed 行を追加。
  - **実装は別タスク**（提案フェーズ R0）。段階導入順は R1 record-only → R2 pokerkit engine（flag）→
    R3 actor/corrections/fusion → R4 contracts → R5 freeze + session 統合。

### Docs / Planning (Phase S2.x — hand logger × session integration strategy)

- **Hand logger × session/seating integration の戦略 planning**（docs-only, code 未変更）:
  既存 hand logger world（`HandSummary` / `JsonWriter` / `PHHExporter` / `IntegrationThread` /
  `GameStateManager` / `main.py`）と S2 core（`SessionRepository`）の段階接続方針を確定。
  - `docs/contracts/hand-integration.md`（新規 draft）: 現状フロー整理 / 接続パターン A・B・C 比較 /
    推奨アーキテクチャ（Pattern A, write-through）/ player_id・session_id・hand_ref の決定タイミング /
    Phase 2.0〜2.4 → 3.x の段階 migration / HandSummary draft schema sketch / 互換ルール / open 論点。
  - `docs/contracts/session-seating.md` 更新: § freeze 状態 に ADR-0008 と hand-integration.md を相互リンク。
- **ADR-0008** (Accepted): Hand logger × session/seating integration strategy。
  Pattern A（write-through, additive）を採用。`HandSummary.players[i].player_id` を additive、
  `session_id` を session レイヤの UUID4 hex に切替（Phase 2.2）、PHH は無改変、`hand_ref` は
  session レイヤ側に住む、rollback path として `config.session_layer.enabled` フラグ planned、
  legacy logs/*.json は破壊しない。
- **ISSUE-0006**（新規 Open）: hand 開始時の seat→player_id 選択 UX が未確定。Phase 2.3 で確定。
- **ISSUE-0007**（新規 Open）: legacy hand log（timestamp session_id / player_id 無し）の取り込み
  方針が未確定。Phase 2.4 着手判断時に決める。
- **CLAUDE.md** 更新: Phase 2 セクションに「Phase 2.x（hand logger 接続, planning 済 / 実装 planned）」
  サブ節を追加。Pattern A / 細分 phase 2.1〜2.4 を記述。
- **decision-log.md** 更新: ADR-0008、ISSUE-0006、ISSUE-0007 を index に追加。ISSUE-0005 行に
  ADR-0008 リンクを追記。
- **本タスクで `.py` ファイルは変更していない**（planning-only ガード）。

### Added (Phase S2 — session + hand-based seating core)

- **Session & Seating core (S2)**: hand logger とは独立した session レイヤと hand-based
  seating の core 最小実装を追加（contract draft に対する実装。schema は未 freeze のまま）。
  - `core/session.py`: `Session` / `SeatAssignment` / `HandRef` データクラス。
  - `core/session_repository.py`: `SessionRepository`。create / list / get / close session、
    `assign_seat`（hand 単位の seat→player 割り当て）、`list_seat_assignments` /
    `resolve_seat_map_for_hand` / `resolve_hand_ref` / `current_seating`。
  - validation / errors: unknown session（`not_found`）/ already_closed / session_closed /
    seat_taken / **player_already_seated**（新 code）/ unknown_player / invalid_seat。
    `docs/contracts/error-shapes.md` の session セクションと 1:1。
  - 永続化: プロジェクト直下 `sessions.json`（アトミックリネーム、`.gitignore` 追加）。
    seat_assignment は session 配下に hand 単位で入れ子保持（将来 ledger を additive 拡張しやすい配置）。
  - 識別子: `session_id` は session レイヤが UUID4 hex で採番（hand logger の timestamp
    session_id とは別 namespace）。`hand_id` は int 据え置き（ADR-0006）。
  - **hand logger とは未接続**（`HandSummary` への player_id 接続 / reconciliation は S2 scope 外）。
- **ADR-0007** (Accepted): S2 session layer の永続形と `session_id` 採番方式の決定
  （独立採番 + 専用ストア = decoupled）。ISSUE-0005 #1 / #2 を core について確定。
- **ISSUE-0005** 更新: `session_id` 採番（#1）と seat_assignment 永続形（#2）を core について
  Resolved。hand logger 接続・seat change UI 要件は Open のまま（schema `1.0` freeze の残 blocker）。
- **Tests**: `tests/test_session_repository.py` を追加（session CRUD / persistence roundtrip /
  assign 成否 / 各 reject / resolve / code↔contract 整合）。
- **Docs**: `error-shapes.md`（session error を実装済に更新 + `player_already_seated` 追記）/
  `repository-interfaces.md` / `session-seating.md`（core 実装済を反映）/ `CLAUDE.md`
  （§ Session & Seating, 実装状況表, Phase 2 / freeze order）を更新。

### Docs / Planning (Phase 0b — S2 contracts)

- **S2 contract draft (session / seat_assignment / hand_ref)**: `docs/contracts/` に S2 の
  契約草案を追加（**未 freeze**, schema version 0.x）。
  - `session-seating.md`（モデル定義 / hand_id boundary / interface 草案 / freeze 状態・blockers）。
  - `schemas/session.schema.json` / `seat_assignment.schema.json` / `hand_ref.schema.json` と
    各 `fixtures/`（canonical / valid-minimal / invalid-*）。
  - `repository-interfaces.md` に session/seating の interface 草案を追記、`error-shapes.md` に
    S2 error code（`session_closed` / `seat_taken` / `unknown_player` / `invalid_seat` 等）を additive 追記。
  - `tests/test_contracts.py` の `_MODELS` に session / seat_assignment / hand_ref を登録（schema↔fixture 整合）。
- **ADR-0006** (Accepted): S2 session/seating contract boundary と hand_id の cross-app 参照。
  `hand_id` は session 内連番 int 据え置き、cross-app は `(session_id, hand_id)` 複合キー、
  参照単位は `hand_ref`（ISSUE-0004 の選択肢 A 採用）。
- **ISSUE-0004** → **Resolved**（ADR-0006）。**ISSUE-0005**（Open）: S2 freeze の未確定事項
  （`session_id` 採番方式 / seat_assignment 永続形 / seat change 表現）を登録。
- **shared-ids.md / versioning-and-freeze.md / README.md / CLAUDE.md**: hand_id reconcile 済・
  S2 draft 状況・freeze order #3 の状態を更新。
- **decision-log.md**: ADR-0006 / ISSUE-0005 を登録、ISSUE-0004 を Resolved に更新。

### Added

- **Player Registry (Phase S1)**: hand logger とは **別画面** の player 管理機能を追加。
  - `python main.py --players` で Player Registry 画面を起動（hand logger とは別起動）。
  - player の新規作成 / 一覧表示 / display_name リネーム。属性は `player_id`（UUID hex,
    永続・安定）+ `display_name` + `created_at`。
  - `players.json` への永続化（アプリ再起動を跨いで player_id が安定）。
  - validation: 空文字 / 前後空白のみ / 完全一致重複（前後空白除去後）を拒否。
  - 実装: `core/player.py`, `core/player_repository.py`, `gui/player_registry.py`。
  - hand logger とは未接続（session / ledger / settlement 接続は後続 Phase）。

### Docs / Planning

- **Contracts bootstrap (Phase 0a)**: `docs/contracts/` を新設し、contract-first 並行開発の
  単一 source を凍結。
  - `README.md` / `shared-ids.md` / `versioning-and-freeze.md` / `repository-interfaces.md` /
    `error-shapes.md` / `validation-rules.md`。
  - shared ID 契約: `player_id`（UUID4 hex, S1 確定）/ `session_id`（opaque string, S2 確定）/
    `hand_id`（現状 int, cross-app は (session_id, hand_id) 複合, S2 reconcile）。
  - `schemas/player.schema.json` (v1.0) + `schemas/shared-ids.schema.json` と、
    `fixtures/player/`（canonical / valid / invalid）。
  - freeze 定義・versioning（additive vs breaking）・drift detection の最小方針を文書化。
  - `tests/test_contracts.py`（schema 妥当性 / fixtures 整合 / code↔contract）を追加。
    `requirements.txt` に `jsonschema>=4.0.0` を追加。
- **ADR-0005** (Accepted): contracts repository layout & freeze workflow（ADR-0004 の具体化）。
- **Issue 0004** (Open): `hand_id` が int（hand logger）と cross-app 文字列契約で不整合。
  S2 の `hand_ref` で reconcile。
- **Issue 0003**: Phase 0a で部分緩和（contracts bootstrap + 最小 contract test）。
- **decision-log.md**: ADR-0005 / ISSUE-0004 を登録。
- **CLAUDE.md**: ディレクトリ構成に `docs/contracts/` を追加、Parallel development plan に
  契約 source 参照と Phase 0a 完了状況を追記。

- **Parallel development plan**: `CLAUDE.md` に `# Parallel development plan` 節を追加。
  4 workstream（WS0 contract / WS1 core / WS2 desktop / WS3 mobile）の依存関係、
  parallelizable / blocker、contract freeze order、mobile が mock で先行できる範囲、
  desktop / mobile 責務分離、将来 API/sync を入れても壊れにくい境界、phase 0–5 の
  構造化計画（goal / prerequisites / parallel tasks / blockers / done criteria）を明文化。
- **ADR-0004** (Accepted): contract-first parallel development / shared IDs
  (`player_id` / `session_id` / `hand_id`) / separate front-ends の判断。Alternatives
  （core-first 逐次 / front-end-owned logic / implementation-first 暗黙契約 /
  mobile = hand logger 移植）を却下。
- **Issue 0003** (Open): 並行開発の contract drift / 凍結タイミング / mock 乖離の
  blocking risk を登録（ISSUE-0001 が S3 ledger WS の直接 gate）。
- **decision-log.md**: ADR Index に ADR-0004、Major Issue Index に ISSUE-0003 を登録。
- 提案: mobile は **React Native**（iOS / Android 両対応のたたき台、最初は mock repository）を
  技術選定案とし、初期 screen skeleton は player registry の list / add / rename に限定。

### Docs / Spec

- **Bootstrap docs-as-code structure**: `docs/adr/`, `docs/issues/`, `docs/worklog/`,
  `docs/templates/`, `docs/decision-log.md` を新設。`CLAUDE.md` / `CHANGELOG.md` を
  本リポジトリに追加した。
- **CLAUDE.md**: 現時点の正仕様（hand logger Phase 7 まで）を記述するとともに、
  `Future Scope` セクションで **player registry / session ledger / point ledger /
  session settlement / cross-app boundary** を planned scope として明文化。
  `Documentation and Traceability Rules` を恒常ルールとして追加。
- **ADR-0003** (Accepted): hand logging 単体モデルから session ledger / store settlement /
  point ledger へドメインを拡張する判断と、Alternatives（HandSummary 埋め込み /
  後付け JSON / player-to-player settlement / session 単位 seat_assignment）を記録。
- **Issue 0001** (Open): point ledger の残高計算 source of truth が未確定であることを
  open question として登録（S3 着手前に解決必要）。
- **Issue 0002** (Open): player の display_name uniqueness 仕様（大文字小文字 / 全半角の
  同一視）の将来拡張を open question として登録。
- **CLAUDE.md**: § Player Registry (S1, 実装済) を追加し、future scope 表・Phase candidates・
  実装状況表を S1 実装に合わせて更新。
- **decision-log.md**: ADR Index に ADR-0003、Major Issue Index に ISSUE-0001 / ISSUE-0002
  を登録。

### Notes

- S1 は ADR-0003 の player モデル定義の最小実装。別画面分離は ADR-0003 の cross-app boundary
  方針の帰結であり、新規 ADR は起こさず worklog / decision-log に記録した。
- 次フェーズ候補: S2 (session + hand-based seating) → S3 (ledger entries + point ledger) →
  S4 (session settlement + paid/unpaid) → S5 (cross-app contract).
