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

## 追記 2（同日）: 11 台満載の初測定と「位相ずらし」

配線振替で **11/11 ready** が揃い、満載（8 席 × 2 枚 + board 3/1/1 = 22 枚）で初測定:

```
1 周 min/avg/max = 238/324/495 ms, 最長 reader #7 = 60 ms, probe 最大 4,
狙い撃ち 533/540 命中, 簡略 180/297 reader周, 応答待ち最長 7.4 ms, coll_pos fallback 0, ノイズ再試行 3
```

平均 324 ms は見積り（320 ms）どおりだが **max 495 ms** が残った。原因は
**簡略サイクルが全 reader で lockstep していた**こと（ISSUE-0021 Root Cause 7）。
`s_confirm_skips[slot]` は「完全確認をした周に 0 に畳む連続カウンタ」なので、
**ハンドの切れ目で全 reader が 0 枚になると一斉に 0 に畳まれ**、次のハンドで札が載ると
11 台が同じ周で完全確認に入る。1 周は「全部簡略」か「全部完全確認」のどちらかに振れていた。

実測 3 点が 1 台あたりの内訳を一意に決める: **簡略 21.6 ms / 完全確認 45.0 ms**（238÷11, 495÷11）。
検算 `(2×238 + 495)/3 = 323.7` = 実測 avg 324 ⇒ lockstep 仮説で説明がつく。

- **位相を自由走行カウンタで決める**ようにした: 完全確認は
  `(s_poll_cycle + reader index) % PN5180_FAST_CONFIRM_EVERY == 0` の reader だけ。
  `s_poll_cycle` は **poll 1 周の末尾で 1 回だけ** +1 する（周の途中で変わると割り当てがずれる）。
  初期値をずらすだけでは毎ハンド再同期するので、「0 枚を跨いでも崩れない位相源」が要点。
- **周期はどの reader も厳密に `CONFIRM_EVERY`**（カウンタを畳まないので frequency が変わらない）。
  1 周に完全確認するのは `ceil(11/N)` 台だけ。
- `PN5180_FAST_CONFIRM_EVERY` 3 → **6**: 予測 1 周は N=3 → 308〜331 / N=4 → 284〜308 /
  N=5 → 284〜308 / **N=6 → 261〜284 ms**（**max が初めて 300 ms 未満**になる最小の N。
  7 以上は `ceil(11/6)=2` から減らないので max は縮まない）。遅れの上限は N 周 ≈ 1.7 s。
- 旧「連続スキップ回数」は `#if !PN5180_FAST_TARGETED_PROBE`（実装 B）側だけが持つ形に整理。
- simulator に **位相ずらしの回帰テスト**を追加（実 RF 不要）: 11 台に 1 枚ずつ載せて N 周まわし、
  ①1 周の完全確認が `ceil(11/N)` 台以下 ②N 周で全 reader がちょうど 1 回ずつ完全確認 を assert
  （1 枚の reader は 簡略 1 probe / 完全確認 2 probe で見分ける）。実測 `周 1..6: 完全確認 2,2,2,2,1,2 台`。
- 「増えた 1 枚」の期待値を**位相に依存しない形**に修正（「次の poll で 4 probe」→「N poll 以内に
  見つかり、見つけた poll が 4 probe」）。減った poll / 定常 poll も probe 数を範囲で見る。

## 追記 3（同日）: 位相ずらしの実機結果 = 目標達成、SPI 5 MHz は見送り

`83ad762`（位相ずらし + N=6）を 11 台 ready・満載 22 枚で実行:

```
（置いた直後の過渡）min/avg/max = 276/311/720 ms, probe 最大 8, coll_pos fallback 16
（定常 1）          min/avg/max = 275/297/311 ms, probe 最大 4, 狙い撃ち 628/638, 簡略 266/319
（定常 2）          min/avg/max = 272/301/331 ms, probe 最大 4, 狙い撃ち 621/638, 簡略 266/319
```

| | 前（`081c9bb`） | 後（`83ad762`） |
|---|---|---|
| 1 周 min〜max | 238〜**495** ms | 272〜**331** ms |
| min/max の差 | **257 ms**（lockstep） | **36〜59 ms** |
| avg | 324 ms | **297〜301 ms** |

- `簡略 266/319`（319 = 29 周 × 11 台）→ full 53 件 ÷ 29 周 = **1.83 台/周 = 11/6 ちょうど**。
  位相の配分が設計どおり `ceil(11/6)=2` 台以下に収まった。
- **目標 ≤ 300 ms を平均で達成**（hard bound 0.5 s に 34% の余裕）→ **ISSUE-0021 Fixed**。
- 予測（261〜284 ms）との差 +11〜47 ms は **`ノイズ再試行 8〜14` と狙い撃ちの外れ 10〜17 件**
  （各 ≈ 10 ms の RX 上限待ち = 約 0.9 件/周）。過渡の 720 ms / probe 8 / fallback 16 は札を置いた
  瞬間だけで、定常では `fallback 0 / probe 最大 4`。
- **`PN5180_SPI_HZ` 1 MHz → 5 MHz は見送り**（`app_config.h` の「11 台が安定したら上げる」を撤回し、
  据え置きを明記）。効果は 1 周 ≈ 10〜20 ms（3〜7%）に対し、この配線は **13 コネクタ中 2 本で BUSY
  導通不良**（ISSUE-0023）= 信号品質に実績のある弱さがあり、間欠的な SPI 化けは「本番中にランダムに
  読めない」最悪の形で出る。さらに詰める必要が出たときの順序は
  `PN5180_FAST_RX_TIMEOUT_MS` 8→6（応答に使っているのは 4.7 ms なので余裕 28%）→ `CONFIRM_EVERY` を上げる。
- **次は poll 最適化ではなく Phase H の通し**: `tools/register_cards.py` で実カード UID を
  `rfid_cards.json` に登録 → 音声→JSON/PHH + ストリート遷移（flop 3 / turn / river）の通し。

## Related ADRs

- ADR-0041（1 slot + P2 で 11 台）/ ADR-0040（slot 常時 present）

## Related Issues

- ISSUE-0021（poll 周期。本件は実機フィードバック 6）/ ISSUE-0023（11 台目の BUSY 配線）

## Related Commits

- `405f218` skip した reader の chip 生存確認（本タスクの土台）
- （本 worklog と同一の変更セット）狙い撃ち probe + 応答待ちのフレーム長連動 + 統計 2 項目
