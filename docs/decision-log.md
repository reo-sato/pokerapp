# Decision Log

このファイルは、リポジトリ内の **ADR (Architecture Decision Records)** と
**主要な issue / mismatch log** を横断参照するための索引。
個別の理由付け・経緯は各 ADR / issue ファイル本体を参照すること。
ここは「どの判断・どの不具合が、どのファイル・どの commit・どのテストに紐付くか」を
一覧するための薄い index に徹する。

## 運用ルール

詳細は `CLAUDE.md` の **Documentation and Traceability Rules** を参照。要点:

- 新しい ADR (`docs/adr/NNNN-*.md`) を追加したら、下の **ADR Index** に必ず 1 行追加する。
- 主要な issue / mismatch (`docs/issues/NNNN-*.md`) を追加したら、下の
  **Major Issue / Mismatch Index** に必ず 1 行追加する。
- ADR を supersede する場合は、新 ADR を追加した上で **旧 ADR の Status を `Superseded` に
  更新** し、両側に back-link を入れる（旧 ADR を削除しない）。
- 表は ID 昇順を維持。新規行は末尾に追加する。
- 各テンプレートの所在:
  - ADR: `docs/templates/adr-template.md`
  - Worklog: `docs/templates/worklog-template.md`
  - Issue: `docs/templates/issue-template.md`

## ADR Index

| ID       | Title                                                                              | Status   | Date       | Related Area                | File                                                                                              | Supersedes / Superseded by |
|----------|------------------------------------------------------------------------------------|----------|------------|-----------------------------|---------------------------------------------------------------------------------------------------|----------------------------|
| ADR-0003 | Expand domain from hand logging to session ledger and store settlement             | Accepted | 2026-05-22 | spec / domain model / scope | `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`         | —                          |
| ADR-0004 | Contract-first parallel development with shared IDs and separate front-ends        | Accepted | 2026-05-22 | planning / workstreams      | `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`          | 関連: ADR-0003             |
| ADR-0005 | Contracts repository layout and freeze workflow                                    | Accepted | 2026-05-22 | contracts / drift detection | `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`                                | 関連: ADR-0004             |
| ADR-0006 | S2 session/seating contract boundary and hand_id cross-app reference               | Accepted | 2026-05-25 | contracts / shared-ids (S2) | `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md`                    | 関連: ADR-0003 / ISSUE-0004 |
| ADR-0007 | S2 session layer persistence and session_id issuance                               | Accepted | 2026-05-25 | core / session (S2)         | `docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md`                                   | 関連: ADR-0006 / ISSUE-0005 |
| ADR-0008 | Hand logger × session/seating integration strategy (Phase 2.x)                     | Accepted | 2026-06-03 | hand logger × session (S2.x) | `docs/adr/0008-hand-logger-session-integration-strategy.md`                                       | 関連: ADR-0006 / ADR-0007 / ISSUE-0005 / 0006 / 0007 |
| ADR-0009 | Rules-aware hand reconstruction — pokerkit live authority + state estimation & fusion | Accepted | 2026-06-03 | reconstruct / core+integration | `docs/adr/0009-pokerkit-live-rules-authority.md` | R2 engine 実装済(default-off)。R3 planned。関連: ADR-0010 / ISSUE-0008(Fixed) / ISSUE-0009 |
| ADR-0010 | Contract-first hand core via deterministic record/replay | Accepted | 2026-06-03 | reconstruct / contracts | `docs/adr/0010-contract-first-hand-core-record-replay.md` | 関連: ADR-0008 / ADR-0009 / ISSUE-0010 / ISSUE-0011 |
| ADR-0011 | Deterministic replay harness (sync timestamp-ordered driver + clock injection) | Accepted | 2026-06-05 | reconstruct / replay (F1) | `docs/adr/0011-deterministic-replay-harness.md` | 関連: ADR-0010 / ADR-0009 / ISSUE-0010 |
| ADR-0012 | pokerkit を live 既定 backend に切替（legacy は rollback, pokerkit pin） | Accepted | 2026-06-05 | reconstruct / engine (G) | `docs/adr/0012-pokerkit-live-default.md` | 関連: ADR-0009。実機 E2E は Phase H |
| ADR-0013 | S3 point 残高 = point ledger の fold + ledger 永続化方式 | **Superseded** | 2026-06-10 | core / ledger・points (S3) | `docs/adr/0013-s3-point-balance-fold-and-ledger-persistence.md` | fold 採用は ADR-0016 に踏襲。**Superseded by ADR-0016**（verify-v1 マージで実装一本化）/ 関連: ISSUE-0001 |
| ADR-0014 | Migrate RFID hardware to PN5180 + ESP32-S3 and make HTTP the canonical transport   | **Superseded** | 2026-06-01 | rfid / hardware migration   | `docs/adr/0014-migrate-rfid-to-pn5180-esp32s3-and-canonical-http-transport.md`                    | 旧番号 0007 から採番替え。**Superseded by ADR-0015**   |
| ADR-0015 | PN5180 + ESP32-S3 via USB CCID — PC/SC is the canonical RFID transport             | Accepted | 2026-06-01 | rfid / hardware migration   | `docs/adr/0015-pn5180-esp32s3-usb-ccid-pcsc-canonical.md`                                         | 旧番号 0008 から採番替え。Supersedes ADR-0014 / 関連: ISSUE-0015 |
| ADR-0016 | Ledger / points / settlement design direction (S3) | Accepted | 2026-06-10 | contracts / core (S3) | `docs/adr/0016-ledger-points-settlement-design-direction.md` | **Supersedes ADR-0013**（fold 踏襲 + session_settlement / desktop viewer / CSV export 追加）/ 関連: ADR-0003 / ADR-0007 / ISSUE-0001 |
| ADR-0017 | Player 向け参照は API-first（viewer API 前倒し）+ Expo（web export 先行） | Accepted | 2026-06-10 | viewer API / mobile (M1) | `docs/adr/0017-player-facing-viewer-api-first-architecture.md` | serene ADR-0013 から採番替え（verify-v1 統合）。補完: ADR-0004。関連: ADR-0008 / ADR-0016 / ISSUE-0019 |
| ADR-0018 | M5 — 注文リクエスト write path（staff-in-the-loop / in-process API / menu master / name-pick 確定） | Accepted | 2026-06-11 | orders / viewer API (M5) | `docs/adr/0018-m5-order-request-write-path.md` | serene ADR-0015 から採番替え（verify-v1 統合）。ISSUE-0019 を v1 決着。関連: ADR-0017 / ADR-0016（ledger） |
| ADR-0019 | S2 / S3 / viewer model の schema を `1.0` に freeze（統合後） | Accepted | 2026-06-13 | contracts / freeze | `docs/adr/0019-schema-1_0-freeze-post-integration.md` | session/seat/hand_ref + ledger/point/settlement + order_request/player_session_summary を 1.0 化。ISSUE-0005 Resolved。関連: ADR-0006/0007/0016/0017/0018 |
| ADR-0020 | S5 — cross-app boundary（repository interface 凍結 + Python API client） | Accepted | 2026-06-13 | contracts / boundary (S5) | `docs/adr/0020-s5-cross-app-boundary-repository-interface-and-api-client.md` | repository interface frozen（freeze order #6）+ `api/client.py:ViewerApiClient` + round-trip test。on-demand pull / 単一書き手。write/sync 拡張は後続。**単一書き手 / 双方向 auto-sync 先送りは ADR-0022 で更新**（複数書き手 + 収束マージ）。関連: ADR-0004/0017/0018/0019/0022 |
| ADR-0021 | S5 write 拡張 — スタッフ会計 write API（staff shared token 認証） | Accepted | 2026-06-13 | viewer API / boundary (S5) | `docs/adr/0021-s5-staff-write-api-token-auth.md` | `/api/staff/...`（ledger 追加 / settlement 確定 / paid-unpaid / 注文確定・却下 + staff read）を `Authorization: Bearer <staff_token>` で公開。`LedgerRepository` を RLock で thread-safe 化。単一書き手維持（read-only は 503）。player read / 注文 POST は無認証のまま。新 error: `unauthorized`(401) / `staff_writes_disabled`(403)。関連: ADR-0017/0018/0020, ISSUE-0019 |
| ADR-0022 | S5 — 双方向 sync（state-based merge / append-only union + 単調フィールド解決） | Accepted | 2026-06-13 | sync / boundary (S5) | `docs/adr/0022-s5-bidirectional-sync-state-based-merge.md` | peer-to-peer の on-demand state-based merge（`core/sync.py` 純粋関数）。UUID union + 単調解決で可換・結合・冪等 ⇒ 収束。`GET/POST /api/staff/sync/{snapshot,merge}`（staff-token gate, write 所有のみ merge 受理）+ `ViewerApiClient.sync_*`。`LedgerRepository`/`OrderRequestRepository` に `reload()`、全 repo に `path` property を additive 追加。**ADR-0020 の「単一書き手 / 双方向 auto-sync 先送り」を更新**（複数書き手 + 収束マージ。ADR-0020 は Superseded ではなく拡張）。関連: ADR-0004/0017/0020/0021 |
| ADR-0023 | S4 — settlement partial-paid（paid_amount additive / payment_status 導出） | Accepted | 2026-06-13 | ledger / settlement (S4) | `docs/adr/0023-settlement-partial-paid.md` | `SessionSettlement` に optional `paid_amount`（既定 0）を additive 追加し、`payment_status`（paid/unpaid/**partial**）を `_derive_payment_status(net, paid)` で単一導出。schema 1.0→1.1（optional field + enum 値 = additive）。`record_payment(session,player,paid)` 新設・`set_payment_status` は record_payment 経由の shortcut（partial は ValueError）。`from_dict` は paid_amount 欠落時に payment_status から後方互換に推定。staff API `PUT .../payment` + `ViewerApiClient.record_payment` + GUI 受領額入力 + mobile/player summary に paid_amount/partial 表示。過払い（paid>net）は paid に丸め。Business rule #4 更新（ADR-0016/0019 を additive 拡張）。関連: ADR-0016/0019/0024 |
| ADR-0024 | sync の settlement マージを paid_amount monotonic max に（partial-paid 対応） | Accepted | 2026-06-13 | sync / settlement (S5) | `docs/adr/0024-sync-settlement-merge-paid-amount-monotonic.md` | ADR-0022 の settlement-merge 節を更新: 両 committed は `paid_amount=max` + payment_status 導出（partial が早い unpaid に上書きされない単調収束）。`core/sync.py:_resolve_settlement`。関連: ADR-0022/0023 |
| ADR-0025 | player アイデンティティ / 認証の進化方針（name-pick → PIN → 外部 IdP） | Accepted | 2026-06-13 | identity / auth (future) | `docs/adr/0025-player-identity-auth-evolution.md` | player_id を内部不変キーに保ち、認証を additive レイヤ（L0 name-pick / L1 PIN / L2 LINE・Google OIDC = `auth_identity` バインディング）で重ねる。外部 IdP は LAN-only 前提を変える hosted モード（別 ADR）。方針記録のみ、実装は後続。関連: ADR-0004 / ADR-0021 / ISSUE-0019 |
| ADR-0026 | buy-in 金額のプリセット選択（メニュー方式 / chip↔円換算なし） | Accepted | 2026-06-14 | ledger / gui (auto-ledger) | `docs/adr/0026-auto-ledger-default-template-generation.md` | 「auto ledger 生成」= buy-in 記帳時にスタッフが config `ledger.buyin_presets`（固定円）から金額選択。`--ledger` 画面にプリセットボタン + staff API `GET /api/staff/buyin-presets`。schema/業務ルール不変、chip↔cash 分離維持。関連: ADR-0016/0018/0021 |
| ADR-0027 | L1 per-player PIN 認証（player principal 解決レイヤ） | Accepted | 2026-06-14 | identity / auth (L1) | `docs/adr/0027-l1-per-player-pin-auth-design.md` | **実装済**。PIN を node-local `player_credentials.json`（PBKDF2 + lockout、players.json/sync 非対象）に分離。`core/auth_token.py` の stateless 署名トークン + `_resolve_player_principal`/`_require_player` で player self-write を認可。`POST /api/auth/login`・`/api/players/{id}/pin`。config `viewer_api.player_auth` 既定 off で後方互換（557 passed）。関連: ADR-0025/0021/0004/ISSUE-0019 |
| ADR-0028 | L2 外部 IdP（LINE / Google OIDC）認証の詳細設計 | Proposed | 2026-06-14 | identity / auth (L2, 設計) | `docs/adr/0028-l2-external-idp-oidc-auth-design.md` | `(provider, subject) → player_id` の auth_identity（多対一、player_id は外部 sub から導出しない）。OIDC Authorization Code フロー（サーバ側 JWKS 検証）→ L1 と同形の player トークン発行。hosted モードで LAN 会場モードと player_id + sync 共存。PII 最小化（sub のみ保存）。**前提**: 運用 ADR + player merge。**設計のみ・コードなし**。関連: ADR-0025/0027/0004/0022/ISSUE-0019 |

<!--
Note: ADR-0001 / ADR-0002 は本リポジトリの spec expansion phase (S0) 時点で空番。
過去判断のうち ADR 化したい既存決定が出てきた場合は、その時点で 0001/0002 を遡及採番する。
-->

## Major Issue / Mismatch Index

| ID         | Title                                                  | Status | Date       | Area                     | File                                                       | Related Fix / Commit |
|------------|--------------------------------------------------------|--------|------------|--------------------------|------------------------------------------------------------|----------------------|
| ISSUE-0001 | point ledger の残高計算と source of truth が未確定     | Resolved | 2026-05-22 | core / ledger (S3)     | `docs/issues/0001-point-balance-source-of-truth.md`        | ADR-0013→**ADR-0016**（fold 採用）+ S3 core 実装 + `tests/test_ledger_repository.py` |
| ISSUE-0002 | display_name の uniqueness 仕様の将来拡張が未確定      | Open   | 2026-05-22 | player registry (S1)     | `docs/issues/0002-display-name-uniqueness-scope.md`        | —                    |
| ISSUE-0003 | 並行開発の contract drift / 凍結タイミング risk       | Open   | 2026-05-22 | planning (WS0–WS3)       | `docs/issues/0003-parallel-dev-contract-drift.md`          | Phase 0a で部分緩和  |
| ISSUE-0004 | hand_id が int と cross-app 文字列契約で不整合        | Resolved | 2026-05-22 | contracts / shared-ids   | `docs/issues/0004-hand-id-int-vs-cross-app-string.md`      | ADR-0006（複合キー採用） |
| ISSUE-0005 | S2 session/seating freeze の未確定事項               | Resolved | 2026-05-25 | contracts / session (S2) | `docs/issues/0005-s2-session-seating-freeze-blockers.md`   | 残 blocker は E1/E2/E3 で解消 → **ADR-0019 で schema `1.0` freeze**（2026-06-13） |
| ISSUE-0006 | Hand 開始時の seat→player_id 選択 UX が未確定        | Resolved | 2026-06-07 | hand logger × session / GUI | `docs/issues/0006-seat-selection-ux-at-hand-start.md`      | E3 で UX 確定＋実装（座席設定ダイアログ／開始時設定＋carry-forward／その場 create／空席 skip） |
| ISSUE-0007 | Legacy hand log（timestamp/no player_id）の取り込み方針 | Open | 2026-06-03 | data migration / S2.x       | `docs/issues/0007-legacy-hand-log-migration-policy.md`     | Phase 2.4 着手判断時 |
| ISSUE-0008 | pokerkit を live engine として incremental 駆動できるか | Fixed | 2026-06-03 | reconstruct / dependency | `docs/issues/0008-pokerkit-online-feeding-feasibility.md` | spike で feasibility 確認 (pokerkit 0.7.4)。実装は R2 |
| ISSUE-0009 | actor 競合解決と silent-fold 合成のポリシー未確定 | Open | 2026-06-03 | reconstruct / integration | `docs/issues/0009-actor-conflict-silent-fold-policy.md` | ADR-0009（R3 で確定） |
| ISSUE-0010 | 決定的 replay の記録境界とスレッド順序近似 | Resolved | 2026-06-03 | reconstruct / integration | `docs/issues/0010-replay-determinism-record-boundary.md` | Phase B+C #6（記録境界・clock 源を確定 + AudioEvent.{seat,confidence} 露出）。replayer/許容度は Phase F #8 |
| ISSUE-0011 | hand / action schema の freeze 未確定事項 | Fixed | 2026-06-05 | contracts / hand core | `docs/issues/0011-hand-action-schema-freeze-blockers.md` | Phase F3b #8（hand/action を `1.0` freeze, additionalProperties:true。`_MODELS` 登録 + code↔contract + golden→schema） |
| ISSUE-0012 | GUI/CLI スレッドが GameStateManager を直接変更しレース | Fixed | 2026-06-08 | threading / GUI / CLI | `docs/issues/0012-gui-thread-rebuy-race.md` | review hardening（rebuy/new_hand を queue 経由に一元化 + 回帰テスト） |
| ISSUE-0013 | Session/Seating Viewer の data source 依存と将来拡張    | Open   | 2026-06-03 | desktop (WS2-α) / GUI       | `docs/issues/0013-session-viewer-data-source-and-enhancements.md` | 旧 0008 から採番替え（verify-v1 統合時の ID 衝突解消）。E1〜E3 実装で #1 は概ね解消 |
| ISSUE-0014 | PN5180 + ESP32-S3 firmware ↔ Python の HTTP API 契約 | **Superseded** | 2026-06-01 | rfid / firmware boundary | `docs/issues/0014-pn5180-firmware-http-contract.md`        | 旧番号 0006 から採番替え。Superseded by ISSUE-0015 |
| ISSUE-0015 | ESP32-S3 (PN5180) USB CCID firmware contract         | Open   | 2026-06-01 | rfid / firmware boundary | `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md`    | 旧番号 0007 から採番替え。ADR-0015 follow-up   |
| ISSUE-0016 | S3.1 ledger / points core schema + repository 実装   | Fixed  | 2026-06-10 | core / contracts (S3.1)  | `docs/issues/0016-ledger-core-implementation.md`           | S3.1 実装済（`core/ledger*.py`, schema/fixtures, tests 緑）。ADR-0016。旧 ISSUE-0012（verify-v1 統合で採番替え） |
| ISSUE-0017 | S3.2 desktop ledger viewer / editor（別画面）         | Fixed  | 2026-06-10 | gui / desktop (S3.2)     | `docs/issues/0017-ledger-desktop-viewer.md`                | S3.2 実装済（`gui/ledger_view.py`, `main.py --ledger`）。ADR-0016。旧 ISSUE-0013（採番替え） |
| ISSUE-0018 | S3.3 settlement 確定 + paid/unpaid + CSV export      | Fixed  | 2026-06-10 | core / export (S3.3)     | `docs/issues/0018-settlement-export.md`                    | S3.3 実装済（`output/ledger_csv_exporter.py`, `main.py --export-ledger`）。ADR-0016。旧 ISSUE-0014（採番替え） |
| ISSUE-0019 | player viewer のプライバシーモデル（本人確認 / 閲覧範囲） | Fixed  | 2026-06-10 | viewer API / mobile (M1/M5) | `docs/issues/0019-player-viewer-privacy-model.md`          | serene ISSUE-0013 から採番替え（verify-v1 統合）。M5 (ADR-0018): v1 = name-pick で確定（注文はスタッフ確定を挟む）。PIN は将来再評価 |
