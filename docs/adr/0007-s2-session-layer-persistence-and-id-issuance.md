# ADR-0007: S2 session layer persistence and session_id issuance

## Status

Accepted

## Date

2026-05-25

## Context

S2 core 実装（`session` + hand-based `seat_assignment` + `hand_ref`）に着手するにあたり、
ISSUE-0005 が残していた 2 つの未確定事項を core 層について確定する必要がある:

1. **`session_id` の最終採番方式** — 現 hand logger は timestamp 文字列
   `"%Y-%m-%d_%H%M%S_session1"`（`main.py`）。共有 ID 契約（`shared-ids.md`）は
   「opaque 非空・UUID4 hex 推奨」を要求するだけで両方可。
2. **`seat_assignment` の永続ストア形** — hand logger の session JSON（`HandSummary`）内に
   持つか、別ストアに持つか。現 `HandSummary.players` は `{seat, name, ...}` で `player_id` を
   持たず registry 未接続（ISSUE-0005 #2）。

制約（forces）:

- **hand logger を壊さない**（ADR-0006）。`core/hand_log.py` / JSON writer / PHH を変更しない。
- **registry の `player_id` に依存**する（seat_assignment は player_id を持つ）。hand logger の
  name ベース seating とは識別子体系が異なる。
- **将来 ledger（S3）を同じ session 配下に additive 拡張**できる配置にしたい。
- hand logger ↔ session レイヤの reconciliation / migration は **S2 scope 外**。

関連: ADR-0006（S2 contract / 複合キー）、ISSUE-0005（freeze blockers）、S1 player registry。

## Decision

S2 core では session レイヤを hand logger から **意図的に decouple** する:

1. **`session_id` は session レイヤが UUID4 hex で採番する**（`uuid.uuid4().hex`、player_id と
   同形式）。hand logger 側の timestamp `session_id` は据え置き、両者は別 namespace。
   この session_id は contract schema の「opaque 非空文字列」に適合する。
2. **永続ストアは session レイヤ専用の単一 JSON（既定 `sessions.json`、`.gitignore` 済）**。
   hand logger の session JSON には手を入れない。seat_assignment は session 配下に hand 単位で
   入れ子に持つ（`sessions[].hands["<hand_id>"].seats[]`）。将来 ledger entry を
   `sessions[].ledger[]` 等として同じ session 配下に additive で足せる。
3. 永続化は player registry / JsonWriter と同じ **アトミックリネーム**方式に揃える。
4. hand logger の hand との突き合わせ（同一 `hand_id` の意味的接続、`HandSummary` への
   `player_id` 追加）は **S2 core では行わない**（ISSUE-0005 #2 の hand-logger 接続側は open のまま）。

## Alternatives Considered

- **Alternative A（採用）— 独立採番 + 専用ストア（decoupled）**
  - Pros: hand logger を一切変更しない。registry の player_id 体系に閉じて整合が単純。ledger 拡張も同居しやすい。
  - Cons: session レイヤと hand logger で `session_id` / `hand_id` が二重に存在し、突き合わせは将来課題。
  - Why chosen: 「hand logger 不変」と S2 の最小 core を両立する最短路。

- **Alternative B — hand logger の session JSON に seat_assignment を相乗りさせる**
  - Pros: hand と seating が 1 ファイルに集約。
  - Cons: `HandSummary` に `player_id` を足す breaking 変更が必要。registry 接続・migration を
    S2 で抱える。JSON writer / PHH への波及。
  - Why rejected: S2 scope（core 最小・hand logger 不変）を超える。

- **Alternative C — `session_id` を hand logger の timestamp 形式に統一**
  - Pros: 1 種類の session_id。
  - Cons: timestamp は衝突可能性があり opaque 安定キーに不向き。複数卓同時運用で曖昧。
  - Why rejected: 共有 ID 契約の「安定・一意」に UUID4 のほうが合致。

## Consequences

- Positive
  - hand logger・S1 を無改修で S2 core を追加できる。
  - `session_id` が UUID4 hex で player_id と同形式 → 契約・テストが単純。
  - `sessions.json` 配下が将来 ledger（S3）の自然な置き場になる。
- Negative / trade-offs
  - hand logger の hand と session レイヤの hand を結ぶ reconciliation が将来必要（ISSUE-0005 に残す）。
  - session_id が 2 系統（hand logger timestamp / session レイヤ UUID）並存する。
- Neutral / new constraints
  - seat_no 範囲は contract どおり 1..9。mid-session seat change は ADR-0006 のとおり hand 間差分で導出
    （専用イベントを持たない）。

## Validation / Follow-up

- [x] `core/session.py`（`Session` / `SeatAssignment` / `HandRef`）+ `core/session_repository.py` を追加。
- [x] `sessions.json` を `.gitignore` に追加。
- [x] code↔contract test（`tests/test_session_repository.py::test_core_session_matches_contract`）を追加。
- [x] ISSUE-0005 を更新（#1 / #2 を core について Resolved、hand-logger 接続側は Open）。
- [ ] hand logger ↔ session レイヤの reconciliation（同一 hand の意味的接続）。S2 拡張 / S5。
- [ ] schema を 1.0 へ昇格（freeze）。ISSUE-0005 残項目（hand-logger 接続・seat change UI 要件）決着後。

## Related Files

- `core/session.py`, `core/session_repository.py`
- `tests/test_session_repository.py`
- `.gitignore`（`sessions.json`）
- `docs/contracts/session-seating.md`, `docs/contracts/repository-interfaces.md`,
  `docs/contracts/error-shapes.md`
- `docs/issues/0005-s2-session-seating-freeze-blockers.md`

## Related Tests

- `tests/test_session_repository.py`（全テスト）
- `tests/test_contracts.py::test_fixtures_match_schema`

## Related Commits

- 本 ADR と同じコミット（S2 core implementation）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0006（S2 contract / 複合キー）、ISSUE-0005（残 open question）、ADR-0005（永続化スタイル）
