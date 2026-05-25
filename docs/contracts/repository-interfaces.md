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

## session 以降（planned）

| model | interface | phase |
|-------|-----------|-------|
| session / seat_assignment / hand_ref | session 開始・終了、hand 単位 seat snapshot 取得 | S2 |
| ledger_entry / point_ledger_entry | entry 追加、中間集計（buy-in 合計 / 注文合計）、残高取得 | S3（ISSUE-0003 が gate） |
| session_settlement | settlement 確定、paid/unpaid 操作 | S4 |

各 interface は対応 phase の freeze 時に本 doc へ追記する。S5 で local 実装と API client 実装に
分離する（interface は不変のまま backend を差し替える）。
