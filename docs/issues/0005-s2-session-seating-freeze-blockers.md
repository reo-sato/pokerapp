# Issue 0005: S2 (session / seat_assignment / hand_ref) freeze の未確定事項

## Date

2026-05-25

## Status

Resolved（2026-06-13, ADR-0019 で schema `1.0` freeze）

## Resolution (2026-06-13, ADR-0019)

残 blocker（hand logger ↔ session hand の reconciliation、mid-session seat change の運用 UI 要件）は
統合後に解消済み:

- **hand logger 接続**: E1+E2-core（ADR-0008 write-through、`HandSummary.players[].player_id` additive、
  `session_id` を session レイヤ UUID4 hex に切替）+ E3（`main.py` 結線、`config.session_layer.enabled`）。
- **mid-session seat change UI**: E3（`gui/seat_selection.py` + carry-forward）。明示 move イベントは
  持たず最新 hand との差分で導出（ADR-0006）で確定。
- #1 session_id（UUID4 hex, ADR-0007）/ #2 seat_assignment 永続形（`sessions.json`）/ #4 seat_no（1..9）も
  core 確定済み。

→ session / seat_assignment / hand_ref schema を `1.0` に freeze（ADR-0019）。code↔contract test
（`tests/test_contracts.py::test_core_session_matches_contract`）追加。下流 S3/settlement/viewer も
同 ADR で同時 freeze。

## Update (2026-05-25, S2 core 実装)

S2 core 実装（`core/session.py` / `core/session_repository.py`, ADR-0007）で以下を確定:

- **#1 `session_id` 採番** → **確定**: session レイヤが UUID4 hex で採番（player_id と同形式）。
  hand logger の timestamp session_id は据え置き、両者は別 namespace（reconciliation は将来課題）。
- **#2 `seat_assignment` 永続ストア形** → **確定（core 側）**: hand logger の session JSON では
  なく専用ストア `sessions.json` に hand 単位で入れ子保持（`HandSummary` は不変）。hand logger 側
  への player_id 接続は **未着手のまま Open**。
- **#3 mid-session seat change** → ADR-0006 のとおり「最新 hand との差分で導出」を採用し、専用
  move イベントは持たない。明示的 seat-change 表現の要否は運用 UI 要件次第で Open。
- **#4 `seat_no` 範囲** → 1..9 を core で enforce（`InvalidSeatError`）。テーブル最大席数の最終
  確定は運用要件待ち。

**残（schema `1.0` freeze の blocker）**: hand logger hand ↔ session レイヤ hand の
reconciliation（同一 hand の意味的接続）、mid-session seat change の運用 UI 要件。これらが
決着するまで session/seat/hand_ref schema を `1.0` に昇格しない。

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
- `docs/worklog/2026-05-25-s2-session-seating-core.md`（S2 core 実装）

## Related ADRs

- `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md`
- `docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md`（#1/#2 を core で確定）

## Related Commits

- 本 issue と同じコミット（S2 contracts planning, Phase 0b）

## Notes

draft 段階の risk register。Open のまま保持し、S2 着手時に各項目を順次クローズする。
hand_id の cross-app 形は ISSUE-0004（ADR-0006 で Resolved）で別途決着済み。

**下流依存（S3, 2026-06-10 追記）**: ledger / settlement schema は session schema（freeze order #3）の
上に乗るため、その `1.0` freeze（#4 ledger / #5 settlement）は **本 issue の session freeze 後**に行う
（依存順, `versioning-and-freeze.md` § freeze order）。S3 の設計・契約 draft 自体は session freeze を
待たず進められる（ADR-0011, `docs/contracts/ledger-overview.md`）。`session_id` を ledger 側でも
session レイヤ UUID に揃える点は ADR-0007 / ADR-0011 で確定済み。
