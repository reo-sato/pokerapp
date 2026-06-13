# ADR-0011: 決定的 replay ハーネス（synchronous timestamp-ordered driver + clock 注入）

## Status

Accepted（F1 / #8 実装済。golden fixtures は green 2 ケース + pending 3 ケース＝D2b/F3）

## Date

2026-06-05

## Context

ADR-0010 で hand core を **record（R1）→ replay（R4）→ freeze（R5）** で contract 化する方針を立て、R1
（`output/event_recorder.py`、`reconstruction_event` sidecar）は実装済。Phase D（R3）の残り D2b
（silent-fold 合成）/ D3（派生 confidence）は **誤 fold が核リスク**で、golden fixtures を oracle に
回帰固定しないと「完成」とみなせない（ロードマップ R-1）。そのため D を仕上げる前に **決定的 replay
ハーネス**が要る。

`integration/engine.py` の非決定性の源は wall-clock のみ:
- `_now_iso()`（`ActionRecord`/`HandSummary` の timestamp）= `datetime.now()`。
- `_expire_buffers()` の `time.time()`（`CAMERA_BUFFER_TTL` による buffer 期限）。

live の `IntegrationThread.run()` は 3 キュー（audio 1 件ずつ + camera/rfid drain）を 0.1s ポーリングで
回し、**スレッドスケジューリングと wall-clock に依存**する。これをそのまま replay に使うと決定性を保証
しにくい。

## Decision

1. **clock を注入する**。`IntegrationThread(clock: Callable[[], float] = time.time)` を追加し、
   `_now_iso()` を `datetime.fromtimestamp(self._clock()).isoformat(...)` に、`_expire_buffers()` を
   `self._clock()` に変更。**既定 `time.time` で live は不変**（`fromtimestamp(time.time()) ≡ now()`）。
2. **replay は threaded `run()` を回さない**。`integration/replay.py:replay_events` が event を
   **timestamp 昇順**に並べ、各 event の timestamp を clock にセットしてから、live と同じ per-event
   メソッド（`_handle_audio_event` / `_process_rfid_event` / `_camera_buffer.append` / `_expire_buffers`）
   を**同期的に**呼ぶ。`_record`（sidecar）は呼ばない（replay 入力が記録そのもの）。
3. **HandSummary は `on_hand` コールバックで捕捉**（additive。`_finalize_hand` で発火）。
4. **golden 比較は timestamp を正規化**。`datetime.fromtimestamp` は local tz のため、
   `started_at`/`ended_at`/`actions[].timestamp` を除去し confidence を丸めて machine/timezone 非依存に
   する。**round-trip 決定性**（同一 events を 2 回 replay → 完全一致、timestamp 込み）は同一マシン・
   同一 clock 派生で成立し、DoD #3 を実証する。

## Consequences

- **良**: D2b/D3 を安全に検証する oracle ができ、`apply_corrections`+actor 推定（D1/D2a）の挙動が
  golden で固定される。CLI `tools/replay_hand.py` で記録ログを手動再生できる。live は完全不変
  （既定 clock）。
- **良**: 同期 driver は live のキュー競合・ポーリングを排し、決定的で読みやすい。
- **注意/代償**: driver が `IntegrationThread` の「private」メソッドを呼ぶ（同一コードベース内の許容。
  tests も既に `_handle_audio_event` を呼ぶ）。live `run()` のキュー順序 ≈ timestamp 昇順の乖離は
  replay では昇順に正規化するため、その乖離自体の fidelity は別途 live 観測で詰める（ISSUE-0010 の
  許容度）。
- **将来**: F2 残ケース（silent-fold/out-of-turn-rfid=D2b、unequal-allin=F3）と F3 の `hand`/`action`
  schema freeze（ISSUE-0011）はこのハーネス上に積む。

## Alternatives considered

- **threaded `run()` を replay でも使う**: live と同一経路だが、ポーリング/スケジューリング依存で
  決定性の保証が難しく、テストが遅く不安定。→ 却下。
- **clock 注入せず timestamp を正規化のみ**: buffer 期限が実時計依存のままで replay 中に誤期限切れ。
  → 不可。
- **expected_hand.json に実 timestamp を凍結**: local tz 依存で machine 非依存にならない。→ 正規化を採用。

## References

- ADR-0010（record/replay 全体方針）/ ADR-0009（pokerkit ルール権威）
- `docs/contracts/event-replay.md` §4（決定性条件）/ §5（golden fixtures）
- 実装: `integration/replay.py`, `tools/replay_hand.py`, `integration/engine.py`（clock 注入）,
  `tests/test_reconstruction.py`, `tests/fixtures/reconstruction/{check-facing-bet,call-amount-from-state}/`
- GitHub issue #8（Phase F）/ Epic #4 / ISSUE-0010 / ISSUE-0011
