# Issue 0029: ラウンドを閉じたアクションが次ストリートに記録される（pokerkit backend）

## Date

2026-09-12

## Status

Fixed

## Severity / Priority

- Severity: High（**ハンド履歴そのものが間違う**。エラーも警告も出ず、後から気づけない）
- Priority: P1（live 既定 backend = pokerkit なので、実運用のログが常に影響を受ける）

## Area

integration（`integration/engine.py`）/ contracts（`action.schema.json` の `street`）

## Expected Behavior

`ActionRecord.street` は **そのアクションが行われたストリート**。BB のチェック（プリフロップを
閉じる）は `preflop`、フロップを閉じるコールは `flop`、ハンドを終わらせる fold はそのとき
プレイしていたストリート。

## Actual Behavior

実機 2026-09-12 の通しハンド（heads-up, SB=1/BB=2）を再現したもの:

```
  [preflop] 席2(b) call 1   pot=4  [要確認]
  [flop]    席1(a) check 0  pot=4     ← BB のチェックは preflop のアクション
  [flop]    席1(a) bet 5    pot=9
  [turn]    席2(b) call 5   pot=14    ← flop を閉じたコール
  [turn]    席1(a) bet 50   pot=64
  [showdown]席2(b) fold 0   pot=64    ← turn の fold
```

実機ログで「フロップで 席1 が check → 席1 が bet」と同じ席が 2 回続いて見えたのもこれが原因
（前者は本当は preflop の BB チェック）。

## Root Cause

`ActionRecord` を作るとき `street=gs.street` を **`apply_action` の後**に読んでいた
（rules-aware 経路 / legacy 経路の両方）。

- **legacy backend では無害**だった。`GameStateManager.apply_action` はストリートを動かさず、
  ストリートは RFID のボード枚数から `advance_street` で別途進むため、適用前後で値が同じ。
- **pokerkit backend では壊れる**。ベッティングラウンドが閉じると `apply_action` の内部で
  次ストリートへ自動進行するので、**ラウンドを閉じたアクションだけ**が次ストリートに記録される。

Phase G で live 既定を pokerkit に切り替えた時点から埋まっていた（ADR-0012）。golden fixtures は
ストリート境界を跨ぐケースを含んでいなかったため、テストでも検出できていなかった。

## Fix

適用**前**の `gs.street` を `street_at_action` に控え、`ActionRecord.street` はそれを使う
（`_handle_rules_aware_action` / `_handle_legacy_action` の両方）。`pot_after` / `stack_after` は
名前どおり適用後のままで正しい。legacy は適用前後で同値なので **挙動不変**。

合成 silent-fold（`_append_synth_fold`）は `apply_action` より前に記録するため元から適用前の
ストリートを見ており、変更不要。

契約側は `action.schema.json` の `street` に「行われたストリート（適用後ではない）」という
description を追加し `1.1` へ（validation 不変の additive, `versioning-and-freeze.md` §2）。

## Regression Test

`tests/test_action_street_label.py`:

- `test_heads_up_hand_labels_each_street_correctly` — 実機と同じ 6 アクションの street 列を固定。
- `test_round_closing_action_is_not_labeled_next_street` — backend が先に進んでいても記録は
  行われたストリート（最小形）。
- `test_legacy_backend_is_unaffected` — legacy は従来値（挙動不変）。

前 2 件は fix を戻すと落ちる（確認済み）。

## Affected Files

- `integration/engine.py`
- `docs/contracts/schemas/action.schema.json`
- `docs/contracts/versioning-and-freeze.md`
- `tests/test_action_street_label.py`

## Related

- ADR-0012（live 既定を pokerkit に切替 = 本バグの発生時点）
- ISSUE-0028（同じ実機通しで見つかった「ハンドが無いときのアクション」）
- `docs/worklog/2026-09-12-no-active-hand-guard.md`
