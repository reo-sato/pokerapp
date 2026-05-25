# Issue 0006: hand_id が int (hand logger) と cross-app 文字列契約で不整合

## Date

2026-05-22

## Status

Open

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

未対応（S2 の `hand_ref` 設計で確定する）。選択肢:

- **A. 複合キーのまま契約化**: cross-app の hand 参照は常に `(session_id, hand_id)` 複合。
  hand logger の int は変えない。`hand_ref` が複合キーを保持する。
  - Pros: 既存実装を変えない。Cons: 参照のたびに複合キーを扱う。
- **B. 文字列 hand_id に正規化**: 境界で `f"{session_id}:{hand_id}"` 等の派生文字列 ID を作る。
  - Pros: 他 ID（player_id/session_id）と同じく「単一 opaque 文字列」で統一。
  - Cons: 派生規則を契約に固定する必要。hand logger 内部は int のまま二重表現になる。
- **C. hand logger 側も globally unique な文字列 hand_id を採番**: 破壊的。既存 JSON ログと
  PHH エクスポートに影響。
  - Pros: 最もクリーン。Cons: hand logger 既存挙動・既存ログの互換性に breaking。

いずれも shared-ids 契約の breaking change を伴う可能性があるため、S2 着手時に ADR を起こして
確定する（`versioning-and-freeze.md` の freeze order #3 = session/seat/hand_ref のタイミング）。

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

- `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`
- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`（hand_ref 定義）

## Related Commits

- 本 issue と同じコミット（contracts bootstrap, Phase 0a）

## Notes

現時点では hand logger 単体運用に影響しないため Open のまま保持（risk register）。
S2（session + hand-based seating）の `hand_ref` 設計が直接の解決タイミング。
