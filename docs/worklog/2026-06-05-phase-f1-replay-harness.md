# Worklog: Phase F part 1 (F1) — 決定的 replay ハーネス + 最初の golden fixtures

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase F（GitHub issue #8）の **F1**。Phase D の残り D2b（silent-fold 合成）/
D3（派生 confidence）を安全に仕上げるための **oracle**（決定的 replay + golden fixtures）を先に用意する。
統合ブランチ `v1-integration`、作業ブランチ `claude/phaseF-replay-harness`（#17 D2a マージ後の
v1-integration から分岐）。

## Goal

- `integration/engine.py` の非決定性（`_now_iso` / `_expire_buffers` の wall-clock）を clock 注入で除去
  （既定 `time.time` で live 不変）。
- `integration/replay.py`（replay 関数）+ `tools/replay_hand.py`（CLI）。
- D1/D2a で既に緑にできる 2 ケースの golden fixtures + round-trip 決定性テスト。
- 既存テストの回帰ゼロ。

## Changed Files

- `integration/engine.py`: `__init__` に `clock` / `on_hand`（additive）。`_now_iso()` を method 化
  （`datetime.fromtimestamp(self._clock())`）、module-level `_now_iso` 削除、5 呼び出しを `self._now_iso()` に。
  `_expire_buffers` の `time.time()` → `self._clock()`。`_finalize_hand` で `on_hand` 発火。
- `integration/replay.py`（新規）: `event_from_envelope` / `load_events` / `replay_events` / `replay_fixture`。
  timestamp 昇順の同期 driver が `_handle_audio_event` / `_process_rfid_event` / `_camera_buffer` /
  `_expire_buffers` を再利用。`_ReplayClock` で各 event の timestamp を clock にセット。
- `tools/replay_hand.py`（新規・CLI）。
- `tests/fixtures/reconstruction/{check-facing-bet,call-amount-from-state}/{setup.json,events.jsonl,expected_hand.json}`（新規）。
- `tests/test_reconstruction.py`（新規, 7 + pending 3 skip）。
- docs: `docs/adr/0011-deterministic-replay-harness.md`（新規）, `docs/decision-log.md`（登録）,
  `docs/contracts/event-replay.md §4/§5`, `CLAUDE.md` 実装状況, `CHANGELOG.md`。

## Expected vs Implemented

- 期待どおり。green 2 ケースは replay で正しく再構築（check→call+review / call 額 200=state、heard 9999 無視）。
- expected_hand.json は replay 出力を **正規化して凍結**（wall-clock 由来の `started_at`/`ended_at`/
  `actions[].timestamp` を null、confidence を丸め）。`datetime.fromtimestamp` が local tz のため、
  golden 比較は正規化で machine/timezone 非依存にした。round-trip は同一マシン・同一 clock 派生で
  timestamp 込み完全一致。

## Test Results

- `python -m pytest tests/test_reconstruction.py -v` — **7 passed, 3 skipped**（pending = D2b/F3）。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **255 passed, 3 skipped**
  （D2a baseline 248 + 7。clock 既定で既存テスト回帰なし）。
- CLI smoke: `python tools/replay_hand.py tests/fixtures/reconstruction/check-facing-bet` → call/amount=200/review=True。

## Mismatches Found During Testing

- なし（green 2 ケースは初回から想定挙動）。winner をハンド途中で宣言しても pokerkit `end_hand` が
  権威的に pot を push する live モデルどおりに動作。

## Fixes Applied

- なし（新規ハーネス + additive な clock/on_hand 注入）。

## Remaining Gaps / Out-of-Scope（Phase F の残り）

- [ ] **F2 残ケース**: `silent-fold` / `out-of-turn-rfid` は **D2b**（fold_through 合成）実装と同時に
      fixtures を authoring。`unequal-allin` は **F3**（side-pot を HandSummary に連携）後。
- [ ] **F3**: `hand` / `action` schema を実ファイル化し `_MODELS` 登録、`additionalProperties` 方針確定
      （**ISSUE-0011**）。PHH の call/check 区別。**D・E 完了後**。
- [ ] **D3** の派生 confidence 重みは F2 の golden fixtures で較正。
- [ ] pokerkit を CI/requirements に追加（Phase G/H で pin）。

## Related ADRs / Issues

- ADR-0011（本タスク）/ ADR-0010（record/replay 方針）/ ADR-0009（pokerkit 権威）。
- ISSUE-0010（replay 決定性・記録境界、§4）/ ISSUE-0011（schema freeze、F3）。
- GitHub issue #8（Phase F）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
