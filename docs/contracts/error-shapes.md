# Error shapes contract

front-end が UI 分岐できるよう、core / repository が返す error を **種別（code）で識別** できる
形に固定する。文言（message）は表示用であり、分岐に使わない。

## 原則

- **分岐は `code`、表示は `message`**。front-end は `code` で UI を出し分ける。`message` は
  ローカライズ・改稿されうるので分岐キーにしない。
- **core が source of truth**。error の発生条件（validation 等）は core 側のルール
  （`validation-rules.md`）に従う。front-end は error を作らず受け取るだけ。
- 言語非依存の契約。Python は例外クラス、mobile は reject/Result で表現してよいが `code` は共通。

## 共通 error code（bootstrap）

| code | 意味 | 発生する操作（例） | Python 例外（現状 core） |
|------|------|------------------|------------------------|
| `not_found` | 指定 ID のエンティティが存在しない | get / rename | `PlayerNotFoundError` |
| `empty_display_name` | display_name が空 / 前後空白のみ | create / rename | `EmptyDisplayNameError` |
| `duplicate_display_name` | display_name が既存と完全一致（前後空白除去後） | create / rename | `DuplicateDisplayNameError` |
| `validation_error` | 上記に当てはまらない汎用 validation 失敗（基底） | 各種 | `PlayerValidationError` |

> 現状 `core/player_repository.py` の例外階層:
> `PlayerValidationError`（基底）← `EmptyDisplayNameError` / `DuplicateDisplayNameError` /
> `PlayerNotFoundError`。本 doc の `code` はこの階層と 1:1 対応する。

## error の論理形（cross-app / API 化を見据えた表現）

将来 API / mobile で受け渡す論理形（今回は schema 化までは不要、契約の方向性のみ示す）:

```text
{
  "code": "duplicate_display_name",   // 機械可読・分岐キー（安定）
  "message": "「Alice」は既に存在します。",  // 表示用（可変・ローカライズ対象）
  "field": "display_name"             // 任意: 該当フィールド（フォーム表示用）
}
```

## 拡張ルール

- 新しい error は **code を追加**（additive）。既存 code の意味変更・削除は breaking
  （`versioning-and-freeze.md` §2）。
- 各 model の固有 error（session / ledger / settlement）は対応 phase の freeze 時に本表へ追記する。

### session / seating error code（S2 core 実装済）

`core/session_repository.py` が返す code（additive）。Python 例外階層は
`SessionError`（基底）← 各 code に 1:1 対応する subclass:

| code | 意味 | 発生する操作（例） | Python 例外（core） |
|------|------|------------------|---------------------|
| `not_found` | 指定 `session_id` / hand が存在しない（既存 code を再利用） | get / assign / resolve | `SessionNotFoundError` |
| `already_closed` | closed の session を再度 close / 変更しようとした | close session | `SessionAlreadyClosedError` |
| `session_closed` | closed の session に seat 割り当てしようとした | assign seat | `SessionClosedError` |
| `seat_taken` | 同一 hand 内で席が既に埋まっている | assign seat | `SeatTakenError` |
| `player_already_seated` | 同一 hand 内で player が既に別の席に着いている | assign seat | `PlayerAlreadySeatedError` |
| `unknown_player` | 指定 `player_id` が registry に実在しない | assign seat | `UnknownPlayerError` |
| `invalid_seat` | `seat_no` が範囲外（1..9 外）/ `hand_id` が不正 | assign seat | `InvalidSeatError` |

### ledger error code（S3a cash-only 実装済, ADR-0014）

`core/ledger_repository.py` が返す code（additive）。Python 例外階層は
`LedgerError`（基底）← 各 code に 1:1 対応する subclass:

| code | 意味 | 発生する操作（例） | Python 例外（core） |
|------|------|------------------|---------------------|
| `not_found` | 指定 `session_id` が存在しない（既存 code を再利用） | add / list / summary | `SessionNotFoundError`（session 側を透過） |
| `session_closed` | closed の session に entry を追加しようとした（既存 code を再利用） | add entry | `LedgerSessionClosedError` |
| `unknown_player` | `player_id` が registry に実在しない（既存 code を再利用） | add / summary | `LedgerUnknownPlayerError` |
| `invalid_kind` | kind が 5 種別以外 | add entry | `InvalidKindError` |
| `invalid_amount` | kind 別の金額規則違反（buy_in/rebuy/add_on ≤0、order の単価×数量不一致 等） | add entry | `InvalidAmountError` |
| `points_not_supported` | S3a で `point_amount != 0`（point は M6 で解放） | add entry | `PointsNotSupportedError` |

### order_request error code（M5 実装済, ADR-0015）

`core/order_request_repository.py` / viewer API 境界が返す code（additive）:

| code | 意味 | 発生する操作（例） | 発生層 |
|------|------|------------------|--------|
| `not_found` | 指定 `request_id` / session / player が存在しない（既存 code を再利用） | get / confirm / reject | core |
| `session_closed` | closed の session に注文しようとした（既存 code を再利用） | create request | core（`OrderSessionClosedError`） |
| `unknown_player` | `player_id` が registry に実在しない（既存 code を再利用） | create request | core（`OrderUnknownPlayerError`） |
| `invalid_quantity` | quantity が 1..99 外 / item_name が空・過長 | create request | core（`InvalidOrderRequestError`） |
| `unknown_item` | item_name が menu master に存在しない | create request (POST) | **API 境界**（menu は config 由来のため） |
| `already_resolved` | confirmed / rejected 済みの request を再度 confirm / reject | confirm / reject | core（`AlreadyResolvedError`） |
| `orders_unavailable` | 注文 write が無効（単独 `--viewer-api` の read-only モード等） | POST (HTTP 503) | API 境界 |

### ledger / settlement（planned, S3b〜S4）

例: `insufficient_points`（point 不足→cash 補完判断, S3b/M6）、
`entry_fee_requires_cash`（entry fee は cash only, S3b/M6）、`already_settled`（S4）。
