# Repository / service interface contract

front-end（desktop WS2 / mobile WS3）は **repository interface にのみ依存** し、その具象
（core の local 実装 / in-memory mock / 将来の API client）を注入で差し替える。UI は
「データがどこにあるか」を知らない（CLAUDE.md § 将来 API / sync を入れても壊れにくい境界）。

本 doc は interface の **契約テンプレート**。各 model の interface は phase ごとに確定する。
ここでは player（S1, 実装済）を基準例として示し、session 以降は planned とする。

## 原則

- **業務ルールは core が source of truth**。interface のメソッドは「何ができるか」を定義するが、
  validation・残高計算・settlement 確定の **判断は core 側が行う**。front-end は結果と error を
  受け取って表示するだけ。
- **error は型で返す**（`error-shapes.md`）。front-end は error の種別で UI 分岐する。
- **mock も実装も同じ interface に従う**。mobile mock は fixtures を読むだけで同じ契約を満たす。
- interface は **言語非依存の契約** として記述する（Python の具象シグネチャは一例）。
  mobile（TypeScript 等）は同じ意味の API を各言語で実装する。

## player repository interface（S1, 実装済を契約化）

語彙非依存の契約:

| 操作 | 入力 | 出力 | error |
|------|------|------|-------|
| list players | — | player の配列（作成順） | — |
| get player | `player_id` | player 1 件 | not-found |
| create player | `display_name` | 作成された player（`player_id` 採番済） | empty-display-name / duplicate-display-name |
| rename player | `player_id`, `new_display_name` | 更新後 player | not-found / empty-display-name / duplicate-display-name |

Python 具象（現状 `core/player_repository.py` と一致, 一例）:

```text
list_players() -> list[Player]
get(player_id: str) -> Player                         # raises PlayerNotFoundError
create_player(display_name: str) -> Player            # raises Empty/DuplicateDisplayNameError
rename_player(player_id: str, new_display_name: str) -> Player
```

mobile mock（planned, TypeScript 一例・契約のみ）:

```text
interface PlayerRepository {
  listPlayers(): Promise<Player[]>
  getPlayer(playerId: string): Promise<Player>        // rejects NotFound
  createPlayer(displayName: string): Promise<Player>  // rejects Empty/Duplicate
  renamePlayer(playerId: string, newDisplayName: string): Promise<Player>
}
```

mock は `fixtures/player/*.json` を初期データに読み込み、validation は core と同じ規則
（`validation-rules.md`）を **再実装せず参照** する形にする（UI 側に business logic を複製しない）。

## session / seat_assignment / hand_ref interface（S2 core 実装済, schema は draft）

詳細・モデル定義は `session-seating.md`（draft）/ ADR-0006。core 実装は
`core/session_repository.py`（ADR-0007）。語彙非依存の契約:

| 操作 | 入力 | 出力 | error |
|------|------|------|-------|
| create session | `label?` | `Session`（`session_id` 採番済, status=open） | — |
| list sessions | — | `Session[]`（作成順） | — |
| get session | `session_id` | `Session` | not-found |
| close session | `session_id`, `ended_at` | `Session`（status=closed） | not-found / already-closed |
| assign seat for hand | `session_id`, `hand_id`, `seat_no`, `player_id` | `SeatAssignment` | not-found / session-closed / seat-taken / unknown-player / invalid-seat |
| list seat assignments by hand | `session_id`, `hand_id` | `SeatAssignment[]` | not-found |
| resolve seating for hand_ref | `session_id`, `hand_id` | `HandRef`（snapshot 込み） | not-found |
| current seating | `session_id` | `SeatAssignment[]`（最新 hand から導出） | not-found |

Python 具象（`core/session_repository.py` と一致）:

```text
create_session(label: str | None = None, blinds: dict | None = None) -> Session
list_sessions() -> list[Session]                   # 作成順
get_session(session_id: str) -> Session            # raises SessionNotFoundError
close_session(session_id: str, ended_at: str | None = None) -> Session
                                                   # raises SessionAlreadyClosedError
assign_seat(session_id: str, hand_id: int, seat_no: int, player_id: str) -> SeatAssignment
                                                   # raises SessionNotFound / SessionClosed /
                                                   #   InvalidSeat / UnknownPlayer / SeatTaken /
                                                   #   PlayerAlreadySeated
list_seat_assignments(session_id: str, hand_id: int) -> list[SeatAssignment]   # seat_no 昇順
resolve_seat_map_for_hand(session_id: str, hand_id: int) -> dict[int, str]     # seat_no -> player_id
resolve_hand_ref(session_id: str, hand_id: int) -> HandRef
current_seating(session_id: str) -> list[SeatAssignment]   # 最新 hand から導出
```

- **業務ルールは core が source of truth**。front-end は結果と error code を表示するだけ。
- mobile は同 interface の in-memory mock を `fixtures/{session,seat_assignment,hand_ref}/` で先行実装できる。
- **schema は未 freeze**: core は draft schema（0.x）に対して実装済（code↔contract test 緑）。
  `session_id` 採番方式・永続形は ADR-0007 で core について確定。schema `1.0` への昇格は
  ISSUE-0005 の残項目（hand logger 接続・seat change UI 要件）決着後。

## ledger_entry / point_ledger_entry interface（S3 core 実装済, schema は draft）

詳細・モデル定義は `ledger-points.md` / ADR-0013（ISSUE-0001 決着）。core 実装は
`core/ledger_repository.py`。語彙非依存の契約:

| 操作 | 入力 | 出力 | error |
|------|------|------|-------|
| add ledger entry | `session_id`, `player_id`, `kind`, `cash_amount`, `point_amount`, `note?`, `order?` | `LedgerEntry`（point 充当時は spend entry を core が同時生成） | not-found / session-closed / unknown-player / invalid-kind / invalid-amount / invalid-order-detail / entry-fee-requires-cash / insufficient-points |
| list ledger entries | `session_id?`, `player_id?` | `LedgerEntry[]`（記録順） | — |
| session totals（中間集計・途中値） | `session_id` | player_id → buy-in 合計 / 注文合計 | not-found |
| point balance | `player_id` | int（fold 結果） | unknown-player |
| grant points | `player_id`, `points`, `reason`, `idempotency_key?`, `note?` | `PointLedgerEntry` | unknown-player / invalid-reason / invalid-amount / duplicate-grant |
| adjust points | `player_id`, `delta_points`, `note?` | `PointLedgerEntry` | unknown-player / invalid-amount / insufficient-points |
| list point entries | `player_id?` | `PointLedgerEntry[]`（記録順） | — |
| plan payment（cash 補完） | `player_id`, `total_amount`, `use_points?` | `(cash_amount, point_amount)` | unknown-player / invalid-amount |

Python 具象（`core/ledger_repository.py` と一致）:

```text
add_entry(session_id, player_id, kind, cash_amount=0, point_amount=0,
          note=None, order=None) -> LedgerEntry
list_entries(session_id=None, player_id=None) -> list[LedgerEntry]
session_totals(session_id) -> dict[player_id, {"buy_in_total": int, "order_total": int}]
point_balance(player_id) -> int
grant_points(player_id, points, reason, idempotency_key=None, note=None) -> PointLedgerEntry
adjust_points(player_id, delta_points, note=None) -> PointLedgerEntry
list_point_entries(player_id=None) -> list[PointLedgerEntry]
plan_payment(player_id, total_amount, use_points=True) -> tuple[int, int]
```

- **残高計算と cash 補完の分割は core のみが行う**（front-end は `point_balance` /
  `plan_payment` の結果を表示・転記するだけ）。
- mobile は同 interface の in-memory mock を `fixtures/{ledger_entry,point_ledger_entry}/` で
  先行実装できる。
- **schema は未 freeze**（draft 0.1）。`1.0` 昇格は上流 session schema freeze（ISSUE-0005）後。

## settlement 以降（planned）

| model | interface | phase |
|-------|-----------|-------|
| session_settlement | settlement 確定、paid/unpaid 操作 | S4 |

各 interface は対応 phase の freeze 時に本 doc へ追記する。S5 で local 実装と API client 実装に
分離する（interface は不変のまま backend を差し替える）。
