# Worklog: Phase B+C — イベント記録基盤（AudioEvent 拡張 + replay 境界確定）

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase B+C（GitHub issue #6、R トラック起点）。R3(actor 推定) と
R4(決定的 replay) の **ハード前提**である `AudioEvent` の `seat`/`confidence` additive 化と、
ISSUE-0010（記録境界・決定性）の決着。統合ブランチ `v1-integration`、作業ブランチ
`claude/phaseBC-event-foundation`。

## Goal

- B: `AudioEvent` に optional `seat`/`confidence` を additive 追加し、Whisper の per-segment
  信頼度を `confidence` として、明示発話席を `seat` として populate。`event_to_envelope` で露出。
  既存挙動は不変。
- C: ISSUE-0010 の記録境界（decode 後）と clock 源（観測済み最大 ts）を契約として確定し
  Resolved 化。`event-replay.md §4` を「決定」に更新。
- 既存テストの回帰なし。

## Changed Files

- `core/events.py` — `AudioEvent.seat` / `AudioEvent.confidence`（optional, default None）を additive 追加。
- `audio/recognizer.py` — `WhisperTranscriber.transcribe_with_confidence()` 追加（`transcribe()` は委譲）、
  `parse_action(text, confidence=None)` で confidence 受領、`_extract_seat_no()` で明示席抽出。
- `audio/recorder.py` — `_process_chunk` を `transcribe_with_confidence` 経由に変更し confidence 伝搬。
- `output/event_recorder.py` — audio envelope に `seat`/`confidence` を additive 露出。
- `docs/contracts/event-replay.md` — §4 の open question を「決定（ISSUE-0010 Resolved）」に更新、§7 反映。
- `docs/issues/0010-...md` — Status Open→Resolved、Fix に決定内容と Phase F 残作業。
- `docs/decision-log.md` — ISSUE-0010 行 Status 更新。
- `tests/test_phase_bc_events.py`（新規, 10）/ `tests/test_event_recorder.py`（audio envelope 更新 + schema 検証拡張）。

## Expected Behavior

- `AudioEvent(...)` は `seat`/`confidence` 未指定で従来どおり構築でき、既存呼び出しは不変。
- `parse_action("シート3 レイズ 800", confidence=0.77)` → `seat=3, confidence=0.77, amount=800`。
- 範囲外席（>9）・席表現なしは `seat=None`。
- `transcribe_with_confidence` はモデル未ロード時 `("", None)`。
- `event_to_envelope` の audio 出力に `seat`/`confidence` が含まれ、`reconstruction_event` schema を通る。
- ISSUE-0010: 記録境界 = decode 後、clock 源 = 観測済み最大 ts が契約に明文化される。

## Implemented Behavior

- 期待どおり。`confidence` は segment の `avg_logprob` 平均を `exp` で [0,1] に写像（segment 無し → None）。
- 既存テスト `test_event_recorder.test_audio_envelope`（厳密一致）は `seat:None`/`confidence:None` を
  含むよう更新。`ActionRecord.confidence`（センサー融合スコア、`calc_confidence`）とは別物で無干渉。
- clock 注入・replayer 本体は **本タスク対象外**（Phase F / #8）。C は契約決定のみ。

## Test Results

- `python -m pytest tests/test_phase_bc_events.py tests/test_event_recorder.py tests/test_parser.py -q`
  — **24 passed**。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **204 passed, 10 skipped**
  （従来 193 + 新規 11。skip は pokerkit 未導入分）。回帰なし。

## Mismatches Found During Testing

- None observed.

## Fixes Applied

- なし（新規実装。Phase A レビュー由来の修正はなし）。

## Remaining Gaps / Out-of-Scope

- [ ] engine への clock source 注入 / `tools/replay_hand.py` / golden fixtures による round-trip
      決定性の実証固定は **Phase F（#8）**。
- [ ] `seat` の populate は「明示発話席」のみ。手番 prior × sensor による actor 推定は **Phase D（#7）**。

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`（confidence/seat の用途）
- `docs/adr/0010-contract-first-hand-core-record-replay.md`（record/replay 契約）

## Related Issues

- `docs/issues/0010-replay-determinism-record-boundary.md`（本タスクで Resolved）
- GitHub issue #6（Phase B+C）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
