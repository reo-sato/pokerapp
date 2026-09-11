# Issue 0021: PN5180 の 1 周ポーリングが遅すぎて複数 reader に載らない

## Date

2026-09-10

## Status

Fixed（firmware 実装済 / **実機未検証**）

## Severity / Priority

- Severity: Blocker（本番 11 reader 構成が成立しない）
- Priority: P1

## Area

rfid / firmware（`firmware/esp32s3-pn5180-ccid/main/pn5180_reader.c`）

> **用語（2026-09-10 更新, ADR-0041 / 契約 v1.2）**: 本 issue の「slot」は **物理 reader（index）** を
> 指す。USB 上の CCID slot は常に 1 つで、物理リーダーは Get UID の P2（reader index）で選ぶように
> 変わった（Windows の汎用 CCID ドライバが 1 slot しか公開しないため）。firmware の台数設定も
> `CCID_SLOT_COUNT` → **`PN5180_READER_COUNT`** に分離済み。

## Expected Behavior

- 1 reader あたりの inventory は **カード無しで数十 ms 以内**（目標 ≤ 15 ms）。
- 本番 **11 reader**（席 8 + board 3）で **1 周 ≤ 0.5 s**（できれば ≈ 200 ms）。
  `CARD_POLL_INTERVAL_MS`（既定 100ms）と合わせて、カードを置いてから host（`RFIDThread`）が
  UID を得るまでの遅れが実用の範囲に収まること。
- `PRESENCE_HOLD_MISSES`（3）は**サイクル数**なので、1 周が伸びるとカード離脱の判定時間も
  同じ比率で伸びる（1 周 1 秒なら離脱検知に 3 秒以上かかる）。

## Actual Behavior

commit `90e0533`（複数 reader 準備）の firmware を **実機 1 slot** で動かしたときの
`poll 統計` ログ（10 秒窓 × 3、カード無し・有りの両方を含む）:

```
poll 統計(直近 N 周): 1 周 min/avg/max = 176/365/839 ms, ...
poll 統計(直近 N 周): 1 周 min/avg/max = 361/726/890 ms, ...
poll 統計(直近 N 周): 1 周 min/avg/max = 419/701/860 ms, ...
```

**reader 1 台**で 1 周が 0.18〜0.89 秒。単純比例で 11 台にすると 1 周 **2〜10 秒**となり、
カード検出も離脱判定も実用にならない。

## Reproduction

1. `firmware/esp32s3-pn5180-ccid` を `CCID_SLOT_COUNT=1` / `POLL_STATS_INTERVAL_MS=10000` で
   ビルド・書き込み（`PN5180_FAST_INVENTORY` 導入前 = commit `90e0533`）。
2. `idf.py monitor`（UART 側）で 10 秒ごとの `poll 統計(直近 N 周): 1 周 min/avg/max = …` を見る。
3. カードを置いた状態・置かない状態のどちらでも 100ms を大きく超える。

## Root Cause

jef-sure/pn5180（v0.1.x）の `proto->get_all_uids()` が **堅牢性優先の総当たり**になっている
（`src/pn5180-15693.c: pn5180_15693_get_all_uids` / `pn5180_15693_inventory_single_slot`）:

1. **RF 設定 2 種 × データレート 2 種 = 最大 4 回の inventory** を毎回試す
   （`rf_fallbacks[] = {ASK10, ASK100}` × `iso15693_use_high_rate = true → false`）。
   各 inventory の RX 待ちは `timeout_ms = 40`。
2. **見つかっても break しない**: 打ち切り条件が
   `uids_count > 0 && iso15693_use_high_rate == false` なので、**high rate で見つかった場合は
   break せず**次の RF 設定も走る。カードがあっても必ず 2 回の RF セットアップ + inventory になる。
3. 各 RF 設定の切り替えで `setupRF` = RF off → `LOAD_RF_CONFIG` → RF on（SPI 数往復）。
4. 衝突時は DFS + `esp_random()` による 5〜15 ms の jitter sleep リトライ（最大 3 回 / 枝）。
5. 加えて `pn5180_t.timeout_ms` の既定は **500 ms** で、これは SPI の BUSY 待ち・transceive
   状態待ち・RF off 待ちすべての上限。1 台の不調が 1 周を 0.5 秒伸ばす。

本番カードは **ICODE SLIX（ISO 15693, 8B UID）1 種のみ**なので、この総当たりは丸ごと不要。

## Fix

`firmware/esp32s3-pn5180-ccid/main/` に **自前の高速 inventory 経路**を実装
（`PN5180_FAST_INVENTORY=1`、既定）。ドライバの public API だけを使い、ドライバ本体は変更しない。

- **RF 設定は起動時に 1 回だけ**ロードする（`pn5180_loadRFConfig`）。`pn5180_init` は共有 RST を
  pulse して全チップをリセットするため、**全 reader の init が終わってから**別ループでロードする。
- poll では **RF ON → 1ms 待ち → INVENTORY（1 slot, high data rate）→ 応答待ち ≤ 10ms → RF OFF**。
  応答待ちはログを吐かない自前ループ（`pn5180_wait_for_irq` は timeout ごとに `ESP_LOGE` を出す）。
- **複数枚（重ね置き）対応**: 1 回の INVENTORY は「mask に合致するカードが 1 枚のときだけ」応答が
  成立するので、衝突したら mask を 1 bit 伸ばして 2 分割する **mask ベースの DFS**（固定長スタック）
  で最大 `PN5180_MAX_CARDS_PER_READER`（4）枚まで列挙する。probe 回数は
  `PN5180_FAST_MAX_PROBES`（12）で打ち切り（= 1 reader の所要時間の上限）。
- probe の 3 値判定: 受信 0 byte（protocol error のみ）= NONE / 10 byte で衝突フラグ・CRC・protocol
  error のいずれも無し = UID / **それ以外（衝突フラグ、10 byte 以外、バイトはあるが CRC・protocol
  error）= COLLISION として分割**。CRC 崩れを NONE に倒すと「衝突フラグ無しで 2 枚の応答が重なり
  続ける」ケースで両方とも永久に読めなくなるため、分割側に倒す（ノイズなら子 2 枝が NONE で終わる
  = 2 probe 損するだけ。probe 上限で時間は有界）。
- scan 中だけ `pn5180_t.timeout_ms` を 40 ms に絞る（ドライバの `get_all_uids` と同じ手当て）。
- `PN5180_FAST_INVENTORY=0` でドライバ経路に戻せる（A/B 比較用。cache 形式は共通）。

見積り（simulator 実測の probe 数 × probe あたり ≈ 5〜10 ms）:

| 状態 | probe 数 | 概算 |
|------|---------|------|
| カード無し | 1 | ≈ 10 ms（RX timeout いっぱい） |
| 1 枚 | 1 | ≈ 6 ms |
| 2 枚（下位ビットが違う = 通常） | 3 | ≈ 17 ms |
| 3 枚（flop） | 5 | ≈ 28 ms |
| 2 枚で下位 7bit が同一（最悪） | 12（打ち切り） | ≈ 70 ms |

→ 11 slot がすべて空なら 1 周 ≈ 110〜150 ms、実運用（席に 2 枚 + board）で ≈ 200〜300 ms。

（上表は Stay Quiet 導入前の見積り。実機結果と改訂後の probe 数は次節を参照。）

## 実機結果（2026-09-10, commit `2ebe914` を 1 slot で実行）

### 良かった点

- `poll 統計`: **カード無しで 1 周 15 ms**（従来 176〜890 ms）。**目標達成**。
- 1 枚: 安定して読める。
- 2 枚重ね: 読める（`2 枚 [E0:04:01:53:1A:41:10:9D, E0:04:01:53:1D:CA:FC:10]`）。
- host 側（`probe_pcsc raw` / `watch`）の連結分割・UID 単位 event 化は正しく動作。

### 残った不具合

1. **2 枚重ねが 2↔1 でちらつく**（数百 ms 周期）。常に `1D:CA:FC:10` 側が残り、`1A:41:10:9D` 側が
   欠ける。host の `watch` では欠けた札が 20 秒で **5 回再発火**した。
2. **3 枚重ねで `3 枚` が一度も出ない**（`1 枚` ↔ `2 枚` のみ。2 枚のときは常に同じ 2 枚）。

### 追加の Root Cause

1. **ちらつき = presence hold が slot 単位だった**。`pn5180_reader_poll_once` の debounce は
   「検出 **0 枚**のときだけ前回集合を hold」する作りで、**検出 count>0 なら集合を丸ごと置換**して
   いた。2 枚中 1 枚を 1 回取りこぼした瞬間に host へ「1 枚」が伝わり、次の poll でまた 2 枚に戻る。
2. **3 枚目が出ない = capture effect**。1 slot inventory は本来「mask に合致するのが 1 枚のときだけ
   応答が成立」する前提だが、実機では 2 枚が同時応答しても PN5180 が衝突を検出せず**強い方だけを
   正しく復号**することがある（近接した重ね置きでは受信電力差が大きい）。その枝は `PROBE_UID` で
   終わるため、弱い札は mask DFS でも永久に現れない。ISO/IEC 15693 の標準解は
   **見つけた札に STAY QUIET を送って黙らせ、root(mask 0) を再 probe して残りを拾う**ことで、
   jef-sure ドライバの `get_all_uids()` も UID を読むたび `pn5180_iso15693_stay_quiet` を送っている。
3. **（simulator で追加発見）衝突が「カード無し」に倒れていた**。`fast_probe_15693` は
   `RX_STATUS` の受信バイト数 `n` を先に見て `n == 0` を PROBE_NONE にしていたため、
   **SOF で衝突して「collision=1 / 受信 0 byte」で返るケース**を「カード無し」と判定していた。
   register-level simulator に commit `2ebe914` のコードをリンクして走らせると、2/3/5 枚のどれでも
   probe 1 回で終わり分割が一度も起きなかった。実機で 2 枚が読めていたのは、衝突が
   「バイトはあるが CRC/protocol error」の形で返る回があったため（そちらは分割側に倒していた）。

### 対処（本タスク）

- **Stay Quiet + root 再 probe ループ**（`fast_stay_quiet_15693` / `fast_inventory_15693`）。
  見つけた UID ごとに STAY QUIET（`flags=0x22`, `cmd=0x02`, UID 8B LSB-first、応答なし）を送り、
  DFS が尽きたら root を再 probe する。「応答なし」が返った時点で全枚数を拾い終わり。
  quiet は RF off で解除されるので、**fast 経路は最後に必ず `pn5180_setRF_off()`** を呼ぶ
  （`PN5180_RF_OFF_BETWEEN_READERS=0` でも）。
- **衝突フラグを受信バイト数より先に見る**（上記 3 の修正）。
- **presence hold を UID 単位に**（`s_uid_miss[slot][k]`）。検出集合と前回集合をマージし、
  欠けた UID だけを `PRESENCE_HOLD_MISSES`(3) サイクル保持する。
- `PN5180_FAST_MAX_PROBES` 12 → **16**（root 再 probe と最後の「応答なし」確認ぶん）。
- **実機フィードバック 2（commit 8e737f4 + bc9051b）**: 3 枚重ねで `3 枚` が出て 20 秒放置の再発火も
  0 件になったが、**3 枚載せた 1 周が ≈170 ms**（カード無し 15 ms）= probe 上限 16 回まで毎回空回り。
  原因は Stay Quiet の**送信完了待ち上限 3 ms がフレーム長（12 byte ≈ 3.7 ms @26.48 kbps）より短く**、
  次の INVENTORY の `pn5180_sendData` が idle 遷移で送信中のフレームを打ち切っていたこと（タグに届かない
  = quiet 不成立 → 毎ラウンド同じ 3 枚を DFS で見つけ直す）。上限を 10 ms に、送信後 500 µs（t1）空ける。
  併せて `poll 統計` に `probe 最大 N 回/reader` を追加（quiet 不発の検出用）。
- Stay Quiet が効かない札（規格外 / 送信失敗）で probe 上限まで空回りしないよう、
  **新しい UID が増えなかったラウンドが 2 回続いたら打ち切る**。

改訂後の probe 数（simulator 実測、quiet が効く場合）:

| 状態 | probe 数 | 概算 |
|------|---------|------|
| カード無し | 1 | ≈ 10 ms（RX timeout いっぱい） |
| 1 枚 | 2 | ≈ 16 ms（UID 1 + 確認の「応答なし」1） |
| 2 枚 | 3〜4 | ≈ 22〜28 ms |
| 3 枚（flop） | 4 | ≈ 28 ms |
| Stay Quiet が効かない札 1 枚 | 3（打ち切り） | ≈ 22 ms |

**確認 probe のぶん、カードが載っている reader は 1 台あたり +10 ms 程度増える**（空の reader は
変わらず 1 probe）。11 slot 全部にカードを置いた最悪ケースが 300 ms を超えるようなら、
`PN5180_FAST_RX_TIMEOUT_MS`（10 ms。実応答は ≈ 4 ms で来る）を先に詰める。

## 実機フィードバック 3（2026-09-10, commit `73ecd29` を 1 slot で実行）

### 実測

| 状態 | probe 最大 | 1 周 |
|------|-----------|------|
| カード無し | 1 | **15 ms** |
| 2 枚重ね | 5 | max 64 ms |
| **3 枚重ね** | **14** | **≈150 ms** |
| 1 枚を載せて動かす（`CCID_SLOT_COUNT=2` / ready 1 slot） | **16（上限）** | min/avg/max = 6/72/160 ms |

Stay Quiet 自体は効いている（2 ラウンド目の root が NONE で終わる）。3 枚・ちらつきの不具合は解消。

### Root Cause 4: 下位ビットが揃った UID で 1 bit DFS が空枝を舐める

3 枚（`…1A:41:1D:2E` / `…1A:41:35:B3` / `…1D:CB:00:CE`）の UID を **LSB-first** で見ると先頭バイトが
`2E` / `B3` / `CE` で、`2E` と `CE` は **下位 5 bit が同一**（bit0..4 = 0,1,1,1,0）。1 bit ずつ mask を
伸ばす DFS では衝突が 6 段続き、各段で「札のいない兄弟枝」を `PN5180_FAST_RX_TIMEOUT_MS`(10 ms)
いっぱい待つ。内訳（手計算が実測と一致）:
**root 1 + 衝突 6 + 空枝 NONE 5 + UID 3 + 確認 NONE 1 = 14 probe**。
空枝 5 × 10 ms と衝突 6 段が丸ごと無駄。

### Root Cause 5: 磁界の縁のノイズを「衝突」に倒して DFS を上限まで展開していた

1 枚しか載せていないのに probe が上限 16 に張り付く。`fast_probe_15693` が
**「バイトはあるが CRC/protocol error（衝突フラグ**無し**）」を PROBE_COLLISION に倒していた**ため、
カードが磁界の縁にあるとノイズ受信のたびに 2 分木が上限まで広がる（**ノイズは子枝でもノイズ**なので
分割しても消えない）。実機では本物の同時応答は衝突フラグが立つ（2 枚・3 枚とも分離できている）。

### 対処（本タスク）

- **A. `RX_COLL_POS` で衝突位置まで mask を一気に伸ばす**。`RX_STATUS` の bits 25:19 は受信フレーム内で
  最初に衝突したビット位置。ISO15693 の応答は `flags(8) DSFID(8) UID(64, LSB-first)` なので
  **UID bit i = フレーム bit 16+i**。衝突ビットより前は正しく受信できているので `pn5180_readData` で
  読み、その prefix を mask にして子を `(prefix|1<<pos, pos+1)` / `(prefix, pos+1)` の 2 本にする
  （**どちらにも必ず札がいる** = 空枝 probe が消える）。
  取れない（衝突が flags/DSFID 内 / 受信バイト不足 / readData 失敗 / cur.mask と不整合）ときは
  **従来の 1 bit 伸ばしに fallback**。基準（フレーム先頭か UID 先頭か）が実機で違っても、
  Stay Quiet + root 再 probe ループが取りこぼしを回収するので**正しさは落ちず効率だけが落ちる**。
- **B. 定常状態は「もう居ない」確認 probe を間引く**（`PN5180_FAST_CONFIRM_EVERY=5`, 0 で従来動作）。
  1 ラウンド目で見つけた集合が前回 cache と同一なら、5 poll に 1 回だけ確認 root を送る。
  11 台に札が載っていると確認だけで ≈90 ms/周かかるため。集合が変化した poll・前回 0 枚・
  カード無しでは必ず確認する（capture で隠れた札の発見は最大 5 poll ≈ 1.5 s 遅れる）。
- **C. `PN5180_FAST_RX_TIMEOUT_MS` 10 → 8 ms**。応答は要求送信(≈1.5 ms) + t1(0.32 ms) +
  12 byte(≈3.7 ms) ≈ 5.5 ms で来る（`pn5180_sendData` は送信開始で戻る）ので余裕 2.5 ms。
- **D. ノイズは分割せず「同じ node を 1 回だけ再 probe」**（`PROBE_NOISE`）。再試行でも壊れていれば
  **NONE 扱い**（DFS を広げない）。稀に「衝突フラグ無しで 2 枚が重なる」ケースがあっても、次の poll +
  UID 単位 hold + B の再確認で回収する。probe は 1 node あたり最大 2 回で有界。

改訂後の probe 数（simulator 実測、実機と同じ 3 枚の UID）:

| 状態 | probe 数 | 概算 |
|------|---------|------|
| カード無し | 1 | ≈ 8 ms |
| 1 枚 | 2（定常 1） | ≈ 16 ms |
| 2 枚 | 4（定常 3） | ≈ 32 ms |
| 3 枚（下位 5bit 同一を含む） | **6**（定常 5）※従来 14 | ≈ 48 ms |
| ノイズが続く（1 枚） | 2〜3 | ≈ 24 ms |
| coll_pos 無効（fallback） | 14 | ≈ 112 ms |

## 実機フィードバック 4（2026-09-11, `076b844` を 9 台 ready で実行）

初めての多台数実測（10 台接続、ready 9 = 未通電 #4/#11 を除く。ISSUE-0023 の緩和策込み）:

| 状況 | 1 周 min/avg/max | 最長 reader | probe 最大 |
|---|---|---|---|
| カード無し〜1 台に 1 枚 | **124 / 125〜127 / 139〜150 ms** | 28 ms | 2 |

- 1 台あたり ≈ 14 ms（1 台のときの 15 ms と同じ = 台数に線形）。**11 台なら ≈ 155 ms**（カード無し）。
- 全席に札を載せた実運用（席 2 枚 × 8 + board 3/1/1）は未計測。1 枚で 28 ms、2 枚 ≈ 32 ms、
  3 枚 ≈ 48 ms の見積りから **≈ 270 ms** の見込み = 目標 ≤ 300 ms の上限付近。超えるなら
  `PN5180_FAST_RX_TIMEOUT_MS`（NONE 応答の待ち 8 ms が各 reader の確認 probe に乗る）と
  `CARD_POLL_INTERVAL_MS` を見直す。
- 同じ札を 9 台に順に置いて **全 9 台で検出・離脱が 1 行ずつ**（ちらつき無し、`coll_pos fallback 0`、
  `ノイズ再試行 0`）。

## 実機フィードバック 5（2026-09-11, `405f218` を 10 台 ready で実行）

| 状況 | 1 周 min/avg/max | 最長 reader | probe 最大 |
|---|---|---|---|
| カード無し | **138 / 138 / 140 ms** | 14 ms | 1 |
| 1 台に 1 枚 | 138 / 139〜140 / 153 ms | 28 ms | 2 |

- **1 台あたり ≈ 13.8 ms（カード無し）**で 9 台のとき（≈ 13.8 ms）と同じ = 台数に線形。
  **11 台なら ≈ 152 ms**（カード無し）。
- `poll 統計` の 1 周は **スキャン時間だけ**で、`CARD_POLL_INTERVAL_MS`（100 ms）は含まない。
  実際の周期 = 1 周 + 100 ms なので、カード無しでも周期は ≈ 240 ms。
- **カード無しの 1 台 13.8 ms のうち ≈ 8 ms は `PN5180_FAST_RX_TIMEOUT_MS`**（「もう居ない」確認 probe が
  応答を待つ時間）。11 台では ≈ 88 ms がこの待ちになる。1 周を縮めたいときの第一の lever。
- 全席に札を載せた実測は未取得。見積り（席 2 枚 ≈ 32 ms × 8 + board1 3 枚 ≈ 48 ms + board2/3 1 枚 ≈ 28 ms × 2）
  = **≈ 360 ms** で目標 ≤ 300 ms を超える可能性がある。超えたときの調整順は
  `PN5180_FAST_RX_TIMEOUT_MS`（8→5〜6）→ `PN5180_FAST_CONFIRM_EVERY`（5→10）→
  `CARD_POLL_INTERVAL_MS`（100→50 or 0。周期は縮むが RF デューティと電流は上がる）。

## 実機フィードバック 6（2026-09-11, `405f218` を 10 台 ready + 満載で実行）= 目標未達

```
poll 統計: 1 周 min/avg/max = 529/538/550 ms, 最長 reader 69 ms, probe 最大 6,
           coll_pos fallback 255, ノイズ再試行 0, ready 10 reader
reader 0: coll_pos=17（UID bit -1）, 受信 1 byte, cur.len=0 → fallback(1bit)
```

8 席 × 2 枚 + board を載せると **1 周 538 ms**（目標 ≤ 300 ms / 上限 0.5 s も超える）。

### Root Cause 6: `RX_COLL_POS` は実機では一度も使えない（= 常に 1 bit DFS）

- ISO15693 は UID を **LSB-first** で送るので、変化の大きいシリアル下位バイトが最初に流れ、
  衝突は必ず **UID 先頭バイト**（`coll_pos` 16〜19 = UID bit 0〜3）で起きる。
- そのとき PN5180 が返す受信は **1 byte（flags だけ）**。`fast_fill_coll` は衝突ビットを含むバイトまで
  受信できていることを要求する（`if (n < 2 + pos/8 + 1) return;`）ので、**必ず `pos=0xFF`**
  （ログの `UID bit -1`）になり 1 bit 伸ばしに落ちる。`coll_pos fallback` が 10 秒で 255 回 = 全滅。
- 1 bit DFS は「**衝突位置より手前**で割る」ため、衝突が bit 1 以降だと **必ず片方の子枝が空**になり、
  その probe が RX timeout（8 ms）を丸ごと待つ。残った枝はまた衝突するので bit 0,1,2… と刻む。
  実測 probe 6 / reader 53 ms はこの形。**実装 A（coll_pos DFS）は実機では効いていなかった。**

### 対処（実装 C = 狙い撃ち probe, `PN5180_FAST_TARGETED_PROBE`）

前回 cache の UID を **`mask_len=32` の完全一致 inventory** で 1 枚ずつ直接呼ぶ。合致する札は
最大 1 枚なので **衝突が起きず即答**する。当たった札は Stay Quiet して、続く root probe には
**新しい札だけ**を残す（定常状態は root 1 回 NONE で終了）。

| 状況 | probe | 内訳 |
|---|---|---|
| 定常（N 枚が載ったまま） | **N+1** | 狙い撃ち N（即答）+ root 1（NONE） |
| 1 枚増えた | N+2 | 狙い撃ち N + root(新 UID) + root(NONE) |
| 全部新規（空 → 載る） | 従来と同じ | cache が空なので前段は走らず root + DFS |

**併せて応答待ちの上限をフレーム長に連動**させた（`tx_us + PN5180_FAST_RX_TIMEOUT_MS`）。26.48 kbps で
1 byte ≈ 0.30 ms なので mask 64 bit のフレームは 13 byte ≈ 3.9 ms かかり、固定 8 ms では応答
（tx+t1+rx ≈ 7.9 ms）の直前で打ち切る = **狙い撃ちが常に外れる罠**だった。mask を 32 bit にしたのも
同じ理由（フレーム 9 byte ≈ 2.7 ms、応答到着 6.7 ms、上限 10.7 ms で 4 ms の余裕）。
32 bit は MSB-first 末尾 4 byte = **ICODE のシリアル 4 byte 全部**なので実用上一意。

`PN5180_FAST_CONFIRM_EVERY`（確認 probe の間引き）は定常状態では発動しなくなる（root が 1 ラウンド目で
NONE を返して break するため）。結果 **新しい札の検出が毎 poll に戻る**（間引きの副作用が消える）。

### 追加した計器

`poll 統計` に **`狙い撃ち H/P 命中`**（H = 当たった数 = 載ったままだった札）と
**`応答待ち最長 x.x ms`**（**応答が返った probe だけ**の最長待ち）を追加。後者は
`PN5180_FAST_RX_TIMEOUT_MS` をどこまで下げられるかを実測で決めるための計器
（無応答 probe は timeout いっぱいなので測る意味が無く、除外している）。

## Regression Check

**実機で** `idf.py monitor` の `poll 統計` を見る（自動テスト不可 = 実 RF が要る）:

- `CCID_SLOT_COUNT=1`（カード無し）: **1 周 ≤ 20 ms**（2026-09-10 実機で 15 ms 達成）。
- `CCID_SLOT_COUNT=11`（実運用の配置）: **1 周 ≤ 300 ms**（上限 0.5 s）。
- `python tools/probe_pcsc.py raw` で、席に 2 枚重ねたときの Get UID 応答が **18 byte**（16 + SW）、
  flop 3 枚で **26 byte**。`watch` で 1 slot から UID が枚数ぶん出る（host v1.1 の分割込み）。
- **3 枚重ねで UART に `🎴 reader N: 3 枚 [...]` が出る**（`1 枚`↔`2 枚` を往復しない）。
- **3 枚重ねで `probe 最大 ≤ 8` かつ 1 周 ≤ 60 ms（1 slot）**（実機フィードバック 3 の A/B/C）。
  超えるなら `poll 統計` の `coll_pos fallback` が増えているはず = RX_COLL_POS の基準が想定と違う
  （起動直後の `reader N: coll_pos=… → 採用/fallback` INFO 3 行で生値を確認する）。
- **11 slot 全部に札を載せて 1 周 ≤ 300 ms**（席 2 枚 × 8 + board 3/1/1）。
- **1 枚だけ載せて動かしても `probe 最大` が 16 に張り付かない**（`ノイズ再試行 N` が増えるだけ）。
- **ちらつきが無い**: 2 枚 / 3 枚を置いたまま 20 秒放置して、`probe_pcsc watch` の**再発火が 0 件**
  （UART の `🎴 reader N: … 枚` も置いた瞬間の 1 行だけ。`(hold n)` が付く行が時々出るのは正常
  = 1 枚取りこぼしを UID 単位で吸収したという意味）。

ホスト側の自動テストは無い（firmware 側の実装のため）。firmware ロジックは
`docs/worklog/2026-09-10-pn5180-fast-inventory.md` に書いた register-level simulator
（実 `pn5180_reader.c` / `ccid_slot.c` をリンクして INVENTORY フレームを解釈する）で
0/1/2/3/5 枚・ノイズ・debounce を確認済み。

## Affected Files

- `firmware/esp32s3-pn5180-ccid/main/app_config.h`
- `firmware/esp32s3-pn5180-ccid/main/pn5180_reader.c`
- `firmware/esp32s3-pn5180-ccid/main/pn5180_reader.h`
- `firmware/esp32s3-pn5180-ccid/main/ccid_slot.c`

## Open Questions / Follow-up

1. **重ね置き（複数枚）の要件は確定**: 席 reader = hole card **2 枚重ね**、board1 = flop
   **3 枚重ね** / board2 = turn 1 枚 / board3 = river 1 枚。本番は **11 slot**（席 8 + board 3）。
   → 本タスクで **mask DFS の anti-collision を実装**（`PN5180_MAX_CARDS_PER_READER=4`）。
   CCID の Get UID は **UID を uid_len byte ごとに連結**して返す（`count × 8B + 90 00`）。
2. **host 側は同時進行で対応済**（別担当）: 契約 `docs/contracts/rfid-usb-ccid.md` **v1.1** に
   連結（§6, 8B × k ≤ 4, UID 昇順）と board の `cards`（§4）を明文化、`rfid/bridge.py:split_uid_response`
   / `PCSCBridge.read_uids` と `RFIDThread` の UID 集合デバウンスが実装された。
   firmware（本 issue）と host の**組み合わせでの実機通し確認は未実施**。
   なお v1.1 の分割規則は「応答長 16/24/32 のときだけ 8B 分割」なので、**ISO 14443A の 4/7B UID を
   複数枚**重ねる構成は分離できない（本番カードは ICODE SLIX のみなので実害なし）。
3. **RF 設定（ASK10 / ASK100）**: ドライバの `pn5180_loadRFConfig` は RX 設定を `tx | 0x80` で
   決め打ちする（`src/pn5180.c:816`）。PN5180 の RF config 表では 0x0D→0x8D = ISO15693 26 kbps RX、
   0x0E→0x8E = 53 kbps RX なので、標準 INVENTORY（high data rate = 26.48 kbps 応答）を受けられるのは
   **0x0D/0x8D の組だけ**。よって fast 経路の既定は `PN5180_15693_26KASK100`。実機で届きが悪ければ
   `PN5180_15693_26KASK10` に切り替えて A/B（読めなくなるなら RX 不一致が原因）。
4. ~~**DFS の probe 数削減（未実装）**~~ → **実装済（実機フィードバック 3 の対処 A）**: `RX_COLL_POS`
   （bits 25:19）で衝突位置まで mask を一気に伸ばす。**bit 位置の基準（フレーム先頭 / UID 先頭）は
   実機未確認**なので、`coll_pos >= 16` かつ受信バイトが衝突ビットに届いていることを検証し、
   満たさなければ従来の 1 bit 伸ばしに fallback する。基準が想定と違っても Stay Quiet + root
   再 probe が取りこぼしを回収する（効率だけが落ちる）。実機では起動後 slot ごと 3 回だけ出る
   `reader N: coll_pos=%u（UID bit %d）, 受信 %u byte, cur.len=%u → 採用/fallback` と、
   `poll 統計` の `coll_pos fallback N` で基準を確定する。
5. **重ね置き 3 枚が電力不足で応答しない可能性（firmware では解決できない）**: Stay Quiet を
   入れても 3 枚目が出ない場合、capture ではなく**給電不足**（アンテナに密着した 2 枚が磁束を
   食い、3 枚目が動作電圧に届かない）が原因になり得る。その場合は `PN5180_FAST_FIELD_SETTLE_US`
   を伸ばす / `PN5180_FAST_RF_CONFIG` を ASK10 に振る / カードを少しずらす（完全に重ねない）/
   アンテナ側の出力を上げる、という物理側の対処になる。切り分け: 3 枚のうち任意の 2 枚だけを
   置くと必ず 2 枚とも読めるなら電力不足ではなく分離の問題、どの組み合わせでも 3 枚目だけが
   出ないなら電力不足を疑う。

## Related Worklog

- `docs/worklog/2026-09-10-pn5180-collpos-dfs.md`（実機フィードバック 3 の対処 A/B/C/D）
- `docs/worklog/2026-09-10-pn5180-stay-quiet-per-uid-hold.md`（本節「実機結果」の対処）
- `docs/worklog/2026-09-10-pn5180-fast-inventory.md`
- `docs/worklog/2026-09-10-multi-reader-firmware-prep.md`（前段: RF 時分割 / 未通電 skip / poll 統計）

## Related ADRs

- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 契約 v1.0 の凍結。
  複数枚の連結応答は §6/§7 の **拡張**（v1.1 予定）。
- `docs/adr/0040-ccid-virtual-card-always-present.md` — カード有無は Get UID の SW だけで伝える。
  0 枚 = `6A 81` はこの設計のまま。

## Related Issues

- `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md` — firmware↔host 契約。

## Notes

`get_all_uids` は UID を読むたびに **Stay Quiet** をタグに送る（`pn5180_iso15693_stay_quiet`）。
quiet 状態は RF を切ると解除されるので、`PN5180_RF_OFF_BETWEEN_READERS=1` でたまたま成立していた。
fast 経路は当初「1 slot inventory の mask 分割だけで全枚数を列挙できる」と考えて Stay Quiet を
送っていなかったが、**実機の capture effect でこの前提が崩れた**（上の「実機結果」節）。
現在は fast 経路もドライバと同じく Stay Quiet を送り、そのぶん **inventory の最後に必ず RF off**
して quiet を解除する（`PN5180_RF_OFF_BETWEEN_READERS` の値に依らない）。
