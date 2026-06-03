# Issue 0009: actor 競合解決と silent-fold 合成のポリシー未確定

## Date

2026-06-03

## Status

<!-- One of: Open / Investigating / Fixed / WontFix / Duplicate -->
Open

## Severity / Priority

- Severity: Medium（ADR-0010 の actor 推定の核。誤適用は誤 fold を生むため確定が要る）
- Priority: P2

## Area

reconstruct / integration

## Expected Behavior

ADR-0010 §1 / `docs/contracts/hand-reconstruction.md` §4 の actor 推定で、prior（ルール上の手番）と sensor
証拠（RFID seat / 明示発話 seat / camera）が食い違うときの **確定規則・重み・自動 fold 合成の適用条件・
`needs_review` 閾値**が一意に定まっていること。

## Actual Behavior

設計案は「物理/明示証拠を prior より優先し、選ばれた actor が合法アクターなら prior〜actor 間の席を
silent fold として自動合成する」だが、以下が **未確定**:

1. silent-fold を**何席まで**自動合成してよいか（暴走防止の上限）。1 席飛びは妥当でも、4 席飛びは誤認識の
   可能性が高い。
2. RFID seat と明示発話 seat が**互いに**食い違うときの優先（現案は RFID > audio だが、明示発話の方が
   強い場面もある）。
3. silent-fold 合成した席に `needs_review` を立てるか（合成 fold は推測なので原則 flag すべきか）。
4. §6 の派生 confidence の重み（`w_L` / `w_A` / `w_Q`）と `review_threshold` の初期値。

## Reproduction

仕様レビュー（freeze 前の open question）:

1. `docs/contracts/hand-reconstruction.md` §4 / §6 を参照。
2. `integration/engine.py:276`（`_pop_matching_rfid_event`）, `:313`（`get_current_player` 直結）,
   `:433`（`_extract_seat_from_text`）の現状を確認。

## Root Cause

実運用の「ディーラーが fold を宣言しない」「out-of-turn」を正しく捉えるには prior を sensor で上書きする
必要があるが、上書きは誤 fold / 誤帰属のリスクと表裏。安全な適用条件は ground truth（golden fixtures,
ADR-0011）に対する評価がないと決められない。

## Fix

未対応（R2/R3 着手時に確定）。確定したら:

- 自動 fold 合成の席数上限・`needs_review` 付与条件を `hand-reconstruction.md` §4 に明文化。
- 派生 confidence 重みと閾値を §6 に明記し、golden fixtures で較正。
- ソース優先順位（RFID / audio / camera / prior）の最終形を表で固定。

## Regression Test

- `tests/test_reconstruction.py` の `silent-fold` / `out-of-turn-rfid` ケース（ADR-0011 §5）で、確定した
  ポリシーの期待出力を pin する。

## Affected Files

- `integration/engine.py`（actor 推定 / 派生 confidence の実装先）
- `core/events.py`（`AudioEvent.seat?`）
- `docs/contracts/hand-reconstruction.md` §4 / §6

## Related Worklog

- `docs/worklog/2026-06-03-rules-aware-reconstruction-planning.md`

## Related ADRs

- `docs/adr/0010-rules-constrained-estimation-and-fusion.md`

## Related Commits

- 本 issue と同じコミット（reconstruction engine design planning）

## Notes

risk register として Open 保持。R3 実装時に golden fixtures で評価しながら順次クローズする。
