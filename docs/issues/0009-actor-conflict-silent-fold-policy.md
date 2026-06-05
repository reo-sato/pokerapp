# Issue 0009: actor 競合解決と silent-fold 合成のポリシー未確定

## Date

2026-06-03

## Status

<!-- One of: Open / Investigating / Fixed / WontFix / Duplicate -->
Open

## Severity / Priority

- Severity: Medium（ADR-0009 の actor 推定の核。誤適用は誤 fold を生むため確定が要る）
- Priority: P2

## Area

reconstruct / integration

## Expected Behavior

ADR-0009 §6 / `docs/contracts/hand-reconstruction.md` §4 の actor 推定で、prior（ルール上の手番）と sensor
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
ADR-0010）に対する評価がないと決められない。

## Fix

**初期方針を決定（2026-06-05, Phase D 着手・Epic #4 で承認）**:

- **silent-fold 合成の上限 = 1-2 席**。超過は合成せず prior を維持し `needs_review`。
- **ソース優先順位 = RFID > audio(明示発話) > camera > prior(手番)**。
- **合成した silent-fold には常に `needs_review` を付与**（推測のため）。
- 派生 confidence 重み `w_L`/`w_A`/`w_Q` と `review_threshold` の **初期値は暫定**とし、Phase F (#8) の
  golden fixtures で較正・確定する。

**Phase D part 1（#7, 2026-06-05）実装済**: `apply_corrections`（`audio/recognizer.py`, ADR-0009 §5）。
合法手への射影・call/check の状態一意化・amount snap・`needs_review` トリガを純関数で実装（pokerkit 非依存・
ライブ未結線で挙動不変, `tests/test_phase_d_corrections.py`）。`check` がベットに直面した場合の call/fold
尤度（§8 の open）は **暫定で call + `needs_review`**（プレイヤーを勝手に hand から外さない側）を採用。

**Phase D part 3（#7 D2a, 2026-06-05）実装済**: `apply_corrections` をライブ結線し明示発話席の競合を
検出（`integration/engine.py:_handle_rules_aware_action`、actor は prior 固定）。

**Phase D part 4（#7 D2b, 2026-06-05）実装済**: 上記初期方針どおり **silent-fold 合成**を結線。
`fold_through(sensed, max_folds=SILENT_FOLD_CAP=2)` を **atomic**（`copy.deepcopy` snapshot/restore で
誤 fold を残さない）に強化し、`_resolve_actor` が優先順位 **RFID > 明示発話席** で actor を推定。prior と
異なれば中間席を fold 合成（cap 超過/到達不可は prior 維持 + `needs_review`）、合成 fold は fold アクション
として記録（`confidence=0.3`・常に `needs_review`）。golden fixtures `silent-fold` / `out-of-turn-rfid`
（`tests/test_reconstruction.py`）で pin 済。

**残（Status Open のまま）**: camera 源、派生 confidence 融合（D3、合成 fold の 0.3 含む重み較正）。
これらの完了と pokerkit pin で本 issue をクローズする。

## Regression Test

- `tests/test_reconstruction.py` の `silent-fold` / `out-of-turn-rfid` ケース（ADR-0010 §5）で、確定した
  ポリシーの期待出力を pin する。

## Affected Files

- `integration/engine.py`（actor 推定 / 派生 confidence の実装先）
- `core/events.py`（`AudioEvent.seat?`）
- `docs/contracts/hand-reconstruction.md` §4 / §6

## Related Worklog

- `docs/worklog/2026-06-03-rules-aware-reconstruction-planning.md`

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`

## Related Commits

- 本 issue と同じコミット（reconstruction engine design planning）

## Notes

risk register として Open 保持。R3 実装時に golden fixtures で評価しながら順次クローズする。
