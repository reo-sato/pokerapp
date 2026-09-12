# ADR-0024: sync の settlement マージを paid_amount monotonic max に（partial-paid 対応）

## Status

Accepted

## Date

2026-06-13

## Context

ADR-0022（双方向 sync）の settlement マージは「両 committed なら **paid が unpaid に勝つ**、
settled_at は早い方」という 2 値前提のルールだった。ADR-0023 で settlement に **partial**（一部支払い、
`paid_amount` が真実で `payment_status` は導出）が入り、この 2 値ルールでは破綻する:

- `partial`（例 paid_amount=4000）と `unpaid`（paid_amount=0）はどちらも「`paid` ではない」ため
  `paid==unpaid` 扱いになり、**settled_at の早い方**で決まる。結果、早い settled_at を持つ `unpaid`
  （paid_amount=0）が、後から記録された `partial`（paid_amount=4000）を**上書き**し得る。これは
  「受領額は取り消されない」単調性に反し、収束先が受領額を失う。

ADR-0022 の前提（merge は可換・冪等で収束）を partial-paid 下でも保つ必要がある。

## Decision

**両 committed の settlement マージを `paid_amount` の monotonic max で解決**する（ADR-0022 の
settlement-merge 節をこの ADR が更新）:

1. `paid_amount = max(local.paid_amount, peer.paid_amount)`。受領額は累積・取り消されない単調量
   （ADR-0023）なので max は可換・結合・冪等 ⇒ 収束。
2. `payment_status` は `paid_amount` と `net_due_to_store` から **導出**する（core の
   `_derive_payment_status` を単一導出点として再利用）。partial も一貫して決まる。
3. frozen な totals / `settled_at` は決定的に選んだ **base**（`settled_at` 最早、同値は record の
   安定シリアライズで tiebreak）から採る。これにより同 settled_at で内容が異なる病的ケースでも可換。
4. committed が uncommitted に勝つ点は不変（uncommitted は通常 snapshot に現れない）。

実装は `core/sync.py:_resolve_settlement`。ADR-0022 の他ストア（ledger union / order status /
session closed>open など）の規則は不変。

## Alternatives Considered

- **payment_status の 3 値順序（unpaid<partial<paid）で解決** — paid_amount を見ないため、
  同 status 内の金額差（例 partial 4000 vs partial 6000）を取りこぼす。max(paid_amount) が正確で単調。
- **settled_at の最新で上書き（LWW）** — 受領額の単調性を壊し、古い snapshot の同期で paid_amount が
  減りうる。→ max を採用。
- **ADR-0022 を書き換える** — 旧 ADR は履歴として残す方針（traceability rules）。本 ADR で更新を記録。

## Consequences

- Positive: partial-paid が複数書き手間でも安全に収束（受領額を失わない）。可換・冪等を維持。
- Negative / trade-offs: `core/sync.py` が `_derive_payment_status` に依存（単一導出点の再利用で
  二重定義は避けた）。frozen totals が同 settled_at で食い違う病的ケースは安定 tiebreak で決定的に
  選ぶ（どちらか一方の値、これは元々の「post-commit の ledger 差」caveat と同類）。
- Neutral: 現行既定（単一書き手 + on-demand pull）では実害がなかった latent issue の予防的修正。

## Validation / Follow-up

- [x] `tests/test_sync.py`: max paid 採用 / partial が早い unpaid に上書きされない / 冪等 / 同 paid は
  最早 settled_at / 可換。全ストア収束テストは緑のまま。
- [ ] player rename 伝播（`updated_at`）など他の非単調フィールドは別途（ADR-0022 の残課題）。

## Related Files

- `core/sync.py`（`_resolve_settlement`）/ `core/ledger_repository.py`（`_derive_payment_status` 再利用）
- `docs/contracts/viewer-api.md`（sync 節）

## Related Tests

- `tests/test_sync.py::test_settlement_max_paid_amount_wins` /
  `::test_settlement_partial_not_overwritten_by_unpaid` / `::test_settlement_merge_idempotent`

## Related Commits

- 本 ADR と同じ commit

## Supersedes / Superseded by

- Supersedes: —（ADR-0022 の settlement-merge 節を更新。関連: ADR-0023）
- Superseded by: —
