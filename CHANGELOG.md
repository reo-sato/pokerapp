# Changelog

本ファイルは [Keep a Changelog](https://keepachangelog.com/) の慣習に沿って、
ユーザー可視の挙動変更および仕様 / docs の重要更新を記録する。
詳細な経緯は `docs/adr/` / `docs/worklog/` / `docs/issues/` を参照。

## [Unreleased]

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
