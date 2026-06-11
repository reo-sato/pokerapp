# Validation rules contract

JSON Schema は **構造**（型・required・形式）を検査するが、業務 validation
（重複・残高・cross-field・正規化）は表現しきれない。これらは **core が source of truth** で
あり、本 doc に明文化する。front-end は再実装せず、この規則を参照して表示する。

## 役割分担

| 層 | 担当する検査 |
|----|------------|
| JSON Schema（`schemas/*.schema.json`） | 型 / required / 文字列形式 / enum / additionalProperties |
| core validation（`core/*`） | 重複・一意性・残高・cross-field・正規化・状態遷移 |
| front-end | **検査しない**。core / repository の error（`error-shapes.md`）を表示するだけ |

## player（S1, 実装済 — `core/player_repository.py` が source of truth）

- **display_name は前後空白を除去（strip）して保持する**。schema の `minLength: 1` は「空文字」を
  弾くが、「前後空白のみ（例 `"   "`）」は strip 後に空になるため **core が弾く**（`empty_display_name`）。
- **完全一致重複の禁止**: strip 後の display_name が既存 player と **完全一致** したら拒否
  （`duplicate_display_name`）。
- **大文字小文字・全半角の同一視は scope 外**（"Alice" と "alice" は別名）。将来拡張は ISSUE-0002。
- **rename も同じ規則**。ただし自分自身との一致は許容（no-op rename 可）。
- **player_id は不変**。rename で player_id は変わらない。
- 読み込み時の leniency: 永続ファイル読込で `created_at` 欠損は許容（空文字で補完）。これは
  **ロード堅牢性の実装詳細**であり、正規の永続形（schema canonical）では `created_at` は required。

## ledger / points / settlement（S3 実装済 — `core/ledger_repository.py` が source of truth, ADR-0016）

詳細は `ledger-overview.md` / `ledger-schema.md` / ADR-0016（fold は ADR-0013 を踏襲）。要点:

- **append-only**: `ledger_entry` / `point_ledger_entry` は mutate / delete しない。訂正は新規 reversal
  （`reverses_entry_id`）/ adjustment で表す。
- **entry fee は cash only**（`kind=entry_fee` ⇒ `point_amount == 0` → `entry_fee_requires_cash`）。
- **buy_in / rebuy / add_on / order は cash + point 併用可**。`adjustment` の point 補正は
  `grant_points` / `reverse_entry` で行う（`ledger_entry.point_amount` には載せない）。
- **point 不足は strict reject**（`insufficient_points`）。不足分は呼び出し側が cash で補完する
  （自動分割 `plan_payment` は ADR-0016 では非採用 = 明示性を優先）。
- **残高の source of truth は point_ledger_entry の fold**（ISSUE-0001 決着 = ADR-0013→ADR-0016）。
  残高は player に global・常に 0 以上。cached 残高カラムは持たない。
- **ledger↔point 整合**: `point_amount != 0` の entry には `delta = -point_amount` の `spend_*`
  point entry を core が同時生成（`related_ledger_entry_id` back-link）。
- **grant の冪等性**: 任意の `idempotency_key` の一意性で担保（重複 → `duplicate_grant`）。
- **order 明細**: `kind=order` のみ（item_name 非空 / unit_amount>=0 / quantity>=1）。
- **金額の符号**: 通常 kind は `cash_amount, point_amount >= 0` かつ合計正。`adjustment` / reversal は
  符号付き可（reversal は対象の符号反転）。
- **entry 追加は session 実在を要求**（open/closed の制約は設けない。確定済 settlement への反映は S4）。
- **settlement**: 常に「player → 店」の 1 方向（player 間精算は扱わない）。derived materialized view で
  `net_due_to_store = 符号付き Σ cash_amount`。closed session のみ確定（`session_not_closed` /
  `already_settled`）。`payment_status` は `paid` / `unpaid`（partial は扱わない）。

## 将来 model（planned）

対応 phase の freeze 時に本 doc へ追記する（session_settlement schema の `1.0` freeze は S4）。

## schema で表現する / しないの境界（指針）

- schema で表現する: 型、必須性、文字列パターン（ID 形式等）、enum、構造の入れ子。
- schema で表現しない（core に置く）: 一意性・重複、他レコード参照の整合、残高・合計の計算、
  正規化（strip / casefold 等）、状態遷移（settled 済みは再確定不可 等）。
