# ADR-0035: アミューズメント・ガードレール（負 adjustment = 返金/訂正のみ・監査）

## Status

Accepted（実装済 2026-06-14。v1.0 ローンチレビュー B9）

## Date

2026-06-14

## Context

本プロダクトは **アミューズメント専用**（日本の賭博罪を回避するため、賭け金の player 間分配を行わない）。
会計は **player → 店の 1 方向**で、ledger には payout / cash-out の kind が存在しない
（`net_due_to_store` は cash-in/order/entry_fee の合計、point_credited は net を減らさない, ADR-0016）。

運用/セキュリティ監査（v1.0 ローンチレビュー）で、**唯一の「店 → player」方向の経路**として
`kind=adjustment` の **負 cash** が指摘された（`add_entry` は adjustment に非ゼロ cash を許し、負値も可）。
これは返金・誤記訂正のための正当な機能だが、ガードレールが無いと「賞金/負け分の現金分配」（賭博的 payout）に
転用され得る。構造は 1 方向だが、この 1 点だけ運用規律に依存していた。

## Decision

1. **負の adjustment には理由 `note` を必須**にする（`core/ledger_repository.add_entry`）。空/空白のみは
   `InvalidAmountError`。正の adjustment（誤記の追加訂正）は従来どおり note 任意。
   → あらゆる「店→player」方向の金銭移動に**記録された理由**を強制する。
2. **負の adjustment を監査ログに残す**（`logger.warning("AUDIT 負 adjustment ...", entry, session, player,
   cash, note)`）。締め・調査時に「賞金分配でないこと（返金・訂正のみ）」を検証できるようにする。
3. **明文化**: 「負 adjustment は返金・誤記訂正のみ。賞金・負け分の現金分配には使用しない」を docs
   （usage / ledger 契約）と店向け運用ガイドに記載する。アミューズメント営業の線引きの一部。
4. point は **換金不可**（コード上 cash 化経路なし＝spend のみ, ADR-0016）。これは維持し、運用でも現金化を
   禁止する（規約）。
5. schema / 他 kind の挙動は不変（additive な validation 追加 + ログのみ）。error code は既存
   `invalid_amount` を再利用（新 code なし）。

## Alternatives Considered

- **負 adjustment を全面禁止** → 正当な返金・誤記訂正ができなくなる。→ 理由必須 + 監査で許容（D1/D2）。
- **専用 refund / void kind を新設** → schema 拡張が必要で v1.0 にはオーバー。`adjustment` + note + 監査で十分。
  将来需要が固まれば専用 kind を additive に検討。
- **構造的に 1 方向だから何もしない** → 唯一の抜け穴が運用規律のみに依存し、転用リスクが残る。→ ガードレール化。

## Consequences

- Positive: 「店→player」方向の金銭移動が必ず理由付き + 監査ログ化され、アミューズメントの線引き
  （賞金分配でない）を検証可能に。1 方向会計の不変条件が運用規律だけでなくコードでも補強される。
- Negative / trade-offs: 負 adjustment 時に note 入力が必須になる（GUI/staff API とも note 欄あり＝両立）。
  専用 refund モデルは持たない（汎用 adjustment + note）。
- Neutral: validation 1 条件 + ログ 1 行の追加。schema・error code は不変。

## Validation / Follow-up

- [x] `add_entry`: 負 adjustment の note 必須 + 監査 warning。
- [x] tests: `test_negative_adjustment_requires_note`（note なし/空白で reject、理由付き/正値は許容）。
- [x] docs: usage / ledger 契約に「返金・訂正のみ・賞金分配禁止」を明記。
- [ ] （将来）専用 refund/void kind、負 adjustment の集計レポート（締め監査の補助）。

## Related Files

- `core/ledger_repository.py`（add_entry の guardrail + 監査ログ）/ `tests/test_ledger_repository.py`
- `docs/usage.md` / `docs/contracts/ledger-overview.md`（明文化）

## Related Tests

- `tests/test_ledger_repository.py::test_negative_adjustment_requires_note`

## Related Commits

- 本 ADR の実装 commit（2026-06-14）

## Supersedes / Superseded by

- Supersedes: —（ADR-0016 の 1 方向会計を運用ガードレールで補強。関連: ADR-0016 / v1.0 ローンチレビュー B9）
- Superseded by: —
