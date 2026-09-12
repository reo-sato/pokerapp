# Issue 0034: RFID のログが CLI の入力行に割り込み、コマンドが通らない

## Date

2026-09-12

## Status

Fixed

## Severity / Priority

- Severity: High（実機テストで **`w 1` が一度も通らず、ハンドを確定できなかった** = ログが残らない）
- Priority: P1（実プレイ環境での検証を止める）

## Area

CLI（`main.py:run_cli`）/ integration（ログ水準）

## Expected Behavior

`--cli` は 1 つの端末で「入力プロンプト」と「別スレッドのログ」を共有している。
実プレイ中でも **打ったコマンドがそのまま通る**こと。

## Actual Behavior

実機 2026-09-12（8 席 + board、`audio.enabled=false`）:

```
w2026-09-12 16:00:43,843 [IntegrationThread] INFO integration.engine: Board card [pos=1]: 8c ...
2026-09-12 16:00:43,844 [IntegrationThread] INFO integration.engine: Street auto-advanced to river ...
 w 1
認識できません。コマンド: q / n / w <席> / r <席> <金額>、…
> w1
認識できません。…
```

`w 1` を 3 回打ってすべて弾かれ、**ウィナーを宣言できずハンドが確定しなかった**
（JSON にハンドが書かれない）。

ログの内訳（約 45 秒で 20 行以上）:

- `Board card [pos=1]: 8c` が **同じ位置・同じ札で何度も再検出**される（結合の弱いリーダーの
  間欠読み。位置は固定されているので記録上は無害だが、端末は埋まる）。
- `Street auto-advanced to river by RFID board cards (5 cards detected)` が **毎回出る**。

## Root Cause

3 つが重なっている。

1. **同じ位置・同じ札の再検出を INFO で出していた**（`_handle_board_rfid`）。新しい情報ではない。
2. **ストリート自動遷移が「遷移した」と毎回 INFO を出していた**。pokerkit backend では
   `advance_street` は **no-op**（ストリートはベッティング完了で進む、契約どおり）なので
   `gs.street` は変わらない。よって「既にそのストリート」の早期 return に一度も入らず、
   毎回「自動遷移した」と嘘のログを出し続けていた。
3. **コマンドと引数の間の空白を打ち損ねた**（`w1`）。ログが行に割り込む状況では起きやすい。

## Fix

1. **再検出は DEBUG に落とす**（位置と札が変わらないとき）。ボードが実際に変わったときだけ INFO。
2. **ストリートは実際に動いたときだけ INFO**。動かなければ DEBUG で
   「RFID street hint … — backend keeps …」と出す（嘘をつかない）。
3. **`w1` / `r1 500` を受け付ける**（`_normalize_cli_command` が `w`/`r` + 数字を分割。
   引数を取らない `q`/`n` は対象外、読み上げ文も対象外）。
4. **`--log-file` を追加**（既定 `logs/pokerapp.log`）。ログを端末から切り離せる。
   卓の状態は `tools/table_monitor.py` で見られるので、実機テストではログを端末に出す必要がない。

## Regression Test

`tests/test_main_audio_optional.py::TestCliCommandNormalization`:

- `test_glued_command_is_split` — `w1` / `ｗ１` / `r1 500` が分割される。
- `test_glued_split_does_not_touch_other_input` — `q` / `n` / `w 1` / 読み上げ文は不変。

ログ水準の変更は挙動（記録内容）を変えないため、既存 909 テストで回帰を担保する。

## Affected Files

- `main.py`（`_normalize_cli_command` / `_route_logs_to_file` / `--log-file`）
- `integration/engine.py`（`_handle_board_rfid` / `_try_advance_street_from_rfid` のログ水準）

## Related

- ADR-0045 D5（卓状態モニタ。ログを端末に出さずに卓を見る手段）
- ISSUE-0030（全角コマンド。同じ `_normalize_cli_command` で扱う）
- ISSUE-0026（board 位置の append-only。再検出が無害なのはこの設計のおかげ）
