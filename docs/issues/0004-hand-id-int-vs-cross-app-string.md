# Issue 0004: hand_id が int (hand logger) と cross-app 文字列契約で不整合

## Date

2026-05-22

## Status

Resolved（ADR-0006, 2026-05-25）。schema freeze は S2 着手時（ISSUE-0005 が前提）。

## Severity / Priority

- Severity: Medium（S2 着手前に解決すべき。現状 hand logger 単体では問題なし）
- Priority: P2

## Area

contracts / shared-ids / hand logger ↔ ledger boundary (S2)

## Expected Behavior

共有 ID 契約（`docs/contracts/shared-ids.md`）の原則では、cross-app 境界で参照される ID は
**opaque な文字列で globally unique**。`hand_ref`（planned, S2）は hand を一意に参照できる
不変キーを持つ。

## Actual Behavior

現状の hand logger は `hand_id: int`（`core/hand_log.py` の `ActionRecord` / `HandSummary`）で、
**session 内の連番**。グローバル一意ではなく、`(session_id, hand_id)` の複合でしか一意にならない。
したがって「cross-app では文字列 opaque」という共有 ID 原則と、現実装が一致していない。

## Reproduction

1. `core/hand_log.py` を見ると `hand_id: int`。
2. `docs/contracts/shared-ids.md` の共通原則は「cross-app では opaque string」。
3. 2 つの session が同じ `hand_id=1` を持ちうるため、`hand_id` 単独では player/session のように
   グローバル参照できない。

## Root Cause

hand logger は単体運用を前提に per-session 連番の int を採用していた。cross-app boundary
（ledger app が `hand_ref` で hand を参照する, S2）を後から重ねるため、識別子の一意性スコープが
食い違う。

## Fix

**決着済（ADR-0006）: 選択肢 A を採用**。

- `hand_id` は **session 内連番 int のまま据え置く**（hand logger 不変）。
- `hand_id` 単独は global key ではなく、**`(session_id, hand_id)` の複合キーが globally unique**。
- cross-app の hand 参照は常に `hand_ref`（複合キー保持）を介する。
- 単一 opaque トークン `f"{session_id}:{hand_id}"` は将来 API 化（S5）の additive 拡張として
  予約し、S2 では凍結しない。

却下: B（派生文字列）は現段階で利点がなく既成事実化を招く / C（hand logger を文字列採番）は
既存 JSON・PHH に breaking。詳細は ADR-0006 の Alternatives。

draft schema（`schemas/{seat_assignment,hand_ref}.schema.json`, v0.1）で `hand_id: integer` +
`session_id: string` を複合キーとして表現済み。`1.0` への freeze は ISSUE-0005 決着が前提。

## Regression Test

未実装。S2 で `hand_ref` schema + fixtures を追加し、`tests/test_contracts.py` に hand_ref の
整合検査を加える。確定方式に応じて hand_id 形式の契約テストを追加する。

## Affected Files

- `core/hand_log.py`（`hand_id: int`）
- `docs/contracts/shared-ids.md`（hand_id 節に既知不整合として記載済）
- `docs/contracts/schemas/shared-ids.schema.json`（hand_id を integer として暫定定義）

## Related Worklog

- `docs/worklog/2026-05-22-contracts-bootstrap.md`

## Related ADRs

- `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md`（本 issue を Resolve）
- `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`
- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`（hand_ref 定義）

## Related Commits

- 本 issue と同じコミット（contracts bootstrap, Phase 0a）

## Notes

現時点では hand logger 単体運用に影響しないため Open のまま保持（risk register）。
S2（session + hand-based seating）の `hand_ref` 設計が直接の解決タイミング。
