# Worklog: RFID 実機 bring-up — PN5180 読取りから PC/SC 越し UID 到達まで

## Date

2026-09-10（前段の firmware デバッグは 2026-06-22 の続き）

## Scope / Task

`firmware/esp32s3-pn5180-ccid/` を実機（ESP32-S3-DevKitC-1 + PN5180 ×1 + CD74HC4067 MUX）で
動かし、契約 `docs/contracts/rfid-usb-ccid.md` の §5（connect）/ §6（Get UID）/ §7（UID）/ §8
（hot-plug）を **host の pyscard 越しに**成立させる。ADR-0034 / ISSUE-0015 の「残: watch で実カード
UID 読み取り」を埋める。

## Goal

- firmware が PN5180 からカード UID を読める（MUX 経由 BUSY, 共有 SPI/RST, 個別 NSS）。
- Windows PC/SC 経由で `SCardConnect → FF CA 00 00 00` が UID + `90 00` を返す。
- `tools/probe_pcsc.py watch`（本番 `RFIDThread`）でタップが `RFIDEvent` として出る。

## Changed Files

firmware（`firmware/esp32s3-pn5180-ccid/main/`）:

- `pn5180_reader.c` — MUX 全 ch 走査から通電中 ch を自動選択（`select_bringup_reader`, CCID_SLOT_COUNT=1
  時）。NSS スキャナの偽陽性修正（送信前 BUSY=Low を要求、MISO 全 FF は floating 表示）と
  **2 回目 `pn5180_init` の撤去**（失敗時ドライバ deinit の assert → 再起動ループの除去）。
  presence debounce（`PRESENCE_HOLD_MISSES=3`）。ISO15693 UID を **MSB-first に反転**。
  ISO14443A の試行を `PN5180_TRY_ISO14443`（既定 0）でガード。
- `app_config.h` — `PN5180_READERS` を配線表（#1..#13）の正順に戻す。`PN5180_TRY_ISO14443`、
  `CCID_VIRTUAL_CARD_ALWAYS_PRESENT`（既定 1）を追加。
- `ccid_slot.c` — bmICCStatus を present&active / present&inactive / absent の 3 値に（`s_powered`）。
  IccPowerOff は present&inactive。Parameters を T=1 なら 7 byte（T=0 は 5 byte）に修正。
  仮想カード常時挿入（`icc_status` が absent を返さない）。NotifySlotChange 組立（interrupt 用、既定 OFF）。
  CCID コマンド（IccPowerOn/Off, GetSlotStatus 遷移, XfrBlock, Parameters, 未対応）を UART にログ。
- `ccid_device.c` / `usb_descriptors.[ch]` — interrupt-IN endpoint を `CCID_USE_INTERRUPT_EP`（既定 0）で
  ガード（試行→撤回）。`bcdDevice` 0x0100→0x0102（Windows の記述子キャッシュ無効化）。
  `ccid_notify_slot_change()`（EP 未 open なら false）。
- `main.c` — poll 後の挿抜通知呼び出しを `#if CCID_USE_INTERRUPT_EP` で除外。

host / tools / docs:

- `core/events.py` — `import numpy` を `TYPE_CHECKING` ガードに（RFID 経路を numpy 非依存に）。
- `tools/probe_pcsc.py` — `raw` サブコマンド（pyscard 直叩き: `SCardGetStatusChange` の slot 状態 +
  connect/Get UID の結果・例外を hresult 付きで時系列表示）。純粋ヘルパ `scard_state_table` /
  `decode_reader_state` / `format_scard_error`。
- `tests/test_tools_probe_pcsc.py` — `TestRawHelpers`（6 件）+ pyscard ゲートに `_cmd_raw`。
- `docs/adr/0040-ccid-virtual-card-always-present.md`（新規）、`docs/contracts/rfid-usb-ccid.md` §2/§5/§8、
  `docs/rfid-ccid-firmware-checklist.md` §3/§6/受け入れ表、`CLAUDE.md`、`CHANGELOG.md`、`docs/decision-log.md`。

## Expected Behavior

- 1 台だけ繋いだリーダーがどのコネクタ（MUX ch）にあっても firmware が自動で拾って init する。
- カードを置くと firmware が UID（8B, MSB-first `E0:04:…`）を読み、host が `SCardConnect` →
  Get UID で同じ UID を受け取る。離すと SW≠`90 00`。`watch` で置く→離す→置くが 2 回発火する。

## Implemented Behavior

- firmware 単体: 自動選択 → `reader 0 ready (nss=9 mux_ch=7)` → `🎴 UID=E0:04:01:53:1A:41:19:75 (8B)`
  → `カード離脱`。14443 タイムアウトのログ洪水が消え poll が ~300ms/周に。
- host: `probe_pcsc raw` で `[OS状態] PRESENT`（MUTE 無し）、`connect OK ATR=3B 8F …` →
  カード無し `SW=6A81` → 置いて `Get UID: E0 04 01 53 1A 41 19 75 SW=9000` → 離して `6A81`。
  firmware 側は `IccPowerOn → ATR` → `SetParameters T=1 (7B)` → Windows の探索 APDU に `6D 00`。
- `watch`（本番 `RFIDThread`, 30 秒）: `seat 1  UID=E0:04:01:53:1C:2A:B2:6C (8B) card=(未登録)` が
  「置く→離す→置く」で **2 件**（§8 再発火 OK）。role/seat は config `pcsc_readers[0]` どおり。
  → **契約 §5/§6/§7/§8 を本番 host コードで確認、1 slot の bring-up 完了。**

## Test Results

- `pytest tests/test_tools_probe_pcsc.py -q` → **41 passed**（新規 6 件込み）。
- `core.events` を numpy 無効化（`sys.modules['numpy']=None`）で import → OK。
- 実機（Windows, py -3.13 + pyscard 2.3.1）: `list` matched seat 1 / `raw` 上記 /
  `watch --seconds 30` → `観測した新規タッチ: 2 件`（UID `E0:04:01:53:1C:2A:B2:6C`）/ firmware monitor 上記。
- ESP-IDF v5.3.5 ビルド: 各コミットとも実機でビルド・書き込み成功（`App version` で確認）。

## Mismatches Found During Testing

時系列に、想定と実挙動の乖離と確定した原因:

1. **リーダーが別コネクタに**（MUX scan `1111111011111111` = ch7、設定は ch12）→ 設定固定では init 失敗。
2. **NSS スキャナ偽陽性**: 浮いて High の BUSY を「送信で High に立った」と誤判定 → GPIO1 を偽検出 →
   その NSS で 2 回目 `pn5180_init` → 失敗時ドライバ deinit の `spi_bus_remove_device` assert → **再起動ループ**
   （USB CCID も落ちる）。
3. **ISO14443A の試行**が毎 poll で REQA/anticollision timeout → poll ~800ms、ログ洪水。
4. **UID が LSB-first**（`…:04:E0`）。契約 v1.1 §7（branch `claude/exciting-goldberg-lkuojh`）は MSB-first MUST。
5. **watch 0 件（真因 3 層）**:
   - host の `python` が Microsoft Store のスタブで **スクリプトが実行されていなかった**
     （`python --version` が `Python` とだけ出る）。本物は `py`（3.14.5）だが pyscard が 3.14 用 wheel 無し →
     3.13 を導入して `py -3.13 -m pip install pyscard`（cp313 wheel）。
   - `watch` → `core.events` → `import numpy` で `ModuleNotFoundError`（numpy は型注釈でしか使っていない）。
   - Windows(usbccid) の slot 状態追跡: interrupt-IN ありでは通知を無視、無しでは polling せず
     bind 直後の `IccPowerOn ×3` に `ICC_MUTE` を返して `PRESENT|MUTE` を latch（詳細 ADR-0040）。
6. **Parameters 応答の不整合**: `bProtocolNum=1` と言いつつ T=0 用 5 byte を返していた。
7. リーダー電源が落ちていた時間帯（MUX scan 全 `1`）— firmware は正しく「通電 ch なし」と報告し USB は維持。

## Fixes Applied

- 1 → MUX scan の Low ch から reader を自動選択（`select_bringup_reader`）。配列は配線表順に。
- 2 → 送信前 BUSY=Low を要求（偽陽性除去）、再 init 撤去（診断はログのみ）。
- 3 → `PN5180_TRY_ISO14443=0`。
- 4 → ISO15693 のみ `reverse_bytes`（14443A は元から MSB-first）。
- 5 → `py -3.13` + pyscard wheel / `core.events` の numpy を `TYPE_CHECKING` ガード /
  `probe_pcsc raw` で OS 状態と hresult を可視化 → interrupt-IN 撤回（`CCID_USE_INTERRUPT_EP=0`）→
  **仮想カード常時挿入**（`CCID_VIRTUAL_CARD_ALWAYS_PRESENT=1`, ADR-0040）。
- 6 → T=1 は 7 byte、SetParameters は host 指定の bProtocolNum に追従。
- 途中の誤診（記述子コメントに「interrupt が無いから watch 0 件」と書いた）は訂正済み。

## Remaining Gaps / Out-of-Scope

- [x] `probe_pcsc watch --seconds 30` で `seat 1 UID=…` が 2 回出ることの確認（§8 受け入れの最後）→ OK。
- [x] CCID コマンドログ（IccPowerOn/Off, XfrBlock, Parameters）と `get_all_uids 戻り` を DEBUG に
      （初回 IccPowerOn だけ INFO）。jef-sure ドライバの `Tag Found!` 等は `main.c` の
      `esp_log_level_set(..., ESP_LOG_WARN)` で抑制（13 slot × 10Hz で UART が詰まる対策）。
      → 次回の書き込みで反映（本 worklog 時点の実機は 7091e3c）。
- [ ] `rfid_cards.json` への実カード登録（`watch` 表示 UID → `"E0:04:…": "Ah"`）。
- [ ] 13 slot 化（`CCID_SLOT_COUNT=13`、`PN5180_SPI_HZ` を 5MHz に戻す、RF 時分割 = 同時 RF ON 1 台）。
- [ ] branch `claude/exciting-goldberg-lkuojh`（契約 v1.1: UID MSB-first MUST / seat 1..8 / 配線 docs）との
      統合。firmware は v1.1 §7 に既に準拠。CLAUDE.md / CHANGELOG / 契約 §7 近傍で軽い衝突の見込み。
- [ ] `PCSCBridge.read_uid` の例外を DEBUG に落としている点は据え置き（`raw` が診断を担う）。

## Related ADRs

- `docs/adr/0040-ccid-virtual-card-always-present.md` — 本 worklog の中心的判断
- `docs/adr/0034-…`（契約 freeze）/ `docs/adr/0015-…`（USB CCID canonical）

## Related Issues

- ISSUE-0015（USB CCID firmware 契約）— 「残: watch で実カード UID」は `raw` で疎通確認、`watch` は次

## Related Commits

- `fb2e0a6` presence debounce + CCID ログ / `062c519` UID MSB-first / `9f0bc19` 通電 ch 自動選択 +
  NSS 偽陽性 + 再起動ループ修正 / `af0b092` interrupt-IN 試行 + 状態機械 + 14443 OFF /
  `04e54e0` numpy TYPE_CHECKING / `f69ce60` probe_pcsc raw / `5bcbf9a` interrupt-IN 既定 OFF /
  `9846c13` Parameters T=1 7B + MUTE 可視化 / `7091e3c` 仮想カード常時挿入
