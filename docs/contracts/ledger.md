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

## write の所在（S3a + M5）

- **ledger への write はスタッフの desktop 別画面のみ**（`python main.py --ledger`）。
- viewer API の参照は read-only（`GET /api/players/{id}/sessions/{sid}/ledger`, viewer-api.md）。
- player のスマホからの注文は **order_request 経由**（M5, 下記）。ledger への直接 write は無い。

## 注文リクエスト（M5, ADR-0015 — staff-in-the-loop）

- model: `order_request`（`schemas/order_request.schema.json` 0.x draft）。
  `request_id` / `session_id` / `player_id` / `item_name` / `quantity`(1..99) / `note?` /
  `status`（pending → confirmed | rejected のみ。再変更は `already_resolved`）/
  `requested_at` / `resolved_at?` / `ledger_entry_id?`（confirmed のみ）。
- フロー: player が POST（viewer-api.md）→ pending として `order_requests.json` に記録 →
  スタッフが `--ledger` 画面の「注文リクエスト」欄で **確定**（このとき初めて
  `ledger_entry`(kind=order) が作られ `ledger_entry_id` がリンク）or **却下**。
- **menu master**: `menu.json` = `{"items": [{"item_name", "unit_amount"}]}`（コミット済み
  サンプルを店側で編集、rfid_cards.json と同運用）。player はメニューから選択
  （menu 外は `unknown_item`）。確定時の単価は menu から prefill（スタッフ上書き可 =
  価格の最終決定権はスタッフ）。
- **単一プロセス所有**（ADR-0015 §3）: `order_requests.json` への write は viewer API を
  in-process で抱えた `--ledger` プロセスのみ。単独 `--viewer-api` は read-only
  （POST は 503 `orders_unavailable`、GET は reload-on-read で追従）。
- repository interface は `repository-interfaces.md` § order request 参照。

## 拡張ルール

- M6（S3b）: `point_amount > 0` の解放 + `point_ledger_entry` 導入 + `insufficient_points` /
  `entry_fee_requires_cash` の有効化。ここまでの変更が additive に収まることを確認して
  `1.0` freeze する。
