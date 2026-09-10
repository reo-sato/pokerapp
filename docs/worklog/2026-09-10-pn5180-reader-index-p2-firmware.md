# Worklog: CCID slot を 1 つに固定し、物理リーダーを Get UID の P2 で選ぶ（firmware 側, 契約 v1.2 / ADR-0041）

## Date

2026-09-10（同日の `2026-09-10-pn5180-collpos-dfs.md` の続き。commit `e57c323` の上で作業）

## Scope / Task

実機で判明した **Windows の CCID multi-slot 制限**を受けた設計変更（ADR-0041 / 契約 v1.2）の
**firmware 側**の実装。`firmware/esp32s3-pn5180-ccid/` と firmware 向け docs のみを担当し、
host 側（`rfid/` / `tools/` / `tests/` / 契約本体 / ADR-0041 / ISSUE-0022 など）は別作業が同時進行。

## 背景（実機で確定）

- `CCID_SLOT_COUNT=2` で焼いても Windows（Microsoft 汎用 CCID ドライバ `usbccid`）は
  **slot 0 しか reader として公開しない**: `PokerRFID PN5180-CCID 0` は列挙されるが
  **`Reader not found: 'PokerRFID PN5180-CCID 1'`**。SpringCard / Microsoft Q&A でも汎用ドライバは
  「single-slot only」と明記されている。
- 「slot ごとに USB インターフェースを分ける」回避策は、**ESP32-S3 の USB endpoint が 6 本**
  （双方向 5 + IN 1）なので **最大 5 台**にしかならず、本番 11 台（席 8 + board 3）に届かない。
- → **CCID slot は常に 1 つ**にして、**物理リーダーは Get UID の P2（reader index）で選ぶ**
  （契約 v1.2 §6 / ADR-0041）。

## Goal

- USB 上は 1 slot（`bMaxSlotIndex=0`、reader_name は `PokerRFID PN5180-CCID 0` の 1 件だけ）。
- `FF CA 00 <k> 00`（k = 0..N-1）で reader k の UID（複数枚は 8B 連結・UID 昇順）+ `90 00`、
  0 枚は `6A 81`、`k >= N` は `6A 86`。**k=0 は従来と同一バイト列**（後方互換）。
- `FF CA 00 FF 00` → `<N>`（1 byte = 物理 reader 数）+ `90 00`。
- 物理 reader 台数は `PN5180_READER_COUNT`（既定 11）で、CCID slot 数とは独立。
- ATR / slot 状態（常時 present, ADR-0040）/ Parameters / UID の並び・向きは**不変**。

## Changed Files

firmware（`firmware/esp32s3-pn5180-ccid/`）:

- `main/app_config.h` —
  - `CCID_SLOT_COUNT` を **「USB 上の CCID slot 数 = 常に 1」** と定義し直し（Windows 制限と
    ESP32-S3 の EP 本数を根拠としてコメントに明記）。
  - **`PN5180_READER_COUNT`（既定 11）を新設** = 物理 reader 台数（配線表 `PN5180_READERS` の
    先頭何台を使うか）。段階手順コメントを 1 →（2 以上 = 配列順 = index 順、未通電は skip）→ 11 に更新。
    実機の現状（ch0 と ch10 = index 0 と 10）も記載。
  - 配線表のコメントを「配列順 = 物理 reader index 順 = Get UID の P2 = host config の
    `rfid.pcsc_readers[].reader`」に更新。
- `main/pn5180_reader.c` — `CCID_SLOT_COUNT` に依存していた配列・ループ・条件コンパイルを
  すべて **`PN5180_READER_COUNT`** に変更（`s_readers` / `s_cache` / `s_uid_miss` /
  `s_overflow_warned` / `s_rf_off_fail` / `s_collpos_logs` / `s_confirm_skips` / `_Static_assert` /
  init ループ / `select_bringup_reader` の `#if == 1` / `diag_wiring_map` の `#if > 1` / 未通電 skip /
  poll ループ / `pn5180_reader_get_card` の範囲チェック）。ログの「slot」表記を「reader / index」に
  （`PN5180 ready: N/M reader（skip: …）`、`poll 統計 … ready N reader`、skip 行に `Get UID P2=k`）。
  **1 台も起動できなかったときに NSS スキャン診断を呼ぶ**ようにした（複数 reader 構成では全台が
  MUX scan で skip され `pn5180_init` を一度も呼ばないため、従来は診断が出なかった）。
  併せて **coll_pos の安全弁**（下記）を追加。
- `main/pn5180_reader.h` — index は **物理 reader index（= Get UID の P2）**で CCID slot とは別物、
  と明記。`pn5180_reader_get_card(uint8_t reader_index, …)` に改名。
- `main/ccid_slot.c` — `handle_apdu` を **契約 v1.2 §6** に対応（`slot` 引数を廃止）:
  `is_get_uid` の判定を `apdu_len >= 4 && FF CA 00`（P2 自由）に緩め、`k = apdu[3]` で分岐
  （`0xFF` → `<N> 90 00` / `k >= N` → `6A 86` / それ以外 → `pn5180_reader_get_card(k)` の連結 or `6A 81`）。
  CCID slot 番号（ヘッダ byte5）の扱い（`icc_status` / `s_powered` 等）は従来どおり
  `CCID_SLOT_COUNT`(=1) のまま。`slot_present()` は「**1 台でもカードがあれば present**」に
  （`CCID_VIRTUAL_CARD_ALWAYS_PRESENT=0` のときだけ使われる経路）。
- `main/usb_descriptors.c` — `bcdDevice = 0x0200 | CCID_SLOT_COUNT` = **0x0201 固定**、
  `bMaxSlotIndex = 0` 固定であることをコメントで明記（**物理 reader 台数を変えても記述子は不変**、
  記述子を変えたときだけ上位バイトを上げる）。
- `main/main.c` — 起動ログを `PN5180 USB CCID reader: 1 CCID slot, 11 physical reader(s), product='…'` に。

docs（担当分）:

- `firmware/esp32s3-pn5180-ccid/README.md` — アーキテクチャ節の先頭に「CCID slot は 1 つ /
  物理リーダーは P2 / 台数は `FF CA 00 FF 00` / host は `pcsc_readers[].reader` で対応づけ」を追加。
  11 台化の段階手順を `PN5180_READER_COUNT` ベースに書き換え、受け入れを `physical readers: N` に。
  既知の制限に「CCID multi-slot は使えない」を追記。
- `docs/rfid-ccid-firmware-checklist.md` — §2 を「CCID slot は 1 つ / 物理リーダーは P2 /
  台数問い合わせ / index 順序の固定」に全面書き換え、§4 に `FF CA 00 <k> 00` / `6A 86` /
  `FF CA 00 FF 00` / 未通電 index は `6A 81` を追加、§8 と受け入れマトリクスを
  `PN5180_READER_COUNT` / `physical readers: N` に更新、§0/§1/§3 の文言も 1 slot 前提に修正。
- `docs/issues/0021-pn5180-poll-cycle-latency.md` — 冒頭に「本 issue の slot は物理 reader index を
  指す（ADR-0041 / v1.2 で用語が分離した）」の注記。
- 本 worklog。

## Expected Behavior

- host が `FF CA 00 <k> 00` を送ると **reader k** のキャッシュを返す。CCID のヘッダ slot 番号は
  常に 0 で、応答内容には影響しない。
- 台数問い合わせ（P2=0xFF）は `<N> 90 00` の 3 byte。範囲外 index は `6A 86`、
  範囲内だが未通電/カード無しは `6A 81`。
- 物理 reader 台数を 1 → 11 に増やしても **USB 記述子は変わらない**（Windows の記述子キャッシュ
  問題が起きない）。
- ATR・Parameters・slot 常時 present（ADR-0040）・UID の向き（MSB-first）と連結規則（v1.1 §6）は不変。

### 追加: coll_pos の安全弁（前タスク ISSUE-0021 実装 A の補強）

- 「**衝突位置を採用して分割したのに、そのラウンドで札が 1 枚も応答しなかった**」= 分けた両子枝が
  空、というラウンドが **`PN5180_COLLPOS_DISABLE_STREAK`(=3) 回連続**したら、`RX_COLL_POS` の基準
  （フレーム先頭 or UID 先頭）が想定と違うと判断して以後は **1 bit ずつ伸ばす DFS に固定**
  （`s_collpos_disabled`）し、**WARN を 1 回だけ**（「N 回連続」+ 最後の生値つきで）出す。
- 判定はラウンド末尾: 採用分割あり かつ 応答 0 枚 → `s_collpos_empty_streak++`、
  採用分割あり かつ 応答 1 枚以上 → streak を 0 に戻す、採用分割が無いラウンドは触らない。
- **1 回で切らない理由**: hole card 2 枚を同時に持ち上げる過渡では、root probe の時点ではまだ場に
  あって COLLISION → 子枝を probe する頃には 2 枚とも場外 → 両子枝 NONE、が普通に起こる
  （基準は正しいのに 0 枚）。1 回で恒久無効化すると、この過渡でリブートまで遅い 1bit DFS に
  張り付いてしまう。連続回数で見れば過渡は 1〜2 で途切れ、基準が本当に違う場合だけ積み上がる。
- 正常に採用できているときは何も変わらない。無効化されても取得できる UID は同じ（遅くなるだけ）。

## Implemented Behavior

上記のとおり。**ESP-IDF がこの container に無いため実ビルド・実機検証は未実施**
（スタブ・フルコンパイルと register-level simulator で検証）。

実装上の判断:

- `handle_apdu` から CCID の `slot` 引数を落とした（P2 が唯一の reader 選択軸になったため、
  両方を見ると「どちらが正か」が曖昧になる）。CCID slot 番号はヘッダ処理（`icc_status` /
  `s_powered` / GetSlotStatus / NotifySlotChange）だけで使う。
- 安全弁の判定を「新しい UID が増えなかった」ではなく「**応答した札が 0 枚**」にした。
  前者だと「Stay Quiet が効かず同じ札を再取得した」ケース（既存の `stale_rounds` が扱う事象）で
  誤発火し得るため。
- さらに **1 回では切らず `PN5180_COLLPOS_DISABLE_STREAK`(=3) 回連続を要求**する（親レビュー指摘）。
  札を持ち上げる過渡（root=COLLISION → 子枝の時点で場外 → 両子枝 NONE）が普通に起こり、
  1 回で恒久無効化すると**リブートまで遅い 1bit DFS に固定されてしまう**ため。閾値は
  `PRESENCE_HOLD_MISSES`(3) と同じ「連続回数」の考え方で、`pn5180_reader.c` に file-local で置いた
  （安全弁の状態と同じ場所に置く / 使用箇所より前で定義する必要があるため）。
- `s_collpos_disabled` と streak は **全 reader 共通**（基準は firmware 全体で同じはずなので、
  1 台で判明したら全台に適用する。逆に他の reader で採用が成功すれば streak は 0 に戻る）。
- 未通電 skip と `6A 86` の切り分け: skip した index も **範囲内なら `6A 81`**（host から見れば
  「その席にカードが無い」と同じ扱い）。`6A 86` は config の index が台数を超えているときだけ。

## Test Results

### 1. スタブ・フルコンパイル（`gcc 13` + ESP-IDF/ドライバのスタブヘッダ）

- `PN5180_READER_COUNT`(1/2/11/13) × `POLL_STATS_INTERVAL_MS`(0/10000) ×
  `PN5180_RF_OFF_BETWEEN_READERS`(0/1) × `PN5180_BUSY_VIA_MUX`(0/1) × `PN5180_TRY_ISO14443`(0/1) ×
  `PN5180_FAST_INVENTORY`(0/1) = **128 構成で警告 0 / エラー 0**（`-c -std=gnu17 -Wall -Wextra`）。
- 既定構成 + `-Wshadow -Wundef -Wvla -Wformat=2 -Wpointer-arith -Wcast-align -Wstrict-prototypes
  -Wmissing-prototypes -Wswitch-enum -O2` でも **警告 0**。
- `PN5180_READER_COUNT=14`（配線表 13 を超過）で `_Static_assert` が意図どおりビルドを止める。

### 2. register-level simulator（実 `pn5180_reader.c` / `ccid_slot.c` をリンク、ASan/UBSan）

**Get UID の応答バイト列**（`PN5180_READER_COUNT=11`、reader 0 に 1 枚 / reader 10 に 2 枚）:

| APDU | 応答 | 意味 |
|---|---|---|
| `FF CA 00 00 00` | `E0 04 01 50 11 22 33 40 90 00` | reader 0 = 1 枚（v1.0/1.1 と同一） |
| `FF CA 00 0A 00` | `E0 04 01 50 11 22 33 40 E0 04 01 50 11 22 33 41 90 00` | reader 10 = 2 枚（8B 連結） |
| `FF CA 00 0B 00` | `6A 86` | 範囲外 index |
| `FF CA 00 FF 00` | `0B 90 00` | 台数 = 11 |

`PN5180_READER_COUNT=1` では `FF CA 00 01 00` → `6A 86`、`FF CA 00 FF 00` → `01 90 00`。
CCID ヘッダの slot は常に 0 で送っている（`XfrBlock slot=0 …` のログで確認）。

**coll_pos 安全弁**（`sim_capture.c` に「root probe のあとに札を持ち上げる」過渡モードと
「衝突前の受信 UID を壊す」モードを追加）:

| シナリオ | 結果 |
|---|---|
| 持ち上げ過渡（採用分割で両子枝が空）が **1 poll だけ** | **無効化されない**（streak=1）。次の poll で coll_pos の採用が復活し 2 枚取得（**4 probe**） |
| 同じ空 poll が **3 回連続** | 3 回目で `… 札が取れないラウンドが 3 回連続（最後の coll_pos=21）→ coll_pos を無効化 …` を **WARN 1 回** |
| 無効化後の poll | coll_pos を使わず 1bit DFS（**14 probe**）で 2 枚取得（取れる UID は同じ） |

既存シナリオ（coll_pos DFS 6 probe / 定常の確認間引き / ノイズ再試行 / capture / UID 単位 hold ほか）も
**fast・driver 両経路で失敗 0 件**のまま。`sim_diag.c`（init 失敗診断）は `PN5180_READER_COUNT=1` と
**11（全台 skip → 新しく追加した診断呼び出し）**の両方で 13 候補すべてに SPI を送り結論ログが出ることを確認。

### 3. Python

- `pytest tests/ -q --ignore=tests/test_vision.py` → **805 passed / 2 failed**
  （`tests/test_tools_register_cards.py`）。失敗は **host 側 agent が同時編集中の
  `tools/register_cards.py` / `rfid/bridge.py` 由来**で、本タスク（firmware のみ）とは無関係。
  pristine HEAD の worktree では同テストが green であることを確認済み。

## Mismatches Found During Testing

1. **複数 reader 構成だと init 失敗診断が出なくなる**: `sim_diag.c`（BUSY 常時 High = 実機の
   floating 症状）を `PN5180_READER_COUNT=11` で走らせると、全 reader が MUX scan で skip され
   `pn5180_init` が一度も呼ばれず、`diag_after_init_failure`（BUSY 非依存の NSS スキャン）が
   出ないまま `PN5180 ready 0 台` で終わっていた。「チップが死んでいるのか BUSY/MUX 経路だけが
   壊れているのか」を実機で切り分けられない。
2. simulator の `dump()` が **CCID slot 番号に reader index を入れていた**（v1.1 まではそれで
   reader を選べた）。v1.2 では CCID slot は常に 0 で P2 が reader を選ぶので、そのままでは
   常に reader 0 を読んでしまう。

## Fixes Applied

1. `ready_count == 0` のときに `diag_after_init_failure(cfg0)` を呼ぶようにした（firmware 側の修正）。
   `PN5180_READER_COUNT` 1 / 11 の両方で診断が出ることを simulator で確認。
2. simulator の `dump()` を「CCID slot = 0 / P2 = reader index」に修正し、P2 の受け入れ確認
   （0 / 10 / 範囲外 / 0xFF）を追加した（harness 側の修正）。

## Remaining Gaps / Out-of-Scope

- [ ] **実機検証**（ESP-IDF がこの環境に無い）: 焼いて `probe_pcsc list` の `physical readers: 11`、
      `watch` で index 0 と 10 が別々に発火するか、`FF CA 00 0B 00` が `6A 86` になるか。
- [ ] **11 台の段階 bring-up**（1 → 2 → 11）と `PN5180_SPI_HZ` 1MHz → 5MHz は未消化のまま。
- [ ] **`RX_COLL_POS` の基準は依然として実機未確認**（安全弁と fallback で保護している）。
      起動後 reader ごと 3 回の `coll_pos=…` INFO で確定する。
- [ ] host 側（契約 v1.2 本体 / ADR-0041 / `rfid/bridge.py` の P2 対応 / `probe_pcsc` の
      `physical readers` 表示 / config の `reader` フィールド）は**別作業**。firmware と host の
      組み合わせでの通し確認は両方が入ってから。
- [ ] `CCID_VIRTUAL_CARD_ALWAYS_PRESENT=0` の経路（`slot_present` = いずれかの reader にカード）は
      実機で使っていない（既定は常時 present）。pcsc-lite 環境で使うときに要確認。

## Related ADRs

- `docs/adr/0041-*`（別作業で作成中）— CCID slot 1 つ + Get UID P2 = 物理 reader index の設計判断。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 契約 v1.0 の凍結（v1.2 で §3/§6 を改訂）。
- `docs/adr/0040-ccid-virtual-card-always-present.md` — slot 常時 present / カード有無は SW のみ（不変）。

## Related Issues

- `docs/issues/0021-pn5180-poll-cycle-latency.md` — poll 周期（用語注記を追加）。
- `docs/issues/0022-*`（別作業で作成中）— Windows の CCID multi-slot 制限。

## Related Commits

- `e57c323` — coll_pos DFS + 確認 probe 間引き + ノイズ再試行（本タスクの土台）。
- （本 worklog と同一の変更セット）CCID slot 固定 + P2 reader index + coll_pos 安全弁。
  **未コミット**（親のレビュー後にコミットする運用）。
