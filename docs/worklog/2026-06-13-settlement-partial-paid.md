# Worklog: settlement partial-paid（paid_amount additive / payment_status 導出）

## Date

2026-06-13

## Scope / Task

S4 settlement に一部支払い（partial-paid）を追加する（ADR-0023）。frozen な
`session_settlement` schema を additive（optional field + enum 値）に拡張し、core / staff API /
GUI / mobile / docs を一気通貫で更新する。

## Goal

- 累計受領額 `paid_amount` を真実とし、`payment_status`（paid/unpaid/partial）を core が単一の
  導出関数で計算する。
- 既存 `ledger.json` の確定済 settlement（`paid_amount` 無し）が regression しない。
- staff / GUI / player(mobile) の各面から partial を記録・参照できる。

## Changed Files

- `core/ledger.py` — `SessionSettlement.paid_amount`（既定 0）+ `to_dict`/`from_dict`（後方互換推定）。
- `core/ledger_repository.py` — `_derive_payment_status` / `record_payment` / `set_payment_status` を
  record_payment 経由に再実装 / `commit_settlement` で paid_amount=0・status 導出 /
  `_derive_settlement_rows` が既存 paid_amount を carry。
- `docs/contracts/schemas/session_settlement.schema.json` — version 1.0→1.1, `paid_amount`（optional,
  >=0）+ enum に `partial`。
- `docs/contracts/fixtures/session_settlement/` — `invalid-bad-status.json` を `refunded`（真の不正値）に
  修正、`valid-partial.json` を追加。
- `tests/test_contracts.py` — settlement 契約 test に record_payment(partial) の検証を追加。
- `api/server.py` — `_StaffPaymentBody` + `PUT /api/staff/sessions/{sid}/players/{pid}/payment`。
- `api/client.py` — `ViewerApiClient.record_payment`。
- `api/read_models.py` — player ledger summary に `paid_amount`。
- `gui/ledger_view.py` — 精算パネルに受領額入力 + 「支払額記録」ボタン + `_cmd_record_payment`。
- `tests/test_ledger_view_gui.py` — partial / full / negative の GUI test。
- `tests/test_viewer_api_staff.py` — record_payment の正常 / not_found / invalid test。
- `mobile/src/api/types.ts` — `LedgerSummary.paid_amount?`、payment_status コメント更新。
- `mobile/src/screens/MyLedgerScreen.tsx` — 支払済み / 一部支払い / 未払い の 3 分岐表示。
- `mobile/src/api/mockRepository.ts` / `.test.ts` — summary に `paid_amount: 0`。
- docs: `CLAUDE.md` / `CHANGELOG.md` / `decision-log.md` / `viewer-api.md` / `validation-rules.md` /
  `ledger-overview.md`。

## Expected Behavior

- `record_payment(net 部分額)` → status="partial"・paid_amount 設定。全額 → "paid"。0 → "unpaid"。
  net<=0 → "paid"（徴収不要）。過払い（paid>net）→ "paid" に丸め。
- `set_payment_status("partial")` は金額が無いため ValueError。
- 後方互換: paid_amount 欠落の既存行を load すると payment_status から推定（paid→net, 他→0）。

## Implemented Behavior

期待どおり。core は ADR-0023 の導出ルールを `_derive_payment_status` に一点集約し、
`record_payment` / `set_payment_status` / `commit_settlement` がすべてこれを通る。

## Test Results

- `python -m pytest tests/ --ignore=tests/test_vision.py -q` — **524 passed, 0 skipped**
  （baseline 518 + GUI 3 + staff API 3）。
- `ruff check .` — All checks passed!
- `cd mobile && npm run typecheck && npm test` — typecheck clean, **8 pass**。

## Mismatches Found During Testing

None observed. なお既存 fixture `invalid-bad-status.json` は `payment_status: "partial"` を
不正値として使っていたが、1.1 で partial が valid 化したため `refunded` に差し替えた
（invalid- fixture の意図 = enum 外を保つ）。

## Fixes Applied

- `invalid-bad-status.json` の不正値を `partial`→`refunded` に変更（上記理由）。
- 契約 test の `client` メソッド名を `get_player_session_ledger` に合わせた。

## Remaining Gaps / Out-of-Scope

- [ ] auto ledger 生成（settlement 拡張の別項目）。
- [ ] `core/sync.py` の settlement merge は paid>unpaid の単調解決。partial は両者の中間に位置するが
      paid_amount を比較キーに含めていない。複数書き手で partial を同期する運用が出たら sync 規則の
      additive 見直しが要る（本タスクでは sync.py は無変更）。

## Related ADRs

- `docs/adr/0023-settlement-partial-paid.md` — 本タスクの source of truth。
- `docs/adr/0016-...` / `docs/adr/0019-...` — additive 拡張元（settlement core / schema freeze）。

## Related Issues

- なし（Business rule #4 の方針転換は ADR-0023 で記録）。

## Related Commits

- 本 worklog と同じ commit（`feat(s4): settlement partial-paid (ADR-0023)`）。
