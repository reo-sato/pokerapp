# Ledger contract（S3a draft 0.x — cash-only, 未 freeze）

session 中の金銭イベント 1 件 = `ledger_entry`（ADR-0003 ドメイン / ADR-0014 で S3a に分割導入）。
**S3a（M4）は cash-only**: `point_amount` はフィールドとして存在するが、業務 validation が
0 以外を reject する（`points_not_supported`）。point 連携・残高は M6（ISSUE-0001 決着後）。

## model: `ledger_entry`

| field | 型 | 必須 | 説明 |
|---|---|---|---|
| `entry_id` | string (UUID4 hex 32) | ✓ | アプリ内採番・不変（shared-ids.md の規則に従う） |
| `session_id` | string (非空) | ✓ | 対象 session（実在かつ open。closed は `session_closed`） |
| `player_id` | string (UUID4 hex 32) | ✓ | registry に実在（`unknown_player`） |
| `kind` | enum: `buy_in` / `rebuy` / `add_on` / `order` / `adjustment` | ✓ | 種別 |
| `occurred_at` | string (ISO 8601) | ✓ | 発生時刻 |
| `cash_amount` | integer | ✓ | 現金額。kind 別規則は validation-rules.md（adjustment のみ負可） |
| `point_amount` | integer (≥0) | ✓ | **S3a では常に 0**（0 以外は `points_not_supported`） |
| `note` | string | — | 任意メモ |
| `order` | object | kind=order のみ ✓ | `item_name`(非空) / `unit_amount`(≥0) / `quantity`(≥1) |

- schema: `schemas/ledger_entry.schema.json`（**0.1 draft**。`1.0` freeze は M6 で point 意味論
  決着後 — ADR-0014 §3）。fixtures: `fixtures/ledger_entry/`。
- 永続形: プロジェクト直下 `ledger.json` = `{"entries": [ledger_entry, ...]}`（追記順、
  アトミックリネーム、`.gitignore`）。

## 中間集計（業務ルール 7: 確定値ではない）

`session_player_summary(session_id, player_id)` が返す read model（schema 化はせず本 doc が契約。
viewer API の response にも同形で載る）:

```json
{
  "buy_in_total": 30000,      // buy_in + rebuy + add_on の cash 合計
  "order_total": 1500,        // order の cash 合計
  "adjustment_total": -500,   // adjustment の cash 合計（負あり）
  "total_due": 31000          // 3 つの和（店への支払い見込み。確定は S4 settlement）
}
```

## repository interface（語彙非依存。詳細: repository-interfaces.md）

| 操作 | 入力 | 出力 | error |
|---|---|---|---|
| add entry | `session_id`, `player_id`, `kind`, `cash_amount`, `point_amount?`, `note?`, order 明細? | 作成された `ledger_entry`（`entry_id` 採番済） | not-found / session-closed / unknown-player / invalid-kind / invalid-amount / points-not-supported |
| list entries | `session_id`, `player_id?` | `ledger_entry[]`（追記順） | not-found |
| session player summary | `session_id`, `player_id` | 中間集計（上記） | not-found / unknown-player |

## write の所在（S3a）

- **スタッフの desktop 別画面のみ**（`python main.py --ledger`）。player のスマホからの
  注文 write は M5（order-request フロー、ISSUE-0013 決着後）。
- viewer API は read-only の参照のみ（`GET /api/players/{id}/sessions/{sid}/ledger`,
  viewer-api.md）。

## 拡張ルール

- M6（S3b）: `point_amount > 0` の解放 + `point_ledger_entry` 導入 + `insufficient_points` /
  `entry_fee_requires_cash` の有効化。ここまでの変更が additive に収まることを確認して
  `1.0` freeze する。
