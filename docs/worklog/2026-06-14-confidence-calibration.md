# Worklog: 派生 confidence の重み較正（ADR-0033）

## Date

2026-06-14

## Scope / Task

R 系後続「派生 confidence の重み較正（golden fixtures 由来）」。`derive_confidence` の暫定重みを較正し、
回帰ロックする。実機不要。

## Goal

ラベルデータが無い前提で、golden fixtures の archetype が含意するプロパティを較正の正解仕様とし、
現行重みがそれを満たすことを検証・固定する。「暫定」表記を解除する。

## Approach / Findings

- confidence サーフェスを探索し、現行重み（`_CONF_W_A=0.15` / `_CONF_W_Q=0.85` /
  `_CONF_BASE={rfid:0.78,audio:0.50,camera:0.28}` / `_CONF_L_PENALTY=0.25` / `REVIEW_THRESHOLD=0.40` /
  `SYNTH_FOLD_CONFIDENCE=0.3`）が以下を満たすことを確認:
  - audio-only は whisper に単調（0.6 で 0.405 = 閾値超え、0.5 で 0.362 = review）。
  - ordering: audio(0.532) < audio+cam(0.663) < rfid+audio(0.897) < all3(0.926)。
  - 不一致 rfid(0.457) < 一致(0.897)、非合法は rfid+audio 一致でも 0.224（<閾値）、synth-fold 0.3(<閾値)。
- ラベルデータが無い以上、プロパティを満たす重みを動かすと golden fixtures を理由なく churn させるだけ
  なので **数値は据え置き**、property-based 較正として確定（ADR-0033）。

## Changed Files

- `tools/calibrate_confidence.py`（新規）: サーフェス表示 + `check_properties()`（P1〜P8）+ CLI（違反で exit 1）。
- `tests/test_confidence_calibration.py`（新規）: `check_properties()` を CI 実行 + archetype 値 + 閾値分離。
- `integration/engine.py`: 重み定数コメントの「暫定/最終較正は F」を ADR-0033 参照（較正済み）に更新。
  `SYNTH_FOLD_CONFIDENCE` / `REVIEW_THRESHOLD` のコメントも較正プロパティ参照に。**数値変更なし**。
- docs: `docs/adr/0033-...md` / decision-log / CLAUDE.md（status 行 + commands + 残作業 #6）/ CHANGELOG。

## Expected / Implemented Behavior

- 挙動・出力は不変（golden replay の expected_hand.json も不変 = 数値据え置き）。
- 重みを変更すると `tools/calibrate_confidence.py` / CI が P1〜P8 違反を検知する（drift ガード）。

## Test Results

- `python tools/calibrate_confidence.py` — 全プロパティ PASS, exit 0。
- `pytest tests/test_confidence_calibration.py -q` — 11 passed。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **618 passed, 0 skipped**（既存 607 + 11）。
- `ruff check .` — clean。

## Mismatches Found During Testing

- なし（数値据え置きのため golden replay も不変）。

## Remaining Gaps / Out-of-Scope

- 実運用 review ログが貯まってからの **数値較正**（将来。camera 源統合と併せ）。
- camera 源の統合（R 系後続）。

## Related ADRs / Issues

- ADR-0033（本件）/ ADR-0009（confidence 設計 §6）/ ADR-0011（決定的 replay）/ ADR-0012

## Related Commits

- 本 commit
