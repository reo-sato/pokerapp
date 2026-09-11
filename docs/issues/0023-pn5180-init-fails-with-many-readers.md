# ISSUE-0023: 10 台接続で reader 0 の `pn5180_init` が firmware version 読み取り失敗（RST 診断 during_rst=0）

## Date

2026-09-11

## Status

Open（緩和策 = 全 NSS High 固定 + init 再試行 1 回 + 失敗 reader の個別 skip は **実機で有効を確認**
（2026-09-11, `076b844`）: 同じ 10 台接続のまま reader 0 が **1 発目で init 成功**（再試行なし）、
`PN5180 ready: 9/11 reader（skip: #4, #11）`、9 台すべてでカード検出。残: `RST診断(ch0)` が依然
`during_rst=0`（reader 0 は動作しているので当面は実害なし、下の「実機結果」参照））

## Component

firmware（`firmware/esp32s3-pn5180-ccid/main/pn5180_reader.c`）/ 配線（RST・NSS・コネクタ）

## Expected Behavior

- 11 台構成（`PN5180_READER_COUNT=11`）で、通電している reader はすべて `pn5180_init` に成功し
  `PN5180 ready: N/11 reader` になる。
- 1 台の init 失敗が他の reader を巻き添えにしない。
- 起動時の RST 診断（reader 0）は健全なら `during_rst=1 just_after=0 after_10ms=0`
  （2026-09-10 までの全実機ログがこのパターン）。

## Actual Behavior（実機 2026-09-11, commit `528c678` = `bd14815` + rfid_cards.json）

```
MUX scan ch0..15 (pull-up): 0001000000101111   ← ch0,1,2,4..9,11 が通電（10 台）、ch3/ch10 は floating
BUSY診断(ch0): pull-up読み=0 pull-down読み=0 => LOW駆動
RST診断(ch0, pull-up有): BUSY during_rst=0  just_after=0  after_10ms=0   ← 前日までは 1/0/0
配線チェック: 設定 ch なのに floating = #4(ch3), #11(ch10) → skip
配線チェック: 通電しているが設定範囲外の ch = ch11(=#12)
E PN5180: Failed to read PN5180 firmware version
E pn5180: pn5180_init reader 0 failed (nss=1 busy=47 rst=14 mux_ch=0 via_mux=1)
  → 全 reader 停止
NSS スキャン: [1/13] NSS=GPIO1 (ch0) -> 送信前BUSY=0  BUSY Low→High:YES  FW=00 04 (SPI 応答あり)
✅ ch0 のチップは NSS=GPIO1 で応答（設定は GPIO1）
```

- ドライバの `pn5180_read_firmware_version` は EEPROM 読みが **`FF FF`（MISO を誰も駆動していない）**
  のときだけ黙って false を返す（timeout なら別の ERROR が出るが出ていない）。つまり reset の
  handshake は通り、FIRMWARE_VERSION の応答だけが空だった。
- 直後の診断（同じ NSS=GPIO1 に READ_EEPROM 1 発）には **`00 04`（FW 4.0）で応答**し、BUSY も
  Low→High に動いた = chip は生きていて BUSY 経路も正常。
- 前日（1〜2 台接続）まで同じコードで `PN5180 ready` していた。変わったのは **接続台数**（2 → 10）。

## Reproduction

1. reader を 10 台（ch0,1,2,4..9 + 予備 #12=ch11）に接続、ch3/ch10 は未接続。
2. `PN5180_READER_COUNT=11` で `idf.py build && idf.py -p COM3 flash monitor`。
3. 起動ログが上記になる（reader 0 で init 失敗 → 全停止）。

## Root Cause（仮説, 実機で未確定）

1. **NSS floating による MISO 衝突**: 共有 SPI に通電中の chip が 10 台。init は配列順に 1 台ずつなので、
   reader 0 の init 中は残り 9 台（範囲外の #12 も含む）の NSS が **未駆動（floating）**。floating を
   Low と解釈した chip が「選択された」と思って MISO を駆動すると、reader 0 の応答と衝突する。
   1〜2 台では起きず、台数が増えて顕在化した、と整合する。ただし診断の READ_EEPROM（同条件）は
   通ったので、これだけでは決め手にならない。
2. **RST が reader 0 に届いていない**: `during_rst=0`（RST を Low にしても BUSY が High にならない）は
   この chip がリセットに反応していないことを示す（前日は 1）。RST 不通の chip は ESP32 の再起動
   （flash）を跨いで **前回 firmware の途中状態（応答待ち）** のまま残り、最初の 1 発だけ噛み合わない
   ことがある → init の 1 発目は `FF FF`、診断の 2 発目は応答、と整合する。コネクタ #1 の RST ピン
   （前日は #4 の BUSY ピンが死んでいた = このハーネスのコネクタは接触不良が既出）、または 10 台分の
   RST 入力を GPIO14 1 本で駆動していることによる波形なまりを疑う。
3. 電源: 10 台同時通電で 3.3V が落ちている可能性（idle 電流は小さいので優先度低）。

## Fix（firmware 側の緩和策, 本 issue で実装）

- **全 NSS を init 前に output High で固定**（`nss_deselect_all`, 配線表 13 本すべて = 範囲外の予備も）。
  仮説 1 の経路をコードで塞ぐ。
- **init 失敗時は共有 SPI を作り直して 1 回だけ再試行**（`spi_recreate_after_init_failure`）。
  ドライバの失敗経路 `pn5180_deinit(ret,false)` は共有 SPI device を外し `pn5180_spi_t` も free する
  ので、`spi_bus_free` → `pn5180_spi_init` で作り直し、ready 済み reader の `dev->spi` を差し替える。
  仮説 2 の「1 発目だけ噛み合わない」を吸収する。
- **再試行も失敗した reader は skip して他は続行**（`PN5180 ready: N/11 reader（skip: #5(init失敗), …）`）。
  以前は「1 台失敗 = 全台停止」だった（共有 SPI が壊れるため）。深掘り診断（NSS スキャン）は
  1 台も ready でないときに 1 回だけ。
- RST 診断に「during_rst=0 かつ after=0 → RST 不通の可能性」の判定文、init 失敗診断に
  「SPI には応答するのに RST 診断が 0 → コネクタの RST ピン/配線」の判定文を追加。

## 実機結果（2026-09-11, `076b844`, 接続は前回と同じ 10 台）

```
MUX scan ch0..15 (pull-up): 0001000000101111        ← 前回と同じ
RST診断(ch0, pull-up有): BUSY during_rst=0  just_after=0  after_10ms=0   ← 依然 0
  → during_rst=0 かつ after=0: RST がこの chip に届いていない可能性 …（新しい判定文）
配線チェック: 設定 ch なのに floating = #4(ch3), #11(ch10) → skip / 範囲外 = ch11(=#12)
PN5180 reader 0 ready (nss=1 mux_ch=0)              ← 再試行 WARN なし = 1 発目で成功
PN5180 reader 1 … reader 9 ready（#4/#11 は未通電 skip）
PN5180 ready: 9/11 reader（skip: #4, #11）
🎴 reader 0/1/2/4/5/6/7/8/9: 1 枚 [E0:04:01:53:1A:41:83:4C]  ← 同じ札を 9 台に順に置いて全部検出・離脱
poll 統計(直近 46 周): 1 周 min/avg/max = 124/125/139 ms, 最長 reader 28 ms, probe 最大 2, ready 9 reader
```

- 前回失敗した reader 0 が **再試行なしで init 成功**した。前回との差分は firmware の緩和策のみ
  （配線・台数は同じ）なので、**仮説 1（init していない chip の NSS floating による MISO 衝突）が
  最有力**。仮説 2 の「1 発目だけ噛み合わない」なら再試行 WARN が出ているはず。
- `RST診断(ch0)` は依然 `0 0 0` = reader 0 の chip はリセットに反応していない。ただし init・
  inventory とも正常なので当面は実害なし。RST 不通のままだと chip がハングしたときに電源断でしか
  復帰できないので、後で **reader を #1 と #2 で入れ替えて診断値が入れ替わるか**（コネクタか個体か）
  を見る。
- 9 台で 1 周 ≈ 125 ms（カード無し〜1 枚）= 1 台 ≈ 14 ms。11 台なら ≈ 155 ms、全席に札を載せた
  実運用で ≈ 250〜300 ms の見込み（ISSUE-0021 の目標 ≤ 300 ms の上限付近。要実測）。

## 配線表の振替（2026-09-11, 実機の挿し方に合わせる）

実機は「コネクタ #4（ch3, BUSY 不通）だけ飛ばして若い順」に 11 台を挿している（#1,#2,#3,#5,…,#12。
#13=ch12 は空き）。`app_config.h` の `PN5180_READERS` をその物理順に並べ替えた: index 3（席 4）=
コネクタ #5（nss 6, ch4）… index 10（board3）= コネクタ #12（nss 17, ch11）。コネクタ #4 の行
（nss 5, ch3）は予備（index 11）に下げた。**各行の (nss, mux_ch) の組は変えない**（組がコネクタを表す）。
host config（`reader` 0..10 = 席 1..8 / board1..3）は不変。修理後に #4 を戻す場合はこの表を元に戻す。

## 次に実機で確認すること（切り分け）

1. 新 firmware で起動 → `PN5180 ready: N/11 reader（skip: …）` の N と、reader 0 が
   「再試行で init 成功」か「再試行も失敗 → skip」か。
2. `RST診断(ch0)` が依然 `0 0 0` なら、**コネクタ #1 の RST ピン**を疑う（reader を #1 と #2 で
   入れ替えて診断値が入れ替わるか）。
3. 予備 #12（ch11）に挿した reader は範囲外で常に無視される。**#11（ch10）に挿し替える**か、
   #11 のコネクタが死んでいるなら `PN5180_READERS` の #11/#12 行を入れ替えて #12 を board3 に充てる。
4. `poll 統計` の 1 周時間（8〜10 台で ≤ 200 ms が目安）。

## Regression Test

- register-level simulator `sim_initretry.c`（session scratchpad の stub harness）: reader 0 が
  1 発目失敗 → 再試行で成功、reader 4 が 2 回とも失敗 → skip、未通電/範囲外は init を呼ばない、
  最初の init 前に全 13 本の NSS が output High、共有 SPI 作り直し後に ready 済み reader の
  `dev->spi` が dangling しない、`spi_bus_free` がデバイス残存で失敗しない、poll が skip 混在で走る。
  **失敗 0 件**。既存 sim（capture / multi / diag）も従来どおり。
- スタブ・フルコンパイル 32 構成（readers 1/2/11/13 × stats × mux × fast）警告 0。

## Affected Files

- `firmware/esp32s3-pn5180-ccid/main/pn5180_reader.c`
- `firmware/esp32s3-pn5180-ccid/README.md` / `docs/rfid-ccid-firmware-checklist.md`（「1 台失敗 = 全台停止」の記述を更新）

## Related Worklog

- `docs/worklog/2026-09-11-pn5180-init-resilience.md`

## Related ADRs

- `docs/adr/0041-physical-reader-index-via-get-uid-p2.md`（11 台を 1 slot + P2 で扱う前提）

## Related Issues

- ISSUE-0021（poll 周期）/ ISSUE-0022（1 slot 化）の続き。ハーネスのコネクタ接触不良は前日の
  #4(ch3) BUSY 不通と同系統。
