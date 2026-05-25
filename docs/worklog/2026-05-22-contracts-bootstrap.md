# Worklog: Phase 0a — contracts bootstrap

## Date

2026-05-22

## Scope / Task

Phase 0a (contracts bootstrap): core / desktop / mobile の並行開発を安全に進めるための
**contract-first 基盤** を先に凍結する。`docs/contracts/` を新設し、shared ID 契約・schema/
fixture 配置規則・freeze/versioning ルール・drift detection の最小方針を定義する。
production business logic の実装には入らない。

## なぜ docs/contracts/ を追加したか

- ADR-0004 で contract-first 並行開発を採用したが、それは「方針」であり、契約の **置き場・形式・
  freeze/変更手順・drift 検知** という具体は未定だった。
- 並行開発の主リスクは **contract drift**（ISSUE-0005）。これを test で検知するには、schema と
  fixtures を単一 source として物理的に置き、機械検証できる形にする必要がある。
- front-end（特に mobile, WS3）が core 完成を待たず「契約だけ見て」mock を書ける入口を作る。

## Goal

- `docs/contracts/` の構造と運用ルールを文書化する。
- shared ID 契約（player_id / session_id / hand_id）を定義する。
- schema / fixture の配置・命名規則、freeze / versioning / additive vs breaking を定義する。
- contract drift を検知する最小 test を入れる。
- player の最小 schema + fixtures を追加する（本実装に踏み込みすぎない）。
- docs-as-code（CLAUDE.md / ADR / issue / decision-log / CHANGELOG）を更新する。

## Changed Files

- `docs/contracts/README.md` — 入口。置き場と運用ルール。
- `docs/contracts/shared-ids.md` — player_id / session_id / hand_id 契約。
- `docs/contracts/versioning-and-freeze.md` — freeze 定義 / versioning / additive vs breaking /
  freeze order / drift detection 方針。
- `docs/contracts/repository-interfaces.md` — front-end が呼ぶ抽象 interface 契約（player を基準例）。
- `docs/contracts/error-shapes.md` — error code 契約（分岐は code、表示は message）。
- `docs/contracts/validation-rules.md` — schema で表現しきれない業務 validation（core が SoT）。
- `docs/contracts/schemas/shared-ids.schema.json` — 共有 ID 形式の $defs（documentary）。
- `docs/contracts/schemas/player.schema.json` — player schema (v1.0, S1)。
- `docs/contracts/fixtures/player/*.json` — canonical / valid-minimal / valid-rich /
  invalid-empty-display-name / invalid-missing-player-id / invalid-extra-field。
- `tests/test_contracts.py` — 最小 contract drift detection（schema 妥当性 / fixtures 整合 /
  code↔contract）。
- `requirements.txt` — `jsonschema>=4.0.0` を追加（contract test 用）。
- `CLAUDE.md` — ディレクトリ構成に `docs/contracts/` を追加、Parallel development plan に
  契約 source の参照と Phase 0a 完了状況を追記。
- `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md` — 新規 ADR (Accepted)。
- `docs/issues/0005-parallel-dev-contract-drift.md` — Phase 0a の部分緩和を追記、Status 更新。
- `docs/issues/0006-hand-id-int-vs-cross-app-string.md` — 新規 issue (Open)。
- `docs/decision-log.md` — ADR-0005 / ISSUE-0006 を index に追加。
- `CHANGELOG.md` — Unreleased / Docs & Planning に追記。

## Expected Behavior

- 契約の置き場（`docs/contracts/`）と運用ルールが repo に明示される。
- shared IDs の方針（形式・採番・一意性・永続・cross-app 参照）が固定される。
- schema / fixture / freeze / versioning ルールが読める。
- contract drift を検知する最小 test が緑になる。
- 以後 S1 / S2 / mobile scaffold がこの契約を参照して進められる。

## Implemented Behavior

期待どおり:

- `docs/contracts/` を 6 doc + schemas/ + fixtures/ で bootstrap。
- shared-ids.md で player_id（UUID4 hex, S1 確定）/ session_id（opaque string, S2 確定）/
  hand_id（現状 int, cross-app は複合キー, S2 reconcile）を定義。現実装との不一致（hand_id int）を
  既知不整合として明記し ISSUE-0006 に分離。
- player.schema.json (v1.0) + 6 fixtures。`tests/test_contracts.py` が
  schema 妥当性 / fixtures 整合（valid 通過・invalid 違反）/ code↔contract（core 生成 Player が
  schema 適合）を検証。

## Test Results

- `python -m pytest tests/test_contracts.py -v` → **3 passed**
  （schema 妥当性 / fixtures[player] 整合 / core Player ↔ contract）。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` → **155 passed**
  （S1 後のベースライン 152 + contract test 3、回帰なし）。
- `jsonschema` を新規導入（requirements 追加）。未導入環境では contract test は importorskip で
  skip する設計を確認。

## Mismatches Found During Testing

- **hand_id の int vs cross-app 文字列契約の不整合** を発見。共有 ID 原則は「cross-app では
  opaque 文字列」だが、hand logger は `hand_id: int`（per-session 連番）。これは bug ではなく
  単体運用前提の設計差。shared-ids.md に既知不整合として記載し、**ISSUE-0006** に分離して
  S2（hand_ref）で reconcile することにした。
- それ以外の mismatch なし。schema・fixtures・core 生成物は初回実行で整合。

## Fixes Applied

- hand_id 不整合は本 phase では「文書化 + issue 化」に留め、コード修正はしない（cross-app 形は
  S2 で確定するため。今 int を変えると hand logger 既存挙動・既存 JSON ログに breaking）。
- `created_at` の `format: date-time` は jsonschema のデフォルトで非強制（format assertion 無効）
  であることを利用し、core の `isoformat(timespec="seconds")`（tz なし）が schema を通過する
  ことを確認。format を強制すると false drift になるため強制しない方針とした。

## Remaining Gaps / Out-of-Scope

- [ ] `session_id` / `hand_id` の cross-app 形確定（ISSUE-0006, S2）。
- [ ] repository / service interface の具体契約定義（Phase 0b〜）。
- [ ] mobile mock が同じ fixtures を読む構造（WS3 着手時）。
- [ ] CI への `jsonschema` 追加と contract test 必須化。
- [ ] session / ledger / point / settlement の schema・fixtures（S2〜S4）。
- [ ] production business logic、UI、API、DB、sync 実装は本 phase の scope 外。

## Related ADRs

- `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`
- `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`

## Related Issues

- `docs/issues/0005-parallel-dev-contract-drift.md`（部分緩和）
- `docs/issues/0006-hand-id-int-vs-cross-app-string.md`（新規）
- `docs/issues/0003-point-balance-source-of-truth.md`（S3 の gate）

## Related Commits

- 本 worklog と同じコミット（contracts bootstrap, Phase 0a）
