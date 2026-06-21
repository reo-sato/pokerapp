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
- MUX EN=GND（常時有効）、MUX VCC=**3.3V**（5V 禁止）。SPI は **5MHz**（7MHz 以上で不安定）。
- **bring-up は `CCID_SLOT_COUNT=1` で 1 台検証 → 動いたら 13 に上げる**（`app_config.h` の
  `PN5180_READERS` は 13 台分定義済み）。

## 実機で必ず埋める箇所（TODO）

1. **`app_config.h`**: `CCID_SLOT_COUNT`（1→13）/ 共有 SPI・RST・MUX SIG ピン / MUX S0-S3 /
   `PN5180_READERS` の NSS・mux_ch（reader→MUX channel 対応）/ `USB_VID`/`USB_PID`。
   ESP32-S3 で USB の 19/20、strapping 0/3/45/46、NeoPixel 38、（PSRAM 有効時の 35-37）を避ける。
2. **`pn5180_reader.c`**: `jef-sure` コンポーネントの実 API（ヘッダ名・`nfc_uids_array_t` /
   `nfc_uid_t` のフィールド名、`pn5180_15693_init` の modulation 値）に合わせる。`get_all_uids` の
   戻り値構造体を実 README/examples で確認。
3. **`ccid_device.c`**: `usbd_class_driver_t` の構成（`name` は `CFG_TUSB_DEBUG>=2` のみ、`deinit`
   の有無、`sof` 署名）を、使用中の tinyusb `device/usbd_pvt.h` に合わせる。
4. **`main.c`**: `tinyusb_config_t` のフィールド名（`configuration_descriptor` /
   `fs_configuration_descriptor` 等）を使用中の esp_tinyusb に合わせる。
5. **`ccid_slot.c` の ATR / `usb_descriptors.c` の CCID functional descriptor**: そのままで host が
   connect できるはずだが、Windows が CCID として bind しない場合は usbview で記述子を確認し調整。

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
