# ESP32-S3 + PN5180 → USB CCID smart card reader（firmware）

ESP32-S3（native USB）に PN5180 ×N を載せ、PC へ **USB CCID smart card reader** として公開する
ESP-IDF firmware。host 側の pokerapp（`rfid/reader_thread.py` 等）が PC/SC 経由で UID を読み、
`tools/probe_pcsc.py` でそのまま検証できる。

- **契約（正準）**: [`../../docs/contracts/rfid-usb-ccid.md`](../../docs/contracts/rfid-usb-ccid.md) v1.0
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
    ├── app_config.h           ★ 実機設定（slot 数 / SPI ピン / VID-PID / product 文字列）
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
- PN5180 ×N（SPI 接続。NSS/BUSY/RST は slot ごと、SCK/MOSI/MISO は共有）。

## ビルド & フラッシュ

```bash
cd firmware/esp32s3-pn5180-ccid
idf.py set-target esp32s3
# 実機に合わせて main/app_config.h（ピン/slot/VID-PID）を編集
idf.py build
idf.py -p <PORT> flash monitor   # フラッシュは UART でも native USB(USB-Serial/JTAG)でも可
```

## アーキテクチャ（実機: 13台 PN5180 + CD74HC4067 MUX）

- SCK/MOSI/MISO/RST は **13 台共通**（直結）、**NSS は reader 個別**。
- **BUSY 13 本は CD74HC4067（16ch アナログ MUX）に集約**。S0-S3 で 1 本を選んで SIG に出し、
  ESP32 は SIG(`PN5180_PIN_BUSY_SIG`)を読む。`pn5180_reader.c` が **各 reader 処理の直前に MUX channel を
  切替**（`mux_select`）してから jef-sure ドライバを呼ぶ（ドライバは MUX 非依存、busy=SIG GPIO を渡すだけ）。
- MUX EN=GND（常時有効）、MUX VCC=**3.3V**（5V 禁止）。SPI は **5MHz**（7MHz 以上で不安定。bring-up 中は
  `PN5180_SPI_HZ=1MHz` に落としてあるので 13 台化のときに戻す）。
- **bring-up は `CCID_SLOT_COUNT=1` で 1 台検証 → 動いたら 13 に上げる**（`app_config.h` の
  `PN5180_READERS` は 13 台分定義済み）。1 台検証中は起動時の **MUX 全 ch 走査で通電中の ch を自動選択**
  するので、どのコネクタに挿しても再ビルド不要（`pn5180_reader.c: select_bringup_reader`）。
- **CCID slot は仮想カード常時挿入（ADR-0040）**: `IccPowerOn` には常に固定 ATR を返し、カード有無は
  Get UID の SW（あり `90 00`+UID / なし `6A 81`）だけで伝える。Windows(usbccid) は interrupt-IN の挿抜通知を
  無視し、無ければ polling もせず bind 時の IccPowerOn しか送らないため（実機で確定）。interrupt-IN は
  `CCID_USE_INTERRUPT_EP=0` で記述子から外してある（EP 構成を変えたら `bcdDevice` を上げる）。
- UID は **MSB-first** で返す（PN5180 の ISO15693 生レスポンスは LSB-first なので反転。契約 §7）。
  ISO14443A の試行は `PN5180_TRY_ISO14443=0`（本番カードは ICODE SLIX のみ）。

## 実機で確定済み（2026-09-10）と残 TODO

確定済み（1 slot で契約 §5–§8 を host の `probe_pcsc raw` / `watch` で確認, worklog
`docs/worklog/2026-09-10-rfid-ccid-end-to-end-bringup.md`）:

- ピン割当は `app_config.h` = `docs/hardware/pn5180-esp32s3-wiring.md` §3（NSS #1..#13 = 1/2/4/5/6/7/8/9/10/15/16/17/18、
  SCK/MOSI/MISO/RST = 12/11/13/14、MUX SIG/S0-S3 = 47/37/39/40/41）。PSRAM は `CONFIG_SPIRAM=n`（GPIO37 を使うため）。
- `jef-sure/pn5180` 0.1.1 の実 API（`pn5180-14443.h` / `pn5180-15693.h`、`nfc_uids_array_t.uids_count`、
  `nfc_uid_t.uid_length`）、tinyusb 0.19 / esp_tinyusb 1.7.6 の struct 構成、ATR
  `3B 8F 80 01 80 4F 0C A0 00 00 03 06 03 00 01 00 00 00 00 6A`（Windows が受理）。
- USB: VID 0x303A / PID 0x8B5D / `PokerRFID PN5180-CCID <slot>` / bcdDevice 0x0102 / bulk OUT+IN のみ。

残 TODO:

1. `CCID_SLOT_COUNT` 1→13、`PN5180_SPI_HZ` 1MHz→5MHz、RF 時分割（同時 RF ON は 1 台のみ、
   `docs/rfid-ccid-firmware-checklist.md` §8）。
2. 診断ログの整理（bring-up 用の MUX/BUSY/RST 診断は起動時 1 回なので残してよい）。

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
| §3-4 slot↔reader_name | `usb_descriptors.c`（EP/IF）, host config | `probe_pcsc list` |
| §5 ATR | `ccid_slot.c`（`ATR[]` + IccPowerOn） | `probe_pcsc check` |
| §6 Get UID `FF CA 00 00 00` | `ccid_slot.c`（`handle_apdu`） | `probe_pcsc watch` |
| §7 UID 4/7/8B 生バイト | `pn5180_reader.c` → `ccid_slot.c` | `probe_pcsc watch` |
| §8 present/removed・hot-plug | `pn5180_reader.c`（poll）+ host debounce | `probe_pcsc watch` |

## 既知の制限（scaffold 時点）

- CCID コマンドの 64byte 超チェイン受信は未対応（Get UID/Status は小さいので可。`ccid_device.c` TODO）。
- live hot-add（稼働中の USB 再列挙追従）は契約上も v1.0 対象外。
- ATR / dwFeatures は一般的な非接触リーダー値。host は ATR 非依存だが Windows の bind 検証は実機で。
- マルチ slot は配線・ピン拡張が前提（`app_config.h` の `PN5180_SLOT_PINS` を slot 数ぶん用意）。

## 参考

- RevK, "Native TinyUSB on ESP32S3 using my own device class"（CCID カスタムクラスの手法）
- `polhenarejos/pico-openpgp`（ESP32-S3 で動く CCID 実装の先例）
- `jef-sure/esp32-component-pn5180`（PN5180 ESP-IDF ドライバ）
- USB CCID 1.1 仕様（メッセージ形式 PC_to_RDR_* / RDR_to_PC_*）
