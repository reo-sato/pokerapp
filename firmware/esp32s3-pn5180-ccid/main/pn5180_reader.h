// pn5180_reader.h — PN5180 ×N の UID 読み取り + slot ごとのカード状態キャッシュ。
//
// host(RFIDThread)も polling/debounce するので、ここは「今この slot にどの UID があるか」を
// RF から更新し続けるだけ。CCID 層(ccid_slot.c)はこのキャッシュを読む（USB と RF の分離）。
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool present;        // カードが場にあるか
    uint8_t uid[16];     // 生 UID（4/7/8B, 契約 §7）
    uint8_t uid_len;     // UID バイト長
} pn5180_card_t;

// 全 slot の PN5180 を初期化（SPI バス + 各 reader）。成功で true。
bool pn5180_reader_init(void);

// slot のカード状態キャッシュをコピーする（mutex 保護）。slot 範囲外は false。
bool pn5180_reader_get_card(uint8_t slot, pn5180_card_t *out);

// 全 slot を 1 周ポーリングしてキャッシュを更新する（main の poll task から周期実行）。
void pn5180_reader_poll_once(void);
