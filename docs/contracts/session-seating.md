# Session / seat_assignment / hand_ref contract (S2 draft)

> **Status: draft / 未 freeze**（freeze order #3）。本 doc と `schemas/{session,seat_assignment,hand_ref}.schema.json`
> + `fixtures/` は **S2 の契約草案**。実装済を意味しない。freeze は S2 着手時に ADR-0006 Accepted +
> contract test 緑 + fixtures 完備 + ISSUE-0005 決着をもって行う（`versioning-and-freeze.md` の freeze 定義）。
> 草案段階の schema version は `0.x`。freeze 時に `1.0` へ昇格する。

S2 は hand logger に対して **session レイヤ** と **hand-based seating** を重ねる。core /
desktop / mobile が同じ契約を参照できるよう、ここで境界を凍結する。重い識別子判断（hand の
cross-app 参照）は ADR-0006 で確定済み。

## モデル要約

| model | 役割 | 一意キー | 永続/参照 |
|-------|------|---------|-----------|
| `session` | 1 卓 1 回の運営単位 | `session_id` | session レイヤが採番・永続化 |
| `seat_assignment` | hand-based の seat→player スナップショット 1 行 | `(session_id, hand_id, seat_no)` | session レイヤが hand 開始ごとに記録 |
| `hand_ref` | ledger app が hand を参照する不変軽量参照 | `(session_id, hand_id)` | hand logger 側 hand を cross-app から読む |

### 1. `session`

- **属性**: `session_id`（必須）/ `started_at`（必須）/ `status`（必須, `open`|`closed`）/
  `label`（任意・表示用）/ `ended_at`（任意, open 中は absent）/ `blinds`（任意・参考値）。
- **player membership は持たない**。「誰が参加しているか / どの席にいるか」は seat_assignment
  （hand-based）から **導出** する。これにより seat change が最新 hand との差分として自然に表れる。
- **cross-field**（schema 外, core が enforce）: `status=closed` なら `ended_at` 必須。
  `open`→`closed` は一方向遷移（closed の再 open は不可）。
- `session_id` 形式は opaque 非空文字列（UUID4 hex 推奨）。**最終採番方式は ISSUE-0005**
  （現 hand logger は timestamp 文字列 `..._session1`）。

### 2. `seat_assignment`

- **hand-based**: 1 行 = あるハンド `(session_id, hand_id)` のある席 `seat_no` への 1 player 割り当て。
- **属性**: `session_id` / `hand_id` / `seat_no`（1..9）/ `player_id`（S1 形式）/
  `status`（任意, `active`|`sitting_out`, absent は active）。
- **partial assignment**: 空席は **行を作らない**。全席分の行を要求しない。
- **active/inactive**: 一時離席は `status=sitting_out` で表す（任意フィールド, additive）。
- **mid-session seat change**: session 中の席移動は「次 hand の seat_assignment が前 hand と違う」
  という hand 間差分として現れる。専用の move イベントは持たない（導出で足りる）。詳細・確定は ISSUE-0005。
- **整合**（schema 外, core が enforce 予定）: 同一 `(session_id, hand_id)` 内で `seat_no` 重複不可、
  `player_id` 重複不可（1 player が同 hand で 2 席に座らない）、`player_id` は registry に実在。

### 3. `hand_ref`

- **不変軽量参照**。ledger app が hand logger の 1 ハンドを後から安定参照するための読み取り用。
- **属性**: `session_id` / `hand_id` / `started_at` / `seat_assignments`（hand 開始時点の
  seat→player スナップショット, 空配列可）。
- `seat_assignments` の各要素は standalone `seat_assignment` から `session_id`/`hand_id` を
  除いた **denormalized 埋め込み形**（`{seat_no, player_id, status?}`）。両表現の整合は
  session レイヤが書き込み時に保証する（drift 注意点。ADR-0006 Consequences 参照）。

## hand_id boundary（ISSUE-0004 の決着 = ADR-0006）

- `hand_id` は **session 内連番 int のまま据え置く**（hand logger 不変）。
- `hand_id` 単独は global key **ではない**。**`(session_id, hand_id)` の複合キーが globally unique**。
- cross-app の hand 参照は常に `hand_ref`（複合キー保持）を介する。
- 単一 opaque トークン `f"{session_id}:{hand_id}"` は **将来 API 化（S5）の additive 拡張として予約**、
  S2 では凍結しない。
- 採用根拠・却下案は ADR-0006 を参照。

## repository / service interface 草案（S2, planned）

語彙非依存の契約（`repository-interfaces.md` に同期）。具象シグネチャは一例。

| 操作 | 入力 | 出力 | error（`error-shapes.md`, planned） |
|------|------|------|------|
| create session | `label?` | `Session`（`session_id` 採番済, status=open） | — |
| list sessions | — | `Session[]`（作成順） | — |
| get session | `session_id` | `Session` | `not_found` |
| close session | `session_id`, `ended_at` | `Session`（status=closed） | `not_found` / `already_closed` |
| assign seat for hand | `session_id`, `hand_id`, `seat_no`, `player_id` | `SeatAssignment` | `not_found` / `session_closed` / `seat_taken` / `unknown_player` / `invalid_seat` |
| list seat assignments by hand | `session_id`, `hand_id` | `SeatAssignment[]` | `not_found` |
| resolve seating for hand_ref | `session_id`, `hand_id` | `HandRef`（snapshot 込み） | `not_found` |
| current seating | `session_id` | `SeatAssignment[]`（最新 hand から導出） | `not_found` |

- **業務ルールは core が source of truth**。front-end は結果と error code を表示するだけ
  （`validation-rules.md` / `error-shapes.md`）。
- mobile は同 interface の in-memory mock を `fixtures/{session,seat_assignment,hand_ref}/` で先行実装できる。

## fixtures（contract test の oracle）

`fixtures/session/`, `fixtures/seat_assignment/`, `fixtures/hand_ref/` に
`canonical` / `valid-minimal` / `invalid-*` を追加済み。`tests/test_contracts.py` の `_MODELS`
に 3 model を登録し、schema↔fixture 整合（valid 通過 / invalid 違反）を検証する。

## freeze 状態 / 未確定事項 / blockers

- **本 doc + 3 schema + fixtures + ADR-0006 は draft（schema version `0.x`, 未 freeze）**。
  ただし **S2 core は実装済**（`core/session.py` / `core/session_repository.py`, ADR-0007）。
  core は draft schema に対して `code↔contract` test 緑
  （`tests/test_session_repository.py::test_core_session_matches_contract`）。
- **core 実装で確定（ADR-0007）**: `session_id` は session レイヤが UUID4 hex で採番（hand logger
  の timestamp session_id とは別 namespace）。seat_assignment は専用ストア `sessions.json` に
  hand 単位で入れ子保持（hand logger JSON は不変）。
- **freeze の残 blocker（ISSUE-0005）**: hand logger の hand ↔ session レイヤ hand の
  reconciliation（`HandSummary` への player_id 接続）、mid-session seat change の運用 UI 要件。
  これらが決まるまで schema を `1.0` に昇格しない。
- **据え置き（決定済・S2 では変更しない）**: `hand_id: int`（ADR-0006）。seat_no 範囲 1..9。
- **out of scope（S2 core 段階）**: ledger / point / settlement（S3〜S4）、sync / API（S5）、
  desktop/mobile UI 実装、hand logger との自動接続 / migration。
