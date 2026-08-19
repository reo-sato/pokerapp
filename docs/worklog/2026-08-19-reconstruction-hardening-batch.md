# 2026-08-19 — アクション履歴復元アルゴリズムの改善バッチ（ADR-0047/0048/0049/0050）

## Goal

Phase A KPI（3 軸 ≥95%）に向け、3 系統の深掘り調査で特定した「静かな誤り」経路・バグ・計測歪みを
一括修正する（ユーザー承認済みプラン: バッチ 1 = 正当性、バッチ 2 = ガード/時刻/recorder/chop/
fixture 拡充。バッチ 3 = 実データ後の項目は文書化のみ）。

## Changed files

- `core/constants.py` — WHISPER_PROMPT_JA 自然文化、語彙拡充（リレイズ/スリーベット/フォルド/
  降ります/マック/チョップ/スプリット）
- `core/engine_types.py` — LegalContext += `bb` / `committed`（additive, legacy stub は 0）
- `core/events.py` — AudioEvent += `parse_flags` / `utterance_start_ts`（additive）
- `core/hand_log.py` — ActionRecord += 監査 5 フィールド（emit-when-not-None）、
  HandSummary += `pot_awards`（optional）
- `core/poker_engine.py` — legal_context に bb/committed、`end_hand_split`、
  `_snapshot_pots`（未回収 bet の残差合成 = pot_total 権威化）
- `core/game_state.py` — legacy `end_hand_split`
- `audio/recognizer.py` — parse_amount_ex（S1 + ambiguous）、席表現統一（S2）、NFKC、
  複数キーワード flag（V1）、apply_corrections 強化（S3/S4/V4/G4）、no_speech_prob 減衰
- `audio/recorder.py` — T4 再構築（キャプチャ/推論分離・有音ゲート・サンプル数 flush・
  utterance_start_ts・テスト seam）
- `integration/engine.py` — 全面改修: G2 監査配線 / B1 stale ctx 再取得 / B2/B4 unresolved
  レコード + 例外統一 / B5 winner fallback / S5 stack_start 順序 / S6 pot_total / G1 制御語
  ガード + mid-hand new_hand 異常確定 / G3 active 席フィルタ / T1-T3 照合窓・時刻 / S7 chop
- `integration/replay.py` / `output/event_recorder.py` — envelope 0.2（additive, 後方互換）
- `tools/measure_capture_accuracy.py` — G6 SequenceMatcher アライメント
- `tools/calibrate_confidence.py` — P9 追加
- `tools/play_hand_text.py` — confidence=1.0 明示（B3 対応）
- `main.py` / `config_default.json` — `engine.control_conf_threshold` 結線（既定 0.0）
- schemas: `action` 1.0→1.1（reason/apply_ok）、`hand` 1.0→1.1（pot_awards）、
  `reconstruction_event` 0.1→0.2（utterance_start_ts/parse_flags）
- fixtures: 既存 5 件を手計算検証の上で再 pin（差分表 = ADR-0047）+ 新規 8 件
  （postflop-street-transition / full-ring-6max / multi-hand-session /
  rfid-vs-spoken-seat-conflict / camera-corroboration / low-whisper-confidence /
  cap-exceeded-negative / split-pot-chop）
- tests: `test_reconstruction_hardening.py`（新規 63）、`test_measure_capture_accuracy.py`
  （G6 回帰 2）、`test_confidence_calibration.py`（P9）、`test_phase_d2_wiring.py`
  （conf 明示 + 監査アサート）、`test_reconstruction.py`（multi-hand 対応 + GREEN_CASES 13）
- docs: ADR-0047/0048/0049/0050、ADR-0033 追記、ISSUE-0009 追記、hand-reconstruction.md §5、
  event-replay.md §6.5（pin 規約）、measurement-plan §1.2（アライメント + GT 規約）、
  decision-log、CHANGELOG、CLAUDE.md

## Expected vs implemented

プランどおり。逸脱 2 点（いずれも ADR に明記）:
1. **S3 の flag 範囲を絞った** — 「to/by 両解釈が乖離したら flag」は全合法 raise が flag され
   review が飽和するため、「to 解釈が非合法・by 解釈なら合法」（従来なら無警告 snap）に限定。
2. **winner 保留の条件を緩めた** — 「進行中ハンドなし → 保留」だと明示 new_hand なし運用
  （既存テストが固定）が壊れるため、「アクション・カード・review 状態が何も無い」場合のみ保留。

## Test results

- `pytest tests/ --ignore=tests/test_vision.py`: **836 passed**（skip 0 維持）
- `ruff check .`: pass
- `python tools/calibrate_confidence.py`: P1〜P9 全 PASS（数値不変）
- `python tools/replay_hand.py` 相当（replay_fixture）: 13 fixtures 全緑 + round-trip 決定性
- テキスト駆動 E2E: 「シート1 レイズ 2千」→ 2000 円で記録（従来は 2 → min clamp 無警告）

## Mismatches / fixes

- 既存 pin の誤り: golden 5 件の stack_start/result/pot_total（S5/S6）と out-of-turn-rfid の
  call 額（B1）は誤値の pin だった → 手計算検証の上で再 pin（差分表 = ADR-0047）。
- `test_phase_d2_wiring` の 2 件は conf=None → 満点補完に依存していた → conf=0.9 明示 +
  監査フィールドのアサートを追加。
- recorder の最小長判定が末尾無音でかさ増しされるバグを新テストが検出 → 有音サンプル数で判定。

## Remaining gaps

- `engine.control_conf_threshold` の実運用値（既定 0 = off。実運用ログを見て設定）。
- pot_awards の viewer / リプレイ UI 表示（additive、未表示でも壊れない）。
- side pot ごとの個別勝者指定（ADR-0050 future scope）。
- バッチ 3（実データ後）: confidence 数値較正 / V2 複数アクション発話 / HMM 化の要否判断。
