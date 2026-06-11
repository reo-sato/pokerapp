# Ledger / points / settlement contract (S3 draft)

> **Status: draft / 未 freeze**（freeze order #4 ledger / #5 settlement）。本 doc と
> `ledger-schema.md` の擬似 schema は **S3 の契約草案**。実装済を意味しない。freeze は S3 着手時に
> ADR-0011 Accepted + 実 schema/fixtures + contract test 緑 + ISSUE-0001 残項目決着をもって行う
> （`versioning-and-freeze.md` の freeze 定義）。草案段階の schema version は `0.x`。
> 設計判断は **ADR-0011**、残高 source of truth は **ISSUE-0001** を参照。

S3 は session world（S2）の上に **ledger（金銭イベント）/ points（店内ポイント残高）/
settlement（session 締めの精算）** を重ねる。狙いは「home game host が混乱しないレベルの帳簿」で
あり、**複式簿記（double-entry accounting）ではない**。core / desktop が同じ契約を参照できるよう、
ここで境界を定義する。重い識別子・抽象度の判断は ADR-0011 で確定済み。

## 役割と非目標

- **役割**: (a) 各 player が **店（store）に対して** いくら cash を支払うべきか、(b) 各 player の
  **店内ポイント残高**、(c) session 終了時の player ごとの **settlement**（net due to store /
  paid・unpaid）を記録する。
- **非目標（out of scope）**: 複式簿記・厳密会計・税務、決済手段連携（銀行 / QR / blockchain）、
  player 間精算（player-to-player）、online gambling 規制。これらは外部ツール / 法務領域。
- **PHH / hand logger JSON は read-only**。ledger は一切書き込まない（ADR-0008 の immutability を継承）。
- **chips ≠ yen**。hand logger の chip 単位と ledger の cash（円）単位は別物。S3 は両者を
  **自動換算しない**（rate / rake は future scope）。

## モデル要約

| model | 役割 | 一意キー | 永続/参照 |
|-------|------|---------|-----------|
| `ledger_entry` | 金銭/価値イベント 1 件（buy_in/rebuy/add_on/order/entry_fee/adjustment） | `entry_id` | ledger レイヤが採番・永続化（`ledger.json`） |
| `point_ledger_entry` | ポイント増減 1 件（grant / spend）。**ポイント残高の権威台帳** | `entry_id` | ledger レイヤが採番・永続化 |
| `session_settlement` | `(session, player)` 1 行の精算結果。entries の **fold（materialized view）** | `(session_id, player_id)` | session close 時に確定・永続化 |

`ledger_entry` と `point_ledger_entry` は **append-only**（不変）。訂正は新規の reversal /
adjustment entry で表す（後述 § invariants）。`player_id` / `session_id` / `hand_id` は
`shared-ids.md` の共有 ID をそのまま参照する。

### 1. `ledger_entry`（金銭イベント台帳）

- **属性**: `entry_id`（必須, UUID4 hex）/ `session_id`（必須, S2 session）/ `player_id`（必須, registry）/
  `kind`（必須, enum）/ `occurred_at`（必須, ISO 8601）/ `cash_amount`（必須, int 円）/
  `point_amount`（必須, int ポイント）/ `note`（任意）/ `hand_id`（任意, future rake/fee フック）/
  `reverses_entry_id`（任意, 訂正リンク）/ `order`（任意, `kind=order` のみ: `{item_name, unit_amount, quantity}`）。
- **kind**: `buy_in` / `rebuy` / `add_on` / `order` / `entry_fee` / `adjustment`。
  - CLAUDE.md の future-scope enum に `entry_fee` を **additive 追加**（business rule 1「entry fee は
    cash only」を 1 台帳で扱うため）。`adjustment` は訂正・手動補正用。
- **cash + point 併用**: 1 件の buy_in/rebuy/add_on/order は `cash_amount` と `point_amount` の
  両方を持ち得る（business rule 2）。point で払った分は `point_amount` に、cash 分は `cash_amount` に入る。
- **cross-field**（schema 外, core が enforce）:
  - `kind=entry_fee` ⇒ `point_amount == 0`（cash only, rule 1）。
  - `buy_in/rebuy/add_on/order/entry_fee` ⇒ `cash_amount ≥ 0` かつ `point_amount ≥ 0` かつ
    `cash_amount + point_amount > 0`（価値が動く）。
  - `adjustment` / reversal ⇒ 符号付き可。reversal は `reverses_entry_id` 必須で、対象 entry の
    金額の符号反転に一致する。
  - `point_amount > 0` の entry には、対応する `spend_on_*` の `point_ledger_entry` が
    **ちょうど 1 件** 存在する（§ point_ledger_entry / invariants）。

### 2. `point_ledger_entry`（ポイント残高の権威台帳）

- **属性**: `entry_id`（必須, UUID4 hex）/ `player_id`（必須）/ `delta_points`（必須, 符号付き int）/
  `reason`（必須, enum）/ `occurred_at`（必須, ISO 8601）/
  `related_ledger_entry_id`（任意, spend を ledger_entry にリンク）/ `session_id`（任意）/
  `idempotency_key`（任意, grant 重複防止 — ISSUE-0001 Q4）。
- **reason**:
  - 増加（`delta_points > 0`）: `manual_grant` / `result_credit` / `campaign_grant`（business rule 6）。
  - 減少（`delta_points < 0`）: `spend_on_buyin` / `spend_on_rebuy` / `spend_on_addon` / `spend_on_order`。
  - `adjustment`: 符号付き訂正。
- **ポイント残高 = この台帳の fold**: ある player の残高 = その player の `delta_points` 総和。
  cached カラムを権威にしない（**ISSUE-0001 → 選択肢 A 採用**, ADR-0011）。
- **cross-field**（schema 外, core が enforce）:
  - `delta_points != 0`。
  - `spend_on_*` ⇒ `delta_points < 0` かつ `related_ledger_entry_id` 必須。
  - 残高は **負にならない**: 残高を割り込む spend は拒否（`insufficient_points`）。不足分は cash で
    補完（rule 3、cash_amount に計上）。
  - `manual_grant` / `campaign_grant` の重複は `idempotency_key` で防ぐ（指定時）。

### 3. `session_settlement`（session 締めの精算結果）

- **属性**: `session_id` / `player_id` / `cash_in_total` / `point_spent_total` / `order_total` /
  `entry_fee` / `point_credited_total` / `net_due_to_store` / `payment_status`（`paid`|`unpaid`）/
  `settled_at`（すべて CLAUDE.md § Domain model 準拠）。金額は円（int）、point は点（int）。
- **derived materialized view**: entries の fold。session close 時に `(session, player)` ごと
  1 行を確定・凍結する。中間集計（open 中）は同じ計算式の **途中値（speculative）** であり、
  確定値ではない（UI で区別表示。ISSUE-0001 Q2）。
- **計算式**:
  - `cash_in_total` = Σ `cash_amount`（`kind ∈ {buy_in, rebuy, add_on}`）。
  - `order_total` = Σ `cash_amount`（`kind = order`）。
  - `entry_fee` = Σ `cash_amount`（`kind = entry_fee`）。
  - `point_spent_total` = Σ |`delta_points`|（spend_* / 当該 session）。
  - `point_credited_total` = Σ `delta_points`（`result_credit` / `campaign_grant` の正値 / 当該 session）。
  - **`net_due_to_store` = 当該 session・player の `cash_amount` の符号付き総和**（adjustment / reversal 含む）。
    point は cash 義務を増やさない（point で払った分は cash_amount を下げているため二重計上しない）。
- **方向**: 常に **player → 店** の 1 方向。player 間精算は扱わない（rule 5）。
- **状態**: `payment_status` は唯一の可変フィールド。`unpaid → paid`（訂正のため逆も可）。
  partial paid は扱わない（rule 4）。closed session のみ確定対象（open は中間集計まで）。

## 単位とスコープ

- **cash は整数円**（1 = ¥1, 小数なし）。単一通貨 JPY を暗黙前提（`currency` フィールドは将来 additive）。
- **point は整数点**（小数なし）。cash とは別単位で、settlement で net cash には合算しない。
- **chips（hand logger）とは無関係**。S3 は chip↔円の換算をしない。`ledger_entry.hand_id` は
  将来 per-hand rake/fee を載せるための optional フックに留める（S3 では未使用）。

## 残高の source of truth（ISSUE-0001 の決着）

- **point 残高 = `point_ledger_entry.delta_points` の fold**（選択肢 A）。cached balance は
  権威にしない。性能最適化として cache を **後で** 足してよいが、その場合も fold が正で cache は
  導出（再計算ジョブで整合を担保）。
- `ledger_entry.point_amount` は **point_ledger_entry のミラー**（host が 1 イベントを 1 行で
  読めるようにする便宜）。権威は常に point_ledger_entry 側（§ invariants の整合条件）。
- 中間集計（open session）で提示する残高 / 収支は **speculative**（途中値）であり、UI で「確定でない」
  ことを明示する。確定は session close 時の settlement 生成をもって行う。

## 主要 invariants（壊さない不変条件 — core が source of truth）

1. **append-only**: `ledger_entry` / `point_ledger_entry` は mutate / delete しない。訂正は新規の
   reversal（`reverses_entry_id`）/ adjustment / `spend`-相殺 entry で表す。
2. **point 残高 = fold**: player の残高は `point_ledger_entry.delta_points` の総和に一致する。
   cached カラムを権威にしない（ISSUE-0001）。
3. **残高非負 + cash 補完**: 残高を割り込む spend は拒否（`insufficient_points`）。不足分は cash 計上（rule 3）。
4. **ledger↔point 整合**: `point_amount > 0` の `ledger_entry` には、`delta_points = −point_amount`・
   `related_ledger_entry_id` 設定済の `spend_*` point entry が **ちょうど 1 件** 対応する。
5. **参照整合**: `player_id` は registry に実在。`session_id` / `hand_id`（指定時）は session レイヤに実在。
   `entry_id` は一意。
6. **entry fee は cash only**: `kind=entry_fee` ⇒ `point_amount == 0`（rule 1）。
7. **非ゼロ移動**: 通常 entry は `cash_amount,point_amount ≥ 0` かつ `cash_amount+point_amount > 0`。
   `point_ledger_entry.delta_points ≠ 0`。
8. **settlement は derived & player→店**: `net_due_to_store = 符号付き Σ cash_amount`。closed session のみ
   確定。`payment_status ∈ {paid, unpaid}`（partial なし）。player 間精算は持たない（rule 5）。
9. **PHH/hand-log 不変 + 単位分離**: ledger は `(session_id, hand_id)` を read のみ。chips ≠ 円、S3 で換算しない。

## repository / service interface（S3.1, core 実装済）

語彙非依存の契約。**core 実装済**（`core/ledger_repository.py`, S3.1）。下表のシグネチャに準拠する。

| 操作 | 入力 | 出力 | error（`error-shapes.md`） |
|------|------|------|------|
| add ledger entry | `session_id`, `player_id`, `kind`, `cash_amount`, `point_amount`, `note?`, `hand_id?`, `order?` | `LedgerEntry`（`entry_id` 採番済） | `not_found` / `unknown_player` / `invalid_amount` / `entry_fee_requires_cash` / `insufficient_points` |
| reverse entry | `entry_id` | `LedgerEntry`（reversal, `reverses_entry_id` 設定） | `not_found` |
| list entries | `session_id?`, `player_id?` | `LedgerEntry[]` | `not_found` |
| grant points | `player_id`, `delta_points`, `reason`, `idempotency_key?` | `PointLedgerEntry` | `unknown_player` / `invalid_amount` / `duplicate_grant` |
| point balance | `player_id` | `int`（fold） | `unknown_player` |
| compute settlement | `session_id` | `SessionSettlement[]`（player ごと, 中間集計は speculative） | `not_found` |
| commit settlement | `session_id` | `SessionSettlement[]`（確定・凍結） | `not_found` / `session_not_closed` / `already_settled` |
| set payment status | `session_id`, `player_id`, `paid|unpaid` | `SessionSettlement` | `not_found` |

- **業務ルールは core が source of truth**。front-end は結果と error code を表示するだけ
  （`validation-rules.md` / `error-shapes.md`）。
- desktop は同 interface に対して `fixtures/{ledger_entry,point_ledger_entry,session_settlement}/`（S3.1 で
  整備済）で先行実装できる（S3.2, ISSUE-0013）。

## error code（`error-shapes.md` の ledger セクションに追記済）

`insufficient_points` / `entry_fee_requires_cash` / `invalid_amount` / `duplicate_grant` /
`session_not_closed` / `already_settled`。共通 `not_found` / `unknown_player` を再利用。詳細は
`error-shapes.md`。

## migration / legacy 方針

- hand logs（PHH / hand logger JSON）は **金銭・point を持たず**、player は **名前ベース**
  （chips ≠ cash）。したがって ledger は過去ログから **自動再構築できない**。
- 安全な自動化は **roster の read-only 先読み** のみ: S2 `sessions.json` の seating から session の
  参加 player を一覧化し、host が player ごとに金額を入力する（「人間が安心して補完できる」レベル、
  捏造金額なし）。
- S2 リンクの無い legacy session（ISSUE-0007）は、registry から player を手動選択して standalone な
  ledger を作る。
- 結論: **金銭の自動再構築はしない / 手動入力 / seating からの roster 先読みのみ**。詳細・残課題は
  ISSUE-0007 と本 phase の ISSUE-0012〜0014。

## freeze 状態 / 未確定事項 / blockers

- **本 doc + `ledger-schema.md` + ADR-0011 + 実 schema/fixtures + core repository は実装済（S3.1, ISSUE-0012）**
  だが schema version は `0.x`（**未 freeze**）。追加済: `schemas/{ledger_entry,point_ledger_entry,session_settlement}.schema.json`
  + `fixtures/`（canonical / valid-* / invalid-*）+ `tests/test_contracts.py` 登録 + `core/ledger.py` /
  `core/ledger_repository.py` + `tests/test_ledger_repository.py`（code↔contract test 緑）。
- **決定済（ADR-0011）**: 2 台帳モデル（ledger_entry + point_ledger_entry）/ 整数円・整数点 /
  別ストア `ledger.json` / 残高 = fold / reversal による訂正 / manual-first。
- **freeze の残 blocker**: ISSUE-0001 の残サブ問題（idempotency_key 運用、speculative の UI 表現）、
  および ledger は session schema（freeze order #3）の上に乗るため、session freeze 後に ledger を
  freeze する（依存順）。settlement（#5）は S3.3 で derived view + export を先行し、schema freeze は
  S4 で行う。
- **out of scope（S3 段階）**: 自動 ledger 生成（hand 結果→entry）、chip↔cash 換算、rake/fee、
  決済連携、mobile UI、cross-app sync（S5）。
