# Worklog: 段階 1 — 書き起こしを whisper-1 に・プレー中にハンドごとに推定（ADR-0072 追記 2）

## Date

2026-11-09

## Scope / Task

ADR-0072 の段階 1 を、オーナーの答え（テスト中は店舗 PC から API を直接呼ぶ・推定器は店舗 PC でハンドごとに）の形で実装する。
計画は `/root/.claude/plans/deep-wobbling-avalanche.md`（承認済）。

## Goal

- プレー中にロガーへ声が入る（書き起こしの後回しをやめる）。店舗 PC が書き起こしでビジーにならない。
- ハンドの数分後に推定がお客さん・スタッフの画面に出る。
- 録音の告知どおり、音声を 90 日で消す。

## Changed Files

- `core/remote_asr.py`（新）— OpenAI の whisper-1 の Transcriber（`WhisperTranscriber` と同じ約束）・`multipart`・`wav_bytes`・`post`。
- `audio/recorder.py` — API の Transcriber のとき、失敗した発話を音声ごと後回しの列へ・つながらない間は呼ばない（5 → 60 秒）・
  その間は第 2 の耳だけ（ブラインドの宣言）・知らせ・行に `asr`。
- `output/transcript_log.py` — 行に `asr`。
- `main.py` — `audio.transcriber=openai` の切り替え（鍵が無ければ Whisper に戻す）・音声の保存と送り直しの印を必ず。
- `tools/offplay_audio.py` — セッション後の書き起こしは API につながらなければ 1 件で止める。
- `tools/cloud_asr.py` — `multipart` を `core/remote_asr.py` と共有。
- `core/live_estimator.py`（新）— 店舗サーバーのハンドごとの推定の見回り・窓の中の入力の目印 `hand_marker`。
- `tools/estimate_logs.py` — `--hands`（指定したハンドだけ推して書き足す）・各ハンドに `inputs`。
- `core/audio_retention.py`（新）— 発話の音声を `audio.keep_days`（既定 90）日で消す。
- `api/store_server.py` — ハンドごとの推定と古い音声の削除を店舗サーバーの中で。
- `core/truth_hand.py`・`api/read_models.py` — `show_truth` のときも、真のアクションが無いハンドは推定を見せる。
- `config_default.json` — `audio.transcriber`・`audio.keep_days`・`estimator.live`。
- 試験: `tests/test_remote_asr.py`・`tests/test_live_estimator.py`・`tests/test_audio_retention.py`・`tests/test_truth_hand.py`・
  `tests/test_estimator.py`（指紋に入れないファイル）。
- 文書: ADR-0072 追記 2・`docs/usage.md`・`docs/troubleshooting.md`・`docs/plan-board.md`・CHANGELOG。
- 紙（リポジトリの外）: ディーラー用・プレイヤー用の PDF と Docs のページの録音の一文。

## Expected Behavior

- `audio.transcriber=openai` と鍵があれば、Whisper のモデルを読まずに API で書き起こし、声がすぐロガーに入る。
- API が落ちても録音・ライブの記録は止まらず、音声を残して後で送り直す。
- 店舗サーバーが、次のハンドが始まったらそのハンドを推し、`.estimate.json` に書き足す。入力が増えたら推し直す。
- お客さんの画面: 真のアクション → 推定 → （どちらも無ければ見せない）。

## Implemented Behavior

上のとおり。決めた細部:

- 推す時機: 次のハンドが始まったら（`<sid>.json` の次のハンドか `live_hand.json`）、無ければ確定から 60 秒。
- 推し直し: 窓の中の書き起こしの行（＋ハンドの間の発話）・events の数・窓が閉じたかが変わったとき。1 ハンド 3 回まで
  （失敗も数える）。目印は `tools/estimate_logs.py` も同じ関数・同じファイルから数えて推定のファイルに残すので、入力が同じなら
  一致する（店舗サーバーを起動し直しても、推し済みのハンドを推し直さない）。
- 子プロセスは Windows で BELOW_NORMAL_PRIORITY_CLASS + CREATE_NO_WINDOW、ほかは nice 10。5 分で打ち切り。
- いまのセッション = events を最後に書いたセッション（12 時間以内）。
- お客さんの画面に要確認の印・推定の印は出さない（ADR-0044 の移植でお客さんには隠した）。

## Tests

- 速い組 3695 passed・22 skipped。ruff OK。
- 遅い組のうち、足した試験（`test_live_estimator.py`・`test_estimate_logs.py`）は通過。推定器・engine は変えていない。
- 推定器の内容の指紋は変わらない（`ae111e4ee3ff`。推定器のファイルは触っていない）ので、物差しの書き直しは要らない。

## 確かめたこと（端から端まで）

### whisper-1 の本物の呼び出し（11/05 の 20 発話）

`OpenAITranscriber` → `parse_actions` → `apply_ear`（第 2 の耳は記録の点数）に通した。10/28 の比較の道具（`tools/cloud_asr.py`）
の結果と **20 件とも同じ文・同じアクション**。1 発話 中央値 1.26 秒・最長 2.26 秒（ここの環境から。声かどうかの判定は
faster-whisper が無いので切った）。

### ハンドごとの推定（11/05 のログをハンドごとに足しながら）

11/05 の 73 ハンドのログを、ハンド k の確定まで（＋ 次のハンドの配布から 5 秒まで）に切り詰めて写したフォルダで
`LiveEstimator.step()` を本物の実行役（子プロセスの `tools/estimate_logs.py --hands k --write`）で回し、セッションのあとで
一度に推した `.estimate.json` と比べた（ハンドの間の発話は無し・真のアクションは外した、両方とも同じ条件）。

- 73 ハンドとも推した（失敗 0）。**アクション・勝者・ボタン・要確認は 73/73 で同じ**。
- 違ったのは記録に見えないところだけ: ハンドの終わりの時刻（`ended_at`、窓の最後の観測）・注記の「使えなかった語」
  （窓の端の発話）・事後確率（最大 0.27 の差。画面には出さない）。
- 1 ハンドの時間（この環境の 4 コア、1 本ずつ）: 中央値 22 秒・最長 472 秒（ハンド 16）。最初の確認では全体の推定と同時に
  回したため、ハンド 4 が 300 秒の打ち切りに当たった → 打ち切りを 900 秒にし、同じ入力で失敗したハンドは推し直さないようにした
  （fa41fe4）。店舗 PC（8 コア・Whisper を止めたあと）では短くなる見込み。店舗テストで `logs\store_server.log` の秒数を見る。

## Mismatches / Fixes

- 店舗の設定は `viewer_api.show_truth=true`（お客さんには真のアクションだけ）なので、そのままでは推定が出なかった。
  `truth_view` を「真のアクション → 推定 → 見せない」にした（ライブだけのハンドは今までどおり見せない）。
- プレイヤー用の紙の「表示されるのは、スタッフが確かめたアクションです」を「まだのハンドは自動の推定です（違うことがあります）」に直した。

## Remaining Gaps

- 店舗テストで確かめる: プレー中の画面に声の入った記録と推定が数分で出るか・店舗 PC が重くならないか・API の失敗と送り直し。
- オーナーに確かめる: `store-data` ブランチの音声（開発のデータ）を 90 日の約束に合わせるか。お客さんの画面に推定の印を出すか。
- 他の店に配る前: クラウドの中継（鍵をクラウドに）・推定をクラウドで・ADR-0067 2e。
- whisper-1 の提供の終わりを公式で確かめる。

## Related Commits

- このコミット（`claude/confident-hawking-5e4hff` と `verify-v1`）
