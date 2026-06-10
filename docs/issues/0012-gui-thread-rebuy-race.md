# ISSUE-0012: GUI/CLI スレッドが GameStateManager を直接変更しレースしていた

## Date

2026-06-08

## Status

Fixed（同日、review hardening タスクで修正）

## Expected

コーディング規約（CLAUDE.md）:

- 「スレッド間通信は `queue.Queue` のみ（共有変数の直接参照禁止）」
- 「GUI スレッドからビジネスロジックを呼ばない」

ゲーム状態の変更はすべて IntegrationThread に一元化され、GUI/CLI は queue 経由で
コマンドを送る（winner / new_hand は GUI でこのパターンに従っていた）。

## Actual

プロジェクト全体レビュー（2026-06-08）で検出:

1. **GUI リバイ**: `gui/dashboard.py:_cmd_rebuy` が GUI スレッドから
   `self._gs.rebuy(seat, amount)` を直接呼んでいた。`GameStateManager` は
   ロックを一切持たないため、IntegrationThread の `apply_action` / `end_hand` と
   並行実行されると stack 値の更新が競合し得る。
2. **CLI `r`（リバイ）**: `main.py run_cli` の入力ループ（メインスレッド）も同様に直呼び。
3. **CLI `n`（新ハンド）**: `game_state.new_hand()` を直呼びしていたため、レースに加えて
   IntegrationThread 側のハンドバッファ（`_current_actions` / `_stack_start` /
   board / hole_cards / session `assign_seat`）が**リセットされない**実バグがあった
   （GUI の新ハンドボタンは queue 経由で正しく `_start_new_hand` を通る）。

## Reproduction

- コードレビューで特定（`gui/dashboard.py:259` 旧実装、`main.py` 旧 `n`/`r` 分岐）。
- レース自体は timing 依存で再現困難だが、CLI `n` のバッファ未リセットは
  「`n` → アクション → `w` → `n` → `w`」で 2 ハンド目の `stack_start` が
  1 ハンド目開始時の値のままになることで観察できる。

## Root Cause

リバイ/新ハンドのコマンド経路が winner と異なり、queue を介さず共有状態を直接変更する
実装になっていた（Phase 4 GUI 実装時からの残存。規約は後から明文化されたが追従されていなかった）。

## Fix

- `integration/engine.py`: `AudioEvent(action="rebuy")` を処理する `_handle_rebuy` を追加。
  seat（`event.seat` / raw_text フォールバック）と amount を IntegrationThread 内で
  `gs.rebuy()` に適用し、`on_action` へ通知レコード（`action="rebuy"`, confidence=1.0,
  needs_review=False）を発行する。**`HandSummary.actions` には積まない**。
- `gui/dashboard.py:_cmd_rebuy`: 直呼びを廃止し、winner と同様に queue へ put
  （GUI 側は入力 validation のみ）。
- `main.py run_cli`: `n` / `r` を queue 経由に変更（`w` と同パターン）。

## Regression Test

- `tests/test_engine_rebuy.py`:
  - queue 経由の rebuy がスタックに反映され `on_action` 通知が出る
  - raw_text からの seat フォールバック / 不正席・非正値の拒否（クラッシュなし）
  - **rebuy が `HandSummary.actions` に積まれない**ことの固定
- 既存 `tests/test_gui.py`（ダッシュボードロジック層）は不変で緑。
