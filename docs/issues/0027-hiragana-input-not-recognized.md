# Issue 0027: ひらがなの読み上げ文が認識されない（「ちぇっく」が通らない）

## Date

2026-09-12

## Status

Fixed

## Severity / Priority

- Severity: Low（カタカナで打てば通る）
- Priority: P2（マイク無し運用の操作性。ASR の書き起こし揺れにも同じ経路で効く）

## Area

audio（`audio/recognizer.py:parse_action`）

## Expected Behavior

`--cli` の読み上げ文入力は、**IME 変換の有無にかかわらず**同じアクションとして解釈される。
`チェック` / `ちぇっく` はどちらも `check`。

## Actual Behavior

実機 2026-09-12:

```
> ちぇっく
認識できません。コマンド: q / n / w <席> / r <席> <金額>、または読み上げ文（例: チェック / …）
> チェック
  → check
```

## Root Cause

`core/constants.py:ACTION_KEYWORDS` は **カタカナ + 英字**のみ（`"チェック": "check"`, `"check": "check"`）。
`parse_action` は `text.lower()` して部分一致を取るだけなので、ひらがなは一致しない。
席表現（`_SEAT_PATTERN` / `_SEAT_NO_PATTERN`）も `シート` のカタカナのみ。

## Fix

キーワード表を増やす（ひらがな別名を全部書く）のではなく、**照合の前にひらがな→カタカナへ正規化**する
（`audio/recognizer.py:_to_katakana`）。表を 1 つに保てるので語彙の二重管理が発生しない。

- `U+3041..U+3096`（ぁ..ゖ）を `+0x60` してカタカナ帯に移す **1:1 写像**なので **文字位置が保たれる**。
  `parse_action` は正規化後の位置で最左キーワードを選ぶため、位置がずれると誤った採用が起き得る
  — そこを崩さないのが要点。
- 適用箇所は 3 つ: `parse_action` のキーワード照合 / `_strip_seat_references` / `_extract_seat_no`。
  金額（`parse_amount`）は数字・漢数字なので正規化不要（漢字はこの写像の対象外）。
- **`AudioEvent.raw_text` は正規化前のまま**（ログ・監査で実際の入力が分かるように）。
- ASR 側にも同じ経路で効く（Whisper が「ちぇっく」と書き起こしても拾える）。

## 副作用の検討

ひらがな語がカタカナ語として誤マッチする可能性はある（例: 「あるこーる」→「アルコール」に
`コール` が含まれる）。ただし **カタカナの「アルコール」は正規化前から既に `コール` に一致する**ので、
本 fix が新たに作るリスクではない（既存の部分一致仕様に内在する性質）。ディーラーの読み上げは
定型文なので実害はないと判断した。厳密化が必要になったら、キーワードを語境界付きの正規表現に
変える方が筋が良い（本 issue の範囲外）。

## Regression Test

`tests/test_main_audio_optional.py::TestDummyActionVocabulary`:

- `test_hiragana_is_normalized_to_katakana` — ちぇっく / こーる / ふぉーるど / れいず /
  おーるいん / うぃなー / はんど開始
- `test_hiragana_keeps_amount_and_seat` — `べっと 500` → 500 / `しーと3 こーる` → seat 3 /
  `しーと1 れいず 800` で席番号を金額に誤採用しない
- `test_raw_text_keeps_original_form` — `raw_text` は正規化前

## Affected Files

- `audio/recognizer.py`
- `tests/test_main_audio_optional.py`

## Related

- ISSUE-0024 / ADR-0053（マイク無し運用の入口。`--cli` の読み上げ文投入は同タスクで追加）
