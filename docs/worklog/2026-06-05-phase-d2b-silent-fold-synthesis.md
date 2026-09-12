# Worklog: Phase D part 4 (D2b) — silent-fold 合成

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase D（GitHub issue #7）の **D2b**。ディーラー未宣言の fold（最頻のズレ）を
補い、actor を物理/明示証拠が指す席へ追従させる silent-fold 合成を結線する。Phase F1（#18）で用意した
決定的 replay ハーネス + golden fixtures を oracle に、誤 fold（リスク R-1）を回帰固定しながら実装。
統合ブランチ `v1-integration`、作業ブランチ `claude/phaseD-silent-fold`。

## Goal

- `fold_through` を atomic + cap 対応にし、合成 fold した席列を返す。
- `_resolve_actor` を「RFID seat 読み > 明示発話席」優先で actor 推定 + silent-fold 合成に。
- 合成 fold を fold アクションとして記録（needs_review）。
- golden fixtures `silent-fold` / `out-of-turn-rfid` を緑化。legacy 不変・回帰ゼロ。

## Changed Files

- `core/poker_engine.py`: `import copy`、Protocol/impl の `fold_through(until_seat, max_folds=None)
  -> list[int]`。**atomic**（`copy.deepcopy` snapshot、失敗時 `self._state` を巻き戻し）+ `max_folds`
  cap + 合成席列を返す。
- `core/game_state.py`: legacy `fold_through` シグネチャ追随（NotImplementedError のまま）。
- `integration/engine.py`: `SILENT_FOLD_CAP=2` / `SYNTH_FOLD_CONFIDENCE=0.3` 定数、
  `_pop_nearest_rfid_seat`（窓内最近傍 RFID seat を席不問で 1 件消費）、`_resolve_actor` を D2b 化
  （sensed=RFID>明示席、prior と異なれば `fold_through` 合成、cap 超過/到達不可は prior 維持）、
  `_append_synth_fold`（合成 fold を記録）、`_handle_rules_aware_action` を合成対応に。
- `tests/test_phase_d0_engine.py`: fold_through の返り値/cap-atomic/unreachable-atomic 3 テスト追加。
- `tests/test_phase_d2_wiring.py`: 明示席競合テストを silent-fold 合成テストに更新。
- `tests/fixtures/reconstruction/{silent-fold,out-of-turn-rfid}/`（新規）+ `test_reconstruction.py`
  の GREEN_CASES 昇格。
- docs: CHANGELOG / event-replay.md §5 / hand-reconstruction.md §4 / CLAUDE.md / ISSUE-0009 / 本 worklog。

## Expected vs Implemented

- silent-fold（audio 駆動）: 明示席 seat1（prior=seat3）→ seat3 を fold 合成、seat1 が raise 600。
  actions = [fold(seat3, synth, review, conf0.3), raise(seat1, 600, review)]。
- out-of-turn-rfid（RFID 駆動）: RFID seat1 読み（prior=seat3）→ seat3 fold 合成、seat1 が call 200。
  seat1 の call は **source.rfid=True**（推定に使った RFID が最終 actor と一致 → corroboration、conf 0.95）。
- cap 超過/到達不可は prior 維持（合成せず）+ needs_review。`fold_through` は失敗時に状態を巻き戻す。

## Test Results

- `pytest tests/test_reconstruction.py -v` — **12 passed, 1 skipped**（green 4: 射影 2 + 合成 2、
  pending=unequal-allin/F3。round-trip 決定性も全 green）。
- `pytest tests/ -q --ignore=tests/test_vision.py` — **263 passed, 1 skipped**（回帰なし、legacy 不変）。
- `fold_through` atomic/cap は `tests/test_phase_d0_engine.py` で unit 固定。

## Mismatches Found During Testing

- D2a の `test_explicit_seat_conflict_flags_review` は D2b で挙動が変わる（prior 固定 → 合成）ため、
  silent-fold 合成テストに更新（想定どおりの挙動変更）。

## Fixes Applied

- 既存 D0 `fold_through` は非 atomic（途中 fold が残る）+ cap なしだったため、D2b で atomic + max_folds を
  追加（誤 fold 安全装置、R-1）。

## Remaining Gaps / Out-of-Scope

- [ ] **D3**: 派生 confidence（3 因子 L/A/Q）+ `needs_review` 5 条件。合成 fold の `0.3` と
      `SILENT_FOLD_CAP` の最終較正は golden fixtures で。
- [ ] camera 源の actor 寄与（現状 RFID/audio のみ）。
- [ ] **F3**: `unequal-allin`（side-pot を HandSummary に連携）、hand/action schema freeze（ISSUE-0011）。
- [ ] pokerkit を CI/requirements に追加（Phase G/H で pin）。

## Related ADRs / Issues

- ADR-0009 §4（actor 推定）/ ADR-0011（replay harness, oracle）/ ISSUE-0009（silent-fold ポリシー、D2b 反映）。
- GitHub issue #7（Phase D）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
