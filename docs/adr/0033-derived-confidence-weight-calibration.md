# ADR-0033: 派生 confidence の重み較正（property-based, golden fixtures 由来）

## Status

Accepted（**実装済 2026-06-14**。重みは変更せず、満たすべき較正プロパティを固定して回帰ロック）

## Date

2026-06-14

## Context

rules-aware 経路の派生 confidence（`integration/engine.py:derive_confidence`, 3 因子 L/A/Q,
ADR-0009 §6 / Phase D3）の重み（`_CONF_W_A` / `_CONF_W_Q` / `_CONF_BASE` / `_CONF_L_PENALTY` /
`REVIEW_THRESHOLD` / `SYNTH_FOLD_CONFIDENCE`）はコメント上「暫定値・最終較正は golden fixtures / F」と
され、較正が未了だった（CLAUDE.md「R 系の後続: 派生 confidence の重み較正」）。

較正のための **ラベル付き大規模データセットは存在しない**（golden fixtures は 5 ケース）。よって
統計的フィッティングではなく、**golden fixtures の archetype が含意するプロパティ**（順序・閾値分離・
合法性ゲート）を較正の正解仕様とし、重みがそれを満たすことを検証・固定する方針を採る。

## Decision

### D1. 較正は property-based（数値フィッティングではない）

golden fixtures（`tests/fixtures/reconstruction/`）の archetype と境界グリッドに対し、derived
confidence が満たすべきプロパティを **較正スペック**として定義する:

- **P1 bounds**: 全入力で `0 ≤ conf ≤ 1`。
- **P2 whisper 単調**: audio-only は `whisper_conf` に対し単調増加。
- **P3 source ordering**: 合意ソースの質で順序づく（audio < audio+camera < rfid+audio < all3）。
- **P4 rfid>camera corroboration**: RFID 補強 > camera 補強（base rfid 0.78 > camera 0.28）。
- **P5 disagreement penalty**: 同席ソースの不一致は一致より低い。
- **P6 illegal gate**: `apply_ok=False`（pokerkit 非受理）は合法より低く、高合意でも `REVIEW_THRESHOLD`
  未満に落ちる（合法性ゲート L が最重要）。
- **P7 threshold separation**: 良好 audio-only（whisper≥0.6）は非 review、低品質（≤0.5）は review。
- **P8 synth-fold**: 合成 silent-fold（`SYNTH_FOLD_CONFIDENCE`）は常に閾値未満（必ず review）。

### D2. 既存重みは較正済みとして確定（数値は変更しない）

上記プロパティを現行の暫定重みが **すべて満たす**ことを確認した（`tools/calibrate_confidence.py`）。
ラベルデータが無い以上、プロパティを満たす重みを恣意的に動かすと golden fixtures（`expected_hand.json`）を
理由なく churn させるだけなので、**数値は据え置き、コメントの「暫定」表記を解除**して較正済みとする。

確認された較正サーフェス（要点）:

| シナリオ | confidence | review |
|---|---|---|
| audio-only, whisper 0.5 / 0.6 / 0.9 | 0.362 / 0.405 / 0.532 | ✓ / – / – |
| rfid+audio 一致, whisper 0.9 | 0.897 | – |
| rfid+audio+camera 一致, whisper 0.9 | 0.926 | – |
| rfid 不一致, whisper 0.9 | 0.457 | – |
| 非合法（L penalty）, rfid+audio 一致 | 0.224 | ✓ |
| 合成 silent-fold | 0.300 | ✓ |

### D3. 回帰ロック（ハーネス + CI テスト）

- `tools/calibrate_confidence.py`: confidence サーフェスを表示し、`check_properties()` で P1〜P8 を検証
  （違反で exit 1）。重み変更時に手動で回す**較正ハーネス**。
- `tests/test_confidence_calibration.py`: 同じ `check_properties()` を CI で実行し、重みの意図しない drift を
  検知。golden archetype の代表値（audio-only whisper0.7≈0.448 / rfid+audio whisper0.9≈0.897）も固定。
- derived path は legacy 固定表（`calc_confidence`, RFID+audio+camera=1.00 等）に**一致させない**のが意図
  （rules-aware は別モデル。noisy-OR で 1.0 に漸近）。較正の正解はプロパティであって legacy テーブルではない。

## Alternatives Considered

- **ラベルデータで数値フィッティング** → そのデータが存在しない（5 fixtures のみ）。→ property-based（D1）。
- **legacy 固定表に一致させる** → derived は別モデルで、全合意でも 1.0 には漸近しない。一致は目的でない。
  → プロパティを正解仕様に（D3）。
- **較正を機に重みを動かす** → golden fixtures を理由なく変えるだけ。プロパティを満たす以上据え置く（D2）。

## Consequences

- Positive: 「暫定」だった重みが、明文化された較正スペック（P1〜P8）で**正当化・回帰ロック**された。重みを
  変更すると CI が壊れる（意図しない drift 検知）。再較正の手順（ハーネス）が残る。挙動・数値は不変。
- Negative / trade-offs: property-based なので「最適」重みを主張するものではない（プロパティを満たす一例の
  固定）。実運用ログが貯まれば数値較正に発展可能（将来）。
- Neutral: コードの数値変更なし。ハーネス + テスト + コメント更新のみ。

## Validation / Follow-up

- [x] `tools/calibrate_confidence.py`（surface + P1〜P8 検証）。
- [x] `tests/test_confidence_calibration.py`（P1〜P8 + archetype 値 + 閾値分離の回帰）。
- [x] `integration/engine.py` の「暫定/最終較正は F」コメントを ADR-0033 参照（較正済み）に更新。
- [ ] 実運用 review ログが貯まったら数値較正（将来。camera 源統合 = R 系後続と併せて）。

## Related Files

- `integration/engine.py`（`derive_confidence` / 重み定数 / `REVIEW_THRESHOLD` / `SYNTH_FOLD_CONFIDENCE`）
- `tools/calibrate_confidence.py` / `tests/test_confidence_calibration.py`
- `tests/fixtures/reconstruction/`（archetype の出所）

## Related Tests

- `tests/test_confidence_calibration.py` / `tests/test_reconstruction.py`（golden replay）

## Related Commits

- 本 ADR の実装 commit（2026-06-14）

## Supersedes / Superseded by

- Supersedes: —（ADR-0009 §6 の confidence 設計の「最終較正」を確定。関連: ADR-0009/0011/0012）
- Superseded by: —
