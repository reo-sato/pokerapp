# Ledger / point ledger contract (S3 draft)

> **Status: draft / 未 freeze**（freeze order #4）。本 doc と
> `schemas/{ledger_entry,point_ledger_entry}.schema.json` + `fixtures/` は **S3 の契約草案**。
> core は実装済（`core/ledger.py` / `core/ledger_repository.py`, ADR-0013）で
> code↔contract test 緑だが、schema version は `0.x` のまま。`1.0` への昇格は
> 依存上流の session schema freeze（ISSUE-0005）と S4 settlement 着手時の見直し後。

S3 は session レイヤの上に **session ledger**（金銭イベント）と **point ledger**
（prize point の増減）を重ねる。ISSUE-0001（残高の source of truth）は **ADR-0013 で決着**:
残高 = `point_ledger_entry` の fold、計算者は core のみ。

## モデル要約

| model | 役割 | 一意キー | 永続/参照 |
|-------|------|---------|-----------|
| `ledger_entry` | session 中の金銭イベント 1 件（cash+point 併用可） | `entry_id` | ledger レイヤが採番・永続化（`ledger.json`） |
| `point_ledger_entry` | prize point の増減 1 件。fold が残高の source of truth | `entry_id` | 同上。spend 系は core が ledger_entry から同時生成 |

### 1. `ledger_entry`

- **属性**: `entry_id` / `session_id` / `player_id` / `kind` / `occurred_at` /
  `cash_amount` / `point_amount`（すべて必須）、`note` / `order`（任意）。
- **kind**: `buy_in` | `rebuy` | `add_on` | `order` | `adjustment` | `entry_fee`。
  `entry_fee` は ADR-0013 で additive 追加（業務ルール 1 を ledger 上で enforce するため）。
- **金額制約**（schema 外, core が enforce）:
  - `point_amount >= 0` 常時（schema でも表現）。
  - **entry fee は cash only**（`point_amount > 0` → `entry_fee_requires_cash`）。
  - **point 充当可能な kind は `buy_in` / `rebuy` / `add_on` / `order` のみ**（業務ルール 2）。
    `adjustment` への point は不可（point 補正は `adjust_points` を使う）。
  - `adjustment` のみ `cash_amount` 負値可（返金等）かつ非 0。他 kind は
    `cash_amount >= 0` かつ `cash_amount + point_amount > 0`。
  - `point_amount > 0` は残高検証を通る（不足 → `insufficient_points`、黙って減額しない）。
- **order 明細**: `kind=order` のみ `order: {item_name, unit_amount, quantity}` が **必須**。
  他 kind には付けられない。`unit_amount * quantity == cash_amount + point_amount` を enforce。
- **session 状態**: entry を追加できるのは **open session のみ**（closed → `session_closed`、
  unknown → `not_found`）。`player_id` は registry 実在（`unknown_player`）。

### 2. `point_ledger_entry`

- **属性**: `entry_id` / `player_id` / `delta_points` / `reason` / `occurred_at`（必須）、
  `related_ledger_entry_id` / `idempotency_key` / `note`（任意）。
- **reason**: 増加 = `manual_grant` | `result_credit` | `campaign_grant`、
  減少 = `spend_on_buyin` | `spend_on_rebuy` | `spend_on_addon` | `spend_on_order`、
  補正 = `adjustment`（両方向）。
- **残高 = fold（ADR-0013）**: `point_balance(player_id)` = 当該 player の `delta_points` 総和。
  残高は player に global（session を跨ぐ。繰越 entry なし）。残高が負になる操作は
  `insufficient_points` で拒否（残高は常に 0 以上）。
- **spend 系は core 生成のみ**: point 充当付き `ledger_entry` の追加時に core が
  `delta_points = -point_amount` の spend entry を atomic に併記し、
  `related_ledger_entry_id` で back-link する。front-end が spend を直接書く API はない。
- **冪等性**: grant 系は任意の `idempotency_key` を持てる。同一キーの再 grant は
  `duplicate_grant`。キーなし grant は重複チェック対象外。
- **同一 session 内の grant → spend 同居可**（fold は記録順）。

## repository / service interface（S3, core 実装済）

語彙非依存の契約（`repository-interfaces.md` に同期）。具象は `core/ledger_repository.py`。

| 操作 | 入力 | 出力 | error（`error-shapes.md`） |
|------|------|------|------|
| add ledger entry | `session_id`, `player_id`, `kind`, `cash_amount`, `point_amount`, `note?`, `order?` | `LedgerEntry`（point 充当時は spend entry を同時生成） | `not_found` / `session_closed` / `unknown_player` / `invalid_kind` / `invalid_amount` / `invalid_order_detail` / `entry_fee_requires_cash` / `insufficient_points` |
| list ledger entries | `session_id?`, `player_id?` | `LedgerEntry[]`（記録順） | — |
| session totals（中間集計） | `session_id` | player_id → `{buy_in_total, order_total}`（**途中値・非確定**） | `not_found` |
| point balance | `player_id` | int（fold 結果, 常に >= 0） | `unknown_player` |
| grant points | `player_id`, `points`, `reason`, `idempotency_key?`, `note?` | `PointLedgerEntry` | `unknown_player` / `invalid_reason` / `invalid_amount` / `duplicate_grant` |
| adjust points | `player_id`, `delta_points`, `note?` | `PointLedgerEntry`（reason=adjustment） | `unknown_player` / `invalid_amount` / `insufficient_points` |
| list point entries | `player_id?` | `PointLedgerEntry[]`（記録順） | — |
| plan payment（cash 補完） | `player_id`, `total_amount`, `use_points?` | `(cash_amount, point_amount)` の分割 | `unknown_player` / `invalid_amount` |

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

- **業務ルールは core が source of truth**。front-end は結果と error code を表示するだけ。
  特に **残高計算と cash 補完の分割は front-end で再実装しない**（`plan_payment` を呼ぶ）。
- 中間集計（`session_totals`）は **確定値ではない**（確定は S4 settlement）。UI は
  途中値であることを明示する（speculative/preview ラベル等。表現は WS2/WS3 で確定）。
- mobile は同 interface の in-memory mock を `fixtures/{ledger_entry,point_ledger_entry}/`
  で先行実装できる。

## 永続化（ADR-0013, core について確定）

- 専用ストア `ledger.json`（プロジェクト直下, `.gitignore`, アトミックリネーム書き込み）:
  `{"ledger_entries": [...], "point_ledger_entries": [...]}`。
- `entry_id` は UUID4 hex をアプリ内採番（外部システム前提を作らない）。
- 参照整合: `session_id` は S2 session レイヤ、`player_id` は S1 registry に依存
  （freeze order の依存順 S1→S2→S3 と一致）。

## fixtures（contract test の oracle）

`fixtures/ledger_entry/`, `fixtures/point_ledger_entry/` に
`canonical` / `valid-*` / `invalid-*` を追加済み。`tests/test_contracts.py` の `_MODELS` に
2 model を登録し、schema↔fixture 整合と code↔contract drift
（`test_core_ledger_matches_contract`）を検証する。

## freeze 状態 / 未確定事項

- **本 doc + 2 schema + fixtures は draft（version `0.1`, 未 freeze）**。core は実装済
  （ADR-0013）で code↔contract test 緑。
- **`1.0` 昇格の前提**: 上流の session schema freeze（ISSUE-0005）、S4 settlement 着手時の
  集計 API 見直し（`entry_fee` 合計の確定形・`result_credit` の idempotency_key 規約）。
- **out of scope（S3 core 段階）**: settlement / paid・unpaid（S4）、desktop/mobile UI（WS2/WS3）、
  API / sync（S5）、entry の削除・訂正履歴（adjustment で代替）、partial paid。
