# ADR-0023: settlement の partial-paid 対応（paid_amount additive / payment_status 導出）

## Status

Accepted

## Date

2026-06-13

## Context

S4 settlement は `payment_status` を `paid` / `unpaid` の 2 値で扱い、**partial paid（一部支払い）は
扱わない**としていた（CLAUDE.md Business rules #4、ADR-0016）。実運用では「net 1万円のうち
5千円だけ受け取った」のような部分支払いが起きるため、これに対応する。

`session_settlement` schema は ADR-0019 で `1.0` frozen。変更は **additive 規則**
（`versioning-and-freeze.md` §2: optional field 追加・enum 値追加は additive = MINOR bump、
worklog で可）に収める。business rule #4 を覆す方針転換のため本 ADR で記録する。

## Decision

1. **`SessionSettlement` に `paid_amount: int`（既定 0）を additive 追加**する。
   「これまでに受け取った金額」。`net_due_to_store` は確定時に凍結された請求額（不変）。
2. **`payment_status` は `paid_amount` と `net_due_to_store` から導出**する（core が単一の導出関数で
   計算し、stored field として serialize する）:
   - `net_due_to_store <= 0` → **`paid`**（徴収不要）。
   - `paid_amount <= 0` → **`unpaid`**。
   - `paid_amount >= net_due_to_store` → **`paid`**。
   - それ以外（`0 < paid_amount < net`）→ **`partial`**（新規 enum 値）。
3. **schema を `1.0` → `1.1`**（additive）: `paid_amount`（integer, minimum 0, **optional / 非 required**）
   追加 + `payment_status` enum に **`partial`** 追加。fixtures は既存（paid_amount 無し）を
   後方互換として残し、partial の fixture を追加。
4. **後方互換 load**: `from_dict` は `paid_amount` 欠落時に `payment_status` から推定する
   （`paid` → `net_due_to_store`、それ以外 → 0）。既存 `ledger.json` の確定済 settlement が
   regression しないようにする。
5. **core API**:
   - `record_payment(session_id, player_id, paid_amount) -> SessionSettlement`（新規, `_locked`）:
     確定済 settlement の `paid_amount` を設定し `payment_status` を導出。`paid_amount < 0` は
     `InvalidAmountError`、未確定は `LedgerNotFoundError`。
   - `set_payment_status(session_id, player_id, status)`（既存・後方互換）: `"paid"` →
     `record_payment(net_due_to_store)`、`"unpaid"` → `record_payment(0)`。`"partial"` は金額が要るため
     status 経由では不可（`ValueError`）。
   - `commit_settlement`: `paid_amount=0`、`payment_status` 導出（net≤0 は paid、他は unpaid）。
6. **公開**: staff write API に `PUT /api/staff/sessions/{sid}/players/{pid}/payment` body `{paid_amount}`
   を追加（既存の paid/unpaid `payment-status` は shortcut として残す）。`ViewerApiClient.record_payment`。
   GUI（`gui/ledger_view.py` 精算パネル）に支払額入力、mobile / player summary に `paid_amount` +
   `partial` 表示。

## Alternatives Considered

- **payment_status を stored の独立 3 値（partial を直接セット）** — 金額を持たないと「いくら払ったか」
  が分からず、status と実額の不整合が起きうる。→ paid_amount を真実とし status を導出。
- **MAJOR version bump（2.0）** — optional field + enum 追加は additive で後方互換。MAJOR は不要
  （`versioning-and-freeze.md` §2）。→ 1.1（MINOR）。
- **partial を扱わない（据え置き）** — 実運用の部分支払いを表現できない。→ 対応する。

## Consequences

- Positive: 部分支払いを正確に記録でき、player は残額を把握できる。schema は additive で
  既存データ・consumer を壊さない。
- Negative / trade-offs: `payment_status` が 3 値になり、front-end の表示分岐が増える。
  既存 `set_payment_status` は維持するが、partial は金額 API（record_payment）経由が必須。
- Neutral: Business rule #4 を「partial-paid 対応」に更新。schema 1.0→1.1。

## Validation / Follow-up

- [x] core（paid_amount / 導出 / record_payment / 後方互換 load）+ schema 1.1 + fixtures + 契約 test。
- [x] staff API `PUT .../payment` + client + GUI 入力 + mobile/player summary 表示。
- [ ] 過払い（paid_amount > net）の扱いは status=paid に丸める（釣り/チップは ledger 側で別途）。

## Related Files

- `core/ledger.py`（SessionSettlement）/ `core/ledger_repository.py`（record_payment / 導出）
- `docs/contracts/schemas/session_settlement.schema.json`（1.1）/ fixtures
- `api/server.py` / `api/client.py` / `api/read_models.py`
- `gui/ledger_view.py` / `mobile/`

## Related Tests

- `tests/test_ledger_repository.py`（partial / 後方互換）/ `tests/test_contracts.py`（settlement schema）
- `tests/test_viewer_api_staff.py` / `tests/test_ledger_view_gui.py::TestSettlement`

## Related Commits

- 本 ADR と同じ commit（partial-paid）

## Supersedes / Superseded by

- Supersedes: —（ADR-0016 / ADR-0019 を additive 拡張。Business rule #4 を更新）
- Superseded by: —
