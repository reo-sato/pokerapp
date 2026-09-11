# ADR-0006: S2 session/seating contract boundary and hand_id cross-app reference

## Status

Accepted

## Date

2026-05-25

## Context

S2（session + hand-based seating）の本実装に入る前に、`session` / `seat_assignment` /
`hand_ref` の境界を contract-first で確定したい（ADR-0004 / ADR-0005、CLAUDE.md
§ Parallel development plan の freeze order #3）。本 ADR はその設計上もっとも重い 1 点、
すなわち **hand を cross-app でどう一意参照するか（= ISSUE-0004 の決着）** を確定する。

制約（forces）:

- **hand logger を壊さない**: 既存 `core/hand_log.py` は `hand_id: int`（session 内連番）で、
  JSON ログ / PHH エクスポートがこれに依存する。グローバル一意ではない。
- **cross-app 境界で曖昧さを残さない**: 共有 ID 原則（`docs/contracts/shared-ids.md`）は
  「cross-app では opaque・globally unique」を要求する。
- **player_id / session_id は既存契約に整合**: `player_id` は S1 で UUID4 hex 確定。
  `session_id` は opaque 非空文字列（UUID 推奨）。
- **core / desktop / mobile が将来同じ contract を参照**できること。

関連: ISSUE-0004（hand_id int vs cross-app string）、ADR-0003（hand_ref の概念定義）、
ADR-0004 / ADR-0005（contract-first / freeze workflow）。

## Decision

S2 の cross-app における **hand 参照の単位を `hand_ref` オブジェクトとし、その複合キー
`(session_id, hand_id)` を globally unique な hand 識別子とする**。具体的には:

1. **`hand_id` は session 内連番 int のまま据え置く**（hand logger を変更しない）。
   `hand_id` 単独はグローバル参照キーではない。
2. **`(session_id, hand_id)` の複合キーが globally unique**。cross-app の hand 参照は常に
   この複合キーを持つ `hand_ref` を介して行う（ISSUE-0004 の選択肢 **A** を採用）。
3. **`hand_ref` は不変の軽量参照**で、最低限 `session_id` / `hand_id` / `started_at` /
   `seat_assignments`（hand 開始時点の seat→player スナップショット）を持つ。
4. **seat_assignment は hand-based**。1 行 = あるハンドのある席への 1 player 割り当て。
   空席は行を作らない（partial assignment）。「現在の着席」は最新 hand の seat_assignment
   から **導出** する（session には player membership を持たせない）。
5. 単一 opaque トークンが必要になった場合（S5 の API 化等）の派生規則
   `f"{session_id}:{hand_id}"` は **将来の additive 拡張として予約** し、S2 では凍結しない。

schema は draft（version 0.x, 未 freeze）として `docs/contracts/schemas/` に置き、freeze は
S2 着手時に本 ADR の Accepted + contract test 緑 + fixtures 完備をもって行う
（`versioning-and-freeze.md` の freeze 定義）。

## Alternatives Considered

- **Alternative A（採用）— `(session_id, hand_id)` 複合キー / `hand_ref` を参照単位にする**
  - Pros: hand logger を一切変更しない。複合キーは曖昧さなく globally unique。`hand_ref` が
    元々複合情報を持つので自然。
  - Cons: 参照のたびに複合キーを扱う（単一文字列より僅かに冗長）。
  - Why chosen: 「hand logger 不変」と「cross-app 一意」を同時に満たす唯一の非破壊案。

- **Alternative B — 境界で派生文字列 `f"{session_id}:{hand_id}"` を hand_id 化**
  - Pros: 他 ID と同じ「単一 opaque 文字列」で統一。
  - Cons: 派生規則を契約に固定する必要。hand logger 内部 int と二重表現。現状 API が無いのに
    文字列トークンを既成事実化する。
  - Why rejected: 現段階（S2 は同一プロセス）では利点が無く、複合キーで十分。ただし将来 API
    化時の additive 拡張として予約する。

- **Alternative C — hand logger も globally unique な文字列 hand_id を採番**
  - Pros: 最もクリーンで単一キー。
  - Cons: 既存 JSON ログ / PHH / `core/hand_log.py` に breaking。共有 ID 契約の MAJOR 変更。
  - Why rejected: hand logger 不変という制約に反する。移行コストが S2 の目的に見合わない。

## Consequences

- Positive
  - hand logger（`hand_id: int`）に手を入れずに S2 を進められる。
  - cross-app の hand 参照が `hand_ref` に一本化され、ledger app は複合キーで曖昧さなく参照できる。
  - seat_assignment を hand-based にしたことで、seat change が「最新 hand との差分」として自然に表現される。
- Negative / trade-offs
  - hand 参照が単一文字列でなく複合キー（呼び出し側がタプル/オブジェクトを扱う）。
  - `seat_assignment` を standalone 行と `hand_ref.seat_assignments` 埋め込みの 2 表現で持つため、
    両者の整合を docs で明示する必要（drift 注意点）。
- Neutral / new constraints
  - `session` は player membership を持たない（seat_assignment から導出）。
  - `session_id` の最終採番方式（timestamp 文字列 vs UUID 統一）は本 ADR では確定せず、
    ISSUE-0005 に残す。

## Validation / Follow-up

- [x] `session` / `seat_assignment` / `hand_ref` の draft schema + fixtures を追加。
- [x] `tests/test_contracts.py` の `_MODELS` に 3 model を追加し、schema↔fixture 整合を緑化。
- [x] ISSUE-0004 を本 ADR の決定で Resolved に更新。
- [ ] `session_id` 最終採番方式の確定（ISSUE-0005）。S2 freeze の前提。
- [ ] S2 core 実装時に `session` / `seat_assignment` repository を追加し、code↔contract test を足す。
- [ ] schema を 1.0 へ昇格（freeze）。本 ADR Accepted + contract test 緑 + fixtures 完備で行う。

## Related Files

- `docs/contracts/session-seating.md`
- `docs/contracts/schemas/session.schema.json`
- `docs/contracts/schemas/seat_assignment.schema.json`
- `docs/contracts/schemas/hand_ref.schema.json`
- `docs/contracts/fixtures/{session,seat_assignment,hand_ref}/*.json`
- `docs/contracts/shared-ids.md`, `docs/contracts/repository-interfaces.md`
- `core/hand_log.py`（`hand_id: int`、本 ADR で据え置き）

## Related Tests

- `tests/test_contracts.py::test_schemas_are_valid_json_with_id_and_version`
- `tests/test_contracts.py::test_fixtures_match_schema`

## Related Commits

- 本 ADR と同じコミット（S2 contracts planning, Phase 0b）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0003（hand_ref 概念定義）、ADR-0005（contracts layout / freeze workflow）、
  ISSUE-0004（本 ADR で Resolved）、ISSUE-0005（残 open question）
