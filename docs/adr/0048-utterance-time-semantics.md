# ADR-0048: 発話時刻セマンティクスとセンサー照合窓（T1-T3）

- Status: Accepted
- Date: 2026-08-19
- 関連: ADR-0009 / ADR-0010（event sidecar）/ ADR-0011（決定的 replay）/ ADR-0047 / ADR-0049

## Context

`AudioEvent.timestamp` は **ASR デコード完了時刻**（`parse_action` 内の `time.time()`）で、
発話そのものはチャンクバッファ（最大 5 秒）+ Whisper 推論の分だけ過去にある。一方 RFID / camera
イベントは物理事象の時刻を持つ。照合窓 `±MATCH_WINDOW(2.0s)` を audio の timestamp 中心に張ると、
**発話と同時の RFID 読みが窓から外れて corroboration を取りこぼす**（T1）。窓が対称なのも実態
（証拠は発話の前後に分布）と合わない（T2）。さらに ActionRecord/HandSummary の時刻が live では
wall-clock（`self._clock()`）、replay では注入 clock 由来で**意味が異なっていた**（T3）。

## Decision

1. **`AudioEvent.utterance_start_ts` を additive 追加**（T1）。recorder（ADR-0049 の再構築版）が
   発話区間の開始時刻を記録し `parse_action` に渡す。envelope（`reconstruction_event` 0.2）にも
   additive に記録（既定値のときは省略 = 旧 replay 実装でもそのまま読める）。
2. **照合窓は発話区間ベースの両側窓**（T2）: `[utterance_start_ts − MATCH_WINDOW,
   timestamp + MATCH_WINDOW]`。`utterance_start_ts` 欠損（旧 events.jsonl / 直接構築 / 制御 UI 由来）
   は従来どおり `timestamp ± MATCH_WINDOW` に退化する（後方互換を明示テストで固定）。
3. **センサーバッファ保持期間を照合窓から分離**: `CAMERA_BUFFER_TTL` を 4s → **12s** に拡大。
   保持は「ASR 遅延で audio が遅れて届いても証拠が expire しない」ための余裕であり、採否は
   あくまで照合窓が決める（保持拡大でマッチ条件は変わらない）。
4. **ActionRecord / HandSummary の時刻は event.timestamp 由来に統一**（T3）:
   `_iso(event.timestamp)`。live と replay が同じ envelope から同じ時刻を出す（決定的 replay の
   時刻まで一致）。注入 clock はバッファ期限にのみ使う。

## Consequences

- 発話→デコード遅延が最大 7 秒程度あっても RFID/camera corroboration が成立する
  （`tests/test_reconstruction_hardening.py::TestTimingT`）。
- 旧 events.jsonl は無変更で再生でき、挙動は従来窓と同一（後方互換）。
- live の ActionRecord.timestamp は「ASR 確定時刻」の意味に統一される（発話開始は
  `utterance_start_ts` として envelope に残る）。
