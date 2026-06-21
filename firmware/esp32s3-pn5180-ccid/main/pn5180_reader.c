// pn5180_reader.c — jef-sure/esp32-component-pn5180 を使った UID 読み取りグルー。
//
// ⚠ 適合ポイント: jef-sure コンポーネントの実 API（ヘッダ名 / 構造体フィールド）に合わせて
//    調整すること。下記は README の例（pn5180_spi_init / pn5180_init / pn5180_1xxxx_init /
//    setup_rf / get_all_uids / nfc_uids_array_t）に基づく想定実装。
//    参照: https://github.com/jef-sure/esp32-component-pn5180 の examples。
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_log.h"

#include "app_config.h"
#include "pn5180_reader.h"

// jef-sure/pn5180 のヘッダ（ハイフン区切り）。
#include "pn5180.h"          // pn5180_spi_init / pn5180_init / pn5180_t / pn5180_proto_t
#include "pn5180-14443.h"    // pn5180_14443_init
#include "pn5180-15693.h"    // pn5180_15693_init

static const char *TAG = "pn5180";

typedef struct {
    pn5180_t *dev;
    pn5180_proto_t *iso14443;
    pn5180_proto_t *iso15693;
} slot_reader_t;

static slot_reader_t s_readers[CCID_SLOT_COUNT];
static pn5180_card_t s_cache[CCID_SLOT_COUNT];
static SemaphoreHandle_t s_lock;

bool pn5180_reader_init(void) {
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return false;
    memset(s_cache, 0, sizeof(s_cache));

    // SPI バスは全 slot 共有（NSS/BUSY/RST のみ slot ごと）。
    pn5180_spi_t *spi = pn5180_spi_init(PN5180_SPI_HOST, PN5180_PIN_SCK,
                                        PN5180_PIN_MISO, PN5180_PIN_MOSI,
                                        PN5180_SPI_HZ);
    if (!spi) {
        ESP_LOGE(TAG, "pn5180_spi_init failed");
        return false;
    }

    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        const pn5180_pins_t *p = &PN5180_SLOT_PINS[i];
        s_readers[i].dev = pn5180_init(spi, p->nss, p->busy, p->rst);
        if (!s_readers[i].dev) {
            ESP_LOGE(TAG, "pn5180_init slot %d failed (nss=%d busy=%d rst=%d)",
                     i, p->nss, p->busy, p->rst);
            return false;
        }
        // 両プロトコルを初期化（slot ごとに ISO14443A と ISO15693 の両方を試す）。
        s_readers[i].iso14443 = pn5180_14443_init(s_readers[i].dev);
        // TODO(実機): 第2引数の modulation_mode はコンポーネントの enum 値に合わせる。
        s_readers[i].iso15693 = pn5180_15693_init(s_readers[i].dev, 0 /* ASK100% 等 */);
        ESP_LOGI(TAG, "PN5180 slot %d ready", i);
    }
    return true;
}

// 1 つの proto から最初の UID を取り出す。取れたら true。
// ⚠ nfc_uids_array_t / nfc_uid_t のフィールド名はコンポーネントのヘッダに合わせて調整。
static bool read_uid_from_proto(pn5180_proto_t *proto, uint8_t *uid, uint8_t *uid_len) {
    if (!proto || !proto->setup_rf || !proto->get_all_uids) return false;
    proto->setup_rf(proto);
    nfc_uids_array_t *uids = proto->get_all_uids(proto);
    if (!uids) return false;

    bool found = false;
    // nfc_uids_array_t { nfc_uid_t *uids; int uids_count; }
    // nfc_uid_t { uint8_t uid[]; int uid_length; int subtype; }
    if (uids->uids_count > 0) {
        int n = uids->uids[0].uid_length;
        if (n > 16) n = 16;
        if (n > 0) {
            memcpy(uid, uids->uids[0].uid, n);
            *uid_len = (uint8_t)n;
            found = true;
        }
    }
    free(uids);  // README: heap 配列は free 必須
    return found;
}

void pn5180_reader_poll_once(void) {
    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        pn5180_card_t c = {0};
        uint8_t uid[16];
        uint8_t len = 0;

        // ISO15693（8B）→ だめなら ISO14443A（4/7B）の順で試す。
        if (read_uid_from_proto(s_readers[i].iso15693, uid, &len) ||
            read_uid_from_proto(s_readers[i].iso14443, uid, &len)) {
            c.present = true;
            c.uid_len = len;
            memcpy(c.uid, uid, len);
        }

        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_cache[i] = c;
        xSemaphoreGive(s_lock);
    }
}

bool pn5180_reader_get_card(uint8_t slot, pn5180_card_t *out) {
    if (slot >= CCID_SLOT_COUNT || !out) return false;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    *out = s_cache[slot];
    xSemaphoreGive(s_lock);
    return true;
}
