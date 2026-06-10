# Ledger / points / settlement — JSON schema draft (S3)

> **Status: draft / 未 freeze**（freeze order #4 ledger / #5 settlement）。本 doc は
> `ledger_entry` / `point_ledger_entry` / `session_settlement` の **擬似 JSON Schema**
> （JSON Schema draft 2020-12 形式）。実 schema ファイル（`schemas/*.schema.json`）+ fixtures +
> `tests/test_contracts.py` 登録は **S3.1（ISSUE-0012）で追加**する。設計は
> `ledger-overview.md` / ADR-0011 を参照。

## versioning 方針

- 各 schema は `"version"`（`MAJOR.MINOR`）を持つ。draft 段階は `0.x`、freeze 時に `1.0` へ昇格
  （`versioning-and-freeze.md` の freeze 定義に従う）。
- `$id` は `https://pokerapp.local/contracts/<model>.schema.json` の論理 URI（解決はしない、識別用）。
- `additionalProperties: false`（厳格）。**absent と null を区別**し、optional は absent を既定とする。
- additive（optional 追加 / enum 追加 / description 追加）は MINOR +1・worklog で可。breaking は
  新 ADR + MAJOR +1（`versioning-and-freeze.md` §2）。
- 永続ストア `ledger.json` 自身にも `schema_version`（例 `"0.1"`）を持たせ、将来の migration を
  可能にする（実装は S3.1）。

## ID 参照（`shared-ids.md` 準拠）

- `entry_id`: 各台帳が UUID4 hex で採番（`^[0-9a-f]{32}$`, player_id と同形式）。
- `player_id`: registry 発行の UUID4 hex（`^[0-9a-f]{32}$`）。
- `session_id`: session レイヤの opaque 非空文字列（UUID4 hex 推奨）。
- `hand_id`: session 内連番 int。cross-app は `(session_id, hand_id)` 複合（ADR-0006）。

---

## `ledger_entry`（v0.1, draft）

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://pokerapp.local/contracts/ledger_entry.schema.json",
  "title": "LedgerEntry",
  "version": "0.1",
  "description": "S3 draft (未 freeze)。金銭/価値イベント 1 件。append-only。cash+point 併用可。符号制約・残高・整合は core が enforce。詳細は docs/contracts/ledger-overview.md / ADR-0011。",
  "type": "object",
  "additionalProperties": false,
  "required": ["entry_id", "session_id", "player_id", "kind", "occurred_at", "cash_amount", "point_amount"],
  "properties": {
    "entry_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{32}$",
      "description": "UUID4 hex (32 文字, lowercase)。ledger レイヤが採番。一意。"
    },
    "session_id": {
      "type": "string",
      "minLength": 1,
      "description": "S2 session の session_id（opaque 非空）。金銭イベントは session スコープ。"
    },
    "player_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{32}$",
      "description": "registry の player_id。実在は core が enforce（unknown_player）。"
    },
    "kind": {
      "type": "string",
      "enum": ["buy_in", "rebuy", "add_on", "order", "entry_fee", "adjustment"],
      "description": "イベント種別。entry_fee は cash only（point_amount=0, core）。adjustment は訂正。"
    },
    "occurred_at": {
      "type": "string",
      "format": "date-time",
      "description": "発生時刻 (ISO 8601)。"
    },
    "cash_amount": {
      "type": "integer",
      "description": "cash 分（整数円, 1=¥1）。通常 kind は >=0、adjustment/reversal は符号付き可（core が per-kind enforce）。"
    },
    "point_amount": {
      "type": "integer",
      "description": "point 充当分（整数点, 通常 >=0）。>0 のとき対応する spend_* point_ledger_entry が 1 件存在（core）。entry_fee では 0。"
    },
    "note": {
      "type": "string",
      "description": "任意の備考（手入力メモ）。"
    },
    "hand_id": {
      "type": "integer",
      "minimum": 0,
      "description": "任意。将来 per-hand rake/fee 用フック（(session_id, hand_id) 複合参照）。S3 では未使用。"
    },
    "reverses_entry_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{32}$",
      "description": "任意。訂正 reversal のとき、相殺対象の entry_id。金額は対象の符号反転（core）。"
    },
    "order": {
      "type": "object",
      "additionalProperties": false,
      "description": "kind=order のときのみ。注文明細。",
      "required": ["item_name", "unit_amount", "quantity"],
      "properties": {
        "item_name": {"type": "string", "minLength": 1, "description": "品目名。"},
        "unit_amount": {"type": "integer", "minimum": 0, "description": "単価（整数円）。"},
        "quantity": {"type": "integer", "minimum": 1, "description": "数量。"}
      }
    }
  }
}
```

---

## `point_ledger_entry`（v0.1, draft）

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://pokerapp.local/contracts/point_ledger_entry.schema.json",
  "title": "PointLedgerEntry",
  "version": "0.1",
  "description": "S3 draft (未 freeze)。ポイント増減 1 件。append-only。player の point 残高はこの台帳の delta_points の fold が source of truth（ISSUE-0001）。詳細は docs/contracts/ledger-overview.md / ADR-0011。",
  "type": "object",
  "additionalProperties": false,
  "required": ["entry_id", "player_id", "delta_points", "reason", "occurred_at"],
  "properties": {
    "entry_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{32}$",
      "description": "UUID4 hex。ledger レイヤが採番。一意。"
    },
    "player_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{32}$",
      "description": "registry の player_id。"
    },
    "delta_points": {
      "type": "integer",
      "description": "符号付き増減（!=0, core）。grant は >0、spend_* は <0。残高は負にならない（core）。"
    },
    "reason": {
      "type": "string",
      "enum": ["manual_grant", "result_credit", "campaign_grant", "spend_on_buyin", "spend_on_rebuy", "spend_on_addon", "spend_on_order", "adjustment"],
      "description": "増減理由。spend_* は related_ledger_entry_id 必須（core）。"
    },
    "occurred_at": {
      "type": "string",
      "format": "date-time",
      "description": "発生時刻 (ISO 8601)。"
    },
    "related_ledger_entry_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{32}$",
      "description": "任意。spend_* のとき、充当先 ledger_entry の entry_id（core が必須化）。"
    },
    "session_id": {
      "type": "string",
      "minLength": 1,
      "description": "任意。session スコープの grant/credit/spend に紐づける。"
    },
    "idempotency_key": {
      "type": "string",
      "minLength": 1,
      "description": "任意。manual_grant / campaign_grant の重複防止キー（ISSUE-0001 Q4）。"
    }
  }
}
```

---

## `session_settlement`（v0.1, draft）

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://pokerapp.local/contracts/session_settlement.schema.json",
  "title": "SessionSettlement",
  "version": "0.1",
  "description": "S3/S4 draft (未 freeze)。session 締めの (session, player) 1 行。entries の fold（materialized view）を close 時に確定。常に player→店の 1 方向。詳細は docs/contracts/ledger-overview.md / ADR-0011。",
  "type": "object",
  "additionalProperties": false,
  "required": ["session_id", "player_id", "cash_in_total", "point_spent_total", "order_total", "entry_fee", "point_credited_total", "net_due_to_store", "payment_status", "settled_at"],
  "properties": {
    "session_id": {"type": "string", "minLength": 1, "description": "対象 session。"},
    "player_id": {"type": "string", "pattern": "^[0-9a-f]{32}$", "description": "対象 player。"},
    "cash_in_total": {"type": "integer", "description": "buy_in+rebuy+add_on の cash 合計（整数円）。"},
    "point_spent_total": {"type": "integer", "minimum": 0, "description": "当該 session の spend ポイント合計（>=0）。"},
    "order_total": {"type": "integer", "description": "order の cash 合計（整数円）。"},
    "entry_fee": {"type": "integer", "description": "entry_fee の cash 合計（整数円, cash only）。"},
    "point_credited_total": {"type": "integer", "minimum": 0, "description": "当該 session で付与された result_credit/campaign の合計（>=0）。"},
    "net_due_to_store": {"type": "integer", "description": "店への net 支払額（符号付き Σ cash_amount, 整数円）。point は加算しない。"},
    "payment_status": {"type": "string", "enum": ["paid", "unpaid"], "description": "支払状態。partial は扱わない。唯一の可変フィールド。"},
    "settled_at": {"type": "string", "format": "date-time", "description": "確定時刻 (ISO 8601)。"}
  }
}
```

---

## 永続ストア（`ledger.json`）形（S3.1 で確定）

別ストア（ADR-0011）。`players.json` / `sessions.json` と同じアトミックリネーム書き込み。

```jsonc
{
  "schema_version": "0.1",
  "ledger_entries": [ /* LedgerEntry[] */ ],
  "point_ledger_entries": [ /* PointLedgerEntry[] */ ],
  "settlements": [ /* SessionSettlement[] */ ]
}
```

## additive な拡張余地（freeze 後も後方互換で足せる範囲）

- `currency`（通貨コード）フィールド: 多通貨対応時に optional 追加（既定 JPY）。
- `ledger_entry.hand_id` を使った per-hand **rake / fee** kind（`rake` / `house_fee` 等の enum 追加）。
- `point_ledger_entry` の grant 由来詳細（campaign id 等）の optional サブ構造。
- settlement の `paid_at` / 支払手段メモ等の optional フィールド（partial paid は breaking なので別 ADR）。
- 上記はいずれも optional 追加 or enum 追加 = additive。`required` 化や enum 削除は breaking。
