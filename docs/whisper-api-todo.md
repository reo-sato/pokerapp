# TODO: Whisper の外部 API（OpenAI whisper-1）の利用

書き起こしを OpenAI の whisper-1 に移す計画（ADR-0072）の、残りの作業の一覧。決めたことと経緯は ADR-0072、並行している
ほかの作業は `docs/plan-board.md`（トラック H）。項目を済ませたら `[x]` にして日付を書く。

## いまの状態（2026-11-18）

- **段階 1 の「テスト中の形」は実装済み**（2026-11-09, ADR-0072 追記 2）:
  - 店舗 PC が whisper-1 を直接呼ぶ（`audio.transcriber=openai`。鍵は環境変数 `POKERAPP_OPENAI_API_KEY`）。
  - 失敗した発話は音声を残して、あとで送り直す。
  - 推定器は店舗サーバーでハンドごとに回す（`estimator.live`）。
  - 音声は 90 日で消す（`audio.keep_days`）。
- **店舗ではまだ試していない**。11/09 の店舗テストは前の設定（店舗 PC の Whisper・書き起こしは後回し）だった。
- 既定の設定は `audio.transcriber=local` のまま。

## A. 店舗テストの前の準備

- [ ] 店舗 PC に鍵を入れる。PowerShell で 1 回 `setx POKERAPP_OPENAI_API_KEY "<鍵>"` を実行し、ロガーを起動し直す。
  - 鍵はリポジトリ・`config.json`・ログ・チャットに置かない。
- [ ] 書き起こしの方式を切り替える: `python tools/set_config.py audio.transcriber openai`。
- [ ] ハンドごとの推定が有効か確かめる: `python tools/set_config.py estimator.live`（true のはず）。
- [ ] OpenAI の管理画面で、月の使用上限とアラートを設定する（ADR-0072 D6 の目安: 月 8 セッションで 1,000 円前後）。
- [ ] 鍵が無いとき・つながらないときの動きを、家で確かめる（鍵を外して起動する）。
  - 鍵が無い: 知らせを出して、店舗 PC の Whisper に戻る。
  - つながらない: 音声を残し、5〜60 秒おきに試し直す。そのあいだもブラインドの宣言は第 2 の耳で聞き分ける。

## B. 店舗テストで確かめる

- [ ] プレー中に声の入った記録が出るか。
  - `transcripts.jsonl` の `asr`（`openai-whisper-1`）の行で、話し始めから `heard_at` までの秒数を測る（目安 1 秒前後）。
- [ ] ハンドの数分後に推定が出るか。ハンドごとの秒数は `logs/store_server.log` で見る。
- [ ] 店舗 PC が重くならないか（Whisper を止めたぶん CPU が空くはず）。
- [ ] API の失敗と送り直し。テストの途中で Wi-Fi を一度切って戻し、残した音声が送り直されて記録に入るかを見る。
- [ ] 1 セッションあたりの費用の実績を、OpenAI の使用量の画面で見て D6 の目安と比べる。
- [ ] 精度: whisper-1 で録ったセッションを前向きの成績表（`tools/forward_ledger.py`）に足し、店舗 PC の Whisper のセッションと比べる。

## C. 道具・記録の足りないところ

- [ ] `tools/eval_store.py` に、書き起こしの方式（`transcripts.jsonl` の `asr`）ごとの「アクションの発話が読めた割合」を出す。
  - いまは方式を区別していない。
- [ ] セッションごとに、API に送った音声の秒数の合計をログに出す（費用の見積もりに使う）。

## D. 方針の確認（オーナー・公式の資料）

- [ ] whisper-1 の提供の終わりを公式で確かめる（一部の資料に 2027-02 とある。未確認）。
- [ ] 後継のモデルが出たら、同じ物差しで測る: `tools/cloud_asr.py` で書き起こし直し、`tools/bench_hands.py` に通す（ADR-0072 追記 1 の方法）。
- [ ] 代わりの候補 gpt-4o-mini-transcribe に、`audio.remote_model` で切り替えられることを確かめる。
  - 費用は whisper-1 の半分。成績は 11/05 で同じ、10/29 で少し下。
- [ ] OpenAI の API のデータの扱いを公式で確かめ、ディーラー用・プレイヤー用の紙の文面と合っているかを見る。
  - 確かめる点: 学習に使わない・保存は不正利用の監視のための最長 30 日。
- [ ] 保存しない申請（Zero Data Retention）をするか → オーナーに確かめる。
- [ ] オーナー待ち: `store-data` ブランチの音声（開発のデータ）を 90 日の約束に合わせるか。
- [ ] オーナー待ち: お客さんの画面に「推定」の印を出すか（いまは出していない）。

## E. 他の店に配る前（段階 2 = ADR-0072 D1 の元の形）

- [ ] ADR-0067 の 2e: Dockerfile・Cloud Build・Secret Manager・予算・D9 の確認。
- [ ] クラウドの中継: 店舗 PC → Cloud Run → OpenAI。
  - 鍵はクラウドの Secret Manager に置く。
  - 店舗の端末トークン（ADR-0067）で認証し、店ごとに上限を設ける。
  - 店舗 PC 側は `core/remote_asr.py` の送り先を切り替えるだけにする（同じ約束 = `OpenAITranscriber`）。
- [ ] 店舗 PC から鍵を消す手順を `docs/cloud-setup.md` に書く。
- [ ] 段階 2 の仕上げ: クラウドが落ちたときの扱い・API の失敗と再送・複数の店の上限。
- [ ] 推定をクラウドで回すかを決める（いまは店舗 PC でハンドごと。卓・店が増えたら比べ直す）。
