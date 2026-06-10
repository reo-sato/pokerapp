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

## ledger / points / settlement（S3 設計確定 / core 実装 planned — ADR-0011）

方針は ADR-0011 / `ledger-overview.md` で確定。**core 実装は S3.1（ISSUE-0012）**。core 実装後に本節の
「planned」を外す。core（`LedgerRepository`, planned）が source of truth:

- **append-only**: `ledger_entry` / `point_ledger_entry` は mutate / delete しない。訂正は新規 reversal
  （`reverses_entry_id`）/ adjustment / 相殺 spend で表す。
- **point 残高 = fold**: 残高は `point_ledger_entry.delta_points` の総和（ISSUE-0001 選択肢 A）。cached
  カラムを権威にしない（cache は将来 fold から導出）。
- **残高非負 + cash 補完**: 残高を割り込む spend は拒否（`insufficient_points`）。不足分は cash 計上（rule 3）。
- **ledger↔point 整合**: `ledger_entry.point_amount > 0` には `delta_points = −point_amount`・
  `related_ledger_entry_id` 設定済の `spend_*` point entry が **ちょうど 1 件** 対応する。
- **entry fee は cash only**（`kind=entry_fee` ⇒ `point_amount == 0`, rule 1）。buy-in / rebuy / add-on /
  order は cash + point 併用可（rule 2）。
- **非ゼロ移動**: 通常 entry は `cash_amount,point_amount ≥ 0` かつ `cash_amount+point_amount > 0`。
  `delta_points ≠ 0`。adjustment / reversal のみ符号付き可。
- **参照整合**: `player_id` は registry 実在、`session_id` / `hand_id`（指定時）は session レイヤ実在、
  `entry_id` 一意。
- **grant 冪等性**: `idempotency_key` 指定時、同キーの manual/campaign grant 重複を拒否（`duplicate_grant`）。
- **settlement**:
  - 常に「player → 店」の 1 方向。player 間精算は扱わない（rule 5）。
  - **derived materialized view**: `net_due_to_store = 符号付き Σ cash_amount`（当該 session・player）。
    closed session のみ確定（`session_not_closed` / `already_settled`）。
  - `payment_status` は `paid` / `unpaid`（partial は扱わない, rule 4）。確定後の唯一の可変フィールド。

## schema で表現する / しないの境界（指針）

- schema で表現する: 型、必須性、文字列パターン（ID 形式等）、enum、構造の入れ子。
- schema で表現しない（core に置く）: 一意性・重複、他レコード参照の整合、残高・合計の計算、
  正規化（strip / casefold 等）、状態遷移（settled 済みは再確定不可 等）。
