// pn5180_reader.h — PN5180 ×N の UID 読み取り + slot ごとのカード状態キャッシュ。
//
// host(RFIDThread)も polling/debounce するので、ここは「今この slot にどの UID があるか」を
// RF から更新し続けるだけ。CCID 層(ccid_slot.c)はこのキャッシュを読む（USB と RF の分離）。
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_config.h"  // PN5180_MAX_CARDS_PER_READER

// 1 slot のカード状態。**1 reader に複数枚が重なって置かれる**（席 = hole card 2 枚、
// board1 = flop 3 枚）ので UID は配列で持つ。順序は memcmp 昇順に正規化されており、
// 同じ組み合わせなら毎 poll 同じ並びになる（host の差分判定を安定させるため）。
typedef struct {
    bool present;        // 1 枚以上あるか（count > 0）
    uint8_t count;       // 検出枚数（0..PN5180_MAX_CARDS_PER_READER）
    uint8_t uid_len;     // UID バイト長（全 UID 共通。ISO15693 = 8, 契約 §7）
    uint8_t uids[PN5180_MAX_CARDS_PER_READER][16];  // 生 UID（MSB-first, 4/7/8B）
} pn5180_card_t;

// 全 slot の PN5180 を初期化（SPI バス + 各 reader）。成功で true。
bool pn5180_reader_init(void);

// slot のカード状態キャッシュをコピーする（mutex 保護）。slot 範囲外は false。
bool pn5180_reader_get_card(uint8_t slot, pn5180_card_t *out);

// 全 slot を 1 周ポーリングしてキャッシュを更新する（main の poll task から周期実行）。
void pn5180_reader_poll_once(void);
