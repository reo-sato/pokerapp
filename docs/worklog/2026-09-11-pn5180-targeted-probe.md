# Worklog: 定常状態の inventory を「前回 UID の狙い撃ち probe」にする（満載 1 周 538 ms の対策）

## Date

2026-09-11（`405f218` の続き。実機 10 台で満載の poll 実測が取れたのを受けて）

## Scope / Task

ISSUE-0021 実機フィードバック 6: 8 席 × 2 枚 + board を載せると **1 周 538 ms**（目標 ≤ 300 ms）。
原因が実機ログで確定したので、定常状態の探索方法を変える。firmware のみ（host / 契約は不変）。

## 根本原因（実機ログから確定）

```
reader 0: coll_pos=17（UID bit -1）, 受信 1 byte, cur.len=0 → fallback(1bit)
poll 統計: 1 周 min/avg/max = 529/538/550 ms, probe 最大 6, coll_pos fallback 255
```

- **`RX_COLL_POS` は実機では一度も使えない**。ISO15693 は UID を **LSB-first** で送るので、
  変化の大きいシリアル下位バイトが最初に流れ、衝突は必ず **UID 先頭バイト**（`coll_pos` 16〜19 =
  UID bit 0〜3）で起きる。そのとき PN5180 が返す受信は **1 byte（flags だけ）** なので
  `fast_fill_coll` の `if (n < 2 + pos/8 + 1) return;` に落ち、`pos=0xFF`（= `UID bit -1`）。
  → 毎回 1 bit 伸ばしの DFS になる（`coll_pos fallback` が 10 秒で 255 回）。
- 1 bit DFS は「**衝突位置より手前**で割る」ので、衝突が bit 1 以降にあると
  **必ず片方の子枝が空**になり、その probe が RX timeout（8 ms）を丸ごと待つ。
  さらに残った枝はまた衝突するので、bit 0,1,2… と刻む。実測 probe 6 / reader 53 ms はこれ。

## Goal

- 定常状態（前回と同じ札が載ったまま）で **DFS と空枝の RX timeout を無くす**。
- 新しい札の検出は **毎 poll** のまま（間引かない）。
- 取れる UID・順序・host 契約・CCID は不変。

## Changed Files

- `firmware/esp32s3-pn5180-ccid/main/app_config.h`
  - `PN5180_FAST_TARGETED_PROBE`（既定 1）= 狙い撃ち probe の有効化。根拠（実機 538 ms と
    coll_pos が使えない理由）をコメントに記載。
  - `PN5180_FAST_TARGETED_MASK_BITS`（既定 32）= 狙い撃ちの mask ビット数。
- `firmware/esp32s3-pn5180-ccid/main/pn5180_reader.c`
  - **実装 C: 狙い撃ち probe**（`fast_inventory_15693` の前段）。前回 cache の UID を
    `mask_len=32` の inventory で 1 枚ずつ直接呼ぶ。**完全一致なので衝突が起きず即答**。
    当たった札は `fast_stay_quiet_15693` で黙らせるので、続く root probe には**新しい札だけ**が残る
    （定常状態は root 1 回 NONE で終了 = N+1 probe）。外れ / 衝突はそのまま root + DFS に任せる。
  - `fast_mask_from_msb_uid()`: MSB-first UID → mask 値（`raw[j] = msb[7-j]` の LSB-first 対応）。
    32 bit は MSB-first 末尾 4 byte = **ICODE のシリアル全部**なので実用上一意。
  - **応答待ちの上限をフレーム長に連動**（`fast_probe_15693`）: `tx_us + PN5180_FAST_RX_TIMEOUT_MS`。
    26.48 kbps で 1 byte ≈ 0.30 ms なので mask 64 bit のフレームは 13 byte ≈ 3.9 ms かかり、
    固定 8 ms では応答（tx+t1+rx ≈ 7.9 ms）の直前で打ち切る = **狙い撃ちが常に外れる罠**だった。
  - poll 統計に **`狙い撃ち H/P 命中`** と **`応答待ち最長 x.x ms`**（応答が返った probe のみ）を追加。
    後者は `PN5180_FAST_RX_TIMEOUT_MS` をどこまで下げられるかを実測で決めるための計器。
- `docs/issues/0021-pn5180-poll-cycle-latency.md` / `CHANGELOG.md` / `CLAUDE.md` / 本 worklog

## Expected Behavior

| 状況 | probe | 内訳 |
|---|---|---|
| 定常（N 枚が載ったまま） | **N+1** | 狙い撃ち N（即答）+ root 1（NONE） |
| 1 枚増えた | N+2 | 狙い撃ち N + root(新 UID) + root(NONE) |
| 1 枚減った | N+1 | 狙い撃ち N（1 つ外れ = timeout）+ root(NONE) |
| 全部新規（空 → 載る） | 従来と同じ | cache が空なので前段は走らず root + DFS |

- フレーム時間の見積り（26.48 kbps, CRC 2 byte 込み）:

| mask | フレーム | 送信 | 応答到着(tx+t1+rx) | 上限(tx+8ms) |
|---|---|---|---|---|
| 0 bit | 5 B | 1.51 ms | 5.46 ms | 9.51 ms |
| 32 bit（狙い撃ち） | 9 B | 2.72 ms | 6.66 ms | 10.72 ms |
| 64 bit | 13 B | 3.93 ms | 7.87 ms | 11.93 ms |

## Implemented Behavior

上記のとおり。**ESP-IDF がこの container に無いため実ビルド・実機は未検証**。

実装上の判断:

- **mask は 32 bit**（64 ではない）。64 だとフレームが 13 byte ≈ 3.9 ms になり 1 probe が単純に遅い。
  32 bit で ICODE のシリアル 4 byte を全部含むので衝突はまず起きず、起きても root + DFS が拾う。
- **Stay Quiet は狙い撃ちでも送る**。これが無いと続く root probe が既知の札で衝突し、
  「新しい札が居るか」を判定できない。
- **`PN5180_FAST_CONFIRM_EVERY`（確認 probe の間引き）は定常状態では発動しなくなる**。
  root が 1 ラウンド目で NONE を返して `break` するため、間引き判定に到達しない。
  結果として **新しい札の検出が毎 poll に戻る**（間引きの副作用が消える）。Stay Quiet と root を
  さらに削る余地はあるが、それは実測を見てから（下の「残」）。
- 応答待ちの上限は「フレーム送信 + 応答予算」に分解した。`PN5180_FAST_RX_TIMEOUT_MS` の意味が
  「応答ぶんの予算」に変わるので、mask 長を変えても誤打ち切りしない。

## Test Results

- スタブ・フルコンパイル: `PN5180_READER_COUNT`(1/11/13) × stats(0/10000) × mux(0/1) × fast(0/1) ×
  **targeted(0/1)** = **48 構成 警告 0**。`PN5180_FAST_TARGETED_MASK_BITS` は 8 の倍数 8..64 を
  `_Static_assert` で強制。
- register-level simulator `sim_capture.c`（実 `pn5180_reader.c` をリンク, ASan/UBSan）:
  - **定常 3 枚 = 4 probe（狙い撃ち 3 + root NONE 1）が 5 poll 連続**、かつ
    **3 probe すべてが `mask_len=32`**（= 衝突しない経路）を assert。
  - 2 枚定常 = 3 probe / 1 枚増 = 4 probe / 1 枚減 = 4 probe に期待値を更新（いずれも従来より少ない）。
  - 既存シナリオ（capture effect / Stay Quiet / UID 単位 hold / ノイズ再試行 / coll_pos 安全弁
    2 ケース）は **失敗 0** のまま。driver 経路（fast=0）も 失敗 0。
  - 狙い撃ち probe の frame 長チェック（`len == 3 + nbytes`）も simulator 側で通過。
- `sim_multi`（P2 APDU 4 種）/ `sim_initretry`（init 再試行・skip・chip 生存確認）/ `sim_diag` /
  `sim`（driver 経路 11 台）すべて 失敗 0。
- Python は未変更。

## Mismatches Found During Testing

1. **狙い撃ちを mask_len=64 で実装すると、固定 8 ms の応答待ちで必ず打ち切る**（フレーム 3.93 ms +
   t1 + 応答 3.8 ms ≈ 7.87 ms）。シミュレータは時間をモデル化していないので気付かず、
   26.48 kbps からの手計算で発見した。→ 上限をフレーム長連動にし、mask を 32 bit に縮めた。
2. simulator の定常状態 assert（「確認 probe は CONFIRM_EVERY 回に 1 回」）が、狙い撃ち導入後は
   成立しない（root が 1 ラウンド目で NONE → break するので間引き判定に来ない）。
   → 「定常は N+1 probe に固定」の assert に置き換えた。これは**意図した挙動変更**
   （新しい札の検出が毎 poll に戻る）。

## Fixes Applied

- 上記 1（フレーム長連動の上限 + mask 32 bit）、2（assert の置き換え）。

## Remaining Gaps / Out-of-Scope

- [ ] **実機で 1 周を再測定**（満載: 8 席 × 2 枚 + board1 3 枚 + board2/3）。目標 ≤ 300 ms。
      新しい統計 `狙い撃ち H/P 命中` が「H = 載っている枚数の合計」になっているか、
      `coll_pos fallback` が 0 近くに落ちるかを確認する。
- [ ] **`応答待ち最長` の実測値**で `PN5180_FAST_RX_TIMEOUT_MS`（8 ms = 応答予算）を詰める。
      見積りでは応答は tx 後 ≈ 4.1 ms で来るので 6 ms まで下げられる見込み。
- [ ] まだ 300 ms を超える場合の次の手（順に）: Stay Quiet と root を定常状態で省く
      （= 狙い撃ちだけで済ませ、full scan は数 poll に 1 回。新しい札の検出が遅れるトレードオフ）→
      `CARD_POLL_INTERVAL_MS` 100 → 50。
- [ ] `coll_pos` 機構は実機では常に fallback になるので、実質死んでいる。害は無い（ログのみ）が、
      整理するかは 11 台の実測が落ち着いてから判断する。

## 追記（同日）: 実機の効果測定と「簡略サイクル」の追加

実機（`047ee1e`, 10 台 ready, 満載 18 枚）: **538 → 433 ms**。狙い撃ちは意図どおり動作
（**命中 340/342 = 99.4%** / `coll_pos fallback` 255 → **0** / `probe 最大` 6 → **3**）。
**`応答待ち最長 7.4 ms`** が取れ、32 bit mask の見積り（6.66 ms）+ IRQ ポーリング粒度で説明できる値
＝ フレーム長連動の上限が正しかったことの裏取りになった。

ただし目標 ≤ 300 ms には未達。内訳（reader 1 台・2 枚）は
狙い撃ち 7.4×2 + **Stay Quiet 4.2×2 + root(空) 9.5** + RF ほか ≈ 43 ms で、
**Stay Quiet と root probe が 4 割**（10 台 18 枚で 171 ms/周）。

→ **簡略サイクル**を追加した。Stay Quiet は「root probe で新しい札だけを見るための下準備」なので、
root を送らないサイクルでは不要。狙い撃ちが使える reader では `PN5180_FAST_CONFIRM_EVERY` 回に
(N-1) 回を **狙い撃ちだけで終える**（Stay Quiet も root も送らない）。

- `PN5180_FAST_CONFIRM_EVERY` 5 → **3**（簡略が安く効くようになったので、発見遅れを短くする方に振った）。
- `CARD_POLL_INTERVAL_MS` 100 → **50**。
- 実装 B の旧「確認 probe 間引き」は `#if !PN5180_FAST_TARGETED_PROBE` に退避（判定の持ち主を 1 つにする。
  `fast_same_as_prev` も同じガード内へ）。
- 統計に **`簡略 N/M reader周`** を追加。
- 見積り: 簡略 ≈ 262 ms / 完全確認 ≈ 433 ms → 平均 ≈ **320 ms**（N=3）。N=5 なら ≈ 296 ms。
- **代償**: 既に札がある reader に増えた札の発見が最大 N poll 遅れる。**空の reader は簡略に入らない**
  ので、席の 1 枚目 / flop / turn / river は毎 poll 検出。遅れるのは「席の 2 枚目」だけ。
- simulator の期待値を更新: 定常 3 枚 = **簡略 2 回（3 probe）+ 完全確認 1 回（4 probe）**、
  hold 切れで落ちた札の復帰は「**N poll 以内**」に変更（= 設計どおりのトレードオフを assert）。
- 併せて **配線表を再振替**（ISSUE-0023）: BUSY 不通の #4(ch3) / #11(ch10) を予備に降格し、
  board2 = #12(ch11) / board3 = #13(ch12) に。`sim_initretry` の index↔nss 期待値も更新。

## Related ADRs

- ADR-0041（1 slot + P2 で 11 台）/ ADR-0040（slot 常時 present）

## Related Issues

- ISSUE-0021（poll 周期。本件は実機フィードバック 6）/ ISSUE-0023（11 台目の BUSY 配線）

## Related Commits

- `405f218` skip した reader の chip 生存確認（本タスクの土台）
- （本 worklog と同一の変更セット）狙い撃ち probe + 応答待ちのフレーム長連動 + 統計 2 項目
