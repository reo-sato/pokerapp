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

## Expected Behavior

- 1 reader あたりの inventory は **カード無しで数十 ms 以内**（目標 ≤ 15 ms）。
- 本番 **11 slot**（席 8 + board 3）で **1 周 ≤ 0.5 s**（できれば ≈ 200 ms）。
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

## Regression Check

**実機で** `idf.py monitor` の `poll 統計` を見る（自動テスト不可 = 実 RF が要る）:

- `CCID_SLOT_COUNT=1`（カード無し）: **1 周 ≤ 20 ms**。
- `CCID_SLOT_COUNT=11`（実運用の配置）: **1 周 ≤ 300 ms**（上限 0.5 s）。
- `python tools/probe_pcsc.py raw` で、席に 2 枚重ねたときの Get UID 応答が **18 byte**（16 + SW）、
  flop 3 枚で **26 byte**。`watch` で 1 slot から UID が枚数ぶん出る（host v1.1 の分割込み）。

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
4. **DFS の probe 数削減（未実装）**: RX_STATUS の `RX_COLL_POS`（bits 25:19 = 最初に衝突した
   ビット位置）を使えば、1 bit ずつではなく衝突位置まで mask を一気に伸ばせる。UID の下位
   ビットが揃った組み合わせの最悪ケース（12 probe）を数 probe に短縮できるが、bit 位置の
   基準（フレーム先頭からか UID 先頭からか）を実機で確かめる必要があるので今回は見送り。

## Related Worklog

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
fast 経路は Stay Quiet を送らない（1 slot inventory の mask 分割だけで全枚数を列挙できるため、
後始末が不要で RF ON/OFF の順序にも依存しない）。
