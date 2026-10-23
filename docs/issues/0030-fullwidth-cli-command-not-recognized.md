# Issue 0030: 全角の CLI コマンド（`ｎ`）が認識されない

## Date

2026-09-12

## Status

Fixed

## Severity / Priority

- Severity: Low（半角で打ち直せば通る）
- Priority: P2（マイク無し運用では日本語 IME のまま打つので毎回踏む）

## Area

CLI（`main.py:run_cli` の入力ループ）

## Expected Behavior

`--cli` のコマンド（`q` / `n` / `w <席>` / `r <席> <金額>`）は、**日本語 IME のまま打っても**通る。
読み上げ文（`チェック` 等）と同じ入力欄なので、IME を切り替えずに操作できる必要がある。

## Actual Behavior

実機 2026-09-12:

```
> ｎ
認識できません。コマンド: q / n / w <席> / r <席> <金額>、または読み上げ文（例: チェック / …）
> n
新ハンド開始を送信しました。
```

## Root Cause

コマンド照合が `line.split()` の生文字列比較だった。金額は `parse_amount` の `\d`（Unicode）と
`int()` が全角数字を解釈するため `ベット５` は通っていた（= 全角が通らないのはコマンド letter だけ）。

## Fix

**コマンド照合用にだけ**全角 ASCII（U+FF01..U+FF5E → `-0xFEE0`）と全角スペース（U+3000 → 半角）を
半角へ寄せる（`main._normalize_cli_command`）。

- 読み上げ文は **正規化前の行**を `parse_action` に渡す（`raw_text` を入力そのままに保つ）。
- カナ・漢字はこの写像の範囲外なので読み上げ文は素通りする。
- `Ｎ` のような全角大文字も、正規化 → `.lower()` の順なので通る。

ひらがな→カタカナ（ISSUE-0027）と同じ方針 = **表を増やさず入力側を正規化する**。

## Regression Test

`tests/test_main_audio_optional.py::TestCliCommandNormalization`:

- `ｎ` / `ｑ` / `Ｎ` → 半角 / `ｗ　１` `ｒ　１　５００` → 全角スペースも半角化 /
  半角はそのまま / 読み上げ文（`シート3 コール`）は不変。

## Affected Files

- `main.py`
- `tests/test_main_audio_optional.py`

## Related

- ISSUE-0027（ひらがなの読み上げ文。同じ「入力側を正規化する」方針）
- ISSUE-0028 / ISSUE-0029（同じ実機通しで見つかった 2 件）
