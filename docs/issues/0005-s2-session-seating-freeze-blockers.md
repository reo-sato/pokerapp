# Issue 0005: S2 (session / seat_assignment / hand_ref) freeze の未確定事項

## Date

2026-05-25

## Status

Open

## Severity / Priority

- Severity: Medium（S2 schema を `1.0` へ freeze する前に確定が必要。draft 段階では着手可）
- Priority: P2

## Area

contracts / session-seating (S2)

## Expected Behavior

S2 の `session` / `seat_assignment` / `hand_ref` 契約が freeze 済（schema `1.0` +
fixtures + contract test 緑 + ADR Accepted）になり、core / desktop / mobile が同一契約で
本実装に入れる状態。

## Actual Behavior

draft（version `0.x`）まで整備済み（`docs/contracts/session-seating.md`、3 schema、fixtures、
ADR-0006、contract test 登録）。ただし freeze に必要な以下の判断が **未確定**:

1. **`session_id` の最終採番方式**: 現 hand logger は timestamp 文字列
   `"%Y-%m-%d_%H%M%S" + "_session1"`（`main.py`）。これを維持するか UUID4 hex に統一するか。
   共有 ID 契約は「opaque 非空」を要求するだけなので両方可。統一すると hand logger 側の
   session_id 生成を変える必要（影響範囲の確認要）。
2. **`seat_assignment` の永続ストア形**: hand logger の session JSON 内（`HandSummary` に
   seat→player_id を足す）に持つか、別ファイル / 別ストアに持つか。現 `HandSummary.players` は
   `{seat, name, ...}` で `player_id` を持たない（registry 未接続）。
3. **mid-session seat change の確定表現**: 「hand 間差分として導出」で十分か、明示的な
   seat-change イベントが要るか（運用 UI の要件次第）。
4. **`seat_no` 範囲**: 現 draft は 1..9。テーブル最大席数の確定（2..9 席運用）と整合させる。

## Reproduction

仕様レビュー（バグではなく freeze 前の open question）:

1. `docs/contracts/session-seating.md` § freeze 状態 / 未確定事項 / blockers を参照。
2. `core/hand_log.py` の `HandSummary.players` に `player_id` が無いことを確認（registry 未接続）。
3. `main.py` の `session_id` 生成が timestamp 文字列であることを確認。

## Root Cause

S2 は hand logger（単体運用・seat は名前ベース）に registry 由来の `player_id` と session
レイヤを重ねる。既存実装の識別子・永続形と新契約の擦り合わせが未了。

## Fix

未対応（S2 着手時に確定）。確定したら:

- `session_id` 採番方式を ADR 化（必要なら）し、`shared-ids.md` / `session.schema.json` を更新。
- `seat_assignment` 永続形を決め、必要なら `hand_log.py` / JSON writer の additive 拡張。
- mid-session seat change 表現を `session-seating.md` に明文化。
- schema を `1.0` に昇格し、`code↔contract` test（core 実装後）を追加。

## Regression Test

- 現 draft は `tests/test_contracts.py`（`_MODELS` に session / seat_assignment / hand_ref 登録）で
  schema↔fixture 整合を固定済み。
- freeze 時に core repository の `code↔contract` test（player の
  `test_core_player_matches_contract` 相当）を追加する。

## Affected Files

- `docs/contracts/session-seating.md`
- `docs/contracts/schemas/{session,seat_assignment,hand_ref}.schema.json`
- `core/hand_log.py`（`HandSummary.players` に player_id 無し）
- `main.py`（`session_id` 生成）

## Related Worklog

- `docs/worklog/2026-05-25-s2-contracts-planning.md`

## Related ADRs

- `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md`

## Related Commits

- 本 issue と同じコミット（S2 contracts planning, Phase 0b）

## Notes

draft 段階の risk register。Open のまま保持し、S2 着手時に各項目を順次クローズする。
hand_id の cross-app 形は ISSUE-0004（ADR-0006 で Resolved）で別途決着済み。
