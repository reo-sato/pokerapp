# Worklog: PN5180 高速 inventory + 重ね置き anti-collision（firmware）

## Date

2026-09-10（同日の `2026-09-10-multi-reader-firmware-prep.md` の続き）

## Scope / Task

`firmware/esp32s3-pn5180-ccid/main/` の RF 読み取り経路を差し替える。

- **(A)** ドライバの総当たり `get_all_uids()` をやめ、**自前の高速 inventory**（1 reader あたり
  INVENTORY を必要最小回数だけ送る）にする（ISSUE-0021）。
- **(B)** 要件変更に対応し、**1 reader に重ねて置かれた複数カードを全部読む**
  （席 = hole card 2 枚 / board1 = flop 3 枚 / board2 = turn / board3 = river）。
  cache と CCID Get UID を複数 UID に拡張する。
- **(C)** 本番構成を **13 → 11 slot**（席 8 + board 3）としてコメント / docs を直す。

契約 `docs/contracts/rfid-usb-ccid.md` と `CHANGELOG.md` / `CLAUDE.md` / `docs/decision-log.md` は
**本タスクでは触っていない**（複数 UID 連結の契約明文化 = v1.1 と host 側対応は別担当が同時進行。
worklog `2026-09-10-rfid-multi-card-host.md`）。本 firmware は **契約 v1.1 §6 の連結規則
（8B × k ≤ 4、UID 昇順、0 枚 = `6A 81`）に準拠**している。

## Goal

- カード無しの reader 1 台 **≤ 15 ms**、本番 11 slot で **1 周 ≈ 200 ms**（上限 0.5 s）。
- 席に 2 枚・flop に 3 枚を重ねても、その reader の UID を **全部** host に渡せる。
- `PN5180_FAST_INVENTORY=0` でドライバ経路に戻せる（A/B）。cache 形式は両経路で同じ。
- 出荷時の `CCID_SLOT_COUNT` は **1 のまま**（実機 1 slot の回帰を起こさない）。

## Changed Files

firmware（`firmware/esp32s3-pn5180-ccid/`）:

- `main/app_config.h` —
  - 追加: `PN5180_FAST_INVENTORY`(1) / `PN5180_FAST_RF_CONFIG`(`PN5180_15693_26KASK100`) /
    `PN5180_FAST_RX_TIMEOUT_MS`(10) / `PN5180_FAST_FIELD_SETTLE_US`(1000) /
    `PN5180_MAX_CARDS_PER_READER`(4) / `PN5180_FAST_MAX_PROBES`(12)。
    実測値（1 台 1 周 176〜890 ms）とドライバの総当たり構造を理由としてコメントに記載。
  - 本番台数を **11**（席 8 + board 3、board1=flop 3 枚 / board2=turn / board3=river）に修正。
    配線表 `PN5180_READERS` は 13 台分のまま（#12/#13 = 予備）で、コメントだけ更新。
- `main/pn5180_reader.h` — `pn5180_card_t` を複数 UID 化:
  `{ bool present; uint8_t count; uint8_t uid_len; uint8_t uids[PN5180_MAX_CARDS_PER_READER][16]; }`
  （旧 `uint8_t uid[16]` は廃止）。`app_config.h` を include。
- `main/pn5180_reader.c` —
  - ISO15693 フラグ / コマンド定数を file-local 定義（ドライバの public ヘッダに無いため）。
  - `slot_reader_t.rf_loaded` 追加。init ループの**後**に第 2 ループで
    `pn5180_loadRFConfig(dev, PN5180_FAST_RF_CONFIG)`（理由は下記「設計根拠」4）。失敗しても
    slot は生かし、`rf_loaded=false` で poll 側が再試行。
  - `fast_probe_15693()`: INVENTORY を 1 回送り結果を 3 値（NONE / UID / COLLISION）で返す。
  - `fast_inventory_15693()`: mask ベースの DFS（固定長スタック `PN5180_FAST_MAX_PROBES + 2`）。
    RF は reader の DFS 全体で ON、終わりは既存の `rf_off_after_read`。
  - `read_uid_from_proto` → `read_uids_from_proto`: ドライバ経路も `uids_count` 個
    （最大 4）を同じ cache 形式に入れる。
  - `sort_uids()`（memcmp 昇順）/ `cards_equal()` / `format_uids()` を追加。poll は
    「UID 集合が変化したときだけ」ログ（枚数変化・カード差し替えも検出）。
  - poll 中だけ `pn5180_t.timeout_ms` を 40 ms に絞る（既定 500 ms の影響を潰す。下記 5）。
  - `#include <stdlib.h>` を追加（既存の `free()` が implicit declaration だった）。
- `main/ccid_slot.c` — Get UID 応答を **UID の連結**に（`count × uid_len` byte + `90 00`）。
  0 枚は従来どおり `6A 81`。コメントに「複数枚は uid_len ごと連結、host が分割」を明記。

docs:

- `docs/issues/0021-pn5180-poll-cycle-latency.md`（新規, Status: Fixed / 実機未検証）。
- `firmware/esp32s3-pn5180-ccid/README.md` — 「複数 reader 化の準備」に高速 inventory と
  重ね置き対応を追加、段階手順を 11 slot 基準にして `poll 統計 ≤ 300 ms` の確認を追記。
- `docs/rfid-ccid-firmware-checklist.md` §8 — 「1 reader あたりの inventory は 1 回に絞る」と
  「重ね置きは連結して返す」を追加、受け入れに `poll 統計` 1 周 ≤ 300 ms（11 slot）。
- 本 worklog。

## 設計根拠（jef-sure/pn5180 のソースで確認した事実）

managed component（リポジトリには入らない）を今回 clone して確認した。前回 worklog は
public ヘッダからの推定だったが、今回は実装まで読んで裏を取っている。

1. **`get_all_uids()` は毎回 RF 設定 2 種 × データレート 2 種を総当たり**
   （`rf_fallbacks[] = {ASK10, ASK100}`、`iso15693_use_high_rate = true → false`、各 40 ms timeout）。
2. **見つかっても break しない**: 打ち切り条件が
   `uids_count > 0 && iso15693_use_high_rate == false` なので、high rate で見つかった場合は
   break せず次の RF 設定も走る（= カードがあっても必ず 2 セット走る）。
3. **`pn5180_loadRFConfig(pn5180, txConf)` は RX 設定を `txConf | 0x80` で決め打ち**
   （`src/pn5180.c:816`）。PN5180 の RF config 表では **0x0D → 0x8D = ISO15693 26 kbps RX** /
   **0x0E → 0x8E = 53 kbps RX**。標準 INVENTORY（high data rate = 26.48 kbps 応答）を受けられるのは
   **0x0D/0x8D の組だけ**なので、fast 経路の既定は `PN5180_15693_26KASK100`（0x0D）にした。
   → **親タスクの指示は既定 ASK10（0x0E）だったが、上記の理由で ASK100 に変えている**。
   ドライバは ASK10 を「先に」試すが必ず ASK100 も走るため、「ドライバで読めている＝ASK10 で
   読めている」ことにはならない。ASK10 は電源が安定する利点があるので、実機で ASK100 の
   届きが悪ければ切り替えて A/B する（切り替えて読めなくなるなら RX 不一致が原因）。
4. **`pn5180_init()` は内部で共有 RST を pulse する**ので、reader k の init が reader k+1 の
   レジスタ（= LOAD_RF_CONFIG の内容）を消す。したがって RF 設定のロードは **全 reader の init が
   終わってから**の別ループでなければならない（init ループ内でロードすると後続の init で失われる）。
5. **`pn5180_t.timeout_ms` の既定は 500 ms**（`src/pn5180.c:193`）。これは SPI の BUSY 待ち・
   transceive 状態待ち・RF off 待ちすべての上限なので、1 台の不調が 1 周を 0.5 秒伸ばす。
   ドライバ自身も `get_all_uids` の間だけ 40 ms に落としているので、fast 経路も同じ手当てをした。
6. **ドライバは UID を読むたび Stay Quiet を送る**（`pn5180_iso15693_stay_quiet`）。quiet は RF を
   切ると解除されるので `PN5180_RF_OFF_BETWEEN_READERS=1` でたまたま成立していた。fast 経路は
   Stay Quiet を送らない（1 slot inventory の mask 分割だけで全枚数を列挙できるため）。
7. 衝突判定はドライバと同じく `RX_STATUS` の `RX_COLLISION_DETECTED` と受信バイト数。
   ドライバは `num_bytes >= 10` を有効フレームとしているので、fast 経路も **`n != 10` ではなく
   `n < 10` を「衝突/無効」扱い**にしてある（CRC が剥がれず 12 byte で来る等の実機差で
   取りこぼさないため）。

## Expected Behavior

- `PN5180_FAST_INVENTORY=1`（既定）: reader ごとに
  RF ON → 1 ms 待ち → INVENTORY(mask) → 応答待ち ≤ 10 ms → （衝突なら mask を 1 bit 伸ばして再試行）
  → RF OFF。probe は最大 12 回。カード無しは 1 probe で終わる。
- 席に 2 枚 / flop に 3 枚を重ねると、その slot の `pn5180_card_t.count` が 2 / 3 になり、
  CCID `FF CA 00 00 00` の応答が `UID×count + 90 00`（2 枚なら 18 byte）になる。
- UID の並びは memcmp 昇順に正規化され、同じ組み合わせなら毎 poll 同じ順序。
- 0 枚は従来どおり `6A 81`。debounce（`PRESENCE_HOLD_MISSES=3`）は集合ごと保持。
- `PN5180_FAST_INVENTORY=0` で同じ cache 形式のままドライバ経路に戻る。
- `CCID_SLOT_COUNT` は 1 のままなので、実機 1 slot の挙動は「速くなる」以外変わらない。

## Implemented Behavior

上記のとおり実装。**ESP-IDF toolchain がこの container に無いためファームの実ビルドと実機検証は
未実施**。代わりに以下 2 つで検証した。

### 1. スタブ・フルコンパイル（gcc 13, `-c -std=gnu17 -Wall -Wextra`）

ESP-IDF / ドライバ API のスタブヘッダ（今回、実ヘッダのソースに合わせて書き直し）で
`pn5180_reader.c` / `ccid_slot.c` / `usb_descriptors.c` を **フルコンパイル**:

- `CCID_SLOT_COUNT`(1/2/11/13) × `POLL_STATS_INTERVAL_MS`(0/10000) ×
  `PN5180_RF_OFF_BETWEEN_READERS`(0/1) × `PN5180_BUSY_VIA_MUX`(0/1) ×
  `PN5180_TRY_ISO14443`(0/1) × `PN5180_FAST_INVENTORY`(0/1) = **128 構成で警告 0 / エラー 0**。
- `CCID_SLOT_COUNT` = 0 / 14 では `_Static_assert` が意図どおりビルドを止める。
- 既定構成に対して追加で
  `-Wshadow -Wundef -Wvla -Wformat=2 -Wpointer-arith -Wcast-align -Wstrict-prototypes
   -Wmissing-prototypes -Wswitch-enum -O2` でも **警告 0**。

### 2. register-level simulator（実コードをリンクして DFS を走らせる）

`pn5180_sendData` を「INVENTORY フレームを解釈して仮想タグの応答を作る」実装に差し替え、
**本物の `pn5180_reader.c` と `ccid_slot.c`** をリンクして実行（ASan/UBSan 有効、エラー 0）。
mask の意味（LSB-first、mask bit i = 受信 rx[2] の bit0 から i 番目）もここで検証している。

| シナリオ | probe 数 | 結果 |
|---|---|---|
| 0 枚 | 1 | `present=0` / GetUID `6A 81` |
| 1 枚 | 1 | 1 枚 / GetUID 10 byte（UID+SW） |
| 2 枚（bit0 が違う = 通常） | 3 | 2 枚 / GetUID 18 byte |
| 2 枚（下位 7bit が同一 = 最悪） | 12（打ち切り） | **2 枚とも取得**（打ち切り前に発見） |
| 3 枚（flop） | 5 | 3 枚 / GetUID 26 byte |
| 5 枚（`MAX_CARDS=4` 超過） | 8 | 4 枚で停止（GetUID 34 byte） |
| ノイズ（protocol error のみ） | 1/poll | 分割せず「カード無し」に倒れる |
| debounce（2 枚 → 0 枚） | — | 3 周 present を保持 → 4 周目に離脱 |

`PN5180_FAST_INVENTORY=0`（ドライバ経路）でも **同じ cache 内容・同じ GetUID バイト列**になることを
同じシナリオで確認（A/B が意味のある比較になる）。

## Test Results

- firmware: 上記 128 構成のフルコンパイル（警告 0）+ simulator（ASan/UBSan clean）。
- `pytest tests/ -q --ignore=tests/test_vision.py` → **774 passed**（31s）。
  **本タスクは Python を 1 行も触っていない**（firmware + docs のみ）。
- **未実施**: ESP-IDF v5.x での実ビルド、実機書き込み、実 RF での 1 周実測と重ね置き読み取り。

## Mismatches Found During Testing

1. **既定 RF 設定の指示と実装の食い違い（意図的）**: 親タスクの指示は
   `PN5180_FAST_RF_CONFIG = PN5180_15693_26KASK10` だったが、ドライバ実装を読むと RX 設定が
   `tx | 0x80` 決め打ちで ASK10 → 53 kbps RX になり、26.48 kbps の標準応答を受けられない。
   実機で「1 枚も読めない」になる可能性が高いと判断し **ASK100 を既定**にした（上記「設計根拠」3）。
2. `n != 10` を UID の条件にすると、CRC が剥がれない実機差で全滅する恐れがある。ドライバは
   `>= 10` を採用しているので合わせた（「設計根拠」7）。
3. ドライバ経路（`PN5180_FAST_INVENTORY=0`）で `free()` が implicit declaration だった
   （**既存**。ESP-IDF では FreeRTOS.h 経由で `<stdlib.h>` が入るためビルドは通っていた。
   前回 worklog の「Remaining」項目）。今回そのコードを触ったので `#include <stdlib.h>` を追加。
4. 最初のスタブは `static void inline`（実ヘッダと同じ書き方）で `-Wold-style-declaration` が
   出た。ドライバヘッダ由来のノイズなので、スタブを `-isystem` で system header 扱いにして
   自分のコードの警告だけを見るようにした（ヘッダの書き方は実物どおりに保つ）。

## Fixes Applied

1. 既定を `PN5180_15693_26KASK100` にし、理由と A/B 手順を `app_config.h` のコメント・
   ISSUE-0021 Open question 3・本 worklog に残した（実機で切り替えられる）。
2. 受信バイト数の判定を `n < 10` に。
3. `#include <stdlib.h>` を追加（128 構成すべてで警告 0 になった）。
4. 検証スクリプト側の対応のみ（firmware は変更なし）。

## Remaining Gaps / Out-of-Scope

- [ ] **firmware × host の突き合わせ**: host 側は同時進行の別担当が対応済（契約 v1.1 §6 の連結 /
      `rfid/bridge.py:split_uid_response` / `RFIDThread` の UID 集合デバウンス、worklog
      `2026-09-10-rfid-multi-card-host.md`）。**両者を組み合わせた実機通しは未実施**。
      バイト列の取り決めは一致していることを確認済（8B × k ≤ 4 / UID 昇順 / 0 枚 = `6A 81` /
      host は応答長 16/24/32 のときだけ分割 ⇔ firmware の `PN5180_MAX_CARDS_PER_READER=4`）。
- [ ] **実機での 1 周実測**: `poll 統計` が 1 slot で ≤ 20 ms、11 slot で ≤ 300 ms か。
- [ ] **実機での重ね置き読み取り**: 席 2 枚 / flop 3 枚が毎 poll 安定して 2 枚 / 3 枚取れるか
      （アンテナの結合が強いと 2 枚目が給電不足で応答しないことがある。取れないときは
      `PN5180_FAST_FIELD_SETTLE_US` を伸ばす / RF 設定を ASK10 に振る / アンテナ間隔を見る）。
- [ ] **`PN5180_FAST_RF_CONFIG` の ASK10 / ASK100 選択**を実機で確定する。
- [ ] **11 slot への段階 bring-up**（1 → 2 → 11）と `PN5180_SPI_HZ` 1MHz → 5MHz。
- [ ] **DFS の probe 数削減（未実装）**: `RX_STATUS` の `RX_COLL_POS`（衝突ビット位置）を使えば
      mask を一気に伸ばせる。最悪ケース（下位ビットが揃った 2 枚 = 12 probe）を数 probe に
      できるが、bit 位置の基準を実機で確かめる必要があるので見送り（ISSUE-0021 Open question 4）。
- [ ] **poll task のスタック**: 今回 `poll_once` / DFS のローカルが約 0.5 KB 増えた
      （`main.c` の `xTaskCreate(..., 4096, ...)`）。実機で `uxTaskGetStackHighWaterMark` を
      一度見ておくと安心。
- [ ] 電源（`bMaxPower=0x32`=100mA 宣言）と `CARD_POLL_INTERVAL_MS` / `PRESENCE_HOLD_MISSES` の
      11 台向け調整は前回 worklog から未消化のまま。

## Related ADRs

- `docs/adr/0040-ccid-virtual-card-always-present.md` — 0 枚 = `6A 81` はこの設計のまま。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 契約 v1.0。複数枚の連結は
  §6/§7 の拡張（v1.1 予定、本タスクでは契約ファイルを変更していない）。

## Related Issues

- `docs/issues/0021-pn5180-poll-cycle-latency.md` — 本タスクの issue（Fixed / 実機未検証）。
- `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md` — firmware↔host 契約。

## Related Commits

- （本 worklog と同一の変更セット）firmware: 高速 inventory + mask anti-collision +
  複数 UID cache / CCID 連結応答、および docs（issue 0021 / README / checklist §8）。
  **未コミット**（親のレビュー後にコミットする運用）。
