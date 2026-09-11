# Worklog: 多台数接続で 1 台の init 失敗が全台停止になる問題の緩和（全 NSS High / 再試行 / 個別 skip）

## Date

2026-09-11（`bd14815` = firmware v1.2 P2 実装 の直後。実機 10 台接続の初回起動ログを受けて）

## Scope / Task

ISSUE-0023: `PN5180_READER_COUNT=11` で 10 台を接続したら reader 0 の `pn5180_init` が
「firmware version 読み取り失敗」で落ち、共有 SPI が壊れて全 reader が停止した。根本原因は
実機で未確定だが、firmware 側でできる緩和策（衝突経路を塞ぐ / 1 発目の失敗を吸収する /
1 台の失敗で全体を止めない）を入れる。

## Goal

- init 前に配線表の **全 13 本の NSS を output High** にして、init していない chip が MISO を
  駆動する経路を塞ぐ。
- `pn5180_init` 失敗時は **共有 SPI を作り直して 1 回だけ再試行**、それでも失敗ならその reader だけ
  **skip して他は続行**（`PN5180 ready: N/11 reader（skip: #k(init失敗), …）`）。
- RST 診断 / init 失敗診断に「RST がこの chip に届いていない可能性」の判定文を足す。
- 挙動不変の範囲: 成功経路（全台 init 成功）の動作・poll・CCID は変えない。

## Changed Files

- `firmware/esp32s3-pn5180-ccid/main/pn5180_reader.c`
  - `nss_deselect_all()`: `pn5180_reader_init` の先頭（MUX init 直後、RST を叩く前）で 13 本すべての
    NSS を `GPIO_MODE_OUTPUT` + High。
  - `spi_recreate_after_init_failure()`: `spi_bus_free` → `pn5180_spi_init` → ready 済み reader の
    `dev->spi` を新しい `pn5180_spi_t` に差し替え（ドライバの失敗経路が device を外し構造体も free
    するため）。
  - init ループ: 失敗 → 作り直し → 20 ms → 再試行 1 回 → 失敗なら作り直し → skip（`continue`）。
    深掘り診断 `diag_after_init_failure` は「まだ 1 台も ready でない」ときに起動につき 1 回。
    `skipped` バッファを 160 に拡張し `#k(init失敗)` を併記。
  - RST 診断: `s_rst_seen_high` を記録し、`during_rst=0 && after=0` で「RST 不通の可能性」を WARN。
    init 失敗診断の「NSS は設定どおり応答」分岐で、`s_rst_seen_high` が false なら
    「コネクタの RST ピン/配線の不通を疑う」を追記。
- `docs/issues/0023-pn5180-init-fails-with-many-readers.md`（新規）
- `firmware/esp32s3-pn5180-ccid/README.md` / `docs/rfid-ccid-firmware-checklist.md` §8:
  「通電しているのに init 失敗 = 全 reader 停止」→「再試行 1 回 → skip、他は続行」に更新。
- `CHANGELOG.md` / `CLAUDE.md` / `docs/decision-log.md`
- 本 worklog

## Expected Behavior

- 10 台接続・1 台だけ init できない状況でも、他の 9 台で起動して host から使える。
- 1 発目だけ噛み合わない chip（RST 不通で前回の途中状態が残る等）は再試行で拾える。
- 成功経路のログ・タイミングは従来と同じ（NSS High 固定は数十 µs、再試行は失敗時のみ）。

## Implemented Behavior

上記のとおり。**ESP-IDF がこの container に無いため実ビルド・実機は未検証**（スタブ・フルコンパイルと
register-level simulator で検証）。

実装上の判断:

- `pn5180_deinit(ret, false)` が **`free(pn5180->spi)` まで行う**ことをドライバ源（`/home/user/jef-sure/`
  の clone）で確認した。以前の「失敗した 1 台だけ skip はできない」はこれが理由で、再試行のためには
  device の add だけでなく `pn5180_spi_t` の作り直しが要る。`spi_bus_initialize` は二重呼び出しで
  `ESP_ERR_INVALID_STATE` を返すので、先に `spi_bus_free`（共有 device は deinit が外し済みなので通る）。
- ready 済み reader の `dev->spi` を差し替えるのは `pn5180_t` が公開 struct だから可能。
  `pn5180_proto_t`（14443/15693）は `dev` を指すだけなので影響なし。
- 再試行は **1 回**に留める（RST 不通 chip の「1 発目だけ噛み合わない」を吸収するのが目的で、
  本当に死んでいる chip に何度も 200 ms ずつ使わない）。
- NSS High 固定は `PN5180_READER_COUNT` の範囲外（予備 #12/#13）も含める。実機で #12 に通電した
  reader が刺さっていた（配線チェックの「設定範囲外の ch = ch11」）。
- 診断 `diag_after_init_failure` は自前で `spi_bus_add_device`/`remove` するだけなので、作り直した
  共有 SPI とは干渉しない（simulator で device 残数 1 を確認）。

## Test Results

- スタブ・フルコンパイル: `PN5180_READER_COUNT`(1/2/11/13) × `POLL_STATS_INTERVAL_MS`(0/10000) ×
  `PN5180_BUSY_VIA_MUX`(0/1) × `PN5180_FAST_INVENTORY`(0/1) = **32 構成 警告 0**
  （`-std=gnu17 -Wall -Wextra`）。
- simulator `sim_initretry.c`（新規, READER_COUNT=11, 実機と同じ通電マスク 0x0BF7）: **12 項目すべて ✅**
  （init true / NSS 13 本 High / reader 0 は 2 回呼ばれ再試行で成功 / reader 4 は 2 回で打ち切り /
  未通電・範囲外は init 未呼び出し / ready 8 台 / spi_init 4 回 / dangling 無し / device 残数 1 /
  poll 走行）。
- 既存 simulator: `sim_capture`（安全弁 2 ケース含む）失敗 0、`sim_multi`（P2 APDU 4 種）従来どおり、
  `sim_diag`（readers 1 / 11）の診断ログ従来どおり。
- Python は変更なし（823 passed のまま）。

## Mismatches Found During Testing

- simulator の実ドライバ模倣を「deinit で spi を free する」まで寄せたところ、作り直し無しの設計
  （device だけ add し直す案）では ready 済み reader が dangling になることが判明 → 構造体ごと作り直し
  + `dev->spi` 差し替えに変更。

## Fixes Applied

- 上記の設計変更（`spi_recreate_after_init_failure`）。

## Remaining Gaps / Out-of-Scope

- [ ] **実機検証**: 再試行で reader 0 が上がるか / `RST診断(ch0)` が 0 0 0 のままか（→ コネクタ #1 の
      RST ピン）。ISSUE-0023 の「次に実機で確認すること」。
- [ ] 根本原因の確定（NSS floating 衝突 / RST 不通 / 電源）。確定したら ISSUE-0023 を更新。
- [ ] 予備 #12（ch11）に挿さっている reader の扱い（#11 に挿し替え or 配線表の入れ替え）。

## 追記（同日）: 実機確認と配線表の振替

- `076b844` を同じ 10 台接続で起動 → reader 0 は **再試行なしで init 成功**、`PN5180 ready: 9/11
  reader（skip: #4, #11）`、9 台すべてでカード検出・離脱、1 周 124〜139 ms（ISSUE-0021 実機フィードバック 4）。
  NSS floating 衝突（仮説 1）が最有力。`RST診断(ch0)` の `during_rst=0` は残る（動作はする）。
- 実機の挿し方は「コネクタ #4（ch3, BUSY 不通）だけ飛ばして若い順」= #1,#2,#3,#5,…,#12 の 11 本
  （#13=ch12 は空き）。これに合わせて `app_config.h` の `PN5180_READERS` を **物理順に振替**:
  index 3（席 4）以降がコネクタ 1 つぶんずれ、index 10（board3）= コネクタ #12（ch11）。
  コネクタ #4 の行（nss 5 / ch3）は予備に下げた。各行の (nss, mux_ch) の組は変えていない
  （組がコネクタを表す）。ログの `#k` は index+1、`chN` がコネクタ。起動要約の skip 一覧に `chN` を併記
  （`skip: #10(ch10)` / `#4(ch4,init失敗)`）。
- simulator `sim_initretry.c` を振替後の表に合わせて更新（index 10 = ch11 を init する / ready 9）。

## 追記 2（同日）: 11 台目の切り分けと chip 生存確認の自動化

- 振替後の起動は `PN5180 ready: 10/11 reader（skip: #10(ch10)）`。host 側も `probe_pcsc list`
  （`physical readers: 11` + 11 件 matched）/ `check` 11 PASS / `watch` 22 タッチで契約 v1.2 経路を通し確認
  （ISSUE-0022）。
- **reader を コネクタ #11(ch10) ↔ #12(ch11) で入れ替えたら floating も ch10→ch11 に移動** =
  コネクタは両方正常で **reader 1 台の故障**と確定（位置依存なら floating は ch10 に残る）。
- そこから先（電源 or BUSY 線）を手で当てるしかなかったので **firmware に自動診断を追加**:
  - `spi_probe_nss(nss, mux_ch, fw_out, …)` = NSS スキャンの 1 候補ぶん（RST pulse → mux_select →
    device 一時 add → `READ_EEPROM(0x12)` → 固定待ち 1ms → 2 byte 受信 → remove）を切り出した共用関数。
    `diag_after_init_failure` もこれを使う（重複を解消）。
  - `diag_skipped_reader(idx, cfg)` = skip した reader ごとに 1 回呼び、`FW=xx xx`（chip 生存 →
    **BUSY 線のみ不通**）/ `FW=FF FF`（SPI 無応答 → **電源/GND・SPI 線・chip 個体**）を WARN で出す。
  - 呼ぶ位置は **init ループの後・RF config ロードの前**（probe が共有 RST を叩くため。RF config は
    その後のループでロードされるので消えない）。
- simulator に `spics_io_num` 別の device add 回数を記録し、「skip した reader（未通電 / init 失敗）に
  生存確認を 1 回送る」「範囲外の予備には送らない」を assert に追加（`sim_initretry.c`, 16 項目 ✅）。
  スタブ 32 構成 警告 0、`sim_capture`（fast/driver 両経路）/ `sim_multi` / `sim_diag` / `sim` 失敗 0。

## Related ADRs

- ADR-0041（1 slot + P2 で 11 台）

## Related Issues

- ISSUE-0023（本件）/ ISSUE-0021 / ISSUE-0022

## Related Commits

- `bd14815` firmware v1.2（P2）— 本件の直前。
- （本 worklog と同一の変更セット）init 再試行 / 個別 skip / 全 NSS High。
