# 2026-11-19 書き起こしの API（gpt-transcribe）で録る準備

## goal

オーナー「iPad からの操作の実装は後回しにして、テストのための変更を先に」→「Whisper API で録る準備」。
次の店舗テストを OpenAI の書き起こし（`audio.transcriber=openai`、既定 gpt-transcribe）で録れるよう、鍵の入れ方・
届くかの確かめ・iPad での見え方をそろえる（ADR-0072, `docs/whisper-api-todo.md` の A）。

## changed files

- `core/remote_asr.py`: `read_user_env` / `write_user_env`（Windows の HKCU\Environment）・`key_source`・`api_key` が登録も読む・
  `redact`（API の返す文の `sk-…` を伏せる。`post` の失敗の文に使う）。
- `tools/asr_check.py` + `asr_check.cmd`（新規）: `check`（既定）/ `set-key`（getpass → 登録 → check）/ `remove-key` /
  `use openai|local|cloud`。
- `audio/recorder.py`: `asr_health()`（方式の名前・API か・つながらない間か・送り直し待ち）。`main.py` が状態の `asr` に結ぶ。
- staff: `LoggerState.asr`・`asrLine`（+ 試験）・パネル・偽物版、`api/static/staff` を作り直し。
- 試験: `tests/test_asr_check.py`（新規）・`tests/test_remote_asr.py`（`asr_health`）・`tests/test_installer.py`（.cmd の一覧）。
- 文書: `docs/whisper-api-todo.md` の A（1 行のコマンド）・CHANGELOG。

## expected vs implemented

- 鍵は画面・履歴・チャット・リポジトリに出さない → getpass で入れて登録に書く。道具は値を表示しない（試験で確認）。
- `setx` の値は、前から開いていた窓・タスクの子には届かない → ロガーが登録を直接読む（PC の再起動が要らない）。
- 店舗で「届くか」を 1 回で確かめる → 保存した発話（`logs/audio/<sid>/*.wav`、聞き流した `off_` は除く、0.6 秒以上）を
  3 つ送り、秒数と文を出す。発話が無ければ 1 秒の無音。
- iPad で API が使われているか・つながっているかが分かる → ロガーの欄の「書き起こし」の行。

## test results

- `tools/asr_check.py` を開発の環境の鍵で実際に実行: gpt-transcribe に届いた（1 秒の音声で 0.85〜1.67 秒、文は空 = 無音）。
- `ruff check .`・`pytest -m "not slow" -n 4`・`pytest -m slow -n 4`（録音・main を変えたため）・staff の `tsc` / `npm test`（72）。

## mismatches / fixes

- なし。

## remaining gaps

- 店舗での確かめ（B: 遅れ・CPU・Wi-Fi を切ったときの送り直し・費用・精度）。
- gpt-transcribe の料金は未確認（目安は 1 分 $0.006 で仮に数えている）。
- 10/29 のハンド 35 の要確認の印なしの誤り。
