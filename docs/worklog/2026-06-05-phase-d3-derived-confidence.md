# Worklog: Phase D part 5 (D3) — 派生 confidence + needs_review 5 条件（Phase D 完了）

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase D（GitHub issue #7）の **D3**。固定 8 行 confidence テーブルを、
解釈可能な 3 因子（L 合法性 / A 合意 / Q 品質）の合成に置き換え（rules-aware 経路のみ）、needs_review
の 5 条件を明文化する。これで **Phase D（D0/D1/D2a/D2b/D3）が完了**。統合ブランチ `v1-integration`、
作業ブランチ `claude/phaseD-confidence`。

## Goal

- `derive_confidence`（3 因子, ADR-0009 §6）を新設し rules-aware 経路に結線。legacy（固定 8 行
  `calc_confidence`）は不変。
- needs_review 5 条件（①非合法 ②ASR×規則矛盾 ③actor 競合 ④amount snap ⑤低 confidence）を明文化。
- 重みは旧テーブル近傍 + 音声優先運用（良好 audio-only は自動 review しない）に較正。golden fixtures 再凍結。

## Changed Files

- `integration/engine.py`: 定数 `_CONF_W_A/_CONF_W_Q/_CONF_L_PENALTY/_CONF_BASE/REVIEW_THRESHOLD`、
  `derive_confidence(...)`（純関数）、`_handle_rules_aware_action` を D3 化（confidence + 5 条件）。
- `tests/test_phase_d3_confidence.py`（新規 7: derive_confidence の順位/ゲート/whisper/合意 + 閾値条件⑤）。
- `tests/fixtures/reconstruction/*/expected_hand.json`（4 ケース再凍結、confidence 更新）。
- docs: CHANGELOG / hand-reconstruction.md §6 / CLAUDE.md / ISSUE-0009 / 本 worklog。

## Expected vs Implemented

- `derive_confidence = clamp(L·(w_A·A + w_Q·Q), 0, 1)`。L=1.0/penalty、A=一致存在ソース/存在ソース、
  Q=一致ソース base の noisy-OR（audio は whisper でスケール）。
- 単調・解釈可能を確認: camera 0.31 < audio 0.58 < rfid 0.74 < rfid+audio 0.91 < all 0.93、
  illegal audio-only 0.14。
- 較正で `REVIEW_THRESHOLD=0.40`、`audio base=0.50`: 良好 audio-only（whisper≥0.7）は閾値超え＝review なし
  （`call-amount-from-state` は review=False を維持）、camera-only / 低 whisper / 合成 fold は閾値未満＝review。

## Test Results

- `pytest tests/test_phase_d3_confidence.py -q` — **7 passed**。
- `pytest tests/test_reconstruction.py -q` — **12 passed, 1 skipped**（4 golden 再凍結後も緑）。
- `pytest tests/ -q --ignore=tests/test_vision.py` — **270 passed, 1 skipped**（回帰なし、legacy 不変）。

## Mismatches Found During Testing

- 初版（threshold=0.5, audio base 0.45）で `call-amount-from-state` が条件⑤により review=True に反転
  （audio-only whisper0.7 → conf 0.42 < 0.5）。音声優先運用では良好な audio-only を自動 review すべきでない
  ため、threshold=0.40 / audio base=0.50 に較正し直し、review=False を回復。

## Fixes Applied

- 重み再較正（上記）。閾値は良好 audio-only と camera-only/低 whisper の間に置いた。

## Remaining Gaps / Out-of-Scope

- [ ] 重み（`_CONF_*`/`REVIEW_THRESHOLD`/`SILENT_FOLD_CAP`/合成 fold 0.3）の**最終較正**は Phase F の
      golden fixtures 拡充で。`REVIEW_THRESHOLD` の config 化も後続。
- [ ] camera 源の actor 寄与（現状 actor 推定は RFID/audio のみ）。
- [ ] **F3**: `unequal-allin`（side-pot を HandSummary に）/ hand・action schema freeze（ISSUE-0011）。
- [ ] pokerkit を CI/requirements に pin（Phase G/H）。

## Related ADRs / Issues

- ADR-0009 §6（派生 confidence）/ ISSUE-0009（D3 反映、重み較正は残）。
- GitHub issue #7（Phase D、**D3 で完了**）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
