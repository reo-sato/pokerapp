# ADR-0019: S2 / S3 / viewer model の schema を `1.0` に freeze（統合後）

## Status

Accepted

## Date

2026-06-13

## Context

`docs/contracts/versioning-and-freeze.md` の freeze 定義は「schema が `$id`/`version` を持つ +
fixtures + contract test 緑 + 承認 ADR が Accepted」。これまで session/seat_assignment/hand_ref（S2）・
ledger_entry/point_ledger_entry/session_settlement（S3/S4）・order_request/player_session_summary
（viewer）はすべて **draft `0.x`** に留めていた。freeze order（同 doc §6）は依存順
（shared-ids → player → S2 → S3 → settlement → interface）で、S2 の freeze は **ISSUE-0005**
（hand logger 接続・mid-session seat change 運用 UI 要件）の決着が blocker だった。

統合（2026-06-13, viewer/mobile/orders を verify-v1 ledger にマージ）後の状況:

- **ISSUE-0005 の残 blocker が解消**: hand logger × session 接続は E1+E2-core（ADR-0008 write-through、
  `HandSummary.players[].player_id` additive）で、mid-session seat change UI は E3
  （`gui/seat_selection.py` + carry-forward、明示 move イベントは持たず最新 hand 差分で導出 = ADR-0006）で
  実装済み。`session_id` 採番（UUID4 hex, ADR-0007）・`seat_assignment` 永続形（`sessions.json`）・
  `seat_no` 範囲（1..9）も core で確定済み。
- **全 model が全 consumer で安定**: S2/S3 は core + desktop（`gui/session_viewer.py` / `gui/ledger_view.py`）
  + CSV export、viewer model は API（`api/`）+ mobile（`mobile/`）で実装済み。draft を変える理由がもう無い。
- player schema は既に `1.0`、hand/action schema は `1.0`（ISSUE-0011）。残る draft はこの 8 model のみ。

## Decision

上記 8 model の schema を **`1.0` に freeze** する（version `0.1` → `1.0`、description の
「draft (未 freeze)」を「1.0 freeze 済 (ADR-0019)」に更新）:

1. **S2**: `session` / `seat_assignment` / `hand_ref`（`additionalProperties: false`）。
2. **S3**: `ledger_entry` / `point_ledger_entry`（cash+point 併用・order 明細・残高 fold = ADR-0016）。
3. **S4(schema)**: `session_settlement`（player→店の 1 方向、`compute`/`commit`/paid-unpaid）。
4. **viewer**: `order_request`（ADR-0018）/ `player_session_summary`（ADR-0017）。

freeze 後は `versioning-and-freeze.md` §2 の **additive-only**（optional field 追加・enum 追加・
description のみは worklog で可。削除・rename・required 化・型変更・`additionalProperties` 厳格化は
新 ADR + version bump）に従う。partial-paid（settlement）等の将来拡張は **additive**（optional field /
enum 追加）に収まる見込みのため、本 freeze の妨げにならない。

**code↔contract drift gate を全 model に整備**: `tests/test_contracts.py` に
`test_core_session_matches_contract`（Session/SeatAssignment/HandRef）・
`test_core_settlement_matches_contract`（SessionSettlement）を追加（ledger_entry/point は既存、
order_request/player_session_summary は viewer テストでカバー済み）。

## Alternatives Considered

- **draft のまま据え置き** — 統合で全 consumer が安定した今、freeze しない理由が無く、front-end が
  「いつ壊れるか分からない契約」に依存し続ける。→ 却下。
- **S2 だけ freeze し S3/viewer は後回し** — 依存順は S2 が最上流だが、S3/settlement/viewer も既に
  安定・実装済みで同時に freeze できる。分割すると freeze 作業が複数回に分散し drift 窓が残る。→ 一括 freeze。
- **`additionalProperties: true` に緩めてから freeze**（hand/action と同様） — S2/S3 model は永続正規形が
  確定しており未知キーを許す必要がない。厳格（false）のまま freeze し、拡張は明示的な additive で行う。→ false 維持。

## Consequences

- Positive: front-end（desktop/mobile/API）が `1.0` 契約に対して安心して実装を続けられる。drift は
  code↔contract test が全 model で検知。freeze order が S2→S3→settlement→viewer まで前進。
- Negative / trade-offs: 以後これら model の breaking change は新 ADR + MAJOR version bump が必須
  （additive は従来どおり軽量）。
- Neutral: `versioning-and-freeze.md` の freeze order 表を「frozen」に更新。ISSUE-0005 を Resolved。

## Validation / Follow-up

- [x] 8 schema の version `1.0` 化 + description 更新、fixtures は不変で contract test 緑。
- [x] code↔contract test 追加（session/settlement）、全 model カバー。
- [ ] repository / service interface（freeze order #6, S5）の freeze は cross-app sync 着手時。
- [ ] settlement の partial-paid 等の拡張は additive で行う（必要時に worklog）。

## Related Files

- `docs/contracts/schemas/{session,seat_assignment,hand_ref,ledger_entry,point_ledger_entry,session_settlement,order_request,player_session_summary}.schema.json`
- `docs/contracts/versioning-and-freeze.md` / `docs/contracts/session-seating.md`
- `tests/test_contracts.py`

## Related Tests

- `tests/test_contracts.py::test_core_session_matches_contract` / `::test_core_settlement_matches_contract`
  / `::test_core_ledger_matches_contract` / `::test_fixtures_match_schema`

## Related Commits

- 本 ADR と同じ commit（schema 1.0 freeze）

## Supersedes / Superseded by

- Supersedes: —（関連: ADR-0006/0007（S2）/ ADR-0016（S3）/ ADR-0017/0018（viewer）/ ISSUE-0005 Resolved）
- Superseded by: —
