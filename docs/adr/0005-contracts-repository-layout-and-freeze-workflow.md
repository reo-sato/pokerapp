# ADR-0005: Contracts repository layout and freeze workflow

## Status

Accepted

## Date

2026-05-22

## Context

ADR-0004 で **contract-first parallel development** を採用したが、ADR-0004 は「方針」であり、
契約を **どこに・どの形式で置き・どう凍結/変更するか** という具体的な仕組みは未定だった。
並行開発の主リスクである **contract drift**（ISSUE-0003）を実際に防ぐには、契約の物理的な
置き場・命名規則・freeze 定義・versioning・drift detection を具体化する必要がある。

制約（CLAUDE.md § Parallel development plan / ADR-0003 / ADR-0004）:

- `player_id` / `session_id` / `hand_id` は全 workstream 共有の安定キー。
- core が業務ルールの source of truth。front-end は契約越しに mock / 実装を差し替える。
- 現時点で実装済は hand logger core と S1 player のみ。session 以降は planned。
- production business logic の実装にはまだ入らない（本タスクは bootstrap）。

関連:

- ADR-0004（contract-first parallel development）— 本 ADR はその具体化。
- `docs/issues/0003-parallel-dev-contract-drift.md`（drift risk）
- `docs/issues/0004-hand-id-int-vs-cross-app-string.md`（hand_id の cross-app 形）
- `docs/worklog/2026-05-22-contracts-bootstrap.md`

## Decision

契約の単一 source として **`docs/contracts/`** を新設し、以下のレイアウトと運用を採用する。

1. **レイアウト**:
   - `README.md`（入口）、`shared-ids.md`、`versioning-and-freeze.md`、
     `repository-interfaces.md`、`error-shapes.md`、`validation-rules.md`。
   - `schemas/<model>.schema.json`（**1 model = 1 schema**、JSON Schema draft 2020-12、
     `$id` + `version` 必須）。
   - `fixtures/<model>/`（`canonical` / `valid-*` / `invalid-*`、contract test の oracle）。
2. **shared IDs**: `player_id`（UUID4 hex, S1 確定）/ `session_id`（opaque string, S2 確定）/
   `hand_id`（現状 int, cross-app は (session_id, hand_id) 複合, S2 reconcile）。アプリ内採番・不変。
3. **freeze の定義**: schema commit + fixtures 揃い + contract test 通過 + **承認 ADR が Accepted**。
4. **versioning**: schema は `MAJOR.MINOR`。additive は MINOR + worklog、breaking は MAJOR +
   新 ADR/issue（旧契約を Superseded）。
5. **drift detection（最小）**: `tests/test_contracts.py` が schema↔fixture 整合
   （valid 通過 / invalid 違反）と code↔contract（core 生成 Player が schema 適合）を検証する。
   `jsonschema` 未導入環境では skip、CI には導入する。
6. **同時更新ルール**: schema 変更 PR は fixtures / docs / front-end stub への影響確認を伴う
   （CLAUDE.md § Documentation and Traceability Rules を contract にも適用）。

JSON Schema を single source とし、将来 OpenAPI（S5 の API 化）や mobile typed models へは
そこから派生させる（contract-first を維持）。

## Alternatives Considered

- **Alternative A — 契約を各 model の ADR 本文に散在させる（専用ディレクトリなし）**
  - Pros: ファイルが増えない。
  - Cons: schema/fixtures を機械検証できず、drift を test で検知できない。front-end が
    「契約だけ見て mock を書く」入口が定まらない。
  - Why rejected: drift detection（ISSUE-0003 の核心）が成立しない。

- **Alternative B — 最初から OpenAPI / gRPC など API spec で契約を書く**
  - Pros: API 化（S5）に直結。型生成エコシステムが豊富。
  - Cons: 現段階は同一プロセス前提（S2〜S4）で API はまだ存在しない。API spec を主にすると
    「まだない API」を既成事実化してしまう。
  - Why rejected: 未実装機能の既成事実化を避ける。JSON Schema なら永続形・mock・将来 API の
    いずれにも展開でき、現状と矛盾しない。

- **Alternative C — コードの型（dataclass）を contract の source にする（code-first）**
  - Pros: 二重管理がない。
  - Cons: core 実装が未着手の model（session/ledger/...）の契約を先に凍結できない。mobile が
    別言語で実装する際に Python の型を直接共有できない。contract-first（実装より先に契約）に反する。
  - Why rejected: 並行開発の前提（front-end が core 完成を待たない）が崩れる。

## Consequences

- Positive
  - 契約が単一 source（`docs/contracts/`）に集約され、drift を test で検知できる。
  - front-end（特に mobile mock）が schema + fixtures だけで先行着手できる。
  - freeze / versioning が明文化され、breaking change が ADR を強制される。

- Negative / trade-offs
  - `jsonschema` を（dev/CI 用に）依存に追加した。
  - schema と core 実装の二重定義が生じる（code↔contract test で drift は抑える）。
  - 契約変更のたびに fixtures / docs の同時更新コストがかかる。

- Neutral / new constraints
  - 新 model は必ず `schemas/` + `fixtures/<model>/` + docs を揃えてから freeze する。
  - `session_id` / `hand_id` の cross-app 形は未確定のまま（ISSUE-0004）。

## Validation / Follow-up

- [x] `docs/contracts/` を bootstrap（README / shared-ids / versioning-and-freeze /
      repository-interfaces / error-shapes / validation-rules）。
- [x] player schema + fixtures（canonical / valid / invalid）を追加。
- [x] 最小 contract test（`tests/test_contracts.py`）を追加し緑を確認。
- [ ] `session_id` / `hand_id` の cross-app 形を S2 で確定（ISSUE-0004）。
- [ ] repository / service interface の具体契約を Phase 0b 以降で定義。
- [ ] CI に `jsonschema` を含め contract test を必須化する。

## Related Files

- `docs/contracts/**`
- `docs/contracts/schemas/player.schema.json`, `docs/contracts/schemas/shared-ids.schema.json`
- `docs/contracts/fixtures/player/*.json`
- `tests/test_contracts.py`
- `requirements.txt`（`jsonschema` 追加）

## Related Tests

- `tests/test_contracts.py::test_schemas_are_valid_json_with_id_and_version`
- `tests/test_contracts.py::test_fixtures_match_schema`
- `tests/test_contracts.py::test_core_player_matches_contract`

## Related Commits

- 本 ADR と同じコミット（contracts bootstrap, Phase 0a）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0004（本 ADR は ADR-0004 の contract-first 方針を物理レイアウトと workflow に具体化する）
