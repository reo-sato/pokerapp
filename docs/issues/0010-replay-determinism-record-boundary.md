# Issue 0010: 決定的 replay のための記録境界とスレッド順序近似

## Date

2026-06-03

## Status

<!-- One of: Open / Investigating / Fixed / WontFix / Duplicate -->
Resolved（記録境界・clock 源を契約で確定: Phase B+C / #6, 2026-06-05。replayer 実装と
スレッド順序許容度の実証固定は Phase F / #8 の golden fixtures）

## Severity / Priority

- Severity: Medium（ADR-0010 の record/replay が決定的であるための前提）
- Priority: P2

## Area

reconstruct / integration / docs (contract)

## Expected Behavior

ADR-0010 / `docs/contracts/event-replay.md` の replay が **決定的**（`replay(record(stream)) == live 結果`）
であること。具体的には (a) **何を記録するか**の境界が定まり、(b) live のスレッド/キュー順序を replay が
再現できる許容度が定まっていること。

## Actual Behavior

未確定の論点:

1. **記録境界**: ASR の **decode 後**（`AudioEvent.action/amount/raw_text/confidence`）を記録すれば Whisper を
   再実行しないので決定的だが、**ASR モデル自体の改善を offline で測れない**。raw audio を保存すれば re-ASR
   できるが重く、faster-whisper のバージョン/ハード差で **非決定的**。どちらを既定にするか。
2. **時刻源**: 現状 `integration/engine.py:429` `_now_iso()` と `:261` `time.time()` の照合窓は **実時計**依存。
   replay で決定的にするには engine に clock を注入し event timestamp で動かす必要がある（設計済みだが未実装）。
3. **スレッド順序**: live は audio/camera/RFID が別キューで opportunistic に drain される。replay は timestamp
   昇順で通す案だが、live の実順序と timestamp 順の **乖離許容度**（どこまでを「同値」とみなすか）が未定義。

## Reproduction

仕様レビュー（freeze 前の open question）:

1. `docs/contracts/event-replay.md` §4 を参照。
2. `integration/engine.py` の `_now_iso`（:429）/ `time.time()` 窓（:261）/ `_drain_*_queue` を確認。
3. `audio/recognizer.py:218` が Whisper per-segment 信頼度を破棄していることを確認（記録するなら露出が要る）。

## Root Cause

realtime パイプラインは本質的に実時計・スレッドスケジューリングに依存しており、これをそのままでは
オフラインで再現できない。記録対象と時刻源を契約として固定しないと replay の決定性が保証されない。

## Fix

**Phase B+C（#6）で記録境界と clock 源を契約として確定**（`event-replay.md` §4 を「決定」に更新）:

- **記録境界 = ASR decode 後**（`action/amount/raw_text/seat/confidence`）に確定。決定的 replay を
  優先し、raw audio 保存は任意の上位レイヤとして分離（re-ASR は重く whisper バージョン間で非決定的）。
  B+C で `AudioEvent.{seat,confidence}` を追加し `event_to_envelope` で露出（`reconstruction_event`
  schema は既に optional 定義済、code↔contract 緑）。
- **clock 源 = 観測済み最大 event timestamp**（live=実時計）に確定。
- **replay は timestamp 昇順**で同一 drain ロジックへ通す方針を `event-replay.md §4` に明文化。

残（Phase F / #8）: engine への clock source 注入（`MATCH_WINDOW`/`CAMERA_BUFFER_TTL` を event time
で駆動）、`tools/replay_hand.py` 実装、live キュー順序 ≈ timestamp 順の許容度を golden fixtures の
round-trip 決定性テストで実証固定。

## Regression Test

- `tests/test_reconstruction.py`: 同一 `events.jsonl` を 2 回 replay して **同一 `HandSummary`** になること
  （決定性の round-trip）。`reconstruction_event` schema で記録形を固定（`tests/test_contracts.py` の `_MODELS`）。

## Affected Files

- `integration/engine.py`（記録境界・clock 注入・drain 順序）
- `audio/recognizer.py`（Whisper 信頼度の露出 = `AudioEvent.confidence`）
- `core/events.py`（envelope の素）
- `docs/contracts/event-replay.md` §3 / §4

## Related Worklog

- `docs/worklog/2026-06-03-rules-aware-reconstruction-planning.md`

## Related ADRs

- `docs/adr/0010-contract-first-hand-core-record-replay.md`

## Related Commits

- 本 issue と同じコミット（reconstruction engine design planning）

## Notes

記録（R1）は挙動非破壊で先行 ship 可能。本 issue の決着は replayer（R4）着手の前提。
