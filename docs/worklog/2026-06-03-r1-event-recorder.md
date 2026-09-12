# Worklog: R1 — event recording sidecar (record-only)

## Date

2026-06-03

## Scope / Task

ADR-0010 の段階導入 R1。生センサーイベントを append-only sidecar
（`logs/{session_id}.events.jsonl`）に**記録だけ**する（解釈・再構築は不変）。後続 R2+ の
pokerkit エンジン／推定をオフラインで決定的に replay・回帰固定するためのデータ基盤の第一歩。

## Goal

- `IntegrationThread` の入口で、解釈する前に各 `AudioEvent` / `RFIDEvent` / `CameraEvent` を
  `reconstruction_event` envelope（camera frame 除外）として 1 行ずつ記録する。
- **挙動完全不変**: recorder 未指定（既定）なら従来と完全に同じ。既存 `logs/*.json` / PHH に触れない。
- `reconstruction_event` の schema/fixtures を契約化し contract test に載せる（`additionalProperties:false`）。
- "done" = code + schema/fixtures + tests + docs が揃い、全テスト緑。

## Changed Files

- `output/event_recorder.py`（新規）— `EventRecorder`（append-only, スレッド安全, I/O 失敗で止めない）＋
  `event_to_envelope()`（type 判別 dict 化, frame 除外）。
- `integration/engine.py` — `IntegrationThread.__init__` に optional `event_recorder` を additive 追加。
  `run()`（audio get 後）/ `_drain_camera_queue` / `_drain_rfid_queue` の 3 dequeue 点で解釈前に
  `self._record(ev)`。`_record` は recorder=None なら no-op。
- `main.py` — `_make_event_recorder(cfg, log_dir, session_id)` ヘルパ追加。`config.recording.enabled`
  （既定 false）で `EventRecorder` を構築し、CLI / GUI 両 `IntegrationThread` 生成点に注入。
- `config_default.json` — `recording.enabled: false`（opt-in）を追加。
- `docs/contracts/schemas/reconstruction_event.schema.json`（新規, v0.1, `additionalProperties:false`）。
- `docs/contracts/fixtures/reconstruction_event/`（新規）— canonical / valid-audio-minimal /
  valid-rfid-seat / valid-rfid-board / valid-camera / invalid-missing-type / invalid-unknown-field /
  invalid-bad-type / invalid-audio-missing-fields。
- `tests/test_contracts.py` — `_MODELS` に `reconstruction_event` を登録。
- `tests/test_event_recorder.py`（新規）/ `tests/test_integration_recording.py`（新規）。
- docs: `ADR-0010` を Accepted（R1 実装済）に更新、`event-replay.md` Status 更新、`decision-log.md`
  ADR-0010 行 Accepted、`CHANGELOG.md` に R1 Added、`CLAUDE.md` 実装状況表＋R0–R5 行を更新。

## Expected Behavior

- recorder を渡すと、解釈前の生イベントが `events.jsonl` に 1 行ずつ JSON で追記される。
- recorder を渡さない（既定）と、sidecar ファイルは作られず、`logs/*.json` / 再構築は従来どおり。
- recorder 出力は `reconstruction_event` schema を通る（code↔contract）。

## Implemented Behavior

- 上記のとおり実装。`event_to_envelope` は AudioEvent→`{type:"audio",...}` / RFIDEvent→`{type:"rfid",...}` /
  CameraEvent→`{type:"camera", seat, timestamp}`（frame 除外）。`EventRecorder.record` は未対応型・I/O 失敗を
  warning に留め例外を投げない（CLAUDE.md エラーハンドリング方針）。
- engine の `_record` は 3 dequeue 点で解釈前に呼ばれ、recorder=None で no-op（挙動不変）。
- schema は `additionalProperties:false` ＋ `allOf`/`if/then` で type 別 required を表現
  （全プロパティを top-level に宣言し strict バリデータでも valid）。

## Test Results

- `python -m pytest tests/ -q --ignore=tests/test_vision.py` → **187 passed**（従来 185 + 新規 2 e2e）。
- 個別: `tests/test_event_recorder.py`（7）/ `tests/test_integration_recording.py`（2）/
  `tests/test_contracts.py`（reconstruction_event の schema↔fixture 整合含む）すべて緑。
- `python -m py_compile main.py integration/engine.py output/event_recorder.py` → OK。
- 環境補足: 本リモート env には pytest/jsonschema/numpy が未導入だったため `pip install` して実行
  （実データ依存の faster-whisper / pyaudio 等は不要な範囲で全緑）。

## Mismatches Found During Testing

- None observed。recorder 未指定で sidecar 非生成・既存 187 テスト不変を確認（挙動不変を実証）。

## Fixes Applied

- なし（新規実装で mismatch なし）。schema は discriminated union の `additionalProperties:false` 罠を避け、
  全プロパティ top-level 宣言＋`if/then` required の形にした。

## Remaining Gaps / Out-of-Scope

- [ ] R2: pokerkit エンジンを façade 背後に（ISSUE-0008 spike が前提）。
- [ ] R4: replayer（`tools/replay_hand.py`）＋ golden fixtures（`tests/fixtures/reconstruction/`）＋
      `hand`/`action` 実 schema 化（ISSUE-0011）。本 R1 では `reconstruction_event` のみ契約化。
- [ ] 注入クロック（決定性, ISSUE-0010）は replayer 着手時（R4）に対応。R1 は記録のみで replay 非対象。

## Related ADRs

- `docs/adr/0010-contract-first-hand-core-record-replay.md`（R1 = 本 worklog で実装）
- `docs/adr/0009-pokerkit-live-rules-authority.md`（R2/R3, 提案のまま）

## Related Issues

- `docs/issues/0010-replay-determinism-record-boundary.md`（記録境界: decode 後を記録＝決定的、を R1 で採用）
- `docs/issues/0011-hand-action-schema-freeze-blockers.md`（hand/action freeze は R4/R5）

## Related Commits

- 本 worklog と同じコミット（R1 record-only 実装）。設計は 2026-06-03 の reconstruction planning worklog。
