# Changelog

本ファイルは [Keep a Changelog](https://keepachangelog.com/) の慣習に沿って、
ユーザー可視の挙動変更および仕様 / docs の重要更新を記録する。
詳細な経緯は `docs/adr/` / `docs/worklog/` / `docs/issues/` を参照。

## [Unreleased]

### Changed (firmware: 定常状態の inventory を「前回 UID の狙い撃ち probe」に — 満載 1 周 538 ms の対策, ISSUE-0021, 2026-09-11)

- **実機で満載（8 席 × 2 枚 + board）の 1 周が 538 ms**（目標 ≤ 300 ms）。原因は **`RX_COLL_POS` が
  実機では一度も使えない**こと: ISO15693 は UID を LSB-first で送るので衝突は必ず UID 先頭バイトで
  起き、そのとき PN5180 が返す受信は 1 byte（flags だけ）で prefix を作れない（`coll_pos fallback` が
  10 秒で 255 回 = 全滅）。1 bit DFS は衝突位置より手前で割るので **必ず片方の子枝が空**になり、
  その probe が RX timeout(8 ms) を丸ごと待っていた。
- **狙い撃ち probe**（`PN5180_FAST_TARGETED_PROBE`, 既定 on）: 前回見えていた UID を
  **`mask_len=32` の完全一致 inventory** で 1 枚ずつ直接呼ぶ。合致する札は最大 1 枚なので
  **衝突が起きず即答**。当たった札は Stay Quiet し、続く root probe には新しい札だけを残す。
  定常状態は **N+1 probe**（N = 載っている枚数）に固定され、DFS と空枝の timeout が消える。
- **応答待ちの上限をフレーム長に連動**（`tx_us + PN5180_FAST_RX_TIMEOUT_MS`）。26.48 kbps で 1 byte
  ≈ 0.30 ms なので、mask を伸ばした probe に固定値を使うと応答の直前で打ち切る（= 狙い撃ちが常に
  外れる罠）。`PN5180_FAST_RX_TIMEOUT_MS` の意味は「応答ぶんの予算」に変わった。
- **新しい札の検出は毎 poll に戻る**（`PN5180_FAST_CONFIRM_EVERY` の間引きが定常状態では発動しなくなる
  ため、capture で隠れた札が最大 5 poll 遅れる副作用が消える）。
- poll 統計に **`狙い撃ち H/P 命中`** と **`応答待ち最長 x.x ms`**（応答が返った probe のみ）を追加。
  後者は RX timeout を詰めるための計器。
- **実機効果（`047ee1e`, 10 台・満載 18 枚）: 538 → 433 ms**。狙い撃ち **命中 340/342（99.4%）**、
  `coll_pos fallback` 255 → **0**、`probe 最大` 6 → **3**、`応答待ち最長 7.4 ms`（フレーム長連動の
  上限が正しかったことの裏取り）。
- **簡略サイクル**を追加（目標 ≤ 300 ms への詰め）: Stay Quiet は「root probe で新しい札だけを見るための
  下準備」なので、root を送らないサイクルでは不要。狙い撃ちが使える reader では
  `PN5180_FAST_CONFIRM_EVERY` 回に (N-1) 回を **狙い撃ちだけで終える**（2 枚なら ≈ 43 → 17 ms）。
  `PN5180_FAST_CONFIRM_EVERY` 5 → **3**、`CARD_POLL_INTERVAL_MS` 100 → **50**。
  統計に **`簡略 N/M reader周`** を追加。見積り 平均 ≈ 320 ms（N=3）。
  **代償**: 既に札がある reader に増えた札の発見が最大 N poll 遅れる。**空の reader は簡略に入らない**
  ので、席の 1 枚目 / flop / turn / river は毎 poll 検出（遅れるのは「席の 2 枚目」だけ）。
- **配線表を再振替**（ISSUE-0023）: BUSY 不通の コネクタ #4(ch3) / #11(ch10) を予備に降格し、
  **board2 = #12(ch11) / board3 = #13(ch12)**。席 1..8 と board1 は変更なし。host config は不変。
  修理しない方針（ハーネスの BUSY 圧着に系統的な弱さがあるため）。
- **簡略サイクルの位相を reader ごとにずらす**（11 台満載の実測 `238/324/**495** ms` への対処）。
  原因は簡略サイクルが **全 reader で lockstep** していたこと: 旧実装は「完全確認をした周に 0 に畳む
  連続カウンタ」なので、**ハンドの切れ目で全 reader が 0 枚になると位相が揃い**、1 周が
  「全部簡略（238 ms）」か「全部完全確認（495 ms）」に振れていた（検算 `(2×238+495)/3 = 323.7` =
  実測 avg 324）。完全確認するのは **`(poll 周回 + reader index) % N == 0` の reader だけ**にし、
  位相源を **poll 1 周の末尾で +1 する自由走行カウンタ**にした（0 枚を跨いでも崩れない。
  周期はどの reader も厳密に N、1 周に完全確認するのは `ceil(11/N)` 台だけ）。
- **`PN5180_FAST_CONFIRM_EVERY` 3 → 6**。実測から出した 1 台あたりの内訳（簡略 21.6 ms /
  完全確認 45.0 ms）による予測 1 周は N=3 → 308〜331 / N=4 → 284〜308 / N=5 → 284〜308 /
  **N=6 → 261〜284 ms**（max が初めて 300 ms 未満になる最小の N。7 以上は max が縮まない）。
  遅れの上限は N 周 ≈ 1.7 s で、対象は「**既に札がある** reader に増えた札」だけ
  （空の reader は毎周完全確認 = 席の 1 枚目 / flop の 1 枚目 / turn / river は影響なし）。
  札の離脱は狙い撃ちが毎周走るので N に影響されない。
- スタブ 59 構成 警告 0（`CONFIRM_EVERY` 0/1/2/3/6 を含む）+ simulator（**位相ずらし: 1 周の完全確認は
  `ceil(11/6)=2` 台以下 / 6 周で全 reader がちょうど 1 回ずつ** / 定常 = 簡略 (N-1) + 完全確認 1 /
  狙い撃ちは全部 `mask_len=32` / 復帰は N poll 以内 / 既存シナリオ 失敗 0）+ pytest 823 passed。
  **位相ずらしと N=6 は実機未検証**。worklog `docs/worklog/2026-09-11-pn5180-targeted-probe.md`。

### Fixed (firmware: 1 台の init 失敗で全 reader が止まらないように — 全 NSS High 固定 + 再試行 + 個別 skip, ISSUE-0023, 2026-09-11)

- **実機 10 台接続で reader 0 の `pn5180_init` が firmware version 読み取り失敗**（応答 `FF FF`）になり、
  ドライバの失敗経路が共有 SPI を壊して全 reader が停止した（直後の診断では同じ chip が `FW=00 04` で
  応答 = chip は生きている。RST 診断は `during_rst=0` で前日の `1` と違う。根本原因は実機で未確定）。
- **全 NSS を init 前に output High で固定**（配線表 13 本すべて、範囲外の予備も）。init していない
  chip の NSS が floating で「選択された」と解釈し MISO を駆動する衝突経路を塞ぐ。
- **init 失敗時は共有 SPI を作り直して 1 回だけ再試行**し、それでも失敗した reader は **skip して他は続行**
  （`PN5180 ready: N/11 reader（skip: #5(init失敗), …）`）。以前は 1 台失敗 = 全台停止だった。
- 診断: RST 診断が `during_rst=0 かつ after=0` なら「RST がこの chip に届いていない可能性（コネクタの
  RST ピン/配線）」、init 失敗診断で SPI 応答ありかつ RST 診断 0 なら同じ疑いを明示。
- docs: ISSUE-0023（新規, Open）/ worklog `docs/worklog/2026-09-11-pn5180-init-resilience.md` /
  firmware README・checklist §8 の「1 台失敗 = 全台停止」記述を更新。スタブ 32 構成 警告 0 +
  simulator（再試行・skip・NSS High・SPI 作り直し）12 項目 ✅。
- **実機確認（2026-09-11, `076b844`）**: 同じ 10 台接続で reader 0 が 1 発目で init 成功（再試行なし）、
  `PN5180 ready: 9/11 reader（skip: #4, #11）`、9 台すべてでカード検出。**多台数の初実測 = 9 台で
  1 周 124〜139 ms**（1 台 ≈ 14 ms、11 台なら ≈ 155 ms。ISSUE-0021 実機フィードバック 4）。
  `RST診断(ch0)` の `during_rst=0` は残る（reader 0 は動作、ISSUE-0023 Open）。
- **配線表を実機の挿し方に振替**（`app_config.h` `PN5180_READERS`）: コネクタ #4（ch3）は BUSY 不通なので
  飛ばし、#1,#2,#3,#5,…,#12 の若い順 = index 0..10（席 1..8 / board1..3）。#4 の行は予備に。
  起動要約の skip 一覧に `chN` を併記（`skip: #10(ch10)`）。host config は不変。
- **skip した reader の chip 生存確認を起動ログに追加**（ISSUE-0023 Fix 2）: BUSY が floating で skip した
  reader ごとに、BUSY 非依存の SPI（`READ_EEPROM(FIRMWARE_VERSION)` 1 発）で chip の生死を判定し
  「**BUSY 線だけが不通**（`FW=xx xx`）」/「**電源か SPI 線か chip 個体**（`FW=FF FF`）」を WARN で出す。
  実機で reader を挿し替えたら floating が付いてきた（= reader 1 台の故障で確定）が、そこから先を
  手で当たるしかなかったため自動化した。NSS スキャンの 1 候補ぶんを `spi_probe_nss` に切り出して共用。
- **実機確認（2026-09-11, `0e93de4`）**: `PN5180 ready: 10/11 reader（skip: #10(ch10)）`（コネクタ #11 のみ
  未通電）。**reader を #11↔#12 で入れ替えると floating も ch10→ch11 に移動** = コネクタは両方正常で
  この時点では **reader 1 台の故障**に見えた（後述のとおり後で覆る, ISSUE-0023）。
- **chip 生存確認の実機結果（`405f218`, ch10 に新品 reader）**: `FW=00 04 = chip は生きている →
  BUSY 線だけが不通`。電源と SPI は届いており **不通は BUSY 1 本**に絞れた（診断は意図どおり動作）。
  位置を元に戻した状態なので ch10 は **3 台のうち 2 台で floating** = **コネクタ接点/圧着の間欠不良が
  第一仮説**（第二は新品 reader のピン配列差）。空き ch12 に挿して MUX scan の ch12 の桁を見れば決まる
  （再ビルド不要）。
- **10 台の poll 実測**: カード無しで **1 周 138 ms**（1 台 ≈ 13.8 ms、11 台なら ≈ 152 ms）。1 周は
  スキャン時間のみで `CARD_POLL_INTERVAL_MS`(100ms) は別途加算。1 台ぶんの内 ≈ 8 ms は
  `PN5180_FAST_RX_TIMEOUT_MS`（ISSUE-0021 実機フィードバック 5）。**host 側の契約 v1.2 経路を初めて多台数で通し確認**: `probe_pcsc list` =
  `physical readers: 11` + 11 件 matched / `check` = 11 行 PASS / `watch` = 席 8 台 × 2 枚 + board で
  22 タッチ、`seat 4 [r3]` も振替どおり（ISSUE-0022）。

### Changed (firmware: CCID slot を 1 つに固定し、物理リーダーを Get UID の P2 で選ぶ — 契約 **v1.2** / firmware 側, ADR-0041 / ISSUE-0022, 2026-09-10)

- **firmware 側の v1.2 実装**（host 側は次の項）: `CCID_SLOT_COUNT` は **1 固定**（`bMaxSlotIndex=0`、
  `bcdDevice=0x0201`。物理 reader 台数を変えても USB 記述子は変わらないので Windows の記述子
  キャッシュ問題が起きない）、物理 reader 台数は新設の **`PN5180_READER_COUNT`（既定 11）**。
  Get UID は **`FF CA 00 <k> 00` の P2 = reader index k** で reader k の UID（複数枚は 8B 連結・
  昇順）+ `90 00`、無しは `6A 81`、**`k >= 台数` は `6A 86`**、**`FF CA 00 FF 00` は `<N> 90 00`**。
  `k=0` は従来と同一バイト列（1 台構成の挙動は不変）。
- **起動ログ**: `1 CCID slot, 11 physical reader(s)` / `PN5180 ready: N/11 reader（skip: …）`。
  未通電で skip した index は範囲内なので常に `6A 81`（`6A 86` は config の番号が台数を超えたときだけ）。
  1 台も上がらないときも NSS スキャン診断（BUSY 非依存）を出すようにした（複数 reader 構成では
  全台が MUX scan で skip され、従来は診断が出なかった）。
- **coll_pos の安全弁**: 衝突位置を採用した分割で **3 ラウンド連続** 札 0 枚なら `RX_COLL_POS` の
  基準ズレとみなし、以後は 1 bit ずつ伸ばす DFS に固定（WARN 1 回、取れる UID は同じで遅くなるだけ）。
  1 回だけの空振り（hole card 2 枚を同時に持ち上げた過渡）では発動しない。
- docs: firmware README / `docs/rfid-ccid-firmware-checklist.md`（§2/§4/§8 を v1.2 に）/
  ISSUE-0021 の用語注記 / worklog `docs/worklog/2026-09-10-pn5180-reader-index-p2-firmware.md`。
  スタブ 128 構成で警告 0 + register-level simulator（APDU 応答 4 種 + 安全弁）で確認。**実機未検証**
  （次: 焼いて `probe_pcsc list` の `physical readers: 11`、`watch` で index 0 と 10 が別々に発火）。

### Changed (RFID: 物理リーダーは Get UID の P2 で選ぶ — 契約 **v1.2** / host, ADR-0041 / ISSUE-0022, 2026-09-10)

- **背景**: Windows の Microsoft 汎用 CCID ドライバは **1 インターフェース 1 slot** しか公開せず、
  firmware を 2 slot（`bMaxSlotIndex=1`）にしても PC/SC には `PokerRFID PN5180-CCID 0` しか現れない
  （実機で確定, ISSUE-0022）。slot ごとに USB インターフェースを分ける回避策も ESP32-S3 の
  endpoint 数（6 本）で最大 5 台までで、本番 11 台（席 8 + board 3）に届かない。
- **設計変更（ADR-0041 / 契約 v1.2）**: **CCID slot は常に 1 つ**（PC/SC の reader 名も 1 つ）にし、
  **物理リーダー k は Get UID の P2 で選ぶ**（`FF CA 00 <k> 00`。範囲外は `6A 86`、台数問い合わせは
  `FF CA 00 FF 00` → `<N>` + `90 00`）。`k=0` は従来の `FF CA 00 00 00` と同一なので **1 台構成の
  挙動は不変**。v1.1 までの「slot ごとに reader 名を分ける」規約は廃止。
- **設定（要更新）**: `config.rfid.pcsc_readers[]` に **`reader`（物理リーダー番号 0 起点・任意・
  既定 0）** を追加。本番 11 台は **`name` を全要素で同じ**にして `reader` を 0..10 にする
  （`config_default.json` のサンプルを更新）。一意性は `name` から **`(name, reader)`** へ。
  既存の 1 台構成 config（`reader` 無し）はそのまま動く。
- **host**: PC/SC 接続を **reader 名ごとに 1 本持続**し、そこに N 個の Get UID を流すようになった
  （poll ごとの connect/disconnect が消えて 11 台でも軽い。失敗時は接続を捨てて次 poll で再接続）。
  `reader` が firmware の台数を超えていると WARN（`6A 86`）を 1 回出して空扱い。
- **ツール**: `probe_pcsc list` が `physical readers: N` と台数超過の警告を表示 /
  `probe_pcsc check` が connect + Get UID の SW で PASS/FAIL（`6A86` は「範囲外」で FAIL）/
  `probe_pcsc watch` のラベルが `seat 1 [r0]` / `probe_pcsc raw --reader <k>`（reader 名は `--name` に移動）/
  `register_cards run --reader "seat 1"` で config 要素（= 物理リーダー）を選べる。
- **Fixed**: GUI モード（`python main.py`）で `transport="pcsc"` のとき `RFIDThread` に HTTP 用の
  `readers`(dict) を渡していて RFID スレッドが起動しなかった（CLI 経路のみ ADR-0034 で修正済だった）。
- docs: 契約 `docs/contracts/rfid-usb-ccid.md` **v1.2**、ADR-0041、ISSUE-0022、
  `docs/hardware-qa-checklist.md`（手順 1/2/3/4 + 受け入れ基準）、`docs/installation.md`、
  worklog `docs/worklog/2026-09-10-rfid-reader-index-p2-host.md`。823 passed。
  **残**: 実機 2 台 → 11 台の通し QA（firmware 側の P2 実装は上の項）。

### Changed (firmware: 重ね置きの読み取りを高速化 — 衝突位置 DFS + 確認 probe 間引き + ノイズ再試行, ISSUE-0021, 2026-09-10)

- **3 枚重ねの 1 周が ≈150 ms → 目標 ≤ 60 ms（1 slot）**。実機で 3 枚重ねの probe が 14 回に
  なっていた（UID を LSB-first で見ると 2 枚の下位 5 bit が同一で、mask を 1 bit ずつ伸ばす
  anti-collision が「札のいない枝」を毎回 RX タイムアウトぶん待っていた）。`RX_STATUS` の
  **衝突ビット位置まで mask を一気に伸ばす**ようにして 3 枚 = 6 probe / 2 枚 = 4 probe に短縮。
  衝突位置が取れない・辻褄が合わない場合は従来の 1 bit 伸ばしに自動フォールバックする
  （基準は実機未確認のため。起動後 reader ごと 3 回だけ判定ログを INFO で出す）。
- **札が動かない間は「もう居ない」確認の問い合わせを間引く**（`PN5180_FAST_CONFIRM_EVERY=5`、
  0 で従来動作）。11 台すべてに札が載っていると確認だけで ≈90 ms/周かかるため。カードの
  増減があった周は従来どおり必ず確認する。
- **カードを動かしている最中に 1 周が 160 ms まで伸びる問題を修正**。磁界の縁での壊れた受信
  （衝突フラグ無しの CRC エラー）を「複数枚」と解釈して探索を上限まで広げていた（1 枚しか
  載せていないのに問い合わせ 16 回 = 上限）。壊れた受信は **同じ条件で 1 回だけ再問い合わせし、
  それでも駄目なら「無し」** として扱う。
- 応答待ちの上限を 10 → 8 ms に（実応答は ≈5.5 ms）。`poll 統計` に
  `coll_pos fallback N` / `ノイズ再試行 N` を追加（実機での効き具合を見るため）。
- 取得できる UID・枚数・順序と host 契約（v1.1 §6/§7）は変更なし。**実機未検証**
  （スタブ 128 構成コンパイル + register-level simulator で確認）。
  docs: ISSUE-0021（実機フィードバック 3 + Regression Check）、firmware README、
  firmware checklist §8、worklog `docs/worklog/2026-09-10-pn5180-collpos-dfs.md`。

### Fixed (firmware: 重ね置きの 3 枚目が読めない / 枚数がちらつく — Stay Quiet 再 probe + UID 単位 hold, ISSUE-0021, 2026-09-10)

- **3 枚重ねが読めるように（capture effect 対策）**: 実機で 2 枚が同時応答しても PN5180 が衝突を
  検出せず**強い方だけを復号**することがあり、その枝が 1 枚で確定するため弱い札が anti-collision の
  探索に現れなかった（3 枚重ねで `3 枚` が一度も出ない）。firmware の高速 inventory を、
  **見つけた札に STAY QUIET を送って黙らせ、root を再 probe して残りを拾う**ループに変更した。
  quiet は RF off で解除されるため、fast 経路は inventory の最後に必ず RF を落とす。
- **枚数のちらつきを解消**: カード保持（debounce）を **UID 単位**にした。従来は「検出 0 枚のときだけ
  前回の集合を保持」だったため、2 枚中 1 枚を 1 回取りこぼすと host に「1 枚」が即座に伝わり、
  2↔1 が数百 ms 周期で往復して同じ札が何度も再発火していた。今は**欠けた 1 枚だけ**を 3 サイクル
  保持する（UART ログ末尾に `(hold n)`）。
- **衝突判定の取りこぼしを修正**: 「衝突フラグあり・受信 0 byte」で返る衝突を「カード無し」と
  判定していたため、重ね置きの分割が起きないことがあった。**衝突フラグを受信バイト数より先に**
  見るようにした。
- 併せて `PN5180_FAST_MAX_PROBES` を 12 → 16（root 再 probe ぶん）、Stay Quiet が効かない札で
  probe 上限まで空回りしないよう「新しい UID が増えないラウンドが続いたら打ち切る」を追加。
- **実機 1 slot の実測**: カード無しで **1 周 15 ms**（従来 176〜890 ms）、1 枚 / 2 枚重ねは読み取り OK。
  上記の 3 枚重ね・ちらつきの改善は **実機確認中**（host / 契約 v1.1 側の変更は無し）。
  docs: ISSUE-0021（実機結果 + 追加 Root Cause + Regression Check）、firmware README、
  firmware checklist §8、worklog `docs/worklog/2026-09-10-pn5180-stay-quiet-per-uid-hold.md`。

### Fixed (firmware: Stay Quiet の送信完了待ちが短すぎてタグに届かない, ISSUE-0021, 2026-09-10)

- 実機（1 slot, 3 枚重ね）で `3 枚` は読めるが 1 周が ≈170 ms（カード無し 15 ms）だった。Stay Quiet
  フレーム（12 byte）は 26.48 kbps で送信に ≈3.7 ms かかるのに、送信完了待ちの上限が 3 ms で、次の
  INVENTORY の `pn5180_sendData`（idle→transceive）が**送信中のフレームを打ち切っていた**ため quiet が
  効かず、毎 poll probe 上限（16 回）まで空回りしていた。上限を 10 ms にし、タグの処理時間（t1）ぶん
  500 µs 空けてから次の要求を送る。
- `poll 統計` に `probe 最大 N 回/reader` を追加（Stay Quiet 不発や衝突の空回りをログで見えるように）。
  期待: カード無し 1 / 1 枚 2 / 2 枚 3〜5 / 3 枚 4〜8。16 に張り付くなら quiet が効いていない。

### Changed (firmware: 起動時 MUX scan の前に共有 RST を 1 回叩く, 2026-09-10)

- `pn5180_reader_init` は MUX 全 ch 走査の**前に**共有 RST を pulse して全 PN5180 を idle（BUSY=Low）に
  揃えるようにした。電源投入直後や前回稼働の途中状態では BUSY が High のままのチップがあり、そのまま
  走査すると通電中の ch を floating と誤読して bring-up の自動選択（`select_bringup_reader`）が外れる
  （実機で ch0 に挿した reader が scan では全 `1` になり、設定既定が ch0 だったため偶然 init できていた）。

### Changed (firmware: init 失敗時の NSS 診断を BUSY 非依存に, 2026-09-10)

- PN5180 の初期化に失敗したときの NSS スキャンが、**BUSY が High（floating/stuck）だと SPI を
  一度も送らずに終了**していたため、「PN5180 が死んでいる」のか「BUSY/MUX 経路だけが壊れている」
  のかを切り分けられなかった（実機で発生）。BUSY ハンドシェイクの代わりに固定待ち 1 ms を使い、
  **どの候補にも必ず READ_EEPROM（firmware version）を送って応答を `FW=xx xx` で表示**するようにした。
  応答があれば「PN5180 は生きている → BUSY/MUX 経路（MUX VCC/EN/SIG・その ch の BUSY 線）を疑う」、
  全候補が `FF FF`/`00 00` なら「電源/RST/SPI 配線」と結論をログに出す。候補は配線表
  `PN5180_READERS` の 13 本すべて（ハードコード配列を廃止）。起動時の診断ログのみの変更。

### Added (RFID: 1 リーダーに複数枚を重ねて置ける — 席 2 枚 / フロップ 3 枚, 契約 v1.1, 2026-09-10)

- **重ね置き対応（host）**: 1 つの reader に複数カードが載ったとき、Get UID 応答が **8B UID × 枚数の連結**
  で返る（firmware, 最大 4 枚）。host は応答長 16/24/32 のときだけ 8 バイトずつ分割し
  （`rfid/bridge.py:split_uid_response` / `PCSCBridge.read_uids`）、**UID 単位のデバウンス**（reader ごとの
  UID 集合の差分）で増えた UID ごとに `RFIDEvent` を 1 件出す。置きっぱなしは再発火せず、1 枚だけ外して
  戻すとその UID だけ再発火する。1 枚運用の挙動は従来と同一。
- **board の `cards`（位置割り当て）**: `config.rfid.pcsc_readers[]` に任意の `cards`（1..5, 既定 1）。
  board reader は `[index, index+cards-1]` を占有し、検出順に `board_index = index + offset` を割り当てる
  （外して戻すと同じ位置に戻る。`cards` 超過は WARN + 位置なし）。ボードの street 自動遷移（3/4/5 枚）は不変。
- **本番 11 slot を既定サンプルに**: `config_default.json` の `pcsc_readers` を席 8 台
  （`PokerRFID PN5180-CCID 0..7` = seat 1..8）+ board 3 台（`8` = flop 3 枚重ね / `9` = turn / `10` = river）に。
- **tools**: `probe_pcsc check` の lint が `cards`（1..5 / `cards>1` は index 必須 / `index+cards-1` が 5 超 /
  board 位置の重なり）を検出、`watch` は `board 1-3` 表記、`raw` は複数 UID を `UID×k = A, B, C` で表示。
  `register_cards run` は**複数枚が載っている間は登録せず** `⚠ N 枚検出 — 1 枚だけ置いてください` で待つ。
- **契約 `docs/contracts/rfid-usb-ccid.md` を v1.1 に（additive over 1.0 frozen）**: §3 本番 11 slot /
  §4 `cards` と位置割り当て / §6 複数 UID 連結 / §7 UID は MSB-first（firmware が ISO15693 の LSB-first を反転）/
  §8 UID 単位デバウンス / §10 v1.1 の内容。v1.0 の要求は不変（後方互換）。docs: hardware QA チェックリスト
  （11 件 config 例・重ね置きの期待値）、installation、worklog `docs/worklog/2026-09-10-rfid-multi-card-host.md`。
- **firmware 側の対応**（anti-collision の mask DFS + Get UID 連結 + 高速 inventory, ISSUE-0021）は
  別途実装。**実機未検証**（重ね置きの通し確認は Phase H の残作業）。

### Added (firmware: PN5180 複数 reader（最大 13 台）への拡張準備, 2026-09-10)

- **RF 時分割**: 各 reader の inventory 直後に `pn5180_setRF_off()` を呼び、**同時に RF 磁界を張るのは
  1 台だけ**にした（`PN5180_RF_OFF_BETWEEN_READERS`, 既定 1）。ドライバの `get_all_uids()` は RF を ON の
  まま戻るため、13 台では干渉と電流の積み上がりになる。
- **未通電 reader の skip（部分成功）**: `CCID_SLOT_COUNT > 1` のとき、起動時の MUX scan で floating の
  ch は `pn5180_init` を呼ばずに飛ばし、残りの台で起動する（`PN5180 ready: N/M slot（skip: …）`）。
  併せて **配線チェック**ログ（設定 ch なのに floating / 通電しているのに設定範囲外 を列挙、
  全一致なら `配線 OK`）と、`CCID_SLOT_COUNT` ≤ 配線表要素数の静的アサート。
- **poll 周期の計測ログ**: 10 秒ごとに 1 周の min/avg/max と最長 reader を出力（`POLL_STATS_INTERVAL_MS`,
  0 で無効）。13 台化したときのカード検出の遅れを実測するため。
- docs: firmware checklist に **§8 複数 slot（13 台）** + 受け入れ表の行、firmware README に
  「13 台化の段階手順」とドライバ制約（`pn5180_init` 失敗 = 共有 SPI 解放 = 全 reader 停止）、
  契約 `rfid-usb-ccid.md` §2 に 1 文 additive（version 1.0 据え置き）、worklog
  `docs/worklog/2026-09-10-multi-reader-firmware-prep.md`。

### Changed (firmware: USB 記述子)

- **`bcdDevice` を slot 数に連動**（`0x0200 | CCID_SLOT_COUNT` → 1 slot = `0x0201`、13 slot = `0x020D`）。
  slot 数の変更は `bMaxSlotIndex` の変更 = 記述子の変更であり、Windows は VID/PID/REV で記述子を
  キャッシュするため、REV を変えないと反映されない。
- **`bMaxCCIDBusySlots` を `1` 固定**（従来は slot 数に連動）。実装は bulk OUT を 1 コマンドずつ処理し、
  複数 slot を並行実行しないため。
- 出荷値の `CCID_SLOT_COUNT` は **1 のまま**（実機 1 slot の挙動は不変）。上記はいずれも **実機未検証**。

### Added / Fixed (RFID 実機: PN5180 読取り → PC/SC 越し UID 到達まで通し, ADR-0040, 2026-09-10)

- **実機 1 slot で契約 §5–§8 を本番 host コードで確認**: `tools/probe_pcsc.py watch` で
  `seat 1 UID=E0:04:…(8B)` が置く→離す→置くで 2 回発火。`raw` で `PRESENT` + カード無し `SW=6A81` /
  置いて `SW=9000`+UID。
- **firmware（`firmware/esp32s3-pn5180-ccid/`）**:
  - 通電中の MUX ch から reader を自動選択（1 台検証でコネクタを差し替えても再ビルド不要）。NSS スキャナの
    偽陽性と、失敗時の 2 回目 `pn5180_init` による再起動ループを修正。
  - ISO15693 UID を **MSB-first** に（契約 v1.1 §7 相当。`E0:04:…` 先頭）。presence debounce。
    ISO14443A の試行を既定 OFF（poll ~800ms→~300ms、ログ静音化）。
  - **CCID slot を仮想カード常時挿入に（ADR-0040）**: Windows usbccid は interrupt 通知を無視し、無ければ
    polling もせず bind 時の IccPowerOn しか送らないため、IccPowerOn に常に ATR を返しカード有無は
    Get UID の SW だけで伝える。interrupt-IN は `CCID_USE_INTERRUPT_EP=0`。bmICCStatus を 3 値化、
    Parameters を T=1 7 byte に、`bcdDevice` 0x0102。CCID コマンドを UART に診断ログ。
- **host**: `core/events.py` の numpy を `TYPE_CHECKING` ガードに（RFID 経路は pyscard だけで動く）。
  `probe_pcsc raw` サブコマンド（pyscard 直叩き: OS の slot 状態 + connect/Get UID の例外を hresult 付きで
  表示、`watch` 0 件の切り分け）。tests 41 passed。
- docs: ADR-0040、契約 §2/§5/§8 追記、firmware checklist §3/§6/受け入れ表、worklog
  `docs/worklog/2026-09-10-rfid-ccid-end-to-end-bringup.md`（Store 版 `python` スタブ / py -3.13 + pyscard
  wheel の環境メモ含む）。
- **カード登録ツール `tools/register_cards.py`**（`run` / `list` / `unregister`）: 次に置くカードを表示 →
  置くと UID を `rfid_cards.json` に登録 → 離すまで待つ、をタップ駆動で繰り返す。`--deck N` で 2 デッキ目
  （同じ code に別 UID）、中断/再開可、別 code 登録済み UID は拒否、`--order suit-rank|rank-suit` /
  `--only` / `--start-at`。`rfid_cards.json` の説明にあった存在しない `python -m rfid.register` を差し替え。

### Fixed / Added (ESP32-S3 USB CCID firmware 実機 bring-up + 契約に実機確定値転記, ADR-0034 / ISSUE-0015)

- 前日 scaffold した `firmware/esp32s3-pn5180-ccid/` を **実機で起動**。Windows PC/SC に
  `PokerRFID PN5180-CCID 0` として列挙、Status=OK、`tools/probe_pcsc.py list` で見える状態に。
  契約 `docs/contracts/rfid-usb-ccid.md` §2/§4 を実機確定値で更新（VID=0x303A PID=0x8B5D、
  manufacturer="PokerRFID"、product="PN5180-CCID"、Windows reader_name = `PokerRFID PN5180-CCID 0`）。
  **ISSUE-0015 の最後の残作業（実 VID/PID/reader_name 確定）を完了**。ADR-0034 Follow-up を Done に。
- 途中で必要だった非自明な修正:
  - **依存名 fix**: `jef-sure/pn5180` ^0.1.0（`esp32-component-pn5180` は GitHub repo 名で
    registry 名と別、`version solving failed` の原因）+ include をハイフン区切り名 +
    `nfc_uids_array_t.uids_count` / `nfc_uid_t.uid_length` に修正。
  - **USB-Serial/JTAG セカンダリコンソール無効化**: `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y`。
    セカンダリコンソールが TinyUSB(USB-OTG) と内蔵 USB PHY を奪い合い CCID 起動失敗（Windows Code 10）。
  - **`ccid_force_link()` の追加**: TinyUSB は `usbd_app_driver_get_cb` を weak スタブ（0 drivers）で
    持つ。我々の strong 定義は `ccid_device.c`（別 TU）にあり、ESP-IDF は main を whole-archive
    しないため `ccid_device.o` が抽出されず weak スタブ採用 → CCID クラス未登録 = Code 10。
    `main.c → ccid_force_link()` で TU 強制リンク。
- 残: PN5180 SPI 配線→`app_config.h` ピン反映→`probe_pcsc watch` で実カード UID 読み取り（§6/§7）。

### Added (ESP32-S3 + PN5180 USB CCID firmware scaffold, 本番 RFID / ADR-0015/0034)

- 本番 RFID（canonical USB CCID → PC/SC）の **ESP-IDF firmware scaffold** を `firmware/esp32s3-pn5180-ccid/`
  に追加。host 側（`rfid/`, `tools/probe_pcsc.py`）は完成済みのため、これを焼けば `probe_pcsc` で受け入れ確認できる。
- 内容: USB CCID 記述子（class 0x0B / bulk IN-OUT, §2）+ CCID メッセージ処理（IccPowerOn→ATR §5 /
  XfrBlock の `FF CA 00 00 00`→UID+90 00 §6 / UID 4-7-8B 生バイト §7 / GetSlotStatus §8）+ TinyUSB
  カスタムクラス登録（`usbd_app_driver_get_cb`）+ PN5180 読取り（`jef-sure/esp32-component-pn5180`）+
  カード状態キャッシュ（USB と RF を分離）。framework 非依存の中核は `ccid_slot.c`。
- USB/esp_tinyusb/PN5180 の版・実機依存箇所は `TODO(実機)` と README に明示（ピン / VID-PID /
  `usbd_class_driver_t` 構成 / `tinyusb_config_t` フィールド / 構造体フィールド）。ビルド/フラッシュ/USB 検証は
  実機タスク。CCID クラスの手法は RevK 記事 / `polhenarejos/pico-openpgp` を参照。
- ドキュメント: `firmware/.../README.md`（ビルド手順 + 契約 §↔ファイル対応 + host 受け入れ）、`CLAUDE.md`
  ディレクトリ構成 + 実装状況行、ISSUE-0015 残作業に scaffold を追記。

### Added (実機 RFID bring-up 診断ツール + 手順, Phase H / ADR-0015/0034)

- 実機の **ESP32-S3 + PN5180（USB CCID → PC/SC, canonical）** を `docs/contracts/rfid-usb-ccid.md` v1.0 の
  MUST に対して検査する診断 CLI **`tools/probe_pcsc.py`** を追加。`simulate_rfid.py`（HTTP 模擬・実機なし）と
  対になる「実機側」 bring-up ツール。production と同じ `rfid.bridge.PCSCBridge` / `rfid.reader_thread.RFIDThread`
  を叩くため、ここで OK なら hand logger でも OK。
  - `list`: 接続中 reader_name を列挙し `config.rfid.pcsc_readers` と**等値**突き合わせ（matched/MISSING/
    unconfigured, 契約 §3-4/§8）。
  - `check`: config lint（role/seat/index・重複・name 欠落）+ 各 reader connect 検査（§4-5, カード不要）。
  - `watch`: 実 RFIDThread を起動し、タップごとに role/seat/board_index・正規化 UID（4/7/8B 判定）・card 解決を
    表示（§6-8: Get UID / UID 正規化 / デバウンス hot-plug）。pyscard 未導入時は導線付きで gate。
- **手順書** `docs/hardware-qa-checklist.md`（Phase H 実機 QA, 契約 §↔手順の受け入れ基準表つき）を追加。
  `manual-qa-checklist.md`（実機なし）の対。
- **firmware 実装チェックリスト** `docs/rfid-ccid-firmware-checklist.md` を追加（本番 USB CCID 経路）。
  契約 v1.0 の MUST（native USB / USB CCID class / ATR / Get UID `FF CA 00 00 00` / UID 4-7-8B 生バイト /
  hot-plug）を ESP32-S3 firmware 実装手順に落とし、各項目を `probe_pcsc` の出力で受け入れ確認できる形に。
  「LED 点灯のみのテスト firmware → 本番 USB CCID firmware」への橋渡し。
- `CLAUDE.md` のコマンド集 / Phase H 行 / 残作業 #2 を更新。
- テスト: `tests/test_tools_probe_pcsc.py`（35 件。純粋ロジック + DI シーム + コマンド層を pyscard/実機なしで
  検証）。**残**: 実機を繋いだ通し QA、firmware の VID/PID・実 reader_name 確定（契約 §2/§4 追記, ISSUE-0015）。

### Added (ハンド訂正 = append-only オーバーレイ / iPad staff 訂正, ADR-0036 / B4)

- 音声自動記録の誤認識を、**元 hand log を mutate せず append-only な訂正レコードで重ねる**仕組みを追加
  （write-once だった hand log に訂正手段が無かった B4 を解消。元の ASR 記録は監査・再学習のため保持）。
- core: `HandCorrection` + `HandCorrectionRepository`（`hand_corrections.json`, atomic+fsync, node-local）+
  `apply_hand_corrections`（read 時オーバーレイ: 対象 `(session_id, hand_id, action_index)`、field=action/
  amount/winner_seat、元値を `_original` 保持・`corrected` 付与・`needs_review` 解除・hand に `_corrections`
  監査痕）。
- API: `POST /api/staff/sessions/{sid}/hands/{hid}/corrections`（staff write, 400 `invalid_correction` /
  404 `not_found`）。viewer の `GET .../hands/{hid}` と player hands 一覧が**訂正済みビュー**を返す。
  `ViewerApiClient.add_hand_correction`。
- mobile: `ViewerRepository.addHandCorrection`（mock/HTTP, staff token）+ 型（`HandCorrection`/Input）+ mock の
  overlay 適用（getHand/listPlayerHands）+ mock test。
- **iPad 訂正画面** `mobile/src/screens/CorrectionScreen.tsx`: HandDetail から導線、アクションごとに種別
  （check/call/bet/raise/fold/all_in）/ 金額を編集 → staff 訂正送信 → 訂正済みビュー即再読込。
  `EXPO_PUBLIC_STAFF_TOKEN` で staff write 有効化。typecheck + 13 tests + web export green。
- **残**: PHH export へのオーバーレイ適用 / 訂正取消 / board・hole の訂正。

### Added (アミューズメント・ガードレール: 負 adjustment = 返金/訂正のみ・監査, ADR-0035 / B9)

- 会計の唯一の「店 → player」方向（`adjustment` の負 cash）に **理由 `note` を必須**化し、**監査
  warning ログ**を出すようにした。賞金・負け分の現金分配への転用を防ぎ、player→店 の 1 方向会計
  （アミューズメント前提・賭博該当回避）を運用規律だけでなくコードでも補強。正の adjustment（誤記訂正の
  追加）は note 任意のまま。point は換金不可を維持。schema / error code 変更なし（`invalid_amount` 再利用）。
  ledger 契約・ADR-0035 に明文化。

### Fixed / Added (v1.0 ローンチレビューの小粒修正: B7 データ堅牢性)

- **B7 破損検出 + 並行安全**: (1) `core/atomic_io.read_json_file` が JSON 破損時にファイルを脇に退避
  （`*.corrupt-<ts>`）+ ERROR ログ → 次の書き込みで唯一のコピーを上書き消失させない（B2 バックアップと
  併せ手復旧可能に）。players/sessions/ledger/order の `_load` を移行。(2) ledger の不正レコード skip 件数を
  ERROR で集約表示（会計欠損の黙殺を可視化）。(3) `PlayerRepository` / `SessionRepository` に RLock
  （`session_layer.enabled` 時の IntegrationThread × GUI スレッドの同時アクセス対策。ledger/order は既に lock 済）。

### Fixed / Added (v1.0 ローンチレビューの小粒修正: B1/B2/B3/B6)

- **B1 セッション締め→精算の導線**（P0）: `close_session` はコアにあったが GUI/CLI/API から呼べず、
  closed 前提の `commit_settlement`（精算確定）に到達不能だった。`gui/ledger_view.py` の精算パネルに
  「セッション終了（close）」ボタン（reopen 不可のため 2 クリック確認）+ staff API
  `POST /api/staff/sessions/{id}/close`（409 `already_closed`）+ `ViewerApiClient.close_session` を追加。
- **B2 データバックアップ + fsync**（Crit）: 会計データ消失（単一 JSON）対策。`core/backup.py` +
  `tools/backup_data.py` + 起動時自動バックアップ（`config.backup`, 既定 on）。加えて全永続書き込みを
  共有ヘルパ `core/atomic_io.py:atomic_write_json`（temp→flush→**fsync**→os.replace）に集約し、電源断でも
  書き込み内容がロスしないようにした（9 writer = players/sessions/ledger/orders/credentials/auth_identity/
  hand log/sync/card_master を移行）。
- **B3 RFID PC/SC の street 自動遷移バグ**: `RFIDThread` が `board_index` を未設定で、PC/SC 経路の
  board street 自動遷移が機能していなかった。来週の実機テストに向け修正 + 回帰テスト。
- **B6 install docs**: `installation.md` を PC/SC（PN5180+ESP32-S3 USB CCID）canonical に更新
  （HTTP/PN532 は補助に降格）。pcsc_readers(list) 設定手順・実 reader_name 採取を明記。
### Added (hand logger 遠隔制御 + staff アプリ ハンドタブ, ADR-0039 / WS4 §C)

- **hand logger を staff iPad から遠隔操作**できるようにした（**新ハンド / ウィナー / リバイ**）。録音
  （音声/RFID）は録音 PC 常駐のまま、iPad は **append-only control queue** にコマンドを積むだけ。適用は
  hand logger プロセスが行う（状態変更経路は IntegrationThread に一元化, ISSUE-0012 遵守）。
  - `core/control_queue.py`（`ControlCommandLog`: `logs/{session_id}.control.jsonl` への append/offset 読み）
    + `integration/control_consumer.py`（`ControlConsumerThread`: 末尾シーク + command_id 重複排除で新規のみ
    `AudioEvent` に翻訳）。
  - `POST /api/staff/sessions/{session_id}/control` + `ViewerApiClient.send_control`。新 error `invalid_control`(400)。
  - `main.py --`（GUI）に consumer を結線。config `hand_control.enabled`（既定 **false**）+ GUI +
    `session_layer.enabled` のときのみ起動 → **既定では挙動不変**。
  - staff アプリに **ハンドタブ**（`staff/src/screens/HandTab.tsx`）。`StaffRepository.sendControl`（mock/HTTP）。
- テスト: `tests/test_control_queue.py`（append/offset/idempotent/translate/consumer 末尾シーク）+
  staff API control テスト（Python 634 passed）、staff アプリ typecheck + 13 mock tests + E2E 6 件
  （ハンドタブの送信フロー追加）。
- **残**: ハンド履歴の staff read（hands-list endpoint）と実機での反映遅延/死活確認は後続（ISSUE-0020）。

### Added (staff アプリ ブラウザ E2E, Playwright / WS4)

- **staff アプリの UI を Playwright E2E で検証**（`staff/e2e/staff.spec.ts`, 5 tests）。web export
  （`dist/`）+ MockRepository をヘッドレス Chromium で開き、**ログイン→会計（エントリ追加/取消）→
  注文確定→座席割当→セッション作成**の実フローをタップ駆動でテストする。staff アプリの配布形態
  （web export を iPad Safari で開く）に最も近い自動テスト。
- `staff/playwright.config.ts`（iPad 相当 viewport + touch、webServer で `dist/` を配信）+ 依存なしの
  静的サーバー `staff/e2e/serve.mjs` + `npm run e2e`（export:web → 配信 → test）/ `e2e:install`。
  入力欄に E2E 用の placeholder を追補（cash/point/付与pt/席1-9）。
- 実機タッチ/レイアウト/ソフトキーボードの最終確認は**手動 QA**（iPad Safari / Expo Go）で行う方針
  （Linux CI に iOS シミュレータ無し）。`playwright install` は browser 取得にネットワークが要る。

### Added (staff アプリ ledger reversal UI + entry 一覧 read API, ADR-0038 §A / WS4)

- **`GET /api/staff/sessions/{session_id}/ledger-entries`** を追加（session の ledger entry 一覧。
  reversal UI が取消対象を選ぶための staff read。lenient = unknown session は空 list）+
  `ViewerApiClient.list_session_ledger_entries`。
- **staff アプリの会計タブに「エントリ一覧 + 取消(reversal)」UI** を追加。各 entry を表示し、reversal で
  ない entry に「取消」ボタンを出す（取消は append-only な reversal を記録し、中間集計に反映）。
  `StaffRepository.listLedgerEntries` を mock/HTTP に追加。
- テスト: `tests/test_viewer_api_staff_lifecycle.py` に list+reverse の往復を追加（Python green）、
  staff アプリ typecheck + 12 mock tests + web export green。

### Added (staff API 拡張 §A/§B + staff アプリ座席タブ, ADR-0038 / WS4)

- **staff API を additive 拡張**（`/api/staff/...`, staff token 認可・単一書き手維持, ADR-0038）:
  - **§A 会計**: `POST .../ledger-entries/{entry_id}/reverse`（reversal）、
    `POST .../players/{player_id}/point-grants`（ポイント付与）。
  - **§B session/座席/player ライフサイクル**: `GET/POST /api/staff/sessions`、`.../close`、
    `.../seating`（現在 seating + hand_id 一覧）、`PUT .../hands/{hand_id}/seats`（seat→player batch）、
    `GET/POST /api/staff/players`、`PUT /api/staff/players/{player_id}`（rename）。
  - error code は `error-shapes.md`（ledger / session / player）を **1:1 再利用**（新規 code なし）。
    `ViewerApiClient`（Python）に対応 staff メソッドを additive 追加。
- **CORS の `allow_methods` を GET→GET/POST/PUT に拡張**: web クライアント（staff アプリ / mobile 注文 POST）が
  別 origin から write できるように（LAN 限定 + token 認可前提）。
- **staff アプリに座席タブ + session 作成/close を追加**（`staff/`, ADR-0037 §5）: 現在 seating 表示・
  次 hand への seat→player 割当・その場 player 作成、SessionList から session 作成/close、会計タブに
  ポイント付与。`HttpStaffRepository.listSessions` 等を実装済 API に接続（`not_implemented` 解消）。
- テスト: `tests/test_viewer_api_staff_lifecycle.py`（§A/§B round-trip + error code + 認可、Python 629 passed）、
  staff アプリ typecheck + 12 mock tests + web export green。

### Added (店舗用 staff iPad アプリ — 会計/注文 scaffold, ADR-0037 / WS4)

- **新規 `staff/` アプリ**（Expo/RN, TypeScript。player 用 `mobile/` とは別アプリ）を追加。スタッフが
  iPad/web から **会計（Ledger/精算）と注文リクエスト捌き**を操作できる scaffold。画面は
  **Login（staff token）→ SessionList → TableView（会計タブ / 注文タブ）**。
  - **会計タブ**: ledger エントリ追加（buy_in/rebuy/add_on/order/entry_fee/adjustment, cash+point）、
    **buy-in 金額プリセット**（ADR-0026）、**中間集計（暫定）**、closed session の**精算確定（commit）**と
    **支払状態（paid/unpaid/partial）+ 受領額記録**（ADR-0023）。
  - **注文タブ**: pending 注文の**確定**（単価確定 → order ledger entry, staff-in-the-loop / ADR-0018）と
    **却下**。確定単価は menu master から prefill。
- **contract-first**（ADR-0037 §6）: UI は `StaffRepository` interface のみに依存し、`MockStaffRepository`
  （既定 = fixtures、token `demo-staff-token`）/ `HttpStaffRepository`（staff API `/api/staff/...` を fetch、
  `EXPO_PUBLIC_API_URL` 切替、`Authorization: Bearer <staff_token>`）を注入で差し替える。typecheck +
  7 mock 契約テスト + web export green。
- **残（後続）**: 座席タブ・ハンドタブと HTTP の `listSessions`（`GET /api/staff/sessions`）は **ADR-0038**
  の staff API 追加が前提（現状 `not_implemented`）。open question は ISSUE-0020。

### Added (店舗用 staff iPad アプリの設計, ADR-0037 / ADR-0038 / ISSUE-0020 — 設計のみ・コードなし)

- 店舗（スタッフ）操作の UX 改修に向け、**新規 staff iPad アプリ**の設計ドキュメントを追加。現状の
  店舗操作（desktop customtkinter の 5 画面・`main.py` の別プロセス起動 + staff write API）を整理し、
  **player 用 `mobile/` とは別の Expo/RN staff アプリ**（iPad/web、卓単位タブ統合 = 会計/注文/座席/ハンド、
  staff shared token 認可、録音は PC 常駐・iPad は操作）に統合する方針を **ADR-0037** に確定。
- 不足する staff API の設計を **ADR-0038**（A: 会計の reversal/grant、B: session/座席/player ライフサイクル、
  C: hand logger 遠隔制御=プロセス境界のため後続）として追加。schema 変更・新 error code なし（既存再利用）。
- 設計フェーズの未決事項を **ISSUE-0020**（risk register）に集約（hand logger 遠隔制御の方式 / iPad 作成
  session と録音 hand logger の結線 / staff token 配布・回転 / 並行・オフライン UX / desktop と staff app の
  責務分界）。`CLAUDE.md` の Parallel development plan に **WS4（staff iPad app）** を planned で追加。

### Added (RFID USB CCID firmware↔host 契約の凍結, ADR-0034 / ISSUE-0015 Fixed)

- PN5180 + ESP32-S3 の **USB CCID firmware ↔ host (PC/SC) 契約**を `docs/contracts/rfid-usb-ccid.md`
  **v1.0** として凍結（USB descriptor / reader_name 安定規約 / slot↔役割は host config が source of truth /
  ATR は host が ATR-agnostic / Get UID pseudo-APDU `FF CA 00 00 00` / UID 4-7-8B 正規化 / hot-plug は
  PC/SC polling / multi-platform / freeze 規則）。実機なしで境界を確定（ISSUE-0015 Fixed）。
- config に **`rfid.pcsc_readers`（list, PN5180+ESP32-S3 想定）** サンプルを追加し、HTTP 用 `readers`（dict）と
  キー分離。`main.py` の pcsc 経路を `pcsc_readers` 優先に変更し、**dict 誤設定での latent crash を解消**。
- **8B UID（ISO 15693）回帰**: `tests/test_rfid.py` に `bytes_to_tag_id`/`normalize_tag_id` roundtrip +
  `CardMaster` lookup + `MockPCSCBridge → RFIDThread → RFIDEvent` + board(role/index) マッピング。
- 残（実環境）: firmware の VID/PID・実 reader_name を確定して契約 §2/§4 に追記。

### Added (mobile に本人認証 UI を反映: L1 PIN / L2 サインアップ, ADR-0027/0031)

- mobile（Expo/RN）に **PIN ログイン（L1）** と **LINE/Google サインアップ（L2）** の UI を追加
  （`mobile/src/screens/AuthScreen.tsx`）。PlayerSelect 画面に各 player の「🔒 PIN でログイン」と
  「LINE / Google でサインアップ」導線を追加。**既定の name-pick は不変**（`player_auth=off` の会場は
  従来どおり選ぶだけ）。
- `ViewerRepository` に `login` / `oidcExchange` / `currentPrincipal` / `clearAuth` を additive 追加し、
  mock / HTTP の両実装で principal トークンを保持、**注文 POST（self-write）に `Authorization: Bearer`** を
  付与する。error code（invalid_pin / pin_locked / pin_too_short / player_auth_disabled / unknown_provider /
  invalid_idp_code）を UI メッセージにマッピング。
- mobile `Player` 型を schema `1.2` に追従（optional `updated_at` / `merged_into` / `merged_at`）。
- 実 IdP の認可コード取得（SDK / web redirect）は実環境タスク（ADR-0031 D5）。
  検証: `npm run typecheck` + `npm test`（11 passed, 内 3 件が auth）+ `expo export --platform web` 成功。

### Changed (派生 confidence の重み較正を確定・回帰ロック, ADR-0033)

- rules-aware 経路の派生 confidence（`derive_confidence`）の重みを「暫定」から **較正済み**に確定。
  ラベルデータが無いため数値フィッティングではなく、golden fixtures の archetype + 境界グリッドが含意する
  **較正プロパティ P1〜P8**（bounds / whisper 単調 / source ordering / rfid>camera 補強 / 不一致ペナルティ /
  合法性ゲートが閾値未満 / 閾値が audio 品質を分離 / synth-fold は常に review）で正当化。
- **数値は据え置き**（プロパティを満たすため churn しない）。`tools/calibrate_confidence.py`（較正ハーネス =
  サーフェス表示 + プロパティ検証、違反で exit 1）+ `tests/test_confidence_calibration.py`（CI で drift 検知）。
  `integration/engine.py` の「暫定/最終較正は F」コメントを ADR-0033 参照に更新。挙動・出力は不変。

### Added (sync 後続: rename 伝播 / hand log union / 定期 auto-trigger, ADR-0032)

- 双方向 sync（ADR-0022）を additive 拡張（既定挙動不変）:
  - **player rename 伝播**: `Player.updated_at`（schema `1.1`→`1.2` additive、rename で更新）を加え、sync の
    `_resolve_player` を **display=`updated_at` last-writer-wins × `merged_into`=monotonic** に。別端末での改名が
    両ノードに収束する（旧: local 優先で非伝播）。
  - **hand log の file-level union**: `logs/{session_id}.json` を sync 対象に追加（`merge_hand_logs` =
    `hand_id` の append-only union、`build_snapshot`/`merge_snapshot_into` に optional `log_dir`、
    `/api/staff/sync/{snapshot,merge}` で同期）。
  - **定期 auto-trigger**: `core/sync_scheduler.py:SyncScheduler`（clock 注入で決定的、callback 例外耐性）+
    config `viewer_api.sync_auto_interval_sec`（既定 0 = off）。常駐スレッド結線は運用タスク（ADR-0029 会場主導）。
- いずれも可換・冪等・収束を維持。`unmerge` は monotonic 規則により sync 非伝播（局所操作, 既知制約）。

### Added (L2 外部 IdP の実 IdP 非依存コア, ADR-0031)

- L2（LINE/Google サインアップ）のうち **実 IdP の HTTP を伴わないコアを実装**（実機/実 IdP なしで
  E2E までテスト）。LAN 既定（provider 未構成）では **挙動不変**。
- **`auth_identity`**（`(provider, subject) → player_id` の node-local バインディング、多対一、
  **read API / sync 非対象**）+ **OIDC provider 抽象**（`OidcProvider` / `FakeOidcProvider` /
  `VerifiedClaim`）+ **claim→principal 解決**（`resolve_player_for_claim`: 既存は `resolve_canonical`
  で survivor 解決、初回は新規 player 作成 + link。player_id は IdP sub から導出しない）。
- **`POST /api/auth/{provider}/exchange`**（mobile app-driven の認可コード交換 → L1 と同形の principal
  トークン発行。provider 未構成は 404 `unknown_provider`、検証失敗は 401 `invalid_idp_code`）+
  `ViewerApiClient.oidc_exchange`。merge（ADR-0030）後は survivor principal に解決。
- **残（実環境タスク）**: 実 LINE/Google provider の HTTP（token 交換 / JWKS 検証）、hosted デプロイ
  （ADR-0029）、web redirect/callback 変種。

### Added (player merge, ADR-0030)

- 同一人物の複数 `player_id` を統合する **player merge** を実装（L2 の前提 + LAN 単独の重複掃除）。
- merge = absorbed player に **`merged_into`（alias/tombstone）** を付ける方式。**既存の ledger / session /
  order / hand-log は一切 rewrite しない** → **append-only（ADR-0016）・収束 sync（ADR-0022）・player_id
  不変（ADR-0004）を保ち、可逆**。`resolve_canonical` / `equivalence_class` で player をキーにする全 read
  （settlement 集計・point 残高・ledger entry・order フィルタ・viewer per-player・login principal）を
  survivor に解決する。`list_players` は tombstone を既定で隠す。
- staff API `POST /api/staff/players/merge`（400 `invalid_merge` / 404 `not_found`）+
  `ViewerApiClient.merge_players` + registry GUI（統合先設定→統合）。merge 後は LINE/Google/PIN ログインも
  survivor principal を返す。sync は `_resolve_player`（`merged_into` を monotonic 伝播 + survivor 最小
  tiebreak で収束）。
- `player` schema を **`1.0`→`1.1`**（optional `merged_into`/`merged_at` の additive）。未 merge の player の
  シリアライズ形は不変。survivor はスタッフが明示選択（既定 = 会計履歴を持つ古い方 = 通常は会場 player）。

### Docs (L2 hosted モードの運用設計, ADR-0029)

- ADR-0028（L2 外部 IdP）の前提となる運用面を確定（**運用設計のみ・コードなし**）。
- **トポロジー = 会場 source-of-truth + cloud は player ミラー**: 会計/ハンド/session は会場 PC が真実、
  cloud は read ミラー + player のサインアップ（L2）/ 閲覧 / 注文リクエスト（pending）+ sync のみ公開し、
  **会計を originate しない**。cloud 侵害時も会計の真実は会場側で保全、ネット断時は会場が LAN 単独継続。
- **ホスティング = マネージド PaaS**（TLS/デプロイ/secret 内蔵、運用負荷最小）。永続は persistent volume
  or 将来 DB backed（interface frozen で差し替え可）。**IdP = LINE + Google 両方**を登録。
- secret は PaaS env（`player_token_secret` は cloud では固定）/ **PII 最小（APPI, sub のみ保存、IdP
  トークン非保存）** / cloud は会計 write エンドポイント無効・CORS を hosted origin に絞り・レート制限・
  ログ PII マスキング。**残**: player merge（scope 外）。ADR-0028 §D7・decision-log・CLAUDE.md を更新。

### Added (player 本人認証 L1 per-player PIN, ADR-0027)

- player が自分のスマホからの **self-write（注文 POST 等）を PIN ログインで本人認証**できるレイヤを
  追加（既定 `viewer_api.player_auth=off` で **挙動不変** = 従来の name-pick）。`optional`（PIN 登録済
  player の write のみトークン要求）/ `required`（全 player write に要求）で有効化。staff token（ADR-0021）
  とは直交。
- `POST /api/auth/login`（PIN→stateless 署名トークン）/ `POST /api/players/{id}/pin`（初回=staff or
  self-enroll、変更=現 PIN or staff reset）。注文 POST に principal ガード（401 `unauthorized` /
  403 `forbidden`）を additive 追加。`ViewerApiClient.login` / `set_pin`。
- PIN は **node-local `player_credentials.json`**（PBKDF2-HMAC-SHA256 + per-player lockout、平文非保持）に
  分離 — **viewer API の read response にも sync snapshot にも含めない**（players.json / player schema /
  sync は不変）。LAN 限定前提を維持。
- 新 error code（`error-shapes.md`）: `player_auth_disabled` 403 / `invalid_pin` 401 / `pin_locked` 429 /
  `pin_too_short` 400 / `forbidden` 403。

### Docs (player 認証 L1 PIN / L2 外部 IdP の詳細設計, ADR-0027 / ADR-0028)

- ADR-0025 の方針（name-pick → PIN → 外部 IdP）のうち **L1 / L2 を実装可能な詳細設計まで具体化**
  （**設計記録のみ・コードなし**）。
- **L1 per-player PIN（ADR-0027）**: PIN を node-local `player_credentials.json`（PBKDF2 + per-player
  lockout、read API / sync 非対象）に分離（ADR-0025 の「players.json に pin_hash」素描を漏洩・伝播・schema
  リスクから精緻化）。検証後に stateless 署名トークンを発行し **player principal 解決レイヤ**で self-write を
  認可。config `viewer_api.player_auth` 既定 `off` で完全後方互換（name-pick 維持）。
- **L2 外部 IdP（ADR-0028）**: `(provider, subject) → player_id` の `auth_identity`（多対一、player_id は
  外部 sub から導出しない）。OIDC Authorization Code フロー（サーバ側 JWKS 検証）→ L1 と同形の player
  トークン発行。hosted モードで LAN 会場モードと player_id + sync 共存。PII 最小化（sub のみ保存、IdP
  トークン非保存）。**前提**: 運用面 ADR（hosting/secret/PII/法令）+ player merge フロー。
- ISSUE-0019 / `docs/decision-log.md` / CLAUDE.md 残作業を更新。

### Added (buy-in 金額プリセット = staff メニュー選択, ADR-0026)

- buy-in 記帳時に **スタッフが店設定の金額プリセット（整数円）から選んで** ledger に記録できるよう
  にした（「auto ledger 生成」の最終形）。`config.ledger.buyin_presets`（既定 `[10000, 20000, 30000]`,
  店が編集）を `--ledger` 画面のボタンとして表示し、押すと kind=buy_in + cash を prefill（確定は
  従来どおり「エントリ追加」）。staff API `GET /api/staff/buyin-presets`（staff token 必須）/
  `ViewerApiClient.get_buyin_presets()` でも取得できる。
- **schema / 業務ルール変更なし・additive**。hand 結果からの自動 ledger 生成（chip→円換算を伴うもの）
  は引き続き **作らない**（chips は別単位・自動換算なし, ADR-0016/0026）。

### Docs (player アイデンティティ / 認証の進化方針, ADR-0025)

- 将来「プレイヤーが LINE / Google でサインアップ」できる要件に向け、識別と認証を分離する方針を記録。
  `player_id` を内部不変キーに保ち、認証を additive レイヤで重ねる（L0 name-pick → L1 per-player PIN →
  L2 外部 IdP = `auth_identity` バインディング）。外部 IdP は LAN-only 前提を変える hosted モード
  （別 ADR）。**方針記録のみ・実装は後続**。ISSUE-0019 / CLAUDE.md 残作業を更新。

### Fixed (sync の settlement マージを partial-paid 対応に, ADR-0024)

- `core/sync.py` の settlement マージを **`paid_amount` の monotonic max** に変更（旧: paid>unpaid の
  2 値 + settled_at 早い方）。partial-paid（ADR-0023）で、受領額の大きい `partial` が settled_at の
  早い `unpaid`(paid_amount=0) に上書きされ得た収束バグを解消。`payment_status` は max 後の
  paid_amount から導出。可換・冪等は維持（`tests/test_sync.py` に収束テスト追加）。
  現行の単一書き手 + on-demand pull 既定では実害のなかった latent issue の予防修正。

### Added (S4 — settlement partial-paid, ADR-0023)

- 精算に **一部支払い（partial-paid）** を追加。`SessionSettlement` に累計受領額 `paid_amount`
  （>=0, 既定 0, additive）を持ち、`payment_status` を `net_due_to_store` から導出する
  （`paid`/`unpaid`/新規 `partial`。過払いは `paid` に丸め）。
- core: `LedgerRepository.record_payment(session, player, paid_amount)`。既存 `set_payment_status`
  は paid=全額 / unpaid=0 の shortcut として維持（partial は record_payment 必須）。
- staff write API に `PUT /api/staff/sessions/{sid}/players/{pid}/payment`（body `{paid_amount}`）+
  `ViewerApiClient.record_payment`。player の ledger summary に `paid_amount` を additive 追加。
- GUI: `--ledger` 精算パネルに受領額入力 + 「支払額記録」ボタン。
- mobile: `MyLedgerScreen` が **支払済み / 一部支払い (受領/請求 円) / 未払い** を表示。
- schema: `session_settlement.schema.json` を `1.0`→`1.1`（optional `paid_amount` + enum 値 `partial`
  追加 = additive）。`from_dict` は `paid_amount` 欠落時に payment_status から後方互換に推定。
- tests: `test_contracts`（partial 行 + fixture）/ `test_ledger_view_gui::TestSettlement`（partial/full/
  negative）/ `test_viewer_api_staff`（record_payment 正常・not_found・invalid）/ mobile mock。

### Added (S4 — mobile での精算状況表示, ADR-0016/0017)

- player の会計参照（viewer API `/api/players/{id}/sessions/{sid}/ledger` の summary）に
  **確定状態を additive 追加**: `settled`(bool) / `payment_status`（paid|unpaid）/ `settled_at`。
  確定判定は `list_settlements`（確定行）由来（compute_settlement の settled_at は speculative でも
  埋まるため使わない）。
- mobile `MyLedgerScreen` が **精算状況**（未確定（暫定）/ 確定済（支払済み・未払い））を表示。
  プレイヤーが自分のスマホで自分の精算結果を確認できる。
- tests: `test_viewer_api.py::test_player_ledger_settled_status` + mobile mock/typecheck 更新。

### Added (S4 — settlement 確定 GUI, ADR-0016)

- スタッフ用 ledger 画面（`gui/ledger_view.py`, `main.py --ledger`）に **精算パネル**を追加。
  closed session を **確定（commit_settlement）** し、確定済 settlement の **paid/unpaid を player
  ごとに切替**（set_payment_status）できる。これまで CLI/CSV export 経由だった settlement 確定が
  GUI から行えるようになった。open session の確定は core が `session_not_closed`、二重確定は
  `already_settled` を返し、画面に表示する。partial-paid は未対応（paid/unpaid のみ）。
- `tests/test_ledger_view_gui.py::TestSettlement`（6 件）でコマンドロジックを固定。

### Added (S5 — 双方向 sync（state-based merge）, ADR-0022)

- **双方向 sync**（`core/sync.py`）: 複数の運営ノード（LAN）が全ストアのレプリカを
  **state-based merge** で相互最新化できる。マージは純粋関数で **可換・結合・冪等**
  （UUID union + 単調フィールド解決）なので、どのノードがどの順で何度マージしても同じ状態に収束する。
- **per-store マージルール**（ADR-0022）: players=create-only union（local 優先・rename 非伝播）/
  ledger・point=union（append-only, 衝突なし）/ order_request=union + status 解決（pending<終端、
  confirmed が rejected に優先、両 confirmed は resolved_at 早い方）/ settlement=committed>uncommitted・
  paid>unpaid の単調解決 / session=closed>open + 入れ子 hands/seats union（seat 衝突は local 優先）。
- **sync API**（staff-token gate）: `GET /api/staff/sync/snapshot`（自ノード全レコード, read-only でも可）/
  `POST /api/staff/sync/merge`（peer snapshot を取り込み, write 所有プロセスのみ。read-only は 503）。
- **Python client**: `ViewerApiClient.pull_sync_snapshot()` / `push_sync_merge(snapshot)` /
  `sync_bidirectional(peer)`（2 ノードを収束させる helper）。
- **repository additive**: 全 repo に read-only `path` property、`LedgerRepository` /
  `OrderRequestRepository` に public `reload()`（file-level merge 後の in-memory 最新化）。業務ロジックは不変。
- 収束テスト: `tests/test_sync.py`（純粋: 冪等 / 可換 / merge(merge(A,B),B)=merge(A,B) + file-level round-trip）
  / `tests/test_viewer_api_sync.py`（HTTP 2 ノード round-trip + 認可）。
- ADR-0022 は ADR-0020 の「単一書き手 / 双方向 auto-sync 先送り」を **更新**（複数書き手 + 収束マージ）。

### Added (S5 write 拡張 — スタッフ会計 write API（staff shared token 認証）, ADR-0021)

- **staff 会計 write API**（`api/server.py` の `/api/staff/...`）: 別端末のスタッフが会計をリモート
  操作できる。ledger entry 追加 / settlement 確定 / payment status paid-unpaid / 注文確定・却下 +
  staff read（settlement 中間集計 / 全 player の注文 queue）。
- **認証 = staff shared token**: config `viewer_api.staff_token` を設定すると有効。
  `Authorization: Bearer <token>`。token 未設定 → 403 `staff_writes_disabled` / 不一致 → 401
  `unauthorized`。**player read / 注文 POST は従来どおり無認証**（name-pick, ISSUE-0019）。
- **単一書き手維持**: staff *write* は write 所有プロセス（`--ledger`, viewer_api.enabled）のみ。
  単独 `--viewer-api`（read-only）では 503 `orders_unavailable`（staff read は token があれば可）。
- **`LedgerRepository` を thread-safe 化**（`threading.RLock` + `_locked` デコレータ）: `--ledger`
  プロセスで GUI スレッドと in-process API スレッドが同じ ledger を mutate するレースを排除
  （業務ロジックは不変。RLock 再入で inter-method 呼び出し安全。lock ordering は order→ledger 一方向）。
- **Python client**: `api/client.py:ViewerApiClient(staff_token=...)` に staff メソッド群を additive 追加。
  round-trip + 認可 test = `tests/test_viewer_api_staff.py`。
- 新 error code: `unauthorized`(401) / `staff_writes_disabled`(403)（`error-shapes.md`）。

### Added (S5 — cross-app boundary: repository interface 凍結 + Python API client, ADR-0020)

- **repository / service interface 契約を frozen**（freeze order #6）。player / session-seating /
  ledger-points-settlement / viewer read model / order-request の interface を S5 の安定契約に。
- **viewer API の Python client**（`api/client.py:ViewerApiClient`）= mobile `HttpRepository` の Python 版。
  viewer API の read endpoints（+ 注文 GET/POST）を呼び、非 2xx を error-shape の `code` を持つ
  `ViewerApiError` に変換。これで read boundary を二言語（TS / Python）で実証。
- **round-trip 契約 test**（`tests/test_viewer_api_client.py`）: API↔client を in-process（TestClient
  transport）で round-trip し境界の drift を検知。`[api]` extra に `httpx` 追加。
- 同期方式 = **on-demand pull**（push/event/双方向 auto-sync なし）、衝突は **単一書き手 +
  reload-on-read** で回避、ID は app 内採番 UUID で backend 非依存。write/sync 拡張は後続 ADR。

### Contracts (schema `1.0` freeze — S2/S3/viewer, ADR-0019)

- session / seat_assignment / hand_ref（S2）+ ledger_entry / point_ledger_entry（S3）+
  session_settlement（S4）+ order_request / player_session_summary（viewer）の schema を
  draft `0.x` → **`1.0` freeze**。統合後に全 consumer（core/desktop/mobile/API）が安定したため。
  以後 additive-only（breaking は新 ADR + MAJOR bump）。
- 上流 blocker の **ISSUE-0005 を Resolved**（hand logger 接続=E1/E2、seat change UI=E3 で解消）。
- 全 model に code↔contract drift gate を整備（`test_core_session_matches_contract` /
  `test_core_settlement_matches_contract` 追加、ledger/viewer は既存）。**残る draft は無し**
  （freeze order #6 = interface/sync 契約のみ planned）。

### Docs (ロードマップ整理 — 統合後)

- `CLAUDE.md` の Future Scope / Phase 計画を 3 トラック（S=会計 / M=player 向け / R=hand core）
  統合後の実態に整理。「ロードマップ（3 トラック統合後）」表と「残作業」一覧を新設し、M4 破棄・
  M6/M7 の S3 合流・採番替え（ADR-0017/0018・ISSUE-0019）・settlement core 済/freeze 残を明記。
  陳腐化記述（「すべて未実装」「mobile/ledger 未着手」、存在しないテスト参照）を修正
  （`docs/worklog/2026-06-13-roadmap-consolidation-post-merge.md`）。

### Added (player 向け viewer API + mobile + 注文リクエスト — verify-v1 ledger に統合, ADR-0017/0018)

- **player 向け読み取り専用 viewer API**（M1, `api/`, `[api]` extra）。`python main.py --viewer-api`
  で foreground 起動（注文 POST は 503 `orders_unavailable`）。endpoints: health / players /
  player sessions / hands / hand detail / **ledger 参照** / **menu** / **order-requests (GET/POST)**。
  ledger summary は verify-v1 ledger（ADR-0016）の `compute_settlement` を当該 player に絞った
  settlement 由来（`cash_in_total / order_total / entry_fee / point_spent_total /
  point_credited_total / net_due_to_store`）。契約は `docs/contracts/viewer-api.md`（draft 0.x）。
- **mobile viewer**（M2, `mobile/`, Expo/RN）。PlayerSelect→MySessions→MyHands→HandDetail + 会計 +
  注文画面。`ViewerRepository` interface に mock / HTTP 実装を `EXPO_PUBLIC_API_URL` で注入切替。
- **注文リクエスト write path**（M5, `core/order_request*.py` / `core/menu.py` / `menu.json`）。
  player はスマホから order_request（pending）を POST し、スタッフが `--ledger` 画面の確定/却下
  パネルで確定すると `ledger_entry`（kind=order）が作られリンクされる（staff-in-the-loop）。
  `--ledger` は `config.viewer_api.enabled=true` で viewer API を in-process 起動し注文受付を有効化
  （単一プロセス所有）。closed session への確定は order-request 層が 409 `session_closed` で弾く。
- config: `viewer_api`（enabled / bind_host 既定 127.0.0.1 / bind_port 既定 8788）。
  `.gitignore`: `order_requests.json`（`menu.json` はコミット済みサンプル）。
- ADR/ISSUE: serene ブランチからの統合で **ADR-0013→ADR-0017** / **ADR-0015→ADR-0018** /
  **ISSUE-0013→ISSUE-0019** に採番替え（serene の cash-only ledger ADR-0014 は不採用、verify-v1 の
  ADR-0016 が置換）。詳細は `docs/worklog/2026-06-13-integrate-viewer-onto-verify-v1.md`。

### Changed / Added (S3 ledger 統合 — verify-v1 へのマージで実装を一本化, ADR-0016)

- **S3 ledger / points / settlement を ADR-0016 の実装へ一本化**（verify-v1 が持っていた
  ADR-0013 の core-only ledger を統合・置換）。fold による残高（ISSUE-0001）は踏襲し、
  **session_settlement / desktop viewer / CSV export** を追加。
  - 追加: `core/ledger.py` に `SessionSettlement`、`core/ledger_repository.py` に settlement
    （compute / commit / paid-unpaid）+ reversal（append-only 訂正）。
  - 追加: `gui/ledger_view.py`（`main.py --ledger`, S3.2 desktop viewer/editor, 別画面）、
    `output/ledger_csv_exporter.py`（`main.py --export-ledger`, S3.3 settlement/cashflow CSV, utf-8-sig）。
  - 契約: `docs/contracts/ledger-overview.md` / `ledger-schema.md` + `session_settlement.schema.json`
    + fixtures。`tests/test_contracts.py::_MODELS` に `session_settlement` を追加。
  - **ADR-0016**（新規, Accepted）が **ADR-0013 を Supersede**（fold 踏襲 + settlement/desktop/export）。
    番号衝突回避のため採番替え: 旧 ISSUE-0012/0013/0014（ledger）→ **0016/0017/0018**。
  - 削除: verify-v1 の `docs/contracts/ledger-points.md` / `tests/test_point_ledger.py`（ADR-0016 の
    `ledger-overview.md` / `test_ledger_repository.py` に統合）。error 階層は ADR-0016 版に統一
    （`InvalidAmountError` 等。旧 `InvalidKindError` / `plan_payment` / `adjust_points` は非採用）。
  - tests: `tests/test_ledger_repository.py`（23）/ `test_ledger_view_gui.py`（16）/
    `test_ledger_csv_exporter.py`（7）+ contract。dependency-free subset **96 passed**。

### Added (ローカル QA tooling — 実機・Windows なしの検証手段)

- **実機（RFID/Windows）なしで v1 を検証する**チェックリスト + 模擬ツール:
  - `docs/manual-qa-checklist.md`（新規）: インストール / テスト / replay / テキスト駆動 / 音声 E2E /
    RFID 模擬 / PHH / config トグル / GUI の 9 項目を「コマンド / 期待 / 見る点」で記載。各項目に
    要ハード（🖥️/🎤）か不要（💻）かを明示。
  - `tools/simulate_rfid.py`（新規）: 起動中アプリの `POST /rfid` に偽イベントを注入する CLI
    （`send` / `seat` / `board` / `status` / `register-demo`）。物理タグ無しでもカードが解決できるよう
    `register-demo` で合成デッキ（決定的 tag→card）を `rfid_cards.json` に登録。stdlib `urllib` のみ。
  - `tools/play_hand_text.py`（新規）: マイク / Whisper モデルなしで、テキスト（ディーラー読み上げ相当）を
    `parse_action` → `integration/replay.py:replay_events` に流し、rules-aware 再構築（合法手射影 /
    silent-fold / side-pot / 派生 confidence）→ `logs/<session>.json` まで丸ごと駆動する。
  - tests: `tests/test_tools_simulate_rfid.py`（合成タグ・register-demo・実 receiver への POST→queue）/
    `tests/test_tools_play_hand_text.py`（parse/skip/timestamp・legacy 確定&再現性・pokerkit smoke）。
    **全 311 passed, 0 skipped**。既存挙動は不変（additive な追加のみ）。

### Added (Phase E part 2 — seat→player 選択 GUI + session レイヤ live 有効化 / E3, v1 リリーストラック S2.x, ISSUE-0006 Resolved)

- **座席設定ダイアログで hand logger を session レイヤに接続できるようにした**（v1 issue #10 / Epic #4,
  ADR-0008 Pattern A の live 有効化, ISSUE-0006 Resolved）:
  - `gui/seat_selection.py`（新規）: `SeatSelectionDialog`（customtkinter モーダル）。席ごとに
    登録 player を割り当て／**未登録はその場で作成**／空席は割り当てない。map 構築・重複検証・
    carry-forward 解決は GUI 非依存の純関数に分離（CI で unit test、skip 0 維持）。
  - `gui/dashboard.py`: `session_layer.enabled` 時のみ「座席設定」ボタンを表示し、起動時に一度
    seating を促す。確定後は **carry-forward**（毎ハンドは出さない）、変更時のみボタンで再編集。
  - `integration/engine.py`: `IntegrationThread.set_seat_player_map()` を追加（map 更新＋接続の有効/無効を再評価）。
  - `main.py`（GUI モード）: `session_layer.enabled=true` で `PlayerRepository`/`SessionRepository` を構築し、
    `create_session` の **UUID4 hex を session_id** に採用、`session_repo` を `IntegrationThread` に DI。
  - **既定 off では完全に従来動作**（ボタン非表示・timestamp session_id・PHH 不変, rollback path）。
  - tests: `tests/test_seat_selection.py`（純ロジック）/ `tests/test_engine_session_setter.py`（setter 経由の
    write-through・有効/無効再評価）。**全 302 passed, 0 skipped**。

### Added (Phase S3 — session ledger + point ledger core, ADR-0013)

- **session ledger / point ledger の core 実装**（CLAUDE.md § Ledger & Points, hand logger / GUI とは未接続）:
  - `core/ledger.py` + `core/ledger_repository.py`: 金銭イベント記録（`buy_in` / `rebuy` /
    `add_on` / `order` / `adjustment` / `entry_fee`、cash+point 併用可、order 明細 enforce）、
    point grant（冪等性キー対応）/ 補正 / 残高取得、session 中間集計（buy-in 合計 / 注文合計、
    途中値）。永続化は `ledger.json`（アトミックリネーム, `.gitignore`）。
  - **ISSUE-0001 Resolved（ADR-0013）**: point 残高の source of truth は
    **point_ledger_entry の fold**（cached 残高なし・player に global・常に 0 以上）。
    point 不足は strict reject + `plan_payment` による cash 補完分割（業務ルール 3）。
    entry fee は cash only（業務ルール 1）。spend 系 point entry は core が同時生成。
  - **契約 draft（S3, v0.1 未 freeze）**: `docs/contracts/ledger-overview.md` +
    `schemas/{ledger_entry,point_ledger_entry}.schema.json` + fixtures。
    `error-shapes.md` / `validation-rules.md` / `repository-interfaces.md` に S3 セクション追加。
  - テスト: `tests/test_ledger_repository.py`（13）+ `tests/test_point_ledger.py`（5,
    ISSUE-0001 予告の回帰 4 本を含む）+ contract `_MODELS` 2 model 追加 + code↔contract。
    全 349 passed / ruff 緑。

### Fixed / Changed (review hardening — 全体レビューで検出した堅牢化, ISSUE-0012)

- **rebuy / 新ハンドの状態変更を IntegrationThread に一元化**（ISSUE-0012 Fixed）:
  - GUI の「リバイ」と CLI の `n` / `r` コマンドが `GameStateManager` を **GUI/入力スレッドから直接
    変更していたレース**（規約「スレッド間通信は queue のみ」違反）を解消。winner と同様に
    `AudioEvent`（`action="rebuy"` / `"new_hand"`）を queue に積み、IntegrationThread が適用する。
  - rebuy は `on_action` 通知レコードとして GUI/CLI に返る（スタック表示更新）。
    **`HandSummary.actions` には積まれない**（ポーカーアクションではないため。回帰テストで固定）。
  - 副次修正: CLI `n` が `game_state.new_hand()` 直呼びだったためハンドバッファ
    （actions / stack_start / board / hole_cards）が**リセットされていなかった**不具合も解消
    （queue 経由で `_start_new_hand` を通るようになった）。
- **RFID HTTP 受信の堅牢化**:
  - `rfid.bind_host` 既定を `0.0.0.0` → **`127.0.0.1`** に変更（受信は無認証のため安全側へ。
    ESP32 から受ける場合は LAN IP に変更 — `docs/installation.md` §5 / `docs/usage.md` 設定表 /
    `docs/troubleshooting.md` に手順を追記）。
  - `Content-Length` に **上限 16KB** を導入（巨大 POST による OOM 防止、超過は 413）。
    不正な `Content-Length` ヘッダは 400（従来はハンドラ例外）。
- **pokerkit 未導入時の起動クラッシュを解消**: `create_game_state("pokerkit")` が ImportError 時に
  warning を出して **legacy backend へ自動フォールバック**（既定 backend が pokerkit のため、
  未導入環境でも音声記録は継続できる。rules-aware 機能は無効）。
- **テスト/CI**:
  - 新規: `tests/test_engine_rebuy.py`（5）/ `tests/test_poker_engine_fallback.py`（2）/
    `tests/test_recognizer_amounts.py`（31 — `parse_amount`/漢数字/席除去のエッジを直接固定）/
    `tests/test_rfid_http.py` にペイロード上限テスト（3）。**全 330 passed, 0 skipped**。
  - CI に **ruff**（実バグ系 `F`/`E9` の最小ゲート、`pyproject.toml` 設定、vision 除外）を追加。
    既存コードの未使用 import 17 件を除去。

### Added (Phase E part 1 — hand logger × session 統合 write-through / E1+E2-core, v1 リリーストラック S2.x)

- **hand logger を S2 session レイヤに write-through 接続**（v1 issue #10 / Epic #4, ADR-0008 Pattern A）:
  - `config_default.json`: `session_layer.enabled`（既定 `false`）を追加。
  - `integration/engine.py:IntegrationThread`: `session_repo` / `seat_player_map`（seat→player_id）を
    **additive な DI** で受け取る（両方揃ったときのみ有効＝`_session_layer_active`）。
    - `_start_new_hand`: 有効時に `SessionRepository.assign_seat` を hand 単位でバッチ呼び出し
      （write-through）。個々の失敗は当該ハンドを止めず log に留める。
    - `_finalize_hand`: `resolve_seat_map_for_hand` で当該 hand の seat→player_id を解決し、
      **`HandSummary.players[i].player_id` を additive 埋め込み**（接続時のみキー追加、未割当 seat は None）。
  - **非接続時は従来どおり**（`session_repo`/`seat_player_map` 無し → `player_id` キーを足さない＝byte 互換、
    rollback path）。`GameStateManager` / `JsonWriter` / `PHHExporter` は不変（PHH に player_id は載せない）。
  - 出力は F3b で freeze した `hand` schema（`players[i].player_id` は UUID hex pattern・optional）に適合。
  - tests: `tests/test_phase_e_session_integration.py`（接続: assign_seat 永続 + player_id 埋め込み +
    schema 適合 / 非接続: キー不在）。**全 289 passed, 0 skipped**。
  - 残（後続）: `main.py` の session 選択 step（E2 UX）と **seat→player_id 選択 GUI**（E3, `gui/dashboard.py`,
    ISSUE-0006）。本増分は core 結線（DI + write-through）に留め、live 有効化 UX は分離。

### Added (Phase I — エンドユーザードキュメント / v1 リリーストラック)

- **非エンジニア向けドキュメント一式**（v1 issue #12 / Epic #4, ロードマップ Phase I）:
  - `README.md`（新規）: 概要・できること・動作要件・インストール・**クイックスタート**（起動 → 読み上げ例 →
    出力）・各ガイドへのリンク・プロジェクト状態。
  - `docs/installation.md`: Python 準備、`pip install`、**PortAudio（pyaudio）の OS 別手順**、初回モデル DL、
    マイク選択、RFID（HTTP / PC/SC、任意）。
  - `docs/usage.md`: 起動モード、**読み上げ語彙**（席「シートN」/ アクション ベット・コール・レイズ等 / 金額）、
    ハンド 1 回の流れ（ハンド開始 → アクション → ウィナー）、言い間違い/言い忘れへの自動補正、出力（JSON / PHH）、
    **設定リファレンス**（`config.json` 各項目）。
  - `docs/troubleshooting.md`: 起動不可 / マイク未認識 / 認識精度 / モデル DL / RFID / ルール / `needs_review` /
    ログ場所 の対処。
  - `pyproject.toml` に `readme = "README.md"` を追加（パッケージ long description）。
  - 内容は実装（`config_default.json` / `core/constants.py` の語彙 / `main.py` の CLI / 既定 `pokerkit`）と
    一致。検証: doc 間リンク解決、`pip install -e .` ビルド OK、テスト **286 passed**。
  - 残（Phase H, 要 Windows）: ワンクリックインストーラができたら README のインストール節を差し替え。

### Added (Phase H part 1 — パッケージング + CI / H1+H4, v1 リリーストラック)

- **パッケージング基盤（pyproject.toml）と CI（GitHub Actions）**（v1 issue #11 / Epic #4, ロードマップ H1/H4）:
  - `pyproject.toml`（新規, setuptools, `version 1.0.0.dev0`, `requires-python>=3.11`, entry point
    `pokerapp = main:main`）。依存を **core / `[pcsc]` / `[vision]` / `[dev]`** に分割。
    **vision 系（opencv-python / easyocr）を core から除外**（廃止予定 → `[vision]` extra）。
  - 依存に上限を付与（compatible-release pin）: `numpy>=1.24,<3` / `pokerkit>=0.7,<0.8` /
    `faster-whisper>=1.0,<2` / `pyaudio>=0.2.13,<0.3` / `customtkinter>=5.2,<6`。
  - `requirements.txt` を core のみ（vision 除外）に整理、`requirements-dev.txt`（テスト依存 = numpy /
    pokerkit / jsonschema / pytest）を新設。
  - `.github/workflows/ci.yml`（新規）: push / PR で `pytest tests/ --ignore=tests/test_vision.py` を実行。
    テストはローカルパッケージを直接 import し、core の重い依存（faster-whisper/pyaudio/customtkinter）は
    lazy import のため不要。numpy/pokerkit/jsonschema を入れて **skip 0**（importorskip 対象を全て導入）。
  - `.gitignore` に packaging artifacts（`*.egg-info/` 等）を追加。
  - 検証: `pip install -e . --no-deps` で package discovery / entry point OK、CI 相当コマンドで
    **286 passed, 0 skipped**。**PyInstaller ビルド（H2）/ コード署名（H3）/ 実機 E2E は Windows 環境が必要
    で後続**（ADR-0012 のとおり Phase H の E2E）。

### Changed (Phase G — pokerkit を live 既定 backend に切替 / v1 リリーストラック R)

- **live 既定 game-state backend を `legacy` → `pokerkit` に切替**（v1 issue #9 / Epic #4, ADR-0012）:
  - `config_default.json`: `engine.backend = "pokerkit"`。新規インストール（config.json 不在 →
    config_default をコピー）は **rules-aware 再構築**（actor 推定 / 合法手射影 / silent-fold / side-pot /
    派生 confidence）が既定で効く。
  - `requirements.txt`: `pokerkit>=0.7.0,<0.8.0` に pin（golden fixtures が pokerkit 0.7.x 挙動で凍結のため）。
  - **`legacy` は rollback として維持**（`engine.backend="legacy"` で従来 `GameStateManager`、挙動不変）。
    既存 config.json は legacy のまま（破壊的変更にしない）。
  - 切替の必要条件 = golden fixtures 5 ケース全緑（達成済）。**実機 E2E（音声→JSON/PHH）は Phase H**。
  - tests: `tests/test_phase_g_default.py`（既定 pokerkit / legacy rollback / 構築）。**全 286 passed, 0 skipped**
    （テストは明示 backend 構築のため既定切替の影響なし = 回帰なし）。

### Clarified (Phase F — F3c: PHH の check/call は標準どおり統一)

- **PHH の `check`/`call` は標準トークン `cc`（check-or-call）で統一が正**と確認（ロードマップ F3c の「区別」は
  PHH 非標準で pokerkit が parse 不能になるため**変更しない**）。`output/phh_exporter.py` に意図コメントを追加。
  check/call の区別は JSON ログの `action` フィールドに保持される（情報欠落なし）。

### Added (Phase F part 3 — hand / action schema freeze / F3b, v1 リリーストラック R5。ISSUE-0011 Fixed)

- **`hand` / `action` schema を `1.0` で freeze**（v1 issue #8 / Epic #4, ADR-0010 R5, ISSUE-0011）:
  - `docs/contracts/schemas/{hand,action}.schema.json`（draft 2020-12, `version 1.0`）。ISSUE-0011 の決定で
    **`additionalProperties: true`** のまま 1.0（`false` 化は後続、範囲膨張防止）。required は安定 core のみ
    （`action` は常時 12 フィールド、`hand` は cross-app core 6）。`pots`/`player_id`/`committed` 等の
    additive は optional。
  - `docs/contracts/fixtures/{hand,action}/`（canonical / valid-minimal / invalid-*）。
  - `tests/test_contracts.py`: `_MODELS` に `hand`/`action` 登録（schema↔fixture）+ `test_core_hand_action_match_contract`
    （`HandSummary.to_dict()` / `ActionRecord.to_dict()` の **code↔contract** drift 検知）。
  - `tests/test_reconstruction.py`: `test_golden_output_conforms_to_hand_action_schema`（golden 5 ケースの
    **実再構築出力が schema 適合** — code↔contract↔golden を結ぶ）。
  - **全 282 passed, 0 skipped**。**ISSUE-0011 Fixed**。残 F3: PHH の call/check 区別（F3c）。

### Added (Phase F part 2 — side-pot 連携 + golden 5 ケース全緑 / F3a, v1 リリーストラック R5)

- **`HandSummary.pots`（main/side pot スナップショット）を additive 追加**（v1 issue #8 / Epic #4,
  ADR-0009 §3, ロードマップ A3 吸収）:
  - `core/hand_log.py`: `HandSummary.pots: list`（既定 `[]`、`to_dict` に含む）。
    `[{"amount": int, "eligible_seats": [int,...]}, ...]`。
  - `integration/engine.py:_finalize_hand`: `pots=gs.pots()` を埋め込み（rules-aware backend が
    `end_hand` 時に pokerkit から算出、legacy は `[]`）。挙動は additive（既存フィールド不変）。
  - **golden fixtures `unequal-allin` を緑化**: スタック差 all-in（1000/3000/3000）→ main pot
    `3000 [1,2,3]` + side pot `4000 [2,3]`。`tests/test_reconstruction.py` の GREEN_CASES に昇格。
  - **既知バグ 5 ケースが全緑**（check-facing-bet / call-amount-from-state / silent-fold /
    out-of-turn-rfid / unequal-allin）。**DoD #2 達成**。`pots` 追加に伴い既存 4 fixtures を再凍結
    （hand 終了が manual winner のため pots=[]、挙動不変）。
  - tests: `tests/test_reconstruction.py`（`unequal_allin_main_and_side_pots` + 全緑確認）。
    **全 274 passed, 0 skipped**（legacy は `pots=[]` で additive、回帰なし）。
  - 残 F3: `hand`/`action` schema freeze（ISSUE-0011, `_MODELS` 登録）、PHH の call/check 区別。

### Added (Phase D part 5 — 派生 confidence + needs_review 5 条件 / D3, v1 リリーストラック R3。Phase D 完了)

- **解釈可能な 3 因子 confidence + 明示的 needs_review 条件**（v1 issue #7 / Epic #4, ADR-0009 §6,
  ISSUE-0009）— **rules-aware 経路のみ**（legacy の固定 8 行 `calc_confidence` は不変）:
  - `integration/engine.py:derive_confidence` を新設: `confidence = clamp(L·(w_A·A + w_Q·Q), 0, 1)`。
    **L=合法性ゲート**（pokerkit 受理=1.0 / 非受理=`_CONF_L_PENALTY`、最重要）、**A=合意度**（一致した
    存在ソース / 存在ソース）、**Q=ソース品質**（一致ソースの base 信頼度の noisy-OR、audio は whisper で
    スケール、RFID>audio>camera）。固定 8 行テーブルを廃し、単調・解釈可能に。
  - `_handle_rules_aware_action` を D3 化: `derive_confidence` を適用し、**needs_review 5 条件**を明文化
    （①pokerkit 非合法 ②高信頼 ASR×規則矛盾/④amount snap[=`apply_corrections.needs_review`]
    ③actor 競合[prior↔sensor] ⑤`confidence < REVIEW_THRESHOLD`）。これで `HandSummary.review_required`
    が監査可能な意味を持つ。
  - 重み較正（暫定）: 良好な audio-only は閾値超え＝自動 review しない（v1 は音声優先）。camera-only /
    低 whisper / 合成 fold は閾値未満＝review。最終較正は golden fixtures / F。
  - golden fixtures 4 ケースの confidence を再凍結。`call-amount-from-state` は review=False を維持。
  - tests: `tests/test_phase_d3_confidence.py`（derive_confidence の順位/ゲート/whisper/合意 + 閾値条件⑤、7）。
    **全 270 passed, 1 skipped**。**これで Phase D（D0/D1/D2a/D2b/D3）完了**（残 R は F3 の side-pot/freeze）。

### Added (Phase D part 4 — silent-fold 合成 / D2b, v1 リリーストラック R3)

- **silent-fold 合成（未宣言 fold を補い actor を物理/明示証拠へ追従）**（v1 issue #7 / Epic #4,
  ADR-0009 §4, ISSUE-0009）:
  - `core/poker_engine.py:fold_through` を **atomic + cap 対応**に強化: `max_folds` 超過/到達不可は
    `ValueError` で **状態を巻き戻す**（`copy.deepcopy` snapshot/restore、誤 fold を残さない）。
    合成した席列（`list[int]`）を返す。
  - `integration/engine.py:_resolve_actor` を D2b 化: 優先順位 **RFID seat 読み > 明示発話席** で
    sensed を決め、prior と異なれば `fold_through(sensed, max_folds=SILENT_FOLD_CAP=2)` で silent-fold
    合成。cap 超過/到達不可は prior 維持。合成 fold は `_append_synth_fold` で **fold アクションとして
    記録**（`confidence=0.3`、常に `needs_review`）。actor 推定に使った RFID 読みは消費（滞留防止）し、
    最終 actor と一致すれば corroboration に再利用。競合（sensed≠prior）は `needs_review`。
  - **golden fixtures 2 ケースが緑化**: `silent-fold`（audio 駆動）/ `out-of-turn-rfid`（RFID 駆動、
    actor を RFID 席へ補正し corroboration 成立）。`tests/test_reconstruction.py` の GREEN_CASES に昇格。
  - tests: `tests/test_phase_d0_engine.py`（fold_through の返り値/cap/atomic 3 追加）、
    `tests/test_phase_d2_wiring.py`（明示席 → silent-fold 合成に更新）、reconstruction 2 ケース。
    **全 263 passed, 1 skipped**（残 `unequal-allin`=F3）。legacy 既定は不変。
  - 残: 派生 confidence（D3、合成 fold の 0.3 較正含む）。

### Added (Phase F part 1 — 決定的 replay ハーネス + 最初の golden fixtures / F1, v1 リリーストラック R4)

- **決定的 replay ハーネス**（v1 issue #8 / Epic #4, ADR-0011）:
  - `integration/replay.py`（`load_events` / `replay_events` / `replay_fixture`）+ CLI `tools/replay_hand.py`。
    記録済み `events.jsonl`（R1 sidecar）を live と同じ `IntegrationThread` の per-event 処理に **timestamp
    昇順**で通し `HandSummary` を再構築する。
  - `integration/engine.py` に **clock 注入**（`IntegrationThread(clock=..., on_hand=...)`、additive）。
    `_now_iso` を `datetime.fromtimestamp(self._clock())` に、`_expire_buffers` を `self._clock()` に変更。
    **既定 `time.time` で live は完全不変**。
  - **golden fixtures**: `tests/fixtures/reconstruction/<case>/{setup.json, events.jsonl, expected_hand.json}`。
    D1/D2a で既に正しく再構築できる **2 ケースを緑で固定**: `check-facing-bet`（非合法 check→call+review）/
    `call-amount-from-state`（heard 9999 無視→engine の call 額 200）。残り 3 ケース（`silent-fold` /
    `out-of-turn-rfid`=D2b、`unequal-allin`=F3）は実装と同じ増分で追加（skip で明示）。
  - **round-trip 決定性**（DoD #3）: 同一 events.jsonl を 2 回 replay → 完全一致を `tests/test_reconstruction.py`
    で検証。fixtures は `reconstruction_event` schema 適合も確認。
  - tests: `tests/test_reconstruction.py`（7 + pending 3 skip）。**全 255 passed, 3 skipped**（clock 既定で
    既存テスト回帰なし）。

### Added (Phase D part 3 — rules-aware ライブ結線 / D2a, v1 リリーストラック R3)

- **rules-aware 経路（pokerkit）に `apply_corrections` をライブ結線 + actor 競合検出**（v1 issue #7 /
  Epic #4, ADR-0009 §1/§5）:
  - `integration/engine.py`: `_handle_audio_event` のベッティング処理を `gs.legal_context()` で分岐。
    **rules-aware（pokerkit、空でない legal_context）= `_handle_rules_aware_action`**（`apply_corrections`
    で合法手へ射影し ActionRecord に反映、`_resolve_actor` で明示発話席(`event.seat`)が手番(prior)と
    食い違えば `needs_review`）。**legacy（空 legal_context）= `_handle_legacy_action`（従来コードを
    そのまま分離・挙動不変）**。
  - これにより pokerkit backend で **call/check の状態一意化・非合法 action の修復・明示席の out-of-turn
    検出**がライブで効く（既定 legacy は不変）。
  - **silent-fold 合成（`fold_through` 結線で prior を上書き）・RFID/camera を含む多源 actor 解決・派生
    confidence（D3）は後続増分 D2b**（誤 fold リスクと滞留しうる RFID 読みの消費設計のため Phase F #8 の
    golden fixtures で検証）。D2a は prior 固定 + 明示席（イベント単位・滞留しない）の競合 flag に留める。
  - tests: `tests/test_phase_d2_wiring.py`（pokerkit 5: 射影/競合/legacy 分岐）。pokerkit 0.7.4 実走で
    **248 passed**（legacy 既存テストは `_handle_legacy_action` 経由で不変通過）。

### Added (Phase D part 2 — engine 境界の rules-aware メソッド / D0, v1 リリーストラック R3)

- **`PokerEngine` 境界に rules-aware の additive メソッドを追加**（v1 issue #7 / Epic #4, ADR-0009 §2）:
  `legal_context` / `is_legal_actor` / `pots` / `committed` / **`fold_through`** を Protocol に追加し、
  両 backend で conform させた。
  - `core/poker_engine.py`: `PokerkitGameState.fold_through(until_seat)` を**新規実装**（現 actor から
    until_seat 手前までを silent fold 合成 = ディーラー未宣言 fold の表現。到達不能は ValueError。
    上限は呼び出し側=actor 推定が距離で判断, ISSUE-0009）。`legal_context`/`pots`/`committed` は R2 で実装済。
  - `core/engine_types.py`（新規）: `LegalContext` を中立モジュールへ移設（循環 import 回避）。
    `core.poker_engine.LegalContext` として後方互換に再エクスポート。
  - `core/game_state.py`（legacy）: rules-aware でない stub を追加（空 `legal_context` = legacy 印 /
    `fold_through` は `NotImplementedError` / `pots`=[] / `committed`=0）。IntegrationThread は空 context を
    以て legacy 経路（従来挙動）へ分岐する設計（結線は D2）。
  - **ライブ未結線＝挙動不変**。actor 推定の結線（D2）は後続 PR。
  - tests: `tests/test_phase_d0_engine.py`（pokerkit 部は importorskip、legacy stub は常時実行）。
    pokerkit 0.7.4 を導入して実走 **243 passed**（未導入時は pokerkit 部 skip）。

### Added (Phase D part 1 — apply_corrections, v1 リリーストラック R3)

- **合法手への射影 `apply_corrections()` を実装**（v1 issue #7 / Epic #4, ADR-0009 §5）:
  raw ASR の (action, amount) を `LegalContext`（`legal_context()` 由来）の合法手へ射影する**純関数**
  （`audio/recognizer.py`、pokerkit 非依存）。
  - **call/check を状態から決定的に一意化**（`amount_to_call>0→call` / `==0→check`）。JA キーワードの
    曖昧さに依存せず、PHH/JSON で call と check を初めて区別できる核心。
  - bet↔raise を当ストリートのベット有無から再マップ、amount を合法レンジへ snap（大幅 snap / 額不明 /
    非合法は `needs_review`）。`Correction` 結果型（`corrected_from`/`reason`/`asr_confidence` を持つ）。
  - ベットに直面した "check" は暫定で **call + `needs_review`**（ISSUE-0009、尤度導入は後続）。
  - **ライブ未結線＝挙動不変**: actor 推定の engine 結線（D2）/ 派生 confidence 融合（D3）/ silent-fold
    合成は後続 PR（Phase F #8 の golden fixtures と併走）。ISSUE-0009 の初期方針を承認・記録。
  - tests: `tests/test_phase_d_corrections.py`（18, 修復表を網羅）。
    **全 222 passed, 10 skipped**（`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。

### Added (Phase B+C — イベント記録基盤, v1 リリーストラック R)

- **`AudioEvent` に `seat` / `confidence` を additive 追加**（v1 issue #6 / Epic #4, R3/R4 の前提）:
  - `core/events.py`: `AudioEvent.seat`（明示発話席）/ `AudioEvent.confidence`（Whisper 信頼度 [0,1]）を
    optional 追加。既存経路は未使用 = **挙動不変**。
  - `audio/recognizer.py`: `WhisperTranscriber.transcribe_with_confidence()` を追加（segment の
    `avg_logprob` 平均を `exp` で 0..1 に写像）。`transcribe()` は委譲。`parse_action(text, confidence=)`
    で confidence を受け、`_extract_seat_no()` で明示席（"シート3"/"seat 3"/全角）を populate。
  - `audio/recorder.py`: `_process_chunk` を `transcribe_with_confidence` 経由に変更し confidence を伝搬。
  - `output/event_recorder.py`: `event_to_envelope` の audio 分岐に `seat`/`confidence` を additive 露出
    （`reconstruction_event` schema は既に optional 定義済、code↔contract 緑）。
- **ISSUE-0010（記録境界・決定性）を Resolved**: 記録境界 = ASR decode 後（`seat`/`confidence` 含む）、
  clock 源 = 観測済み最大 event timestamp に確定。`docs/contracts/event-replay.md §4` を「決定」に更新。
  clock 注入・replayer・スレッド順序許容度の実証固定は Phase F（#8）の golden fixtures に委譲。
- tests: `tests/test_phase_bc_events.py`（10）+ `tests/test_event_recorder.py` 拡張。
  **全 204 passed, 10 skipped**（`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。

### Fixed (Phase A — コア堅牢化, v1 リリーストラック)

- **RFID カード未解決時にハンドを要レビュー化**（v1 issue #5 / Epic #4）: board / seat RFID
  イベントの `card` がカードマスター未解決（空文字）のままハンドが進んだ場合、その
  `HandSummary.review_required` を `True` にするようにした。従来は `logger.warning` のみで
  ハンドサマリーに反映されず、オペレーターが検出失敗に気付けなかった。
  - `integration/engine.py`: `IntegrationThread._hand_needs_review` フラグを additive 追加。
    card 未解決の board/seat 分岐で立て、`_start_new_hand` でリセット、`_finalize_hand` の
    `review_required` に OR 合成。解決済みカードでは立たない（誤検知ガード）。
  - tests: `tests/test_phase_a_hardening.py`（4）。**全 192 passed, 10 skipped**
    （`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。
  - 検証: faster-whisper 未導入時の起動は `audio/recognizer.py` の遅延 import（`__init__` の
    `try/except ImportError`）で既にクラッシュしないことを確認（コード変更不要）。

### Added (Phase R2 — pokerkit game-state backend, preview / default-off)

- **pokerkit を live ルール権威にした game-state backend**（ADR-0009, **default-off の preview**）:
  ノイジー入力からの正確な再構築のため、actor 順（ポジション順）/ 合法手集合 / amount_to_call / min-raise /
  **side-pot** を pokerkit に委ねる backend を追加。**既定 `legacy` で挙動不変**、`config.engine.backend=pokerkit`
  で opt-in。
  - `core/poker_engine.py`（新規）: `PokerEngine` Protocol（legacy/pokerkit 共通 I/F）＋ `PokerkitGameState`
    ＋ `create_game_state` factory。pokerkit は **遅延 import**（未導入でも legacy は動く）。announced winner を
    手動 push（pokerkit auto-showdown はダミーカードのため無効化）、side-pot スナップショット、seat↔index 固定。
  - `main.py`: `_make_game_state(cfg, ...)` で backend 選択（CLI/GUI 両経路）。`config_default.json` に
    `engine.backend: "legacy"` を追加。
  - **ISSUE-0008（Fixed）**: pokerkit 0.7.4 で必要 API（actor / 合法手 / min-raise / amount_to_call / side-pot の
    incremental 露出、不正額の `ValueError`、`HOLE_DEALING` でカード不要駆動）を spike で実機確認。ADR-0009 の
    gate 解除。**ADR-0009 を Accepted**（R2 engine 実装済 / R3 は planned）。
  - tests: `tests/test_poker_engine.py`（11: actor 順 / legal_context / street 自動進行 / 不正・非手番拒否 /
    side-pot / winner award / rebuy / **allin ショートスタック call-all-in**）。**全 198 passed**。
  - review fix: `apply_action("allin")` を「raise 可なら max へ raise、不可だが call 可なら call-all-in」に
    分離（レイズ不可なショートスタックの「オールイン」での pokerkit state desync を防止）。
  - 既知の差（legacy より正確側・preview）: ブラインド自動 post、合法手のみ受理（raw ASR の射影は R3）、
    street は betting 完了で自動進行。**live 既定動作（legacy）は不変**。

### Added (Phase R1 — event recording sidecar)

- **生センサーイベントの append-only sidecar 記録**（ADR-0010, record-only 先行実装）:
  `IntegrationThread` が**解釈する前**に各 `AudioEvent` / `RFIDEvent` / `CameraEvent` を
  `reconstruction_event` envelope（camera frame 除外）として `logs/{session_id}.events.jsonl` へ 1 行追記する。
  再構築ロジックは不変で、**recorder 未指定（既定）なら挙動完全不変**。
  - `output/event_recorder.py`（新規）: `EventRecorder` ＋ `event_to_envelope()`。append-only・スレッド安全・
    I/O 失敗で再構築を止めない。
  - `integration/engine.py`: `IntegrationThread(event_recorder=...)` を additive 追加。3 つの dequeue 点
    （audio get / camera drain / rfid drain）で解釈前に `_record()`。default None = 従来動作。
  - `main.py`: `config.recording.enabled`（既定 false, opt-in）で `EventRecorder` を構築し CLI / GUI 両経路で注入。
    `config_default.json` に `recording.enabled: false` を追加。
  - `docs/contracts/schemas/reconstruction_event.schema.json`（v0.1, `additionalProperties:false`）＋
    `fixtures/reconstruction_event/`（canonical / valid-* / invalid-*）。`tests/test_contracts.py` の `_MODELS` に登録。
  - tests: `tests/test_event_recorder.py`（envelope / JSONL / code↔contract）、
    `tests/test_integration_recording.py`（engine→recorder e2e / recorder 未指定で sidecar 無し）。
    **全 187 passed**（`pytest tests/ -q --ignore=tests/test_vision.py`）。
  - **ADR-0010** を Accepted に更新（R1 実装済。R4/R5 = hand/action freeze・replayer は planned）。

### Docs / Planning (Phase R0 — rules-aware reconstruction & contract-first hand core, 設計提案)

- **ハンド再構築エンジンと contract-first hand core の設計提案**（**docs-only, `.py` / schema / fixtures は
  未変更**）: 目的（ノイジーな ASR＋RFID からの正確な再構築）と思想（contract-first / fixtures-as-oracle）の
  両面のギャップに対し、再構築を「ルール制約付き状態推定」として捉え直し、既存依存 pokerkit を live
  ルール権威に据える方針を提案。
  - **ADR-0009** (Proposed): `pokerkit.State` を live ルール権威として採用し、その合法手制約で再構築する
    （ルール制約付き状態推定）。現 `GameStateManager` の安定 I/F 背後で `engine.backend` フラグ選択、raw ASR を
    直接流さない「境界での推定」、出力は additive。actor 推定（手番 prior × sensor ＋ silent-fold 自動合成）、
    `apply_corrections()`（合法手制約・call/check の状態一意化・amount スナップ）、派生 confidence（8 行固定
    テーブルの置換）と `needs_review` 条件の明文化。`pokerkit>=0.5.0` は宣言済みだが**未 import** である事実を
    明記（`game_state.py` の Phase 3 TODO の具体化）。当初の engine / algorithm 2 案を 1 ADR に統合。
  - **ADR-0010** (Proposed): hand core の contract 化（`hand`/`action`/`reconstruction_event`）と決定的
    record/replay（append-only event sidecar、注入クロック、golden fixtures を core の oracle に）。ADR-0008
    と整合し hand-logger immutability を維持。
  - `docs/contracts/hand-reconstruction.md`（新規 draft）: `PokerEngine` interface 草案 / actor 推定 /
    `apply_corrections` 修復表 / 派生 confidence / `hand`・`action` の inline schema sketch（freeze せず）。
  - `docs/contracts/event-replay.md`（新規 draft）: record/replay harness / 決定性条件 / `reconstruction_event`
    envelope sketch / golden-fixture レイアウトとテスト計画。
  - **ISSUE-0008**（Open）pokerkit online API 実現性（ADR-0009 の gate）/ **ISSUE-0009**（Open）actor 競合・
    silent-fold ポリシー / **ISSUE-0010**（Open）replay 決定性の記録境界 / **ISSUE-0011**（Open）hand/action
    schema freeze blockers（ISSUE-0005 の hand core 版）。
  - **decision-log.md** に ADR-0009/0010 と ISSUE-0008..0011 を登録。**CLAUDE.md** Future Scope に
    rules-aware reconstruction の planned/proposed 行を追加。
  - **実装は別タスク**（提案フェーズ R0）。段階導入順は R1 record-only → R2 pokerkit engine（flag）→
    R3 actor/corrections/fusion → R4 contracts → R5 freeze + session 統合。

### Added (WS2-α — Session / Seating Viewer, read-only desktop)

- **Session / Seating Viewer**: S2 core の session / hand-based seating を確認する
  **read-only inspection 画面**を desktop に追加（hand logger / player registry とは別画面）。
  - `python main.py --sessions` で起動（hand logger 通常起動 `python main.py` / registry
    `--players` は無改修・従来通り）。
  - session 一覧（session_id / started_at / status / label / hand 数 / assignment 数の要約）→
    選択で 概要 / current seating（最新 hand から導出）/ hand ごとの seat assignments を表示。
  - `player_id` を `PlayerRepository` で `display_name` に解決（不能なら `(unknown)`、`player_id`
    自体は保持）。
  - **再読込（refresh）** ボタンで `SessionRepository` / `PlayerRepository` をディスクから読み直す
    （別プロセスの更新取り込み）。live auto-refresh は持たない。
  - empty state（session 無し）/ no-data state（seating / hand 無し）を明示表示。
  - **read-only**: session/seat/player の作成・編集・削除を一切持たない（許容操作は refresh のみ）。
    業務ルールは core が source of truth、viewer は read API + name 解決 + 表示整形に徹する。
  - 実装: `gui/session_viewer.py`（`SessionViewerWindow`）、`main.py`（`--sessions` /
    `run_session_viewer`）。
- **core read API（additive）**: read-only viewer 用に enumeration / loading を core に追加。
  - `SessionRepository.list_hand_ids(session_id)`（記録済み hand_id を昇順列挙）。
  - `SessionRepository.reload()` / `PlayerRepository.reload()`（ディスクから再読込）。
  - いずれも additive な read 専用 API。既存 API・業務ルール・schema（0.x）は不変。
- **Tests**: `tests/test_session_viewer_gui.py` を追加（empty state / 一覧要約 / 選択→詳細 /
  current seating / name 解決 / unknown player 安全表示 / refresh 再読込 / read-only・別構造の確認）。
  全体 **192 passed**（ベースライン 177 に対し +15、回帰なし）。
- **Docs**: `CLAUDE.md`（§ Session / Seating Viewer 追加 + 実装状況表 / コマンド / Phase 2 WS2 更新）/
  `docs/contracts/repository-interfaces.md` / `session-seating.md`（`list hand ids` / `reload` を
  additive 追記）/ `hand-integration.md`（viewer が inspection 用である旨）/ ISSUE-0013（新規, viewer の
  data source 依存 + 拡張 open question。**旧番号 0008 から採番替え** — verify-v1 統合時に
  pokerkit feasibility の ISSUE-0008 と衝突したため）/ ISSUE-0006（seat change 可視化の関連注記）/
  `decision-log.md`（ISSUE-0013 登録）/ worklog（`2026-06-03-session-seating-viewer.md`）。
- **注（統合時更新）**: 元ブランチ時点では write-through 未実装だったが、verify-v1 統合時点では
  **E1〜E3 で実装済**（`session_layer.enabled`）。有効時は live の hand logger が書いた seating も
  viewer で確認できる（ISSUE-0013）。

### Docs / Planning (Phase S2.x — hand logger × session integration strategy)

- **Hand logger × session/seating integration の戦略 planning**（docs-only, code 未変更）:
  既存 hand logger world（`HandSummary` / `JsonWriter` / `PHHExporter` / `IntegrationThread` /
  `GameStateManager` / `main.py`）と S2 core（`SessionRepository`）の段階接続方針を確定。
  - `docs/contracts/hand-integration.md`（新規 draft）: 現状フロー整理 / 接続パターン A・B・C 比較 /
    推奨アーキテクチャ（Pattern A, write-through）/ player_id・session_id・hand_ref の決定タイミング /
    Phase 2.0〜2.4 → 3.x の段階 migration / HandSummary draft schema sketch / 互換ルール / open 論点。
  - `docs/contracts/session-seating.md` 更新: § freeze 状態 に ADR-0008 と hand-integration.md を相互リンク。
- **ADR-0008** (Accepted): Hand logger × session/seating integration strategy。
  Pattern A（write-through, additive）を採用。`HandSummary.players[i].player_id` を additive、
  `session_id` を session レイヤの UUID4 hex に切替（Phase 2.2）、PHH は無改変、`hand_ref` は
  session レイヤ側に住む、rollback path として `config.session_layer.enabled` フラグ planned、
  legacy logs/*.json は破壊しない。
- **ISSUE-0006**（新規 Open）: hand 開始時の seat→player_id 選択 UX が未確定。Phase 2.3 で確定。
- **ISSUE-0007**（新規 Open）: legacy hand log（timestamp session_id / player_id 無し）の取り込み
  方針が未確定。Phase 2.4 着手判断時に決める。
- **CLAUDE.md** 更新: Phase 2 セクションに「Phase 2.x（hand logger 接続, planning 済 / 実装 planned）」
  サブ節を追加。Pattern A / 細分 phase 2.1〜2.4 を記述。
- **decision-log.md** 更新: ADR-0008、ISSUE-0006、ISSUE-0007 を index に追加。ISSUE-0005 行に
  ADR-0008 リンクを追記。
- **本タスクで `.py` ファイルは変更していない**（planning-only ガード）。

### Added (Phase S2 — session + hand-based seating core)

- **Session & Seating core (S2)**: hand logger とは独立した session レイヤと hand-based
  seating の core 最小実装を追加（contract draft に対する実装。schema は未 freeze のまま）。
  - `core/session.py`: `Session` / `SeatAssignment` / `HandRef` データクラス。
  - `core/session_repository.py`: `SessionRepository`。create / list / get / close session、
    `assign_seat`（hand 単位の seat→player 割り当て）、`list_seat_assignments` /
    `resolve_seat_map_for_hand` / `resolve_hand_ref` / `current_seating`。
  - validation / errors: unknown session（`not_found`）/ already_closed / session_closed /
    seat_taken / **player_already_seated**（新 code）/ unknown_player / invalid_seat。
    `docs/contracts/error-shapes.md` の session セクションと 1:1。
  - 永続化: プロジェクト直下 `sessions.json`（アトミックリネーム、`.gitignore` 追加）。
    seat_assignment は session 配下に hand 単位で入れ子保持（将来 ledger を additive 拡張しやすい配置）。
  - 識別子: `session_id` は session レイヤが UUID4 hex で採番（hand logger の timestamp
    session_id とは別 namespace）。`hand_id` は int 据え置き（ADR-0006）。
  - **hand logger とは未接続**（`HandSummary` への player_id 接続 / reconciliation は S2 scope 外）。
- **ADR-0007** (Accepted): S2 session layer の永続形と `session_id` 採番方式の決定
  （独立採番 + 専用ストア = decoupled）。ISSUE-0005 #1 / #2 を core について確定。
- **ISSUE-0005** 更新: `session_id` 採番（#1）と seat_assignment 永続形（#2）を core について
  Resolved。hand logger 接続・seat change UI 要件は Open のまま（schema `1.0` freeze の残 blocker）。
- **Tests**: `tests/test_session_repository.py` を追加（session CRUD / persistence roundtrip /
  assign 成否 / 各 reject / resolve / code↔contract 整合）。
- **Docs**: `error-shapes.md`（session error を実装済に更新 + `player_already_seated` 追記）/
  `repository-interfaces.md` / `session-seating.md`（core 実装済を反映）/ `CLAUDE.md`
  （§ Session & Seating, 実装状況表, Phase 2 / freeze order）を更新。
### Docs / Planning (RFID hardware migration — PCSC canonical pivot)

> 注: 本セクションの ADR/ISSUE は verify-v1 統合時に **0007/0008→0014/0015（ADR）、0006/0007→0014/0015（ISSUE）に採番替え**（既存 ID との衝突解消）。

- **RFID hardware を PN5180 + ESP32-S3 に移行**する仕様変更の方針 ADR を **訂正**:
  - **ADR-0015** (Accepted, **supersedes ADR-0014**): ESP32-S3 が **USB CCID** として PN5180 ×N を
    PC/SC multi-slot で公開し、ホスト側は **pyscard 経由の PC/SC を canonical（本筋）** とする。
    `rfid/reader_thread.py` が第一系統。`rfid/http_receiver.py` は **optional secondary**
    （debug / remote 用）に降格。`reader_configs` の reader_name ↔ role/seat マッピング契約、
    `rfid_cards.json` 形式、confidence 行列、`RFIDEvent` は不変。`tag_id` UID 長は 4/7/8B 許容。
  - **ADR-0014** (**Superseded by ADR-0015**): 旧 ADR は「HTTP を canonical / PCSC を legacy」
    としていたが、これは作業者の誤想定による誤決定。history として残置。
  - **ISSUE-0015** (Open): ESP32-S3 USB CCID firmware ↔ host の契約（USB descriptors /
    reader_name / ATR / pseudo-APDU / 8B UID 取得 / hot-plug 通知）を register。
  - **ISSUE-0014** (**Superseded by ISSUE-0015**): HTTP API 契約を追跡していた旧 issue は本筋から
    外れたため Supersede。
  - **CLAUDE.md**: プロジェクト概要 / ディレクトリ構成 / 技術スタック / 実装状況 / エラーハンドリング
    方針を「PC/SC canonical（USB CCID 経由）/ HTTP optional secondary」に flip。
  - **decision-log.md**: ADR-0015 / ISSUE-0015 を追加、ADR-0014 / ISSUE-0014 を Superseded に更新。
  - フォローアップ（次タスク）: `rfid/reader_thread.py` / `rfid/bridge.py` docstring と
    `config_default.json` を PCSC canonical 前提に書き直し、`card_master.normalize_tag_id` の 8B UID
    テスト追加、`tests/test_rfid.py` に 8B UID PCSC fixture 追加、ESP32-S3 firmware USB CCID
    descriptor の確定を ISSUE-0015 に貼る。

### Docs / Planning (Phase 0b — S2 contracts)

- **S2 contract draft (session / seat_assignment / hand_ref)**: `docs/contracts/` に S2 の
  契約草案を追加（**未 freeze**, schema version 0.x）。
  - `session-seating.md`（モデル定義 / hand_id boundary / interface 草案 / freeze 状態・blockers）。
  - `schemas/session.schema.json` / `seat_assignment.schema.json` / `hand_ref.schema.json` と
    各 `fixtures/`（canonical / valid-minimal / invalid-*）。
  - `repository-interfaces.md` に session/seating の interface 草案を追記、`error-shapes.md` に
    S2 error code（`session_closed` / `seat_taken` / `unknown_player` / `invalid_seat` 等）を additive 追記。
  - `tests/test_contracts.py` の `_MODELS` に session / seat_assignment / hand_ref を登録（schema↔fixture 整合）。
- **ADR-0006** (Accepted): S2 session/seating contract boundary と hand_id の cross-app 参照。
  `hand_id` は session 内連番 int 据え置き、cross-app は `(session_id, hand_id)` 複合キー、
  参照単位は `hand_ref`（ISSUE-0004 の選択肢 A 採用）。
- **ISSUE-0004** → **Resolved**（ADR-0006）。**ISSUE-0005**（Open）: S2 freeze の未確定事項
  （`session_id` 採番方式 / seat_assignment 永続形 / seat change 表現）を登録。
- **shared-ids.md / versioning-and-freeze.md / README.md / CLAUDE.md**: hand_id reconcile 済・
  S2 draft 状況・freeze order #3 の状態を更新。
- **decision-log.md**: ADR-0006 / ISSUE-0005 を登録、ISSUE-0004 を Resolved に更新。

### Added

- **Player Registry (Phase S1)**: hand logger とは **別画面** の player 管理機能を追加。
  - `python main.py --players` で Player Registry 画面を起動（hand logger とは別起動）。
  - player の新規作成 / 一覧表示 / display_name リネーム。属性は `player_id`（UUID hex,
    永続・安定）+ `display_name` + `created_at`。
  - `players.json` への永続化（アプリ再起動を跨いで player_id が安定）。
  - validation: 空文字 / 前後空白のみ / 完全一致重複（前後空白除去後）を拒否。
  - 実装: `core/player.py`, `core/player_repository.py`, `gui/player_registry.py`。
  - hand logger とは未接続（session / ledger / settlement 接続は後続 Phase）。

### Docs / Planning

- **Contracts bootstrap (Phase 0a)**: `docs/contracts/` を新設し、contract-first 並行開発の
  単一 source を凍結。
  - `README.md` / `shared-ids.md` / `versioning-and-freeze.md` / `repository-interfaces.md` /
    `error-shapes.md` / `validation-rules.md`。
  - shared ID 契約: `player_id`（UUID4 hex, S1 確定）/ `session_id`（opaque string, S2 確定）/
    `hand_id`（現状 int, cross-app は (session_id, hand_id) 複合, S2 reconcile）。
  - `schemas/player.schema.json` (v1.0) + `schemas/shared-ids.schema.json` と、
    `fixtures/player/`（canonical / valid / invalid）。
  - freeze 定義・versioning（additive vs breaking）・drift detection の最小方針を文書化。
  - `tests/test_contracts.py`（schema 妥当性 / fixtures 整合 / code↔contract）を追加。
    `requirements.txt` に `jsonschema>=4.0.0` を追加。
- **ADR-0005** (Accepted): contracts repository layout & freeze workflow（ADR-0004 の具体化）。
- **Issue 0004** (Open): `hand_id` が int（hand logger）と cross-app 文字列契約で不整合。
  S2 の `hand_ref` で reconcile。
- **Issue 0003**: Phase 0a で部分緩和（contracts bootstrap + 最小 contract test）。
- **decision-log.md**: ADR-0005 / ISSUE-0004 を登録。
- **CLAUDE.md**: ディレクトリ構成に `docs/contracts/` を追加、Parallel development plan に
  契約 source 参照と Phase 0a 完了状況を追記。

- **Parallel development plan**: `CLAUDE.md` に `# Parallel development plan` 節を追加。
  4 workstream（WS0 contract / WS1 core / WS2 desktop / WS3 mobile）の依存関係、
  parallelizable / blocker、contract freeze order、mobile が mock で先行できる範囲、
  desktop / mobile 責務分離、将来 API/sync を入れても壊れにくい境界、phase 0–5 の
  構造化計画（goal / prerequisites / parallel tasks / blockers / done criteria）を明文化。
- **ADR-0004** (Accepted): contract-first parallel development / shared IDs
  (`player_id` / `session_id` / `hand_id`) / separate front-ends の判断。Alternatives
  （core-first 逐次 / front-end-owned logic / implementation-first 暗黙契約 /
  mobile = hand logger 移植）を却下。
- **Issue 0003** (Open): 並行開発の contract drift / 凍結タイミング / mock 乖離の
  blocking risk を登録（ISSUE-0001 が S3 ledger WS の直接 gate）。
- **decision-log.md**: ADR Index に ADR-0004、Major Issue Index に ISSUE-0003 を登録。
- 提案: mobile は **React Native**（iOS / Android 両対応のたたき台、最初は mock repository）を
  技術選定案とし、初期 screen skeleton は player registry の list / add / rename に限定。

### Docs / Spec

- **Bootstrap docs-as-code structure**: `docs/adr/`, `docs/issues/`, `docs/worklog/`,
  `docs/templates/`, `docs/decision-log.md` を新設。`CLAUDE.md` / `CHANGELOG.md` を
  本リポジトリに追加した。
- **CLAUDE.md**: 現時点の正仕様（hand logger Phase 7 まで）を記述するとともに、
  `Future Scope` セクションで **player registry / session ledger / point ledger /
  session settlement / cross-app boundary** を planned scope として明文化。
  `Documentation and Traceability Rules` を恒常ルールとして追加。
- **ADR-0003** (Accepted): hand logging 単体モデルから session ledger / store settlement /
  point ledger へドメインを拡張する判断と、Alternatives（HandSummary 埋め込み /
  後付け JSON / player-to-player settlement / session 単位 seat_assignment）を記録。
- **Issue 0001** (Open): point ledger の残高計算 source of truth が未確定であることを
  open question として登録（S3 着手前に解決必要）。
- **Issue 0002** (Open): player の display_name uniqueness 仕様（大文字小文字 / 全半角の
  同一視）の将来拡張を open question として登録。
- **CLAUDE.md**: § Player Registry (S1, 実装済) を追加し、future scope 表・Phase candidates・
  実装状況表を S1 実装に合わせて更新。
- **decision-log.md**: ADR Index に ADR-0003、Major Issue Index に ISSUE-0001 / ISSUE-0002
  を登録。

### Notes

- S1 は ADR-0003 の player モデル定義の最小実装。別画面分離は ADR-0003 の cross-app boundary
  方針の帰結であり、新規 ADR は起こさず worklog / decision-log に記録した。
- 次フェーズ候補: S2 (session + hand-based seating) → S3 (ledger entries + point ledger) →
  S4 (session settlement + paid/unpaid) → S5 (cross-app contract).
