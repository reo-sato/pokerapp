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

### ledger / points / settlement error code（S3 draft, planned）

ADR-0011 / `ledger-overview.md` で確定した方針に対応する code（**実装は S3.1, ISSUE-0012**）。
Python 例外階層は `LedgerError`（基底）← 各 code に 1:1 対応する subclass を想定。共通
`not_found` / `unknown_player` は再利用。

| code | 意味 | 発生する操作（例） | Python 例外（planned, core） |
|------|------|------------------|------------------------------|
| `not_found` | 指定 `entry_id` / `session_id` が存在しない（既存 code 再利用） | reverse / list / settlement | `LedgerNotFoundError` |
| `unknown_player` | 指定 `player_id` が registry に実在しない（既存 code 再利用） | add entry / grant | `UnknownPlayerError` |
| `invalid_amount` | 金額が不正（非ゼロ移動違反 / 符号制約違反 / order 明細不正） | add entry / grant | `InvalidAmountError` |
| `entry_fee_requires_cash` | `kind=entry_fee` に point を充当しようとした（cash only, rule 1） | add entry | `EntryFeeRequiresCashError` |
| `insufficient_points` | spend が残高を割り込む（不足分は cash 補完, rule 3） | add entry（point 充当） | `InsufficientPointsError` |
| `duplicate_grant` | 同一 `idempotency_key` の grant が既に存在（冪等性, ISSUE-0001 Q4） | grant points | `DuplicateGrantError` |
| `session_not_closed` | open session を settlement 確定しようとした（closed のみ確定） | commit settlement | `SessionNotClosedError` |
| `already_settled` | 確定済 settlement を再確定しようとした | commit settlement | `AlreadySettledError` |

> 上表は ADR-0011 確定の **方針**。実装（例外クラス・厳密文言）は S3.1 で確定し、その時点で本表の
> 「planned」を外す。`payment_status` の `unpaid↔paid` 操作は訂正として許容（error にしない）。
> settlement schema の `1.0` freeze は #5（S4）。
