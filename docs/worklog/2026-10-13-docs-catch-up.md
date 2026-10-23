# Worklog: 保留していた文書の更新（09/25〜10/08）と、10/06〜10/09 の直しの合流

## Date

2026-10-13

## Scope / Task

オーナー「テスト中はドキュメントの更新を軽くするといった処置で保留されているドキュメント更新はありますか？あれば全て更新して
ください」。店舗テストの軽い運用（CLAUDE.md §7b, 2026-09-25〜）で CHANGELOG と作業ログだけを更新していたため、CLAUDE.md・
usage・troubleshooting・installation・契約・ADR / ISSUE・decision-log が 09/25〜10/08 のぶん遅れていた。

## 見つけたこと

- 保留は約 40 項目。そのうち、文書がいまの動きと**逆のこと**を書いていた箇所:
  - 「同じコール / チェック / フォールドは 2 回目以降言わない」（09/29 に毎回言う運用へ変更）。
  - ショーダウンで「インポジションのマックではフォールドと言わない」（09/30 に、見せたあとの「フォールド」= 次の人のマックへ変更）。
  - `recording.enabled` を既定 off と記載（09/27 から既定 on）。
  - 仮の勝者を記録する（09/29 から、読めない札で勝者が変わりうるときはチップを動かさない）。
  - 読み取り装置の再起動は「USB の挿し直し」と案内（店の卓は ESP32 を給電アダプタで動かすので、USB の抜き挿しでは再起動しない = 10/06）。
  - CLAUDE.md の契約 v1.3・schema の版（hand 1.4 / reconstruction_event 0.4 など）。
- 記録の検査の表（`reconstruction_event.schema.json`）に、10/02 から記録している `parse_flags` の `garbled_digits` が無かった。
- **10/06〜10/09 の店舗テストの直し 40 コミットが開発版 verify-v1 に入っていなかった**（ISSUE-0040）。製品化の作業（10/11, 10 コミット）が
  verify-v1 の `f14671b`（10/03）から始まっていた。店舗 PC はオーナーによると作業ブランチの版で動いている。

## やったこと

1. **合流**（オーナー決定 10/13: 両方を合わせて verify-v1 にも入れる）:
   - マージ `d3e9071`。衝突は CHANGELOG（日付の新しい順に並べた）と pyproject（`dev` の xdist/timeout と `cloud` extra の両方）だけ。
   - 全体テスト 3259 passed（27 skipped = Postgres の試験。CI では回る）・ruff 通過。作業ブランチと verify-v1 に push（どちらも早送り）。
2. **小さなコードの直し**（`183cb4b`）:
   - schema 0.18 = `parse_flags` に `garbled_digits`。
   - 開発データの記録がすべて schema に通ること、読み取りが付ける印がすべて schema にあることをテストで確かめる（`tests/test_event_recorder.py`）。
   - 再起動の案内を「給電アダプタを抜いて数秒待ってから挿し直す」に（`rfid/reader_thread.py:_POWER_CYCLE`・`tools/rfid_relay.py`・`tools/probe_pcsc.py`）。
3. **文書**（ファイルが重ならないように 4 つに分けて書き、出典は各 CHANGELOG の節・作業ログ・コード・`config_default.json`）:
   - `docs/usage.md`:
     - 読み上げの基本を店の言い方に。札の離脱でフォールド。聞き違いとして読む語の表。ショーダウンの決まり。
     - CLI のコマンドの表（`blinds` / `button` / `w <席> <席>` / `rr` ほか）。ボタンの救済。リーダーの再起動と中継。
     - 読めない札（`??`）。真のアクションの入力画面（評価のモード・行ごとの ✓・評価から外す など）。台本・札の確認・読み上げ集。
     - 見直しのページ。ログを送る（分割の zip）。設定の表（audio・rfid・engine・`ground_truth.evaluation`、`recording.enabled` は既定 on）。
   - `docs/troubleshooting.md`:
     - マイク（名前で固定・開けるまで待つ・切れたら開き直す・RDP の WDM-KS）。聞き取りの遅れ（`cpu_threads`・`audio_check bench` / `gain`）。
     - RFID（RDP の `0x8010001D` → 中継、読めないリーダー → `rr` → 給電アダプタ、使えるリーダーの一覧）。
     - 席の設定の知らせ。読めない札。「Load failed」。ショーダウンの記述の誤りを直した。
   - `docs/installation.md`:
     - ショートカット 11 個の表。第 2 の耳（`.[ear]`・モデル約 170 MB・`models\` は更新でも残る）。
     - RFID の中継の登録・削除（管理者、ポート 8792 は 127.0.0.1 のみ）。
     - マイクを名前で固定。`swap-suits`。ファームウェアの書き換えの案内。ボードの設定例の誤り（廃止済みの `index` / `cards`）を直した。
   - `CLAUDE.md`:
     - 実装状況の表: ADR-0061・0062・RFID・recording・インストーラ・推定器の行を直し、5 行を足した（札の離脱でフォールド / 第 2 の耳と短い語の分類器 / 真のアクション入力の画面 / 評価の道具 / RFID の中継）。
     - ディレクトリ構成（`tools/` の節・足りないモジュール・ランチャ）。よく使うコマンド（テストは `-m "not slow" -n auto` / `-m slow`）。
     - エラーハンドリング方針。ディーラーボタンの節。§7b に今回の追いつきの 1 文。
   - 契約:
     - `docs/contracts/versioning-and-freeze.md`: hand 1.6・1.7、reconstruction_event 0.12〜0.18 の版の履歴。
     - `docs/contracts/event-replay.md`: §3 を 0.18 の形に。
     - `docs/contracts/rfid-usb-ccid.md`: §4 に読めないフロップの札の規則と中継、§6 に給電アダプタの注記。版は変えない。
   - ADR / ISSUE:
     - 新規: ADR-0068 卓の進行の決まり（オーナーの説明 R1〜R11）/ ADR-0069 第 2 の耳 / ADR-0070 RFID の中継とリーダーの再起動 / ISSUE-0039 店舗テスト 09/26〜10/07 の不一致の索引 / ISSUE-0040 直しが verify-v1 に入っていなかった。
     - 追記: ADR-0043・0061・0062（0061・0062 は「一部を ADR-0068 が置き換え」）、ADR-0034（ADR-0070 への 1 行）。
   - `docs/decision-log.md`: 上の行と、運用の決定（テストのダイエット = CI を速い組と遅い組に）の節。
   - `firmware/esp32s3-pn5180-ccid/README.md`: 書き込みのあとの再起動を給電アダプタの抜き挿しに。

## Test Results

- 全体テスト（合流後）3259 passed、ruff 通過。
- `tests/test_event_recorder.py`・`tests/test_contracts.py`・`tests/test_rfid_restart.py`: 通過。
- 文書に書いたファイルの道と config のキーの検査（scratchpad の小さなスクリプト）: 無いものは、型を表す書き方（`{hand,action}` など）と実行時に作られるファイルだけ。

## Remaining Gaps / 確かめていないこと

- 店舗 PC の `config.json` の実際の値（`cpu_threads=8`・`vad_threshold=0.8` は作業ログの値）。
- 店舗の ESP32 に v1.11（一覧と再起動の命令）のファームウェアがもう書き込まれているか。
- 契約 `rfid-usb-ccid.md` は注記だけで版を上げていない（v1.12 にするかは必要になったときに）。
- このリポジトリは浅い clone で 10/01 より前のコミットを引けないので、ADR-0068 の 09/25〜09/30 の Related Commits は作業ログを指す。
- ADR-0069 の未確認: 第 2 の耳（onnxruntime）のスレッド数で結果が変わらないか。

## Related Commits

- `d3e9071` 合流 / `183cb4b` schema と再起動の案内 / usage・troubleshooting・installation・CLAUDE.md・契約と ADR のコミット（このブランチの 10/13）。
