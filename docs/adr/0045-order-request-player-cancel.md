# ADR-0045: 注文リクエストの player 本人キャンセル（status `cancelled` の additive 追加）

- **Status**: Accepted
- **Date**: 2026-07-12
- **Related**: ADR-0018（M5 注文 write path / staff-in-the-loop）/ ADR-0019（schema 1.0 freeze）/
  ADR-0022（sync state-based merge）/ ADR-0023（additive schema 拡張の先行例 = settlement 1.1）/
  ADR-0030（player merge / equivalence class）

## Context

M5（ADR-0018）の注文リクエストは pending → confirmed | rejected の 2 終端のみで、
**player が自分の pending 注文を取り下げる手段がない**。実運用では「間違えて送った」
「やっぱりやめる」が必ず発生し、現状は口頭でスタッフに却下してもらうしかない
（UI 棚卸しで dogfood 中に欲しくなる項目と整理）。

## Decision

### D1: 終端 status `cancelled` を additive 追加（schema `1.0` → `1.1`）

- 状態遷移は **pending → confirmed | rejected | cancelled**。cancelled は rejected と同様
  ledger には何も書かない終端。既存終端の意味は不変（additive）。
- `order_request.schema.json` は enum 値追加のみで `1.0` → `1.1`
  （ADR-0023 の settlement `1.1` と同じ「optional/enum の additive 拡張」パターン）。

### D2: キャンセルは本人のみ・pending のみ（core が enforce）

- `OrderRequestRepository.cancel_request(request_id, player_id, session_id=None)`:
  - request の player_id が `player_id` の **equivalence class**（merge 考慮, ADR-0030 D2）に
    含まれない場合は **not_found**（他人の注文の存在を漏らさない）。
  - `session_id` を渡した場合は request の所属 session と一致しなければ not_found（URL 整合）。
  - pending 以外は `already_resolved`（409）。ledger には何も書かない。
  - closed session の pending も取り下げ **可**（確定と違い会計に影響せず、閉店後の残骸掃除に有用）。

### D3: API は player self-write（注文 POST と同じ認可姿勢）

- `POST /api/players/{player_id}/sessions/{session_id}/order-requests/{request_id}/cancel`
- write 所有プロセスのみ（read-only は 503 `orders_unavailable`）。`player_auth` が
  optional/required の会場では principal 必須（`_require_player`）— 注文 POST と同一。
- 返り値は更新後の `order_request`。エラーは既存 code を再利用
  （`not_found` / `already_resolved` / `orders_unavailable` / `unauthorized`）。新 code なし。

### D4: sync の status 解決順位

`_ORDER_STATUS_RANK` を `pending(0) < cancelled(1) < rejected(2) < confirmed(3)` に拡張。

- **confirmed は常に勝つ**: 片ノードで player がキャンセル・別ノードでスタッフが確定した
  衝突では確定を採用（ledger entry が既に存在し、会計影響を優先）。
- cancelled vs rejected は「どちらも ledger 無しの終端」で実質同義。決定的順序のために
  rejected を上位とする（スタッフ操作 > player 操作）。可換・冪等・収束の性質は不変。

## Consequences

- mobile の注文状況に「キャンセル」導線が付き、staff の pending queue からは自動的に消える
  （status フィルタのまま）。staff 側 UI の変更は status 表示の追加のみ。
- スタッフが取り込み作業中（confirm 直前）に player がキャンセルすると、スタッフの confirm は
  409 `already_resolved` になる — これは正しい挙動（先勝ち）。
- schema `1.1` は additive のため、旧クライアント（cancelled を知らない表示）は未知 status を
  そのまま文字列表示するだけで壊れない。
