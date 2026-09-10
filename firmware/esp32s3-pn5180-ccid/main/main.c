// main.c — USB CCID デバイス + PN5180 ポーリングを起動する。
//
// 構成: esp_tinyusb が native USB(§0) を CCID として公開し、ccid_device.c の app driver が
// bulk を捌く。別タスクで PN5180 を周期ポーリングして slot ごとのカード状態を更新する
// （USB と RF を分離。host の RFIDThread も同様に polling/debounce する）。
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "tinyusb.h"

#include "app_config.h"
#include "usb_descriptors.h"
#include "ccid_device.h"
#include "ccid_slot.h"
#include "pn5180_reader.h"

static const char *TAG = "main";

static void card_poll_task(void *arg) {
    (void)arg;
    for (;;) {
        pn5180_reader_poll_once();
#if CCID_USE_INTERRUPT_EP
        // カード挿抜を host へ通知（interrupt-IN, RDR_to_PC_NotifySlotChange）。送信できた時だけ
        // 「通知済み」を確定し、失敗なら次周で再送。既定は OFF（usb_descriptors.h 参照:
        // Windows が通知を無視して present を登録しなかった実績あり。host は polling で足りる）。
        uint8_t msg[CCID_EP_INT_SIZE];
        size_t n = ccid_slot_build_notify(msg, sizeof(msg));
        if (n > 0 && ccid_notify_slot_change(msg, n)) {
            ccid_slot_notify_committed();
        }
#endif
        vTaskDelay(pdMS_TO_TICKS(CARD_POLL_INTERVAL_MS));
    }
}

void app_main(void) {
    // CCID クラスドライバ(ccid_device.o)を強制リンクして usbd_app_driver_get_cb を有効化する
    // （これが無いと別ファイルの強定義がリンクされず TinyUSB の weak スタブが使われる = Code 10）。
    ccid_force_link();

    ESP_LOGI(TAG, "PN5180 USB CCID reader: %d slot(s), product='%s'",
             CCID_SLOT_COUNT, USB_PRODUCT_STR);

    // ── USB(CCID) 起動 ──
    // ⚠ 版依存: esp_tinyusb の tinyusb_config_t のフィールド名はバージョンで変わる
    //    （configuration_descriptor / fs_configuration_descriptor など）。ビルドエラー時は
    //    使用中の esp_tinyusb の tinyusb.h に合わせて調整する。
    int str_count = 0;
    const char **strings = ccid_string_descriptors(&str_count);
    const tinyusb_config_t tusb_cfg = {
        .device_descriptor = ccid_device_descriptor(),
        .string_descriptor = strings,
        .string_descriptor_count = str_count,
        .external_phy = false,
        .configuration_descriptor = ccid_configuration_descriptor(),
    };
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));
    ESP_LOGI(TAG, "TinyUSB(CCID) installed");

    // ── PN5180 起動 + ポーリング ──
    if (!pn5180_reader_init()) {
        ESP_LOGE(TAG, "PN5180 init failed — 配線/ピン(app_config.h)を確認");
        // USB は上げたままにして host が reader を列挙できる状態は保つ（カードは読めない）。
    } else {
        xTaskCreate(card_poll_task, "card_poll", 4096, NULL, 5, NULL);
        ESP_LOGI(TAG, "card poll task started (%d ms)", CARD_POLL_INTERVAL_MS);
    }
}
