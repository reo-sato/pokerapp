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

### 振替後の起動（`0e93de4`, 11 台を #4 だけ飛ばして挿した状態）

```
配線チェック: 設定 ch なのに floating（未通電/未接続）= #10(ch10) → この reader は skip
PN5180 reader 0..8 ready / reader 10 ready (nss=17 mux_ch=11)
PN5180 ready: 10/11 reader（skip: #10(ch10)）
```

- 振替後は index 10（board3）= コネクタ #12（ch11）が ready。host 側も `list` 11 件 matched /
  `check` 11 PASS / `watch` 22 タッチ（席 8 台 × 2 枚 + board、`seat 4 [r3]` が振替どおり）で通し OK
  （ISSUE-0022 の実機確認）。
- **コネクタ #11（ch10）だけが floating のまま**（reader は挿してある）。前日はこのコネクタで reader が
  動いていたので、今回挿した reader 側（電源/BUSY ピンの接触）かコネクタの再劣化か。切り分けは
  **#11 と #12 の reader を入れ替えて**、`skip: #10(ch10)` のまま（= コネクタ）か `skip: #11(ch11)` に
  移る（= reader）かを見る。
- `RST診断(ch0)` は依然 `0 0 0`（reader 0 は動作）。

### 入れ替え試験の結果（同日）= このときは reader 側に見えた

> **注**: この後 reader を交換して位置を元に戻した状態では ch10 が再び floating になり、
> 「ch10 の BUSY 経路の間欠不良」が第一仮説に戻った（下の「chip 生存確認の実機結果」）。

reader を コネクタ #11（ch10）↔ #12（ch11）で入れ替えて再起動:

```
配線チェック: 設定 ch なのに floating（未通電/未接続）= #11(ch11) → この reader は skip
PN5180 reader 9 ready (nss=16 mux_ch=10)      ← 入れ替え前は floating だった ch10 が ready
reader #11 (index 10, nss=GPIO17, ch11) は未通電/未接続 → skip
PN5180 ready: 10/11 reader（skip: #11(ch11)）
```

- **floating が reader に付いて移動した**（ch10 → ch11）。この試験だけを見ると
  「その reader 1 台の故障」で、位置依存（ハーネス末端の電圧降下など）なら floating は ch10 に残るはず
  だった。**ただし接点の間欠不良でも、抜き差しで一度当たれば同じ結果になる**（後続の観測でそちらが有力に
  なった）。
- 残る切り分けは「電源が来ていない」vs「BUSY 線だけ切れている」。手で当たるしかなかったので
  **firmware に自動診断を追加**（下の Fix 2）。

## Fix 2: skip した reader の chip 生存確認（BUSY 非依存, 同日追加）

起動時、BUSY が floating で skip した reader（および init 失敗で skip した reader）ごとに、
共有 SPI とは別の device を一時 add して `READ_EEPROM(FIRMWARE_VERSION)` を 1 発送り、MISO の応答で
chip の生死を判定して WARN に出す（`spi_probe_nss` / `diag_skipped_reader`）。

```
reader #11 (ch11) の chip 生存確認: FW=0C 03 = **chip は生きている** → BUSY 線だけが不通（…）
reader #11 (ch11) の chip 生存確認: FW=FF FF = SPI 無応答 → この reader の電源(3.3V/5V)/GND か SPI 線…
```

- `FW=xx xx`（FF FF / 00 00 以外）→ chip は生きている = **BUSY 線 1 本の不通**（その reader の BUSY ピン /
  圧着 / コネクタの BUSY を当たる）。
- `FW=FF FF` → SPI 無応答 = **電源/GND か SPI 線か chip 個体**。
- 呼ぶのは **init ループの後・RF config ロードの前**（この probe は共有 RST を叩くのでレジスタが消える）。
- NSS スキャン（`diag_after_init_failure`）の 1 候補ぶんの処理を `spi_probe_nss` に切り出して共用。

### chip 生存確認の実機結果（`405f218`, ch10 に **新品の reader** を挿した状態）

```
MUX scan ch0..15 (pull-up): 0001000000101111        ← ch3（空き）/ ch10 / ch12(空き) が floating
配線チェック: 設定 ch なのに floating（未通電/未接続）= #10(ch10) → この reader は skip
PN5180 reader 0..8 ready / reader 10 ready (nss=17 mux_ch=11)
reader #10 (ch10) の chip 生存確認: FW=00 04 = **chip は生きている** → BUSY 線だけが不通
PN5180 ready: 10/11 reader（skip: #10(ch10)）
```

- **電源と SPI は届いている**（chip が `FW=00 04` を返した）。不通は **BUSY 線 1 本だけ**に絞れた。
  診断そのものは意図どおり動作（ISSUE-0023 Fix 2 の実機確認）。
- **重要**: この起動時点で **入れ替え試験の位置は元に戻してあり**、ch10 に挿してあるのは
  **交換した新品 reader**（元の故障 reader A の位置）。つまり ch10 は
  **reader A（floating）→ reader B（ready, 入れ替え試験）→ 新品（floating）** と、
  **3 台のうち 2 台で落ちている**。
- したがって**仮説の順位は「ch10 の BUSY 経路（コネクタ接点 / 圧着 / ハーネス）の間欠不良」が第一**に戻る
  （reader B のときだけ接点が当たった）。ハーネスは既にコネクタ #4（ch3）でも BUSY が死んでいるので、
  **BUSY の圧着不良が複数箇所にある**線が濃い。
- 第二の候補: **新品 reader のピン配列が違う**（PN5180 ブレークアウトは BUSY/IRQ の並びが版で異なるものが
  ある）。chip が SPI に応答している = SCK/MOSI/MISO/NSS は合っているので、BUSY だけ別ピンに来ている
  可能性がある。動いている reader とシルク印刷を見比べる。
- **次の切り分け（再ビルド不要）**: ch10 の reader を **空きコネクタ #13（ch12）に挿して再起動**し、
  起動ログの `MUX scan ch0..15` の **ch12 の桁**を見る。
  - ch12 が `0`（= Low 駆動）→ その reader の BUSY は正常 → **ch10 のコネクタ / ハーネスの BUSY** が不良。
    `配線チェック: 通電しているが設定範囲外の ch = ch12` も出る。
  - ch12 が `1`（floating のまま）→ **その reader の BUSY ピン / 座り**が不良（新品でも当たりを引いた、
    またはコネクタに挿し込み切れていない）。
  `PN5180_READER_COUNT` の範囲外でも MUX scan は 16 ch 全部を見るので、この判定には再ビルドが要らない。

### ch12 テストの結果 = **ch10 のコネクタ不良で確定**（同日, `405f218`）

新品 reader を空きコネクタ #13（ch12）に挿して再起動したところ、`MUX scan` の **ch12 が `0`**
（Low 駆動 = 通電）になった。

- → **その reader の BUSY は正常**。不通は **コネクタ #11（ch10）側の BUSY**（接点 / 圧着 / ハーネス）。
- コネクタ #4（ch3）に続いて 2 本目の BUSY 不良。**このハーネスは BUSY の圧着に系統的な弱さがある**。
- 修理せず **配線表で振替**する方針に決定（下記）。生きているコネクタは 11 本以上あるので本番構成は成立する。

## 配線表の振替（2026-09-11, 実機の挿し方に合わせる）

**BUSY が不通の 2 本（#4=ch3 / #11=ch10）を飛ばし、生きている 11 本を index 0..10 に割り当てる**
（修理はしない方針）。`app_config.h` の `PN5180_READERS` を実際の挿し順に並べ替えた:

| index | 役割 | コネクタ | nss / mux_ch |
|---|---|---|---|
| 0..2 | 席 1..3 | #1,#2,#3 | 1/ch0, 2/ch1, 4/ch2 |
| 3..7 | 席 4..8 | #5,#6,#7,#8,#9 | 6/ch4, 7/ch5, 8/ch6, 9/ch7, 10/ch8 |
| 8 | board1（flop 3 枚） | #10 | 15/ch9 |
| 9 | board2（turn） | **#12** | 17/ch11 |
| 10 | board3（river） | **#13** | 18/ch12 |
| 11, 12 | 予備（BUSY 不通） | #4, #11 | 5/ch3, 16/ch10 |

- **各行の (nss, mux_ch) の組は変えない**（組が 1 本のコネクタを表す）。並べ替えただけ。
- host config（`reader` 0..10 = 席 1..8 / board1..3）は **不変**。
- 実機側の作業: **turn を #12、river を #13 に挿し替える**（board2/board3）。席 1..8 と board1 は変更なし。
- コネクタを修理したら、この表を元の並び（#N の BUSY = ch(N-1)）に戻す。

## 次に実機で確認すること（切り分け）

0. **11 台目**: → **`FW=00 04` = chip 生存、BUSY 線のみ不通**と判明（上の「chip 生存確認の実機結果」）。
   残りは「ch10 のコネクタ/ハーネス」vs「reader の BUSY ピン」で、空き ch12 に挿して MUX scan の
   ch12 の桁を見れば決まる（同節の「次の切り分け」）。

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
