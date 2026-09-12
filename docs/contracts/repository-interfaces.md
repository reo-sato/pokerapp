# Repository / service interface contract（frozen, S5 / ADR-0020）

> **Status: frozen**（freeze order #6, ADR-0020, 2026-06-13。staff write メソッドは additive 追加,
> ADR-0021）。下の player / session-seating / ledger-points-settlement / viewer read model /
> order-request の interface を S5 の安定契約とする（対応 schema は ADR-0019 で `1.0`）。
> 追加メソッドは additive、シグネチャの削除・変更は ADR を要する。

front-end（desktop WS2 / mobile WS3 / Python client）は **repository interface にのみ依存** し、その
具象（core の local 実装 / in-memory mock / API client）を注入で差し替える。UI は「データがどこに
あるか」を知らない。この境界は **二言語で実証済み**: mobile は `ViewerRepository`（TS）に mock/HTTP を
注入（M2）、Python は `api/client.py:ViewerApiClient` が viewer API を読む（S5, ADR-0020）。

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
reload() -> None                                      # ディスクから再読込（read-only viewer 用, WS2-α 追加）
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
| list hand ids | `session_id` | `int[]`（記録済み hand_id を昇順） | not-found |
| list seat assignments by hand | `session_id`, `hand_id` | `SeatAssignment[]` | not-found |
| resolve seating for hand_ref | `session_id`, `hand_id` | `HandRef`（snapshot 込み） | not-found |
| current seating | `session_id` | `SeatAssignment[]`（最新 hand から導出） | not-found |
| reload | — | —（ディスクから再読込, read-only viewer 用 additive） | — |

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
list_hand_ids(session_id: str) -> list[int]                # 記録済み hand_id を昇順（WS2-α 追加）
list_seat_assignments(session_id: str, hand_id: int) -> list[SeatAssignment]   # seat_no 昇順
resolve_seat_map_for_hand(session_id: str, hand_id: int) -> dict[int, str]     # seat_no -> player_id
resolve_hand_ref(session_id: str, hand_id: int) -> HandRef
current_seating(session_id: str) -> list[SeatAssignment]   # 最新 hand から導出
reload() -> None                                           # ディスクから再読込（read-only viewer 用, WS2-α 追加）
```

- `list_hand_ids` / `reload` は WS2-α（read-only Session/Seating Viewer）で additive 追加した
  **read 専用 API**。enumeration / loading を core に置き、front-end に複製しないための補助。
  `PlayerRepository` 側にも対称な `reload()` を additive 追加（name 解決の外部更新取り込み用）。
- **業務ルールは core が source of truth**。front-end は結果と error code を表示するだけ。
- mobile は同 interface の in-memory mock を `fixtures/{session,seat_assignment,hand_ref}/` で先行実装できる。
- **schema は `1.0` frozen**（ADR-0019, ISSUE-0005 Resolved）。`session_id` 採番方式・永続形は ADR-0007。

## ledger / points / settlement interface（S3 core 実装済, schema は draft — ADR-0016）

詳細・モデル定義は `ledger-overview.md` / `ledger-schema.md` / ADR-0016（ISSUE-0001 決着 = fold）。
core 実装は `core/ledger_repository.py`。語彙非依存の契約:

| 操作 | 入力 | 出力 | error |
|------|------|------|-------|
| add ledger entry | `session_id`, `player_id`, `kind`, `cash_amount`, `point_amount`, `note?`, `hand_id?`, `order?` | `LedgerEntry`（point 充当時は spend entry を core が同時生成） | not-found / unknown-player / invalid-amount / entry-fee-requires-cash / insufficient-points |
| reverse entry | `entry_id` | `LedgerEntry`（reversal, append-only 訂正） | not-found / invalid-amount |
| list ledger entries | `session_id?`, `player_id?` | `LedgerEntry[]`（記録順） | — |
| grant points | `player_id`, `delta_points`, `reason`, `session_id?`, `idempotency_key?` | `PointLedgerEntry` | unknown-player / invalid-amount / duplicate-grant |
| point balance | `player_id` | int（fold 結果） | unknown-player |
| list point entries | `player_id?`, `session_id?` | `PointLedgerEntry[]`（記録順） | — |
| compute settlement（中間集計・speculative） | `session_id` | `SessionSettlement[]` | not-found |
| commit settlement | `session_id` | `SessionSettlement[]`（確定） | not-found / session-not-closed / already-settled |
| set payment status | `session_id`, `player_id`, `paid|unpaid` | `SessionSettlement` | not-found |

Python 具象（`core/ledger_repository.py` と一致）:

```text
add_entry(session_id, player_id, kind, cash_amount=0, point_amount=0,
          note=None, hand_id=None, order=None, occurred_at=None) -> LedgerEntry
reverse_entry(entry_id, occurred_at=None) -> LedgerEntry
list_entries(session_id=None, player_id=None) -> list[LedgerEntry]
grant_points(player_id, delta_points, reason="manual_grant", session_id=None,
             idempotency_key=None, occurred_at=None) -> PointLedgerEntry
point_balance(player_id) -> int
list_point_entries(player_id=None, session_id=None) -> list[PointLedgerEntry]
compute_settlement(session_id) -> list[SessionSettlement]
commit_settlement(session_id) -> list[SessionSettlement]
list_settlements(session_id) -> list[SessionSettlement]
all_settlements() -> list[SessionSettlement]
set_payment_status(session_id, player_id, status) -> SessionSettlement
```

- **残高計算と cash 補完の分割は core のみが行う**（front-end は `point_balance` /
  `plan_payment` の結果を表示・転記するだけ）。
- mobile は同 interface の in-memory mock を `fixtures/{ledger_entry,point_ledger_entry}/` で
  先行実装できる。
- **schema は `1.0` frozen**（ADR-0019）。

## viewer read model interface（M1 実装済 — ADR-0017）

`api/read_models.py`（fastapi 非依存の純関数）。front-end（mobile）は同じ意味論の
`ViewerRepository` interface（`mobile/src/api/repository.ts`）越しに mock / HTTP を差し替える:

| 関数 | 返すもの | 備考 |
|------|----------|------|
| `list_player_sessions(player_id, session_repo)` | `[player_session_summary, ...]` | 着席 hand>0 の session のみ |
| `list_player_hands(player_id, session_id, session_repo, log_dir)` | `[hand, ...]` | seat_assignment 起点で hand log を join |
| `get_player_session_ledger(player_id, session_id, ledger_repo)` | `{entries, summary}` | summary の totals は `compute_settlement` 由来（cash_in_total / order_total / entry_fee / point_spent_total / point_credited_total / net_due_to_store）+ 確定状態 `settled`/`payment_status`/`settled_at`（`list_settlements` 由来, S4 mobile） |
| `get_hand(session_id, hand_id, log_dir)` | `hand` | legacy session_id でも log があれば返す |

## order-request interface（M5 実装済 — ADR-0018）

`core/order_request_repository.py`（thread-safe + reload-on-read）が source of truth:

| 操作 | シグネチャ（要約） | 不変条件 |
|------|---------------------|----------|
| create | `create_request(session_id, player_id, item_name, quantity, note?) -> OrderRequest` | open session / 実在 player / quantity 1..99。ledger には書かない（pending） |
| confirm | `confirm_request(request_id, unit_amount, ledger_repo) -> OrderRequest` | pending のみ。`ledger_repo.add_entry(kind="order", cash_amount=unit×qty, order={...})` を起こしリンク。closed session は不可 |
| reject | `reject_request(request_id) -> OrderRequest` | pending のみ。ledger には何も書かない |
| list | `list_requests(session_id, player_id?, status?) -> [OrderRequest]` | requested_at 順 |

menu 照合（`unknown_item`）と read-only モード（`orders_unavailable`）は viewer API 境界が扱う。

## viewer API client（S5 実装済 — ADR-0020）

`api/client.py:ViewerApiClient`（mobile `HttpRepository` の Python 版）。viewer API の read
endpoints（+ 注文 GET/POST）を HTTP で呼び、非 2xx を error-shape の `code` を載せた
`ViewerApiError` に変換する。メソッドは viewer read model / order-request interface と対称
（`list_players` / `get_player` / `list_player_sessions` / `list_player_hands` / `get_hand` /
`get_player_session_ledger` / `get_menu` / `list_order_requests` / `create_order_request`）。

- **同期方式 = on-demand pull**（v1）。push / event / 双方向 auto-sync は持たない（ADR-0020）。
- **衝突回避 = 単一書き手 + reload-on-read**（注文 = staff-confirm の所有プロセスのみ write）。
- **ID 不変性**: player_id/session_id は app 内採番・不変・backend 非依存（local も API client も同じ ID）。
- round-trip 契約 test: `tests/test_viewer_api_client.py`（API↔client の drift 検知）。

### staff write メソッド（S5 — ADR-0021）

`ViewerApiClient(base_url, client=None, staff_token=None)`。`staff_token` を渡すと staff メソッドが
`Authorization: Bearer <token>` を付与する（player read / 注文 POST は無認証のまま）。staff メソッドは
ledger / settlement / order-request interface（local 実装）と対称:

- `compute_settlement(session_id)` / `list_session_order_requests(session_id, status=None)`（staff read）。
- `add_ledger_entry(session_id, player_id, kind, cash_amount=0, point_amount=0, note=None, hand_id=None, order=None)`。
- `commit_settlement(session_id)` / `set_payment_status(session_id, player_id, status)`。
- `confirm_order(request_id, unit_amount)` / `reject_order(request_id)`。

write は **write 所有プロセス（`--ledger`）のみ**（単独 `--viewer-api` では 503 `orders_unavailable`）。
認可 error は `unauthorized`(401) / `staff_writes_disabled`(403)。round-trip 契約 test:
`tests/test_viewer_api_staff.py`。

### sync メソッド（S5 — ADR-0022）

state-based merge による双方向 sync（`core/sync.py` 純粋関数 + `/api/staff/sync/...`）。staff client に additive:

- `pull_sync_snapshot()` → peer ノードの全レコード snapshot（GET `/api/staff/sync/snapshot`）。
- `push_sync_merge(snapshot)` → snapshot を merge させ summary を受け取る（POST `/api/staff/sync/merge`）。
- `sync_bidirectional(peer)` → pull+merge を双方向に行い 2 ノードを収束させる。

リポジトリ側の additive 拡張（merge を支える）:

- 全 repository（Player / Session / Ledger / OrderRequest）に **read-only `path` property**
  （自分の永続ファイルパス）を追加。`create_app` が snapshot/merge 対象を特定するのに使う。
- `LedgerRepository` / `OrderRequestRepository` に **public `reload()`**（in-memory を捨てて
  ディスクから読み直す）を追加（`Player`/`Session` は既存）。file-level merge 後に live プロセスが
  in-memory を最新化するために呼ぶ。業務ロジックは不変。round-trip test: `tests/test_viewer_api_sync.py`。

## 残（planned）

| 対象 | 内容 | phase |
|------|------|-------|
| 双方向 sync / 衝突解決 | 複数書き手・push/event・auto-sync（単一書き手では不要） | S5 後続（ADR 要） |
| player per-player アクセス制御 | read の PIN 等（staff write は ADR-0021 の token で解決済み） | ISSUE-0019（顕在化時） |
| desktop の API client 化 | desktop GUI を API-backed repository に差し替え | 任意（現状は local 直結で十分） |
