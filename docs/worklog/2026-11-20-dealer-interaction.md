# 2026-11-20 ディーラーへの働きかけ（ADR-0074）

## Goal

オーナー「現状の正答率は 48% だが、ディーラーの発声が不十分である場合が多いので、システムの正答率はそれなりに高いと思う。
各アクションの復唱の間をなるべく区切るなどの工夫とともに、ディーラーへのインタラクションを改善したい」。

オーナーの選択:
- その場でディーラーに返す画面は作らない。
- 音の合図は使わない。
- 11/19 の聞き分けはオーナーが行う。
- ディーラー名を記録に残す。

## Changed files

- `tools/listen_missed.py`: `--summary`（読めなかったフォールドを、札が離れた −1〜+6 秒の声の有無で分ける。種類ごと・時間帯ごと）。
- `docs/dealer-guide.html`（新）: ディーラー用の紙 1 枚（A4 1 ページに収まることを PDF で確認）。
- `docs/usage.md`: 「続けて言っても構いません」→「アクションごとに 1 拍（半秒以上）あけて言う」。
- ディーラー名:
  - `integration/engine.py`: `set_dealer`、次のハンドから。
  - `core/hand_log.py`: `HandSummary.dealer`。
  - `integration/logger_commands.py`: `dealer <名前>` と `control_line`。
  - `core/control_queue.py`: `set_dealer`。
  - `main.py`: 卓の設定の値を最初の入力に・`table.dealer`。
  - `tools/session_setup_ui.py`: 欄と検証。
  - `docs/contracts/schemas/hand.schema.json`: 1.13。
  - `staff/`: 卓を始める・ハンドのタブの「ディーラー交代」・型・mock。`api/static/staff/` を作り直し。
- `tools/dealer_report.py`（新）: 聞き取りの振り返り（ディーラーごと・1 時間ごと・`--brief`）。
- 試験:
  - `tests/test_listen_missed.py`
  - `tests/test_dealer.py`（engine・記録・replay）
  - `tests/test_logger_commands.py`（端末と制御の同一性に `set_dealer`）
  - `tests/test_session_setup_ui.py`
  - `tests/test_dealer_report.py`
  - `staff` の mock の試験
- ADR-0074・decision-log・CHANGELOG。

## 11/19 の数字

**聞き分けの自動の数**（`listen_missed.py --summary`）
- 読めなかったフォールド 2e59 は 94、dc91 は 45。
- 札が離れた前後に声があった: 78/94・43/45。
- 声ではない音だけ: 2・1。
- 離脱の記録なし: 14・1。
- 8 人卓は話し声が多く、自動では「言っていない」を分けられない。オーナーの耳で決める。
- 聞き分けのページ 2 枚をチャットで送った（音声が入るので公開しない）。

**振り返り**（`dealer_report.py`、ディーラー名なし）

| | 数 | 割合 |
|---|---|---|
| フォールドの声 | 254/413 | 62% |
| 補ったチェック / コール | 123/509 | 24% |
| 額の無いベット / レイズ | 5/165 | 3% |
| 続けて言った発話 | 183/719 | 25% |
| 5 秒で切れた発話 | 24 | — |
| 真のアクション: 読めた | 769/994 | 77% |

- 1 時間ごとの読めたコール: 19 時 100%・20 時 89%・21 時 54%・22 時 59%。後半ほど下がる（10/26 と同じ傾向）。
- `--brief` の「次に気をつける」は「フォールドは札が離れたのを見てから、1 人ずつ」。

## Expected vs implemented

- 計画どおり。`eval_store` へのディーラーごとの行は足さず、`dealer_report.py` が真のアクションの読めた割合も出す形にした
  （推定器の内容の指紋に入る `eval_store.py` を触らないため）。

## Remaining gaps

- オーナーの聞き分けの結果（`listen_*.json`）を `--read` でまとめ、`docs/whisper-api-todo.md` §B に書く。
- 次の店舗テスト:
  - 手引きを渡し、ディーラー名を入れて営業する。
  - 振り返りを 11/19 と比べる。
  - 前向きの 2 つの数字と並べて報告する。
- 推定器の内容の指紋は、engine・hand_log・control_queue の変更で変わる（値と結果は変わらない。評価は停止中）。
