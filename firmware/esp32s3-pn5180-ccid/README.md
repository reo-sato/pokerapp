# ESP32-S3 + PN5180 → USB CCID smart card reader（firmware）

ESP32-S3（native USB）に PN5180 ×N を載せ、PC へ **USB CCID smart card reader** として公開する
ESP-IDF firmware。host 側の pokerapp（`rfid/reader_thread.py` 等）が PC/SC 経由で UID を読み、
`tools/probe_pcsc.py` でそのまま検証できる。

- **契約（正準）**: [`../../docs/contracts/rfid-usb-ccid.md`](../../docs/contracts/rfid-usb-ccid.md) **v1.2**
  （v1.2 = CCID slot は 1 つ / 物理リーダーは Get UID の P2, ADR-0041。v1.1 = 重ね置きの UID 連結）
- **実装チェックリスト**: [`../../docs/rfid-ccid-firmware-checklist.md`](../../docs/rfid-ccid-firmware-checklist.md)
- **host 側検証手順**: [`../../docs/hardware-qa-checklist.md`](../../docs/hardware-qa-checklist.md)

> ⚠ **これは scaffold（叩き台）です**。USB CCID クラスや esp_tinyusb / PN5180 コンポーネントの
> 細部はバージョン・実機依存で、**ビルド→フラッシュ→実機での検証が必要**。各ファイルの `TODO(実機)`
> と「要調整」コメントを埋めること。USB の細部は RevK の記事と `polhenarejos/pico-openpgp` を参照。

---

## 構成

```
firmware/esp32s3-pn5180-ccid/
├── CMakeLists.txt              ESP-IDF プロジェクト
├── sdkconfig.defaults         TinyUSB/USB-OTG を有効化
└── main/
    ├── app_config.h           ★ 実機設定（reader 台数 / SPI ピン / VID-PID / product 文字列）
    ├── usb_descriptors.[ch]    CCID の USB 記述子（class 0x0B, bulk IN/OUT, §2）
    ├── ccid_device.[ch]        TinyUSB カスタムクラスとして CCID を登録（bulk plumbing）
    ├── ccid_slot.[ch]          ★ CCID メッセージ処理（ATR §5 / Get UID §6 / UID §7）= 契約の核
    ├── pn5180_reader.[ch]      PN5180 で UID を読みカード状態をキャッシュ（jef-sure 使用）
    └── main.c                  USB 起動 + PN5180 ポーリングタスク
```

役割分担: **USB(版依存・要実機検証)** = `usb_descriptors` / `ccid_device`、
**契約準拠の中核(framework 非依存)** = `ccid_slot`、**RF** = `pn5180_reader`。

## 必要環境

- ESP-IDF v5.1 以降（`idf.py`）。`esp_tinyusb` と `jef-sure/esp32-component-pn5180` は
  `main/idf_component.yml` で自動取得。
- ESP32-S3 ボード（**native USB ポート**を PC に接続。CP2102N の UART 側ではない＝§0）。
- PN5180 ×N（SPI 接続。NSS/BUSY は reader ごと、SCK/MOSI/MISO/RST は共有）。

## ビルド & フラッシュ

```bash
cd firmware/esp32s3-pn5180-ccid
idf.py set-target esp32s3
# 実機に合わせて main/app_config.h（ピン / PN5180_READER_COUNT / VID-PID）を編集
idf.py build
idf.py -p <PORT> flash monitor   # フラッシュは UART でも native USB(USB-Serial/JTAG)でも可
```

## アーキテクチャ（実機: 本番 11 reader / CCID slot は 1 つ / 配線は 13 台ぶん + CD74HC4067 MUX）

- **CCID slot は常に 1 つ、物理リーダーは Get UID の P2 で選ぶ**（契約 **v1.2 §6** / ADR-0041）。
  Windows の汎用 CCID ドライバは **1 インターフェースにつき 1 slot しか reader として公開しない**
  （実機 2026-09-10: `CCID_SLOT_COUNT=2` で焼いても `PokerRFID PN5180-CCID 1` は `Reader not found`）。
  slot ごとに USB インターフェースを分ける手も ESP32-S3 の USB endpoint 6 本（双方向 5 + IN 1）では
  最大 5 台までで 11 台に届かない。よって `CCID_SLOT_COUNT=1` 固定とし、
  **`FF CA 00 <k> 00`（k = 物理 reader index 0..N-1）** で 1 台ずつ読む。台数問い合わせは
  **`FF CA 00 FF 00` → `<N> 90 00`**、範囲外の k は **`6A 86`**。k=0 は v1.0/1.1 と同一バイト列。
  host 側は `config.rfid.pcsc_readers[].reader`（index）で席/board の役割に対応づける
  （reader_name は 1 つだけなので、役割の区別は index で行う）。
- **物理 reader 台数は `PN5180_READER_COUNT`**（既定 11）。`CCID_SLOT_COUNT` は USB 上の slot 数で
  常に 1（両者は別物）。
- **本番は 11 reader**: 席 1..8（各 hole card **2 枚重ね**）+ board1（flop **3 枚重ね**）/
  board2（turn 1 枚）/ board3（river 1 枚）。配線ハーネスと `PN5180_READERS` は 13 台ぶん
  用意してあり、先頭 11 を使う（#12/#13 は予備）。
- SCK/MOSI/MISO/RST は **全台共通**（直結）、**NSS は reader 個別**。
- **BUSY 13 本は CD74HC4067（16ch アナログ MUX）に集約**。S0-S3 で 1 本を選んで SIG に出し、
  ESP32 は SIG(`PN5180_PIN_BUSY_SIG`)を読む。`pn5180_reader.c` が **各 reader 処理の直前に MUX channel を
  切替**（`mux_select`）してから jef-sure ドライバを呼ぶ（ドライバは MUX 非依存、busy=SIG GPIO を渡すだけ）。
- MUX EN=GND（常時有効）、MUX VCC=**3.3V**（5V 禁止）。SPI は **5MHz**（7MHz 以上で不安定。bring-up 中は
  `PN5180_SPI_HZ=1MHz` に落としてあるので 11 台化のときに戻す）。
- **bring-up は `PN5180_READER_COUNT=1` で 1 台検証 → 動いたら 11 に上げる**（`app_config.h` の
  `PN5180_READERS` は 13 台分定義済み）。1 台検証中は起動時の **MUX 全 ch 走査で通電中の ch を自動選択**
  するので、どのコネクタに挿しても再ビルド不要（`pn5180_reader.c: select_bringup_reader`）。
- **CCID slot は仮想カード常時挿入（ADR-0040）**: `IccPowerOn` には常に固定 ATR を返し、カード有無は
  Get UID の SW（あり `90 00`+UID / なし `6A 81`）だけで伝える。Windows(usbccid) は interrupt-IN の挿抜通知を
  無視し、無ければ polling もせず bind 時の IccPowerOn しか送らないため（実機で確定）。interrupt-IN は
  `CCID_USE_INTERRUPT_EP=0` で記述子から外してある（EP 構成を変えたら `bcdDevice` を上げる）。
- UID は **MSB-first** で返す（PN5180 の ISO15693 生レスポンスは LSB-first なので反転。契約 §7）。
  1 reader に複数枚あるときは **UID を 8B ごと連結**（`count × 8B + 90 00`）。並びは memcmp 昇順に
  正規化してあるので、同じ組み合わせなら毎回同じバイト列になる。
  ISO14443A の試行は `PN5180_TRY_ISO14443=0`（本番カードは ICODE SLIX のみ。
  `PN5180_FAST_INVENTORY=1` の経路では 14443 は試さない）。

## 実機で確定済み（2026-09-10）と残 TODO

確定済み（1 slot で契約 §5–§8 を host の `probe_pcsc raw` / `watch` で確認, worklog
`docs/worklog/2026-09-10-rfid-ccid-end-to-end-bringup.md`）:

- ピン割当は `app_config.h` = `docs/hardware/pn5180-esp32s3-wiring.md` §3（NSS #1..#13 = 1/2/4/5/6/7/8/9/10/15/16/17/18、
  SCK/MOSI/MISO/RST = 12/11/13/14、MUX SIG/S0-S3 = 47/37/39/40/41）。PSRAM は `CONFIG_SPIRAM=n`（GPIO37 を使うため）。
- `jef-sure/pn5180` 0.1.1 の実 API（`pn5180-14443.h` / `pn5180-15693.h`、`nfc_uids_array_t.uids_count`、
  `nfc_uid_t.uid_length`）、tinyusb 0.19 / esp_tinyusb 1.7.6 の struct 構成、ATR
  `3B 8F 80 01 80 4F 0C A0 00 00 03 06 03 00 01 00 00 00 00 6A`（Windows が受理）。
- USB: VID 0x303A / PID 0x8B5D / reader_name は **`PokerRFID PN5180-CCID 0` の 1 つだけ**（v1.2）/
  bulk OUT+IN のみ。bcdDevice は実機確認時 0x0102 → 現在は `0x0200 | CCID_SLOT_COUNT` = **0x0201 固定**
  （slot は常に 1 なので、物理 reader を増やしても記述子は変わらない）。

複数 reader 化の準備は **実装済（実機未検証, 2026-09-10, worklog
`docs/worklog/2026-09-10-multi-reader-firmware-prep.md` /
`docs/worklog/2026-09-10-pn5180-fast-inventory.md`）**:

- **RF 時分割**（`PN5180_RF_OFF_BETWEEN_READERS=1`）: 各 reader の inventory 直後に `pn5180_setRF_off()`。
  ドライバの `get_all_uids()` は RF を ON のまま戻るため、切らないと 11 台の磁界が同時に立つ。
- **未通電 reader の skip**: 起動時の MUX scan で floating の ch は `pn5180_init` を呼ばずに飛ばし、
  残りで起動する（`PN5180 ready: N/11 reader（skip: …）`）。skip した index は常に `6A 81`。
  1 台も上がらなければ NSS スキャン診断を出す。設定 ch と通電 ch の食い違いは
  「配線チェック」ログで列挙。
- **poll 周期の計測**（`POLL_STATS_INTERVAL_MS=10000`）: 10 秒ごとに 1 周の min/avg/max と最長 reader。
- **`bcdDevice` = `0x0200 | CCID_SLOT_COUNT` = 0x0201 固定**（slot は 1）+ `bMaxCCIDBusySlots=1`。
  記述子（EP 構成 / functional descriptor）を変えたときだけ上位バイトを上げる。
- **高速 inventory + 重ね置き anti-collision**（`PN5180_FAST_INVENTORY=1`, **ISSUE-0021**）:
  ドライバの `get_all_uids()` は RF 設定 2 種 × データレート 2 種を毎回総当たりするため 1 台で
  0.2〜0.9 秒かかり（実機 `poll 統計`）、11 台には載らない。自前経路では
  **RF ON → 1ms → INVENTORY（1 slot, high rate）→ 応答待ち ≤10ms → RF OFF** に絞り、
  **衝突したときだけ mask を 1 bit ずつ伸ばす DFS** で重ね置きを分離する
  （`PN5180_MAX_CARDS_PER_READER=4` / probe 上限 `PN5180_FAST_MAX_PROBES=16`）。
  → **実機 1 slot で 1 周 15 ms**（カード無し。従来 176〜890 ms）を確認、1 枚 / 2 枚重ねも OK
  （2026-09-10）。`=0` でドライバ経路に戻せる（A/B）。
- **Stay Quiet + root 再 probe（capture effect 対策, ISSUE-0021 実機フィードバック）**:
  実機で **2 枚が同時応答しても PN5180 が衝突を検出せず強い方だけを復号する**ことがあり
  （capture effect）、その枝は 1 枚で確定してしまうので弱い札が DFS でも現れない
  （3 枚重ねで `3 枚` が一度も出なかった）。そこで見つけた札ごとに **STAY QUIET**（`0x22 02` +
  UID 8B、応答なし）を送って黙らせ、**root(mask 0) を再 probe** して残りを拾う。
  quiet は RF off で解除されるので、**fast 経路は inventory の最後に必ず `pn5180_setRF_off()`**
  を呼ぶ（`PN5180_RF_OFF_BETWEEN_READERS=0` でも）。カード無しは 1 probe のまま、
  カードが載っている reader は「もう居ない」確認 probe のぶん +1（1 枚 2 / 2 枚 3〜4 / 3 枚 4）。
- **衝突位置（`RX_COLL_POS`）で mask を一気に伸ばす**（実機フィードバック 3, ISSUE-0021）: 実機の
  3 枚重ねで **probe 14 / 1 周 150 ms** になった（UID を LSB-first で見ると 2 枚の下位 5 bit が同一で、
  1 bit ずつ伸ばす DFS が空の兄弟枝を RX timeout ぶん舐める）。`RX_STATUS` の衝突ビット位置
  （UID bit = `coll_pos - 16`）と衝突前の受信データで **prefix ごと mask を伸ばす**と 3 枚 = 6 probe、
  2 枚 = 4 probe。取れない / 不整合なら 1 bit 伸ばしに fallback（起動後 slot ごと 3 回だけ
  `coll_pos=… → 採用/fallback` を INFO で出す。基準は実機未確認）。
- **定常状態は確認 probe を間引く**（`PN5180_FAST_CONFIRM_EVERY=5`, 0 で従来動作）: 1 ラウンド目の
  集合が前回と同じなら「もう居ない」確認 root を 5 poll に 1 回だけにする（11 台に札が載ると確認
  だけで ≈90 ms/周）。**衝突フラグ無しの壊れた受信（磁界の縁のノイズ）は分割せず同じ node を 1 回だけ
  再 probe** し、駄目なら「無し」扱い（実機で 1 枚なのに probe が上限 16 に張り付いた原因）。
  `PN5180_FAST_RX_TIMEOUT_MS` は 10 → 8 ms（応答は ≈5.5 ms で来る）。
- **presence hold は UID 単位**（`PRESENCE_HOLD_MISSES=3`）: 旧実装は「検出 0 枚のときだけ前回
  集合を保持」だったため、2 枚中 1 枚を 1 回取りこぼすと host へ「1 枚」が即座に伝わり、実機で
  2↔1 のちらつき（`watch` の再発火）になった。現在は UID ごとに miss を数え、**欠けた 1 枚だけ**を
  3 サイクル保持する。hold 中の枚数は UART のログ末尾に `(hold n)` で出る。
- **1 slot に複数カード**（席 = hole card 2 枚 / board1 = flop 3 枚）: `pn5180_card_t` が UID の
  配列を持ち、CCID の Get UID は **UID を 8 byte ごとに連結**して返す
  （`count × 8B + 90 00`、UID 昇順。1 枚なら従来と同一バイト列、0 枚は `6A 81`）。
  契約 [`rfid-usb-ccid.md`](../../docs/contracts/rfid-usb-ccid.md) **v1.1 §6** に準拠。
  host 側の分割は `rfid/bridge.py:split_uid_response`（応答長 16/24/32 のときだけ 8B 分割）。

残 TODO:

1. **11 台の実機検証**（段階 1→2→11。下記「11 台化の段階手順」）。上記はいずれも机上実装で
   **実機未検証**。
2. `PN5180_SPI_HZ` 1MHz→5MHz（**11 台が 1MHz で安定してから**、単独で変更）。
3. 診断ログの整理（bring-up 用の MUX/BUSY/RST 診断は起動時 1 回なので残してよい）。
4. `PN5180_FAST_RF_CONFIG` の ASK100 / ASK10 を実機で確定（`app_config.h` のコメント参照）。

### 11 台化の段階手順

本番は **11 reader**（席 8 + board 3。board1 = flop 3 枚重ね / board2 = turn / board3 = river）。
配線表 `PN5180_READERS` は 13 台分あるが、使うのは先頭 11。変えるのは **`PN5180_READER_COUNT`**
（`CCID_SLOT_COUNT` は常に 1 のまま = USB 記述子も不変）。

1. `PN5180_READER_COUNT=1` … bring-up 済。通電 ch から自動選択。
2. `PN5180_READER_COUNT=2` 以上 … 自動選択せず **配列順 = reader index 順**。起動ログの
   「配線 OK / 配線チェック」「PN5180 ready: N/M reader」を確認 → host で
   `probe_pcsc list` の **`physical readers: N`**（= `FF CA 00 FF 00` の応答）と、
   `watch` で **index ごと**に発火するかを見る。未通電 index は常に `6A 81`。
3. `PN5180_READER_COUNT=11` … 本番全台。`physical readers: 11`、`watch` で全 index、
   `poll 統計` ログで **1 周 ≤ 300 ms** を確認（超えるなら `PN5180_FAST_MAX_PROBES` /
   `PN5180_FAST_RX_TIMEOUT_MS` / `CARD_POLL_INTERVAL_MS` を見直す）。
   併せて席に 2 枚・board1 に 3 枚を重ねて `🎴 reader N: 2 枚 […]` が出るか。
   （実機の現状は ch0 と ch10 に 1 台ずつ = index 0 と 10。11 にすれば両方使われる。）
4. 最後に `PN5180_SPI_HZ` を 5MHz へ（1 変数だけ変える）。

**台数を変えても USB 記述子は変わらない**（slot は常に 1）ので、Windows の記述子キャッシュを
気にする必要はない。記述子そのものを変えたときだけ `bcdDevice` を上げる。

## host 側での受け入れ確認（pokerapp 側, 別マシン or 同じ PC）

firmware を焼いて native USB を挿したら、pokerapp 側で:

```bash
pip install ".[pcsc]"
python tools/probe_pcsc.py list     # reader_name が出る（product='PN5180-CCID …'）→ §2/§3
python tools/probe_pcsc.py check     # connect PASS → §5（ATR 受理）
python tools/probe_pcsc.py watch     # カードをかざすと UID 表示 → §6/§7、置く/離すで再発火 §8
```

- `list` に出た **実 reader_name** を pokerapp の `config.json` の `rfid.pcsc_readers[].name` に等値で記入し、
  `transport` を `"pcsc"` に。
- 確定した **VID/PID・実 reader_name** を契約 `rfid-usb-ccid.md` §2/§4 に転記（ISSUE-0015 残）。
- カードの UID を `watch` 表示から `rfid_cards.json` に登録（tag_id→card）。

詳細手順と受け入れマトリクスは [`../../docs/hardware-qa-checklist.md`](../../docs/hardware-qa-checklist.md) /
[`../../docs/rfid-ccid-firmware-checklist.md`](../../docs/rfid-ccid-firmware-checklist.md)。

## 契約 ↔ 実装ファイル対応

| 契約 § | 実装 | host 確認 |
|--------|------|-----------|
| §2 USB CCID class / VID-PID / product | `usb_descriptors.c`, `app_config.h` | `probe_pcsc list` |
| §3-4 reader_name（1 つ）↔ 物理 reader index | `usb_descriptors.c`（EP/IF）, host config の `reader` | `probe_pcsc list` |
| §5 ATR | `ccid_slot.c`（`ATR[]` + IccPowerOn） | `probe_pcsc check` |
| §6 Get UID `FF CA 00 <k> 00` / 台数 `FF CA 00 FF 00` | `ccid_slot.c`（`handle_apdu`） | `probe_pcsc list` / `watch` |
| §7 UID 4/7/8B 生バイト（重ね置きは連結） | `pn5180_reader.c` → `ccid_slot.c` | `probe_pcsc watch` |
| §8 present/removed・hot-plug | `pn5180_reader.c`（poll）+ host debounce | `probe_pcsc watch` |

## 既知の制限（scaffold 時点）

- **重ね置きは ISO 15693（8B UID）専用**。契約 v1.1 §6 の host 分割規則は「応答長 16/24/32 のときだけ
  8B 分割」なので、**ISO 14443A の 4/7B UID が複数枚**あると分割できない（4B × 2 枚 = 8B が
  単一 UID に見える）。本番カードは ICODE SLIX のみ・fast 経路は 15693 専用なので実害は無いが、
  `PN5180_FAST_INVENTORY=0` かつ `PN5180_TRY_ISO14443=1` で 14443 を重ね置きしないこと。
- CCID コマンドの 64byte 超チェイン受信は未対応（Get UID/Status は小さいので可。`ccid_device.c` TODO）。
  Get UID の応答も **1 パケット 64 byte** に収める必要があるため、`PN5180_MAX_CARDS_PER_READER`
  は 4（= 34 byte）まで。7 枚超に増やすならチェイン送信の実装が要る。
- live hot-add（稼働中の USB 再列挙追従）は契約上も v1.0 対象外。
- ATR / dwFeatures は一般的な非接触リーダー値。host は ATR 非依存だが Windows の bind 検証は実機で。
- 複数 reader は配線・ピン拡張が前提（`app_config.h` の `PN5180_READERS` を台数ぶん用意）。
- **CCID multi-slot は使えない**（Windows の汎用ドライバが 1 slot しか公開しない, ADR-0041）。
  pcsc-lite など multi-slot を扱える環境でも、契約 v1.2 は P2 方式で統一する。
- **ドライバ制約（重要）**: `pn5180_spi_init()` が add する SPI device は **全 reader で 1 本の共有**で、
  `pn5180_init()` の失敗経路はその共有ハンドルを `spi_bus_remove_device` で解放し **`pn5180_spi_t` も
  free する**。firmware は失敗のたびに `spi_bus_free` → `pn5180_spi_init` で共有 SPI を作り直し、ready 済み
  reader の `dev->spi` を差し替えてから **1 回だけ再試行**し、それでも失敗した reader は **skip して他は続行**
  する（ISSUE-0023, `PN5180 ready: N/11 reader（skip: #5(init失敗), …）`）。個別 reader に `pn5180_deinit()` を
  呼ぶのは同じ理由で禁止。未通電の台は **init を呼ぶ前に** skip する。init 前に配線表 13 本の NSS を
  すべて output High にして、init していない chip が MISO を駆動する経路を塞いでいる。

## 参考

- RevK, "Native TinyUSB on ESP32S3 using my own device class"（CCID カスタムクラスの手法）
- `polhenarejos/pico-openpgp`（ESP32-S3 で動く CCID 実装の先例）
- `jef-sure/esp32-component-pn5180`（PN5180 ESP-IDF ドライバ）
- USB CCID 1.1 仕様（メッセージ形式 PC_to_RDR_* / RDR_to_PC_*）
