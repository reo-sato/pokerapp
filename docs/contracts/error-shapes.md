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
  例（planned）: `insufficient_points`（point 不足→cash 補完判断, S3）、
  `entry_fee_requires_cash`（entry fee は cash only, S3）、`already_settled`（S4）。
