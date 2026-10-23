# Worklog: ESP32-S3 + PN5180 USB CCID firmware bring-up（実機通電）

## Date

2026-06-22

## Scope / Task

前日（2026-06-21）scaffold した `firmware/esp32s3-pn5180-ccid/` を実機で動かし、Windows PC/SC に
`PokerRFID PN5180-CCID 0` として列挙させる（契約 `docs/contracts/rfid-usb-ccid.md` v1.0 §2/§3 の
MUST を満たす）。実機ピン未確定のため PN5180 RF 接続は別段階。

## Goal

- ESP-IDF v5.3.5 でビルド→フラッシュ→USB CCID デバイスとして Windows に認識させる。
- `tools/probe_pcsc.py list` で reader_name を可視化し、契約 §2/§4 に実 VID/PID と reader_name を転記する
  （ISSUE-0015 の最後の残作業を埋める）。

## Changed Files

- `firmware/esp32s3-pn5180-ccid/main/idf_component.yml` — レジストリ名修正 `jef-sure/pn5180` ^0.1.0
  （`esp32-component-pn5180` は GitHub repo 名で、registry 名と別 = `version solving failed` の原因）。
- `firmware/.../main/pn5180_reader.c` — include を `pn5180-14443.h`/`pn5180-15693.h` に、構造体フィールドを
  `nfc_uids_array_t.uids_count` / `nfc_uid_t.uid_length` に合わせる（README 例の `count`/`size` は不正確）。
- `firmware/.../sdkconfig.defaults` — `CONFIG_ESP_CONSOLE_UART_DEFAULT=y` + `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y`。
  USB-Serial/JTAG セカンダリコンソールが TinyUSB(USB-OTG) と内蔵 USB PHY を奪い合い CCID 起動失敗（Code 10
  Error）の原因だったため。旧 `CONFIG_TINYUSB_*` Kconfig は unknown symbol 警告のため削除。
- `firmware/.../main/ccid_device.[ch]`, `main.c` — `ccid_force_link()` を追加し main.c から呼ぶ。
  TinyUSB は `usbd_app_driver_get_cb` を **weak スタブ（0 drivers 返す）**として持つ。我々の strong 定義は
  `ccid_device.c`（別 TU）にあり、ESP-IDF が main を whole-archive しないため `ccid_device.o` が抽出されず、
  TinyUSB の weak スタブが採用→**CCID クラス未登録→Code 10**。`main.c → ccid_force_link()` で TU を強制リンク。
- `docs/contracts/rfid-usb-ccid.md` — §2 に実 VID/PID/manufacturer/product、§4 に Windows での実 reader_name
  `PokerRFID PN5180-CCID 0` を確定値として追記。
- `CLAUDE.md` / `docs/adr/0034-...md` / `docs/issues/0015-...md` / `CHANGELOG.md` — 実機確定値の反映と
  bring-up 完了の記録。

## Expected Behavior

- フラッシュ後、ESP32-S3 の native USB が Windows の「スマートカード読み取り装置」に出る。
- デバイスマネージャーで Status=OK（Code 10 でない）。
- `tools/probe_pcsc.py list` に reader_name が出る。

## Implemented Behavior

達成。bring-up ログの主要点:

- `Get-PnpDevice -Class SmartCardReader` で `Microsoft Usbccid Smartcard Reader (WUDF)` Status=OK。
- `probe_pcsc list` 出力: `'PokerRFID PN5180-CCID 0'`（manufacturer + product + slot index、Windows の体裁）。
- `ccid_init (app driver registered)` ログが TinyUSB 起動前に出ることを確認（クラスドライバ登録成立）。
- VID=0x303A PID=0x8B5D は `app_config.h` で固定。

## Test Results

- `idf.py build` → `[1061/1061]` 成功、`esp32s3-pn5180-ccid.bin` 0x429c0 bytes（partition 26% 使用）。
- `idf.py -p COMx flash monitor` 成功、ブート完走。
- Windows: SmartCardReader Status=OK、`probe_pcsc list` で reader 列挙確認。
- Python テスト（host 側）: 影響範囲は firmware（C）なので Python CI は無変化（**701 passed のまま**）。

## Mismatches Found During Testing

| 期待 | 実際 | 原因 |
|------|------|------|
| `jef-sure/esp32-component-pn5180` で依存解決 | `version solving failed` | レジストリ登録名は `jef-sure/pn5180`（GitHub repo 名と別）。README の `add-dependency` 行が不正確 |
| Code 10 はコンソール競合解決で消える想定 | まだ Code 10 | weak シンボル + 別 TU が未抽出。`ccid_init` が呼ばれていないログで確定 |

## Fixes Applied

1. レジストリ名を `jef-sure/pn5180` ^0.1.0 に修正。あわせて include をハイフン区切り名、構造体フィールドを
   実 API（`uids_count` / `uid_length`）に合わせる。
2. `sdkconfig.defaults` で `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y`（USB-Serial/JTAG セカンダリコンソール無効）。
3. `ccid_force_link()` を main.c から呼ぶ（whole-archive 非依存で `ccid_device.o` を強制リンク）。

## Remaining Gaps / Out-of-Scope

- [ ] PN5180 SPI 配線（SCK/MOSI/MISO/NSS/BUSY/RST）の実機ピンを確定し `app_config.h` に反映 →
      `probe_pcsc check` で §5 PASS、`probe_pcsc watch` で実カード UID（§6/§7）の通し確認。
- [ ] `rfid_cards.json` に物理カードの UID を登録（手順は `docs/hardware-qa-checklist.md` §3）。
- [ ] hand logger 通し（`python main.py --cli` で board street 自動遷移）。
- [ ] CCID コマンドの 64byte 超チェイン受信、live hot-add は契約上 future。

## Related ADRs

- `docs/adr/0034-...md` — Follow-up checkbox を埋める（実 VID/PID/reader_name 確定）。
- `docs/adr/0015-...md` — canonical PC/SC 経路。

## Related Issues

- `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md` — 残作業のうち「実 reader_name/VID-PID 確定」を完了。

## Related Commits

- 本ワークログと同じセッションの commit 群（registry 名 fix → コンソール競合 fix → CCID クラス強制リンク）。
