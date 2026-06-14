# Changelog

本ファイルは [Keep a Changelog](https://keepachangelog.com/) の慣習に沿って、
ユーザー可視の挙動変更および仕様 / docs の重要更新を記録する。
詳細な経緯は `docs/adr/` / `docs/worklog/` / `docs/issues/` を参照。

## [Unreleased]

### Added (player 本人認証 L1 per-player PIN, ADR-0027)

- player が自分のスマホからの **self-write（注文 POST 等）を PIN ログインで本人認証**できるレイヤを
  追加（既定 `viewer_api.player_auth=off` で **挙動不変** = 従来の name-pick）。`optional`（PIN 登録済
  player の write のみトークン要求）/ `required`（全 player write に要求）で有効化。staff token（ADR-0021）
  とは直交。
- `POST /api/auth/login`（PIN→stateless 署名トークン）/ `POST /api/players/{id}/pin`（初回=staff or
  self-enroll、変更=現 PIN or staff reset）。注文 POST に principal ガード（401 `unauthorized` /
  403 `forbidden`）を additive 追加。`ViewerApiClient.login` / `set_pin`。
- PIN は **node-local `player_credentials.json`**（PBKDF2-HMAC-SHA256 + per-player lockout、平文非保持）に
  分離 — **viewer API の read response にも sync snapshot にも含めない**（players.json / player schema /
  sync は不変）。LAN 限定前提を維持。
- 新 error code（`error-shapes.md`）: `player_auth_disabled` 403 / `invalid_pin` 401 / `pin_locked` 429 /
  `pin_too_short` 400 / `forbidden` 403。

### Docs (player 認証 L1 PIN / L2 外部 IdP の詳細設計, ADR-0027 / ADR-0028)

- ADR-0025 の方針（name-pick → PIN → 外部 IdP）のうち **L1 / L2 を実装可能な詳細設計まで具体化**
  （**設計記録のみ・コードなし**）。
- **L1 per-player PIN（ADR-0027）**: PIN を node-local `player_credentials.json`（PBKDF2 + per-player
  lockout、read API / sync 非対象）に分離（ADR-0025 の「players.json に pin_hash」素描を漏洩・伝播・schema
  リスクから精緻化）。検証後に stateless 署名トークンを発行し **player principal 解決レイヤ**で self-write を
  認可。config `viewer_api.player_auth` 既定 `off` で完全後方互換（name-pick 維持）。
- **L2 外部 IdP（ADR-0028）**: `(provider, subject) → player_id` の `auth_identity`（多対一、player_id は
  外部 sub から導出しない）。OIDC Authorization Code フロー（サーバ側 JWKS 検証）→ L1 と同形の player
  トークン発行。hosted モードで LAN 会場モードと player_id + sync 共存。PII 最小化（sub のみ保存、IdP
  トークン非保存）。**前提**: 運用面 ADR（hosting/secret/PII/法令）+ player merge フロー。
- ISSUE-0019 / `docs/decision-log.md` / CLAUDE.md 残作業を更新。

### Added (buy-in 金額プリセット = staff メニュー選択, ADR-0026)

- buy-in 記帳時に **スタッフが店設定の金額プリセット（整数円）から選んで** ledger に記録できるよう
  にした（「auto ledger 生成」の最終形）。`config.ledger.buyin_presets`（既定 `[10000, 20000, 30000]`,
  店が編集）を `--ledger` 画面のボタンとして表示し、押すと kind=buy_in + cash を prefill（確定は
  従来どおり「エントリ追加」）。staff API `GET /api/staff/buyin-presets`（staff token 必須）/
  `ViewerApiClient.get_buyin_presets()` でも取得できる。
- **schema / 業務ルール変更なし・additive**。hand 結果からの自動 ledger 生成（chip→円換算を伴うもの）
  は引き続き **作らない**（chips は別単位・自動換算なし, ADR-0016/0026）。

### Docs (player アイデンティティ / 認証の進化方針, ADR-0025)

- 将来「プレイヤーが LINE / Google でサインアップ」できる要件に向け、識別と認証を分離する方針を記録。
  `player_id` を内部不変キーに保ち、認証を additive レイヤで重ねる（L0 name-pick → L1 per-player PIN →
  L2 外部 IdP = `auth_identity` バインディング）。外部 IdP は LAN-only 前提を変える hosted モード
  （別 ADR）。**方針記録のみ・実装は後続**。ISSUE-0019 / CLAUDE.md 残作業を更新。

### Fixed (sync の settlement マージを partial-paid 対応に, ADR-0024)

- `core/sync.py` の settlement マージを **`paid_amount` の monotonic max** に変更（旧: paid>unpaid の
  2 値 + settled_at 早い方）。partial-paid（ADR-0023）で、受領額の大きい `partial` が settled_at の
  早い `unpaid`(paid_amount=0) に上書きされ得た収束バグを解消。`payment_status` は max 後の
  paid_amount から導出。可換・冪等は維持（`tests/test_sync.py` に収束テスト追加）。
  現行の単一書き手 + on-demand pull 既定では実害のなかった latent issue の予防修正。

### Added (S4 — settlement partial-paid, ADR-0023)

- 精算に **一部支払い（partial-paid）** を追加。`SessionSettlement` に累計受領額 `paid_amount`
  （>=0, 既定 0, additive）を持ち、`payment_status` を `net_due_to_store` から導出する
  （`paid`/`unpaid`/新規 `partial`。過払いは `paid` に丸め）。
- core: `LedgerRepository.record_payment(session, player, paid_amount)`。既存 `set_payment_status`
  は paid=全額 / unpaid=0 の shortcut として維持（partial は record_payment 必須）。
- staff write API に `PUT /api/staff/sessions/{sid}/players/{pid}/payment`（body `{paid_amount}`）+
  `ViewerApiClient.record_payment`。player の ledger summary に `paid_amount` を additive 追加。
- GUI: `--ledger` 精算パネルに受領額入力 + 「支払額記録」ボタン。
- mobile: `MyLedgerScreen` が **支払済み / 一部支払い (受領/請求 円) / 未払い** を表示。
- schema: `session_settlement.schema.json` を `1.0`→`1.1`（optional `paid_amount` + enum 値 `partial`
  追加 = additive）。`from_dict` は `paid_amount` 欠落時に payment_status から後方互換に推定。
- tests: `test_contracts`（partial 行 + fixture）/ `test_ledger_view_gui::TestSettlement`（partial/full/
  negative）/ `test_viewer_api_staff`（record_payment 正常・not_found・invalid）/ mobile mock。

### Added (S4 — mobile での精算状況表示, ADR-0016/0017)

- player の会計参照（viewer API `/api/players/{id}/sessions/{sid}/ledger` の summary）に
  **確定状態を additive 追加**: `settled`(bool) / `payment_status`（paid|unpaid）/ `settled_at`。
  確定判定は `list_settlements`（確定行）由来（compute_settlement の settled_at は speculative でも
  埋まるため使わない）。
- mobile `MyLedgerScreen` が **精算状況**（未確定（暫定）/ 確定済（支払済み・未払い））を表示。
  プレイヤーが自分のスマホで自分の精算結果を確認できる。
- tests: `test_viewer_api.py::test_player_ledger_settled_status` + mobile mock/typecheck 更新。

### Added (S4 — settlement 確定 GUI, ADR-0016)

- スタッフ用 ledger 画面（`gui/ledger_view.py`, `main.py --ledger`）に **精算パネル**を追加。
  closed session を **確定（commit_settlement）** し、確定済 settlement の **paid/unpaid を player
  ごとに切替**（set_payment_status）できる。これまで CLI/CSV export 経由だった settlement 確定が
  GUI から行えるようになった。open session の確定は core が `session_not_closed`、二重確定は
  `already_settled` を返し、画面に表示する。partial-paid は未対応（paid/unpaid のみ）。
- `tests/test_ledger_view_gui.py::TestSettlement`（6 件）でコマンドロジックを固定。

### Added (S5 — 双方向 sync（state-based merge）, ADR-0022)

- **双方向 sync**（`core/sync.py`）: 複数の運営ノード（LAN）が全ストアのレプリカを
  **state-based merge** で相互最新化できる。マージは純粋関数で **可換・結合・冪等**
  （UUID union + 単調フィールド解決）なので、どのノードがどの順で何度マージしても同じ状態に収束する。
- **per-store マージルール**（ADR-0022）: players=create-only union（local 優先・rename 非伝播）/
  ledger・point=union（append-only, 衝突なし）/ order_request=union + status 解決（pending<終端、
  confirmed が rejected に優先、両 confirmed は resolved_at 早い方）/ settlement=committed>uncommitted・
  paid>unpaid の単調解決 / session=closed>open + 入れ子 hands/seats union（seat 衝突は local 優先）。
- **sync API**（staff-token gate）: `GET /api/staff/sync/snapshot`（自ノード全レコード, read-only でも可）/
  `POST /api/staff/sync/merge`（peer snapshot を取り込み, write 所有プロセスのみ。read-only は 503）。
- **Python client**: `ViewerApiClient.pull_sync_snapshot()` / `push_sync_merge(snapshot)` /
  `sync_bidirectional(peer)`（2 ノードを収束させる helper）。
- **repository additive**: 全 repo に read-only `path` property、`LedgerRepository` /
  `OrderRequestRepository` に public `reload()`（file-level merge 後の in-memory 最新化）。業務ロジックは不変。
- 収束テスト: `tests/test_sync.py`（純粋: 冪等 / 可換 / merge(merge(A,B),B)=merge(A,B) + file-level round-trip）
  / `tests/test_viewer_api_sync.py`（HTTP 2 ノード round-trip + 認可）。
- ADR-0022 は ADR-0020 の「単一書き手 / 双方向 auto-sync 先送り」を **更新**（複数書き手 + 収束マージ）。

### Added (S5 write 拡張 — スタッフ会計 write API（staff shared token 認証）, ADR-0021)

- **staff 会計 write API**（`api/server.py` の `/api/staff/...`）: 別端末のスタッフが会計をリモート
  操作できる。ledger entry 追加 / settlement 確定 / payment status paid-unpaid / 注文確定・却下 +
  staff read（settlement 中間集計 / 全 player の注文 queue）。
- **認証 = staff shared token**: config `viewer_api.staff_token` を設定すると有効。
  `Authorization: Bearer <token>`。token 未設定 → 403 `staff_writes_disabled` / 不一致 → 401
  `unauthorized`。**player read / 注文 POST は従来どおり無認証**（name-pick, ISSUE-0019）。
- **単一書き手維持**: staff *write* は write 所有プロセス（`--ledger`, viewer_api.enabled）のみ。
  単独 `--viewer-api`（read-only）では 503 `orders_unavailable`（staff read は token があれば可）。
- **`LedgerRepository` を thread-safe 化**（`threading.RLock` + `_locked` デコレータ）: `--ledger`
  プロセスで GUI スレッドと in-process API スレッドが同じ ledger を mutate するレースを排除
  （業務ロジックは不変。RLock 再入で inter-method 呼び出し安全。lock ordering は order→ledger 一方向）。
- **Python client**: `api/client.py:ViewerApiClient(staff_token=...)` に staff メソッド群を additive 追加。
  round-trip + 認可 test = `tests/test_viewer_api_staff.py`。
- 新 error code: `unauthorized`(401) / `staff_writes_disabled`(403)（`error-shapes.md`）。

### Added (S5 — cross-app boundary: repository interface 凍結 + Python API client, ADR-0020)

- **repository / service interface 契約を frozen**（freeze order #6）。player / session-seating /
  ledger-points-settlement / viewer read model / order-request の interface を S5 の安定契約に。
- **viewer API の Python client**（`api/client.py:ViewerApiClient`）= mobile `HttpRepository` の Python 版。
  viewer API の read endpoints（+ 注文 GET/POST）を呼び、非 2xx を error-shape の `code` を持つ
  `ViewerApiError` に変換。これで read boundary を二言語（TS / Python）で実証。
- **round-trip 契約 test**（`tests/test_viewer_api_client.py`）: API↔client を in-process（TestClient
  transport）で round-trip し境界の drift を検知。`[api]` extra に `httpx` 追加。
- 同期方式 = **on-demand pull**（push/event/双方向 auto-sync なし）、衝突は **単一書き手 +
  reload-on-read** で回避、ID は app 内採番 UUID で backend 非依存。write/sync 拡張は後続 ADR。

### Contracts (schema `1.0` freeze — S2/S3/viewer, ADR-0019)

- session / seat_assignment / hand_ref（S2）+ ledger_entry / point_ledger_entry（S3）+
  session_settlement（S4）+ order_request / player_session_summary（viewer）の schema を
  draft `0.x` → **`1.0` freeze**。統合後に全 consumer（core/desktop/mobile/API）が安定したため。
  以後 additive-only（breaking は新 ADR + MAJOR bump）。
- 上流 blocker の **ISSUE-0005 を Resolved**（hand logger 接続=E1/E2、seat change UI=E3 で解消）。
- 全 model に code↔contract drift gate を整備（`test_core_session_matches_contract` /
  `test_core_settlement_matches_contract` 追加、ledger/viewer は既存）。**残る draft は無し**
  （freeze order #6 = interface/sync 契約のみ planned）。

### Docs (ロードマップ整理 — 統合後)

- `CLAUDE.md` の Future Scope / Phase 計画を 3 トラック（S=会計 / M=player 向け / R=hand core）
  統合後の実態に整理。「ロードマップ（3 トラック統合後）」表と「残作業」一覧を新設し、M4 破棄・
  M6/M7 の S3 合流・採番替え（ADR-0017/0018・ISSUE-0019）・settlement core 済/freeze 残を明記。
  陳腐化記述（「すべて未実装」「mobile/ledger 未着手」、存在しないテスト参照）を修正
  （`docs/worklog/2026-06-13-roadmap-consolidation-post-merge.md`）。

### Added (player 向け viewer API + mobile + 注文リクエスト — verify-v1 ledger に統合, ADR-0017/0018)

- **player 向け読み取り専用 viewer API**（M1, `api/`, `[api]` extra）。`python main.py --viewer-api`
  で foreground 起動（注文 POST は 503 `orders_unavailable`）。endpoints: health / players /
  player sessions / hands / hand detail / **ledger 参照** / **menu** / **order-requests (GET/POST)**。
  ledger summary は verify-v1 ledger（ADR-0016）の `compute_settlement` を当該 player に絞った
  settlement 由来（`cash_in_total / order_total / entry_fee / point_spent_total /
  point_credited_total / net_due_to_store`）。契約は `docs/contracts/viewer-api.md`（draft 0.x）。
- **mobile viewer**（M2, `mobile/`, Expo/RN）。PlayerSelect→MySessions→MyHands→HandDetail + 会計 +
  注文画面。`ViewerRepository` interface に mock / HTTP 実装を `EXPO_PUBLIC_API_URL` で注入切替。
- **注文リクエスト write path**（M5, `core/order_request*.py` / `core/menu.py` / `menu.json`）。
  player はスマホから order_request（pending）を POST し、スタッフが `--ledger` 画面の確定/却下
  パネルで確定すると `ledger_entry`（kind=order）が作られリンクされる（staff-in-the-loop）。
  `--ledger` は `config.viewer_api.enabled=true` で viewer API を in-process 起動し注文受付を有効化
  （単一プロセス所有）。closed session への確定は order-request 層が 409 `session_closed` で弾く。
- config: `viewer_api`（enabled / bind_host 既定 127.0.0.1 / bind_port 既定 8788）。
  `.gitignore`: `order_requests.json`（`menu.json` はコミット済みサンプル）。
- ADR/ISSUE: serene ブランチからの統合で **ADR-0013→ADR-0017** / **ADR-0015→ADR-0018** /
  **ISSUE-0013→ISSUE-0019** に採番替え（serene の cash-only ledger ADR-0014 は不採用、verify-v1 の
  ADR-0016 が置換）。詳細は `docs/worklog/2026-06-13-integrate-viewer-onto-verify-v1.md`。

### Changed / Added (S3 ledger 統合 — verify-v1 へのマージで実装を一本化, ADR-0016)

- **S3 ledger / points / settlement を ADR-0016 の実装へ一本化**（verify-v1 が持っていた
  ADR-0013 の core-only ledger を統合・置換）。fold による残高（ISSUE-0001）は踏襲し、
  **session_settlement / desktop viewer / CSV export** を追加。
  - 追加: `core/ledger.py` に `SessionSettlement`、`core/ledger_repository.py` に settlement
    （compute / commit / paid-unpaid）+ reversal（append-only 訂正）。
  - 追加: `gui/ledger_view.py`（`main.py --ledger`, S3.2 desktop viewer/editor, 別画面）、
    `output/ledger_csv_exporter.py`（`main.py --export-ledger`, S3.3 settlement/cashflow CSV, utf-8-sig）。
  - 契約: `docs/contracts/ledger-overview.md` / `ledger-schema.md` + `session_settlement.schema.json`
    + fixtures。`tests/test_contracts.py::_MODELS` に `session_settlement` を追加。
  - **ADR-0016**（新規, Accepted）が **ADR-0013 を Supersede**（fold 踏襲 + settlement/desktop/export）。
    番号衝突回避のため採番替え: 旧 ISSUE-0012/0013/0014（ledger）→ **0016/0017/0018**。
  - 削除: verify-v1 の `docs/contracts/ledger-points.md` / `tests/test_point_ledger.py`（ADR-0016 の
    `ledger-overview.md` / `test_ledger_repository.py` に統合）。error 階層は ADR-0016 版に統一
    （`InvalidAmountError` 等。旧 `InvalidKindError` / `plan_payment` / `adjust_points` は非採用）。
  - tests: `tests/test_ledger_repository.py`（23）/ `test_ledger_view_gui.py`（16）/
    `test_ledger_csv_exporter.py`（7）+ contract。dependency-free subset **96 passed**。

### Added (ローカル QA tooling — 実機・Windows なしの検証手段)

- **実機（RFID/Windows）なしで v1 を検証する**チェックリスト + 模擬ツール:
  - `docs/manual-qa-checklist.md`（新規）: インストール / テスト / replay / テキスト駆動 / 音声 E2E /
    RFID 模擬 / PHH / config トグル / GUI の 9 項目を「コマンド / 期待 / 見る点」で記載。各項目に
    要ハード（🖥️/🎤）か不要（💻）かを明示。
  - `tools/simulate_rfid.py`（新規）: 起動中アプリの `POST /rfid` に偽イベントを注入する CLI
    （`send` / `seat` / `board` / `status` / `register-demo`）。物理タグ無しでもカードが解決できるよう
    `register-demo` で合成デッキ（決定的 tag→card）を `rfid_cards.json` に登録。stdlib `urllib` のみ。
  - `tools/play_hand_text.py`（新規）: マイク / Whisper モデルなしで、テキスト（ディーラー読み上げ相当）を
    `parse_action` → `integration/replay.py:replay_events` に流し、rules-aware 再構築（合法手射影 /
    silent-fold / side-pot / 派生 confidence）→ `logs/<session>.json` まで丸ごと駆動する。
  - tests: `tests/test_tools_simulate_rfid.py`（合成タグ・register-demo・実 receiver への POST→queue）/
    `tests/test_tools_play_hand_text.py`（parse/skip/timestamp・legacy 確定&再現性・pokerkit smoke）。
    **全 311 passed, 0 skipped**。既存挙動は不変（additive な追加のみ）。

### Added (Phase E part 2 — seat→player 選択 GUI + session レイヤ live 有効化 / E3, v1 リリーストラック S2.x, ISSUE-0006 Resolved)

- **座席設定ダイアログで hand logger を session レイヤに接続できるようにした**（v1 issue #10 / Epic #4,
  ADR-0008 Pattern A の live 有効化, ISSUE-0006 Resolved）:
  - `gui/seat_selection.py`（新規）: `SeatSelectionDialog`（customtkinter モーダル）。席ごとに
    登録 player を割り当て／**未登録はその場で作成**／空席は割り当てない。map 構築・重複検証・
    carry-forward 解決は GUI 非依存の純関数に分離（CI で unit test、skip 0 維持）。
  - `gui/dashboard.py`: `session_layer.enabled` 時のみ「座席設定」ボタンを表示し、起動時に一度
    seating を促す。確定後は **carry-forward**（毎ハンドは出さない）、変更時のみボタンで再編集。
  - `integration/engine.py`: `IntegrationThread.set_seat_player_map()` を追加（map 更新＋接続の有効/無効を再評価）。
  - `main.py`（GUI モード）: `session_layer.enabled=true` で `PlayerRepository`/`SessionRepository` を構築し、
    `create_session` の **UUID4 hex を session_id** に採用、`session_repo` を `IntegrationThread` に DI。
  - **既定 off では完全に従来動作**（ボタン非表示・timestamp session_id・PHH 不変, rollback path）。
  - tests: `tests/test_seat_selection.py`（純ロジック）/ `tests/test_engine_session_setter.py`（setter 経由の
    write-through・有効/無効再評価）。**全 302 passed, 0 skipped**。

### Added (Phase S3 — session ledger + point ledger core, ADR-0013)

- **session ledger / point ledger の core 実装**（CLAUDE.md § Ledger & Points, hand logger / GUI とは未接続）:
  - `core/ledger.py` + `core/ledger_repository.py`: 金銭イベント記録（`buy_in` / `rebuy` /
    `add_on` / `order` / `adjustment` / `entry_fee`、cash+point 併用可、order 明細 enforce）、
    point grant（冪等性キー対応）/ 補正 / 残高取得、session 中間集計（buy-in 合計 / 注文合計、
    途中値）。永続化は `ledger.json`（アトミックリネーム, `.gitignore`）。
  - **ISSUE-0001 Resolved（ADR-0013）**: point 残高の source of truth は
    **point_ledger_entry の fold**（cached 残高なし・player に global・常に 0 以上）。
    point 不足は strict reject + `plan_payment` による cash 補完分割（業務ルール 3）。
    entry fee は cash only（業務ルール 1）。spend 系 point entry は core が同時生成。
  - **契約 draft（S3, v0.1 未 freeze）**: `docs/contracts/ledger-overview.md` +
    `schemas/{ledger_entry,point_ledger_entry}.schema.json` + fixtures。
    `error-shapes.md` / `validation-rules.md` / `repository-interfaces.md` に S3 セクション追加。
  - テスト: `tests/test_ledger_repository.py`（13）+ `tests/test_point_ledger.py`（5,
    ISSUE-0001 予告の回帰 4 本を含む）+ contract `_MODELS` 2 model 追加 + code↔contract。
    全 349 passed / ruff 緑。

### Fixed / Changed (review hardening — 全体レビューで検出した堅牢化, ISSUE-0012)

- **rebuy / 新ハンドの状態変更を IntegrationThread に一元化**（ISSUE-0012 Fixed）:
  - GUI の「リバイ」と CLI の `n` / `r` コマンドが `GameStateManager` を **GUI/入力スレッドから直接
    変更していたレース**（規約「スレッド間通信は queue のみ」違反）を解消。winner と同様に
    `AudioEvent`（`action="rebuy"` / `"new_hand"`）を queue に積み、IntegrationThread が適用する。
  - rebuy は `on_action` 通知レコードとして GUI/CLI に返る（スタック表示更新）。
    **`HandSummary.actions` には積まれない**（ポーカーアクションではないため。回帰テストで固定）。
  - 副次修正: CLI `n` が `game_state.new_hand()` 直呼びだったためハンドバッファ
    （actions / stack_start / board / hole_cards）が**リセットされていなかった**不具合も解消
    （queue 経由で `_start_new_hand` を通るようになった）。
- **RFID HTTP 受信の堅牢化**:
  - `rfid.bind_host` 既定を `0.0.0.0` → **`127.0.0.1`** に変更（受信は無認証のため安全側へ。
    ESP32 から受ける場合は LAN IP に変更 — `docs/installation.md` §5 / `docs/usage.md` 設定表 /
    `docs/troubleshooting.md` に手順を追記）。
  - `Content-Length` に **上限 16KB** を導入（巨大 POST による OOM 防止、超過は 413）。
    不正な `Content-Length` ヘッダは 400（従来はハンドラ例外）。
- **pokerkit 未導入時の起動クラッシュを解消**: `create_game_state("pokerkit")` が ImportError 時に
  warning を出して **legacy backend へ自動フォールバック**（既定 backend が pokerkit のため、
  未導入環境でも音声記録は継続できる。rules-aware 機能は無効）。
- **テスト/CI**:
  - 新規: `tests/test_engine_rebuy.py`（5）/ `tests/test_poker_engine_fallback.py`（2）/
    `tests/test_recognizer_amounts.py`（31 — `parse_amount`/漢数字/席除去のエッジを直接固定）/
    `tests/test_rfid_http.py` にペイロード上限テスト（3）。**全 330 passed, 0 skipped**。
  - CI に **ruff**（実バグ系 `F`/`E9` の最小ゲート、`pyproject.toml` 設定、vision 除外）を追加。
    既存コードの未使用 import 17 件を除去。

### Added (Phase E part 1 — hand logger × session 統合 write-through / E1+E2-core, v1 リリーストラック S2.x)

- **hand logger を S2 session レイヤに write-through 接続**（v1 issue #10 / Epic #4, ADR-0008 Pattern A）:
  - `config_default.json`: `session_layer.enabled`（既定 `false`）を追加。
  - `integration/engine.py:IntegrationThread`: `session_repo` / `seat_player_map`（seat→player_id）を
    **additive な DI** で受け取る（両方揃ったときのみ有効＝`_session_layer_active`）。
    - `_start_new_hand`: 有効時に `SessionRepository.assign_seat` を hand 単位でバッチ呼び出し
      （write-through）。個々の失敗は当該ハンドを止めず log に留める。
    - `_finalize_hand`: `resolve_seat_map_for_hand` で当該 hand の seat→player_id を解決し、
      **`HandSummary.players[i].player_id` を additive 埋め込み**（接続時のみキー追加、未割当 seat は None）。
  - **非接続時は従来どおり**（`session_repo`/`seat_player_map` 無し → `player_id` キーを足さない＝byte 互換、
    rollback path）。`GameStateManager` / `JsonWriter` / `PHHExporter` は不変（PHH に player_id は載せない）。
  - 出力は F3b で freeze した `hand` schema（`players[i].player_id` は UUID hex pattern・optional）に適合。
  - tests: `tests/test_phase_e_session_integration.py`（接続: assign_seat 永続 + player_id 埋め込み +
    schema 適合 / 非接続: キー不在）。**全 289 passed, 0 skipped**。
  - 残（後続）: `main.py` の session 選択 step（E2 UX）と **seat→player_id 選択 GUI**（E3, `gui/dashboard.py`,
    ISSUE-0006）。本増分は core 結線（DI + write-through）に留め、live 有効化 UX は分離。

### Added (Phase I — エンドユーザードキュメント / v1 リリーストラック)

- **非エンジニア向けドキュメント一式**（v1 issue #12 / Epic #4, ロードマップ Phase I）:
  - `README.md`（新規）: 概要・できること・動作要件・インストール・**クイックスタート**（起動 → 読み上げ例 →
    出力）・各ガイドへのリンク・プロジェクト状態。
  - `docs/installation.md`: Python 準備、`pip install`、**PortAudio（pyaudio）の OS 別手順**、初回モデル DL、
    マイク選択、RFID（HTTP / PC/SC、任意）。
  - `docs/usage.md`: 起動モード、**読み上げ語彙**（席「シートN」/ アクション ベット・コール・レイズ等 / 金額）、
    ハンド 1 回の流れ（ハンド開始 → アクション → ウィナー）、言い間違い/言い忘れへの自動補正、出力（JSON / PHH）、
    **設定リファレンス**（`config.json` 各項目）。
  - `docs/troubleshooting.md`: 起動不可 / マイク未認識 / 認識精度 / モデル DL / RFID / ルール / `needs_review` /
    ログ場所 の対処。
  - `pyproject.toml` に `readme = "README.md"` を追加（パッケージ long description）。
  - 内容は実装（`config_default.json` / `core/constants.py` の語彙 / `main.py` の CLI / 既定 `pokerkit`）と
    一致。検証: doc 間リンク解決、`pip install -e .` ビルド OK、テスト **286 passed**。
  - 残（Phase H, 要 Windows）: ワンクリックインストーラができたら README のインストール節を差し替え。

### Added (Phase H part 1 — パッケージング + CI / H1+H4, v1 リリーストラック)

- **パッケージング基盤（pyproject.toml）と CI（GitHub Actions）**（v1 issue #11 / Epic #4, ロードマップ H1/H4）:
  - `pyproject.toml`（新規, setuptools, `version 1.0.0.dev0`, `requires-python>=3.11`, entry point
    `pokerapp = main:main`）。依存を **core / `[pcsc]` / `[vision]` / `[dev]`** に分割。
    **vision 系（opencv-python / easyocr）を core から除外**（廃止予定 → `[vision]` extra）。
  - 依存に上限を付与（compatible-release pin）: `numpy>=1.24,<3` / `pokerkit>=0.7,<0.8` /
    `faster-whisper>=1.0,<2` / `pyaudio>=0.2.13,<0.3` / `customtkinter>=5.2,<6`。
  - `requirements.txt` を core のみ（vision 除外）に整理、`requirements-dev.txt`（テスト依存 = numpy /
    pokerkit / jsonschema / pytest）を新設。
  - `.github/workflows/ci.yml`（新規）: push / PR で `pytest tests/ --ignore=tests/test_vision.py` を実行。
    テストはローカルパッケージを直接 import し、core の重い依存（faster-whisper/pyaudio/customtkinter）は
    lazy import のため不要。numpy/pokerkit/jsonschema を入れて **skip 0**（importorskip 対象を全て導入）。
  - `.gitignore` に packaging artifacts（`*.egg-info/` 等）を追加。
  - 検証: `pip install -e . --no-deps` で package discovery / entry point OK、CI 相当コマンドで
    **286 passed, 0 skipped**。**PyInstaller ビルド（H2）/ コード署名（H3）/ 実機 E2E は Windows 環境が必要
    で後続**（ADR-0012 のとおり Phase H の E2E）。

### Changed (Phase G — pokerkit を live 既定 backend に切替 / v1 リリーストラック R)

- **live 既定 game-state backend を `legacy` → `pokerkit` に切替**（v1 issue #9 / Epic #4, ADR-0012）:
  - `config_default.json`: `engine.backend = "pokerkit"`。新規インストール（config.json 不在 →
    config_default をコピー）は **rules-aware 再構築**（actor 推定 / 合法手射影 / silent-fold / side-pot /
    派生 confidence）が既定で効く。
  - `requirements.txt`: `pokerkit>=0.7.0,<0.8.0` に pin（golden fixtures が pokerkit 0.7.x 挙動で凍結のため）。
  - **`legacy` は rollback として維持**（`engine.backend="legacy"` で従来 `GameStateManager`、挙動不変）。
    既存 config.json は legacy のまま（破壊的変更にしない）。
  - 切替の必要条件 = golden fixtures 5 ケース全緑（達成済）。**実機 E2E（音声→JSON/PHH）は Phase H**。
  - tests: `tests/test_phase_g_default.py`（既定 pokerkit / legacy rollback / 構築）。**全 286 passed, 0 skipped**
    （テストは明示 backend 構築のため既定切替の影響なし = 回帰なし）。

### Clarified (Phase F — F3c: PHH の check/call は標準どおり統一)

- **PHH の `check`/`call` は標準トークン `cc`（check-or-call）で統一が正**と確認（ロードマップ F3c の「区別」は
  PHH 非標準で pokerkit が parse 不能になるため**変更しない**）。`output/phh_exporter.py` に意図コメントを追加。
  check/call の区別は JSON ログの `action` フィールドに保持される（情報欠落なし）。

### Added (Phase F part 3 — hand / action schema freeze / F3b, v1 リリーストラック R5。ISSUE-0011 Fixed)

- **`hand` / `action` schema を `1.0` で freeze**（v1 issue #8 / Epic #4, ADR-0010 R5, ISSUE-0011）:
  - `docs/contracts/schemas/{hand,action}.schema.json`（draft 2020-12, `version 1.0`）。ISSUE-0011 の決定で
    **`additionalProperties: true`** のまま 1.0（`false` 化は後続、範囲膨張防止）。required は安定 core のみ
    （`action` は常時 12 フィールド、`hand` は cross-app core 6）。`pots`/`player_id`/`committed` 等の
    additive は optional。
  - `docs/contracts/fixtures/{hand,action}/`（canonical / valid-minimal / invalid-*）。
  - `tests/test_contracts.py`: `_MODELS` に `hand`/`action` 登録（schema↔fixture）+ `test_core_hand_action_match_contract`
    （`HandSummary.to_dict()` / `ActionRecord.to_dict()` の **code↔contract** drift 検知）。
  - `tests/test_reconstruction.py`: `test_golden_output_conforms_to_hand_action_schema`（golden 5 ケースの
    **実再構築出力が schema 適合** — code↔contract↔golden を結ぶ）。
  - **全 282 passed, 0 skipped**。**ISSUE-0011 Fixed**。残 F3: PHH の call/check 区別（F3c）。

### Added (Phase F part 2 — side-pot 連携 + golden 5 ケース全緑 / F3a, v1 リリーストラック R5)

- **`HandSummary.pots`（main/side pot スナップショット）を additive 追加**（v1 issue #8 / Epic #4,
  ADR-0009 §3, ロードマップ A3 吸収）:
  - `core/hand_log.py`: `HandSummary.pots: list`（既定 `[]`、`to_dict` に含む）。
    `[{"amount": int, "eligible_seats": [int,...]}, ...]`。
  - `integration/engine.py:_finalize_hand`: `pots=gs.pots()` を埋め込み（rules-aware backend が
    `end_hand` 時に pokerkit から算出、legacy は `[]`）。挙動は additive（既存フィールド不変）。
  - **golden fixtures `unequal-allin` を緑化**: スタック差 all-in（1000/3000/3000）→ main pot
    `3000 [1,2,3]` + side pot `4000 [2,3]`。`tests/test_reconstruction.py` の GREEN_CASES に昇格。
  - **既知バグ 5 ケースが全緑**（check-facing-bet / call-amount-from-state / silent-fold /
    out-of-turn-rfid / unequal-allin）。**DoD #2 達成**。`pots` 追加に伴い既存 4 fixtures を再凍結
    （hand 終了が manual winner のため pots=[]、挙動不変）。
  - tests: `tests/test_reconstruction.py`（`unequal_allin_main_and_side_pots` + 全緑確認）。
    **全 274 passed, 0 skipped**（legacy は `pots=[]` で additive、回帰なし）。
  - 残 F3: `hand`/`action` schema freeze（ISSUE-0011, `_MODELS` 登録）、PHH の call/check 区別。

### Added (Phase D part 5 — 派生 confidence + needs_review 5 条件 / D3, v1 リリーストラック R3。Phase D 完了)

- **解釈可能な 3 因子 confidence + 明示的 needs_review 条件**（v1 issue #7 / Epic #4, ADR-0009 §6,
  ISSUE-0009）— **rules-aware 経路のみ**（legacy の固定 8 行 `calc_confidence` は不変）:
  - `integration/engine.py:derive_confidence` を新設: `confidence = clamp(L·(w_A·A + w_Q·Q), 0, 1)`。
    **L=合法性ゲート**（pokerkit 受理=1.0 / 非受理=`_CONF_L_PENALTY`、最重要）、**A=合意度**（一致した
    存在ソース / 存在ソース）、**Q=ソース品質**（一致ソースの base 信頼度の noisy-OR、audio は whisper で
    スケール、RFID>audio>camera）。固定 8 行テーブルを廃し、単調・解釈可能に。
  - `_handle_rules_aware_action` を D3 化: `derive_confidence` を適用し、**needs_review 5 条件**を明文化
    （①pokerkit 非合法 ②高信頼 ASR×規則矛盾/④amount snap[=`apply_corrections.needs_review`]
    ③actor 競合[prior↔sensor] ⑤`confidence < REVIEW_THRESHOLD`）。これで `HandSummary.review_required`
    が監査可能な意味を持つ。
  - 重み較正（暫定）: 良好な audio-only は閾値超え＝自動 review しない（v1 は音声優先）。camera-only /
    低 whisper / 合成 fold は閾値未満＝review。最終較正は golden fixtures / F。
  - golden fixtures 4 ケースの confidence を再凍結。`call-amount-from-state` は review=False を維持。
  - tests: `tests/test_phase_d3_confidence.py`（derive_confidence の順位/ゲート/whisper/合意 + 閾値条件⑤、7）。
    **全 270 passed, 1 skipped**。**これで Phase D（D0/D1/D2a/D2b/D3）完了**（残 R は F3 の side-pot/freeze）。

### Added (Phase D part 4 — silent-fold 合成 / D2b, v1 リリーストラック R3)

- **silent-fold 合成（未宣言 fold を補い actor を物理/明示証拠へ追従）**（v1 issue #7 / Epic #4,
  ADR-0009 §4, ISSUE-0009）:
  - `core/poker_engine.py:fold_through` を **atomic + cap 対応**に強化: `max_folds` 超過/到達不可は
    `ValueError` で **状態を巻き戻す**（`copy.deepcopy` snapshot/restore、誤 fold を残さない）。
    合成した席列（`list[int]`）を返す。
  - `integration/engine.py:_resolve_actor` を D2b 化: 優先順位 **RFID seat 読み > 明示発話席** で
    sensed を決め、prior と異なれば `fold_through(sensed, max_folds=SILENT_FOLD_CAP=2)` で silent-fold
    合成。cap 超過/到達不可は prior 維持。合成 fold は `_append_synth_fold` で **fold アクションとして
    記録**（`confidence=0.3`、常に `needs_review`）。actor 推定に使った RFID 読みは消費（滞留防止）し、
    最終 actor と一致すれば corroboration に再利用。競合（sensed≠prior）は `needs_review`。
  - **golden fixtures 2 ケースが緑化**: `silent-fold`（audio 駆動）/ `out-of-turn-rfid`（RFID 駆動、
    actor を RFID 席へ補正し corroboration 成立）。`tests/test_reconstruction.py` の GREEN_CASES に昇格。
  - tests: `tests/test_phase_d0_engine.py`（fold_through の返り値/cap/atomic 3 追加）、
    `tests/test_phase_d2_wiring.py`（明示席 → silent-fold 合成に更新）、reconstruction 2 ケース。
    **全 263 passed, 1 skipped**（残 `unequal-allin`=F3）。legacy 既定は不変。
  - 残: 派生 confidence（D3、合成 fold の 0.3 較正含む）。

### Added (Phase F part 1 — 決定的 replay ハーネス + 最初の golden fixtures / F1, v1 リリーストラック R4)

- **決定的 replay ハーネス**（v1 issue #8 / Epic #4, ADR-0011）:
  - `integration/replay.py`（`load_events` / `replay_events` / `replay_fixture`）+ CLI `tools/replay_hand.py`。
    記録済み `events.jsonl`（R1 sidecar）を live と同じ `IntegrationThread` の per-event 処理に **timestamp
    昇順**で通し `HandSummary` を再構築する。
  - `integration/engine.py` に **clock 注入**（`IntegrationThread(clock=..., on_hand=...)`、additive）。
    `_now_iso` を `datetime.fromtimestamp(self._clock())` に、`_expire_buffers` を `self._clock()` に変更。
    **既定 `time.time` で live は完全不変**。
  - **golden fixtures**: `tests/fixtures/reconstruction/<case>/{setup.json, events.jsonl, expected_hand.json}`。
    D1/D2a で既に正しく再構築できる **2 ケースを緑で固定**: `check-facing-bet`（非合法 check→call+review）/
    `call-amount-from-state`（heard 9999 無視→engine の call 額 200）。残り 3 ケース（`silent-fold` /
    `out-of-turn-rfid`=D2b、`unequal-allin`=F3）は実装と同じ増分で追加（skip で明示）。
  - **round-trip 決定性**（DoD #3）: 同一 events.jsonl を 2 回 replay → 完全一致を `tests/test_reconstruction.py`
    で検証。fixtures は `reconstruction_event` schema 適合も確認。
  - tests: `tests/test_reconstruction.py`（7 + pending 3 skip）。**全 255 passed, 3 skipped**（clock 既定で
    既存テスト回帰なし）。

### Added (Phase D part 3 — rules-aware ライブ結線 / D2a, v1 リリーストラック R3)

- **rules-aware 経路（pokerkit）に `apply_corrections` をライブ結線 + actor 競合検出**（v1 issue #7 /
  Epic #4, ADR-0009 §1/§5）:
  - `integration/engine.py`: `_handle_audio_event` のベッティング処理を `gs.legal_context()` で分岐。
    **rules-aware（pokerkit、空でない legal_context）= `_handle_rules_aware_action`**（`apply_corrections`
    で合法手へ射影し ActionRecord に反映、`_resolve_actor` で明示発話席(`event.seat`)が手番(prior)と
    食い違えば `needs_review`）。**legacy（空 legal_context）= `_handle_legacy_action`（従来コードを
    そのまま分離・挙動不変）**。
  - これにより pokerkit backend で **call/check の状態一意化・非合法 action の修復・明示席の out-of-turn
    検出**がライブで効く（既定 legacy は不変）。
  - **silent-fold 合成（`fold_through` 結線で prior を上書き）・RFID/camera を含む多源 actor 解決・派生
    confidence（D3）は後続増分 D2b**（誤 fold リスクと滞留しうる RFID 読みの消費設計のため Phase F #8 の
    golden fixtures で検証）。D2a は prior 固定 + 明示席（イベント単位・滞留しない）の競合 flag に留める。
  - tests: `tests/test_phase_d2_wiring.py`（pokerkit 5: 射影/競合/legacy 分岐）。pokerkit 0.7.4 実走で
    **248 passed**（legacy 既存テストは `_handle_legacy_action` 経由で不変通過）。

### Added (Phase D part 2 — engine 境界の rules-aware メソッド / D0, v1 リリーストラック R3)

- **`PokerEngine` 境界に rules-aware の additive メソッドを追加**（v1 issue #7 / Epic #4, ADR-0009 §2）:
  `legal_context` / `is_legal_actor` / `pots` / `committed` / **`fold_through`** を Protocol に追加し、
  両 backend で conform させた。
  - `core/poker_engine.py`: `PokerkitGameState.fold_through(until_seat)` を**新規実装**（現 actor から
    until_seat 手前までを silent fold 合成 = ディーラー未宣言 fold の表現。到達不能は ValueError。
    上限は呼び出し側=actor 推定が距離で判断, ISSUE-0009）。`legal_context`/`pots`/`committed` は R2 で実装済。
  - `core/engine_types.py`（新規）: `LegalContext` を中立モジュールへ移設（循環 import 回避）。
    `core.poker_engine.LegalContext` として後方互換に再エクスポート。
  - `core/game_state.py`（legacy）: rules-aware でない stub を追加（空 `legal_context` = legacy 印 /
    `fold_through` は `NotImplementedError` / `pots`=[] / `committed`=0）。IntegrationThread は空 context を
    以て legacy 経路（従来挙動）へ分岐する設計（結線は D2）。
  - **ライブ未結線＝挙動不変**。actor 推定の結線（D2）は後続 PR。
  - tests: `tests/test_phase_d0_engine.py`（pokerkit 部は importorskip、legacy stub は常時実行）。
    pokerkit 0.7.4 を導入して実走 **243 passed**（未導入時は pokerkit 部 skip）。

### Added (Phase D part 1 — apply_corrections, v1 リリーストラック R3)

- **合法手への射影 `apply_corrections()` を実装**（v1 issue #7 / Epic #4, ADR-0009 §5）:
  raw ASR の (action, amount) を `LegalContext`（`legal_context()` 由来）の合法手へ射影する**純関数**
  （`audio/recognizer.py`、pokerkit 非依存）。
  - **call/check を状態から決定的に一意化**（`amount_to_call>0→call` / `==0→check`）。JA キーワードの
    曖昧さに依存せず、PHH/JSON で call と check を初めて区別できる核心。
  - bet↔raise を当ストリートのベット有無から再マップ、amount を合法レンジへ snap（大幅 snap / 額不明 /
    非合法は `needs_review`）。`Correction` 結果型（`corrected_from`/`reason`/`asr_confidence` を持つ）。
  - ベットに直面した "check" は暫定で **call + `needs_review`**（ISSUE-0009、尤度導入は後続）。
  - **ライブ未結線＝挙動不変**: actor 推定の engine 結線（D2）/ 派生 confidence 融合（D3）/ silent-fold
    合成は後続 PR（Phase F #8 の golden fixtures と併走）。ISSUE-0009 の初期方針を承認・記録。
  - tests: `tests/test_phase_d_corrections.py`（18, 修復表を網羅）。
    **全 222 passed, 10 skipped**（`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。

### Added (Phase B+C — イベント記録基盤, v1 リリーストラック R)

- **`AudioEvent` に `seat` / `confidence` を additive 追加**（v1 issue #6 / Epic #4, R3/R4 の前提）:
  - `core/events.py`: `AudioEvent.seat`（明示発話席）/ `AudioEvent.confidence`（Whisper 信頼度 [0,1]）を
    optional 追加。既存経路は未使用 = **挙動不変**。
  - `audio/recognizer.py`: `WhisperTranscriber.transcribe_with_confidence()` を追加（segment の
    `avg_logprob` 平均を `exp` で 0..1 に写像）。`transcribe()` は委譲。`parse_action(text, confidence=)`
    で confidence を受け、`_extract_seat_no()` で明示席（"シート3"/"seat 3"/全角）を populate。
  - `audio/recorder.py`: `_process_chunk` を `transcribe_with_confidence` 経由に変更し confidence を伝搬。
  - `output/event_recorder.py`: `event_to_envelope` の audio 分岐に `seat`/`confidence` を additive 露出
    （`reconstruction_event` schema は既に optional 定義済、code↔contract 緑）。
- **ISSUE-0010（記録境界・決定性）を Resolved**: 記録境界 = ASR decode 後（`seat`/`confidence` 含む）、
  clock 源 = 観測済み最大 event timestamp に確定。`docs/contracts/event-replay.md §4` を「決定」に更新。
  clock 注入・replayer・スレッド順序許容度の実証固定は Phase F（#8）の golden fixtures に委譲。
- tests: `tests/test_phase_bc_events.py`（10）+ `tests/test_event_recorder.py` 拡張。
  **全 204 passed, 10 skipped**（`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。

### Fixed (Phase A — コア堅牢化, v1 リリーストラック)

- **RFID カード未解決時にハンドを要レビュー化**（v1 issue #5 / Epic #4）: board / seat RFID
  イベントの `card` がカードマスター未解決（空文字）のままハンドが進んだ場合、その
  `HandSummary.review_required` を `True` にするようにした。従来は `logger.warning` のみで
  ハンドサマリーに反映されず、オペレーターが検出失敗に気付けなかった。
  - `integration/engine.py`: `IntegrationThread._hand_needs_review` フラグを additive 追加。
    card 未解決の board/seat 分岐で立て、`_start_new_hand` でリセット、`_finalize_hand` の
    `review_required` に OR 合成。解決済みカードでは立たない（誤検知ガード）。
  - tests: `tests/test_phase_a_hardening.py`（4）。**全 192 passed, 10 skipped**
    （`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。
  - 検証: faster-whisper 未導入時の起動は `audio/recognizer.py` の遅延 import（`__init__` の
    `try/except ImportError`）で既にクラッシュしないことを確認（コード変更不要）。

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

### Added (WS2-α — Session / Seating Viewer, read-only desktop)

- **Session / Seating Viewer**: S2 core の session / hand-based seating を確認する
  **read-only inspection 画面**を desktop に追加（hand logger / player registry とは別画面）。
  - `python main.py --sessions` で起動（hand logger 通常起動 `python main.py` / registry
    `--players` は無改修・従来通り）。
  - session 一覧（session_id / started_at / status / label / hand 数 / assignment 数の要約）→
    選択で 概要 / current seating（最新 hand から導出）/ hand ごとの seat assignments を表示。
  - `player_id` を `PlayerRepository` で `display_name` に解決（不能なら `(unknown)`、`player_id`
    自体は保持）。
  - **再読込（refresh）** ボタンで `SessionRepository` / `PlayerRepository` をディスクから読み直す
    （別プロセスの更新取り込み）。live auto-refresh は持たない。
  - empty state（session 無し）/ no-data state（seating / hand 無し）を明示表示。
  - **read-only**: session/seat/player の作成・編集・削除を一切持たない（許容操作は refresh のみ）。
    業務ルールは core が source of truth、viewer は read API + name 解決 + 表示整形に徹する。
  - 実装: `gui/session_viewer.py`（`SessionViewerWindow`）、`main.py`（`--sessions` /
    `run_session_viewer`）。
- **core read API（additive）**: read-only viewer 用に enumeration / loading を core に追加。
  - `SessionRepository.list_hand_ids(session_id)`（記録済み hand_id を昇順列挙）。
  - `SessionRepository.reload()` / `PlayerRepository.reload()`（ディスクから再読込）。
  - いずれも additive な read 専用 API。既存 API・業務ルール・schema（0.x）は不変。
- **Tests**: `tests/test_session_viewer_gui.py` を追加（empty state / 一覧要約 / 選択→詳細 /
  current seating / name 解決 / unknown player 安全表示 / refresh 再読込 / read-only・別構造の確認）。
  全体 **192 passed**（ベースライン 177 に対し +15、回帰なし）。
- **Docs**: `CLAUDE.md`（§ Session / Seating Viewer 追加 + 実装状況表 / コマンド / Phase 2 WS2 更新）/
  `docs/contracts/repository-interfaces.md` / `session-seating.md`（`list hand ids` / `reload` を
  additive 追記）/ `hand-integration.md`（viewer が inspection 用である旨）/ ISSUE-0013（新規, viewer の
  data source 依存 + 拡張 open question。**旧番号 0008 から採番替え** — verify-v1 統合時に
  pokerkit feasibility の ISSUE-0008 と衝突したため）/ ISSUE-0006（seat change 可視化の関連注記）/
  `decision-log.md`（ISSUE-0013 登録）/ worklog（`2026-06-03-session-seating-viewer.md`）。
- **注（統合時更新）**: 元ブランチ時点では write-through 未実装だったが、verify-v1 統合時点では
  **E1〜E3 で実装済**（`session_layer.enabled`）。有効時は live の hand logger が書いた seating も
  viewer で確認できる（ISSUE-0013）。

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
### Docs / Planning (RFID hardware migration — PCSC canonical pivot)

> 注: 本セクションの ADR/ISSUE は verify-v1 統合時に **0007/0008→0014/0015（ADR）、0006/0007→0014/0015（ISSUE）に採番替え**（既存 ID との衝突解消）。

- **RFID hardware を PN5180 + ESP32-S3 に移行**する仕様変更の方針 ADR を **訂正**:
  - **ADR-0015** (Accepted, **supersedes ADR-0014**): ESP32-S3 が **USB CCID** として PN5180 ×N を
    PC/SC multi-slot で公開し、ホスト側は **pyscard 経由の PC/SC を canonical（本筋）** とする。
    `rfid/reader_thread.py` が第一系統。`rfid/http_receiver.py` は **optional secondary**
    （debug / remote 用）に降格。`reader_configs` の reader_name ↔ role/seat マッピング契約、
    `rfid_cards.json` 形式、confidence 行列、`RFIDEvent` は不変。`tag_id` UID 長は 4/7/8B 許容。
  - **ADR-0014** (**Superseded by ADR-0015**): 旧 ADR は「HTTP を canonical / PCSC を legacy」
    としていたが、これは作業者の誤想定による誤決定。history として残置。
  - **ISSUE-0015** (Open): ESP32-S3 USB CCID firmware ↔ host の契約（USB descriptors /
    reader_name / ATR / pseudo-APDU / 8B UID 取得 / hot-plug 通知）を register。
  - **ISSUE-0014** (**Superseded by ISSUE-0015**): HTTP API 契約を追跡していた旧 issue は本筋から
    外れたため Supersede。
  - **CLAUDE.md**: プロジェクト概要 / ディレクトリ構成 / 技術スタック / 実装状況 / エラーハンドリング
    方針を「PC/SC canonical（USB CCID 経由）/ HTTP optional secondary」に flip。
  - **decision-log.md**: ADR-0015 / ISSUE-0015 を追加、ADR-0014 / ISSUE-0014 を Superseded に更新。
  - フォローアップ（次タスク）: `rfid/reader_thread.py` / `rfid/bridge.py` docstring と
    `config_default.json` を PCSC canonical 前提に書き直し、`card_master.normalize_tag_id` の 8B UID
    テスト追加、`tests/test_rfid.py` に 8B UID PCSC fixture 追加、ESP32-S3 firmware USB CCID
    descriptor の確定を ISSUE-0015 に貼る。

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
