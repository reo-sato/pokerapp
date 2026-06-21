// ccid_device.c — CCID を TinyUSB のカスタムクラスドライバとして実装（USB plumbing）。
//
// ⚠ 版依存の注意: usbd_class_driver_t のフィールド構成は TinyUSB のバージョンで変わる
//    （`name`(CFG_TUSB_DEBUG>=2 のみ) / `deinit` の有無 / `sof` 署名）。ビルドエラーが出たら
//    使用中の tinyusb の device/usbd_pvt.h に合わせて struct 初期化子を調整する。
//    手法は RevK の記事と pico-openpgp を参照（vendor_device.c を CCID(0x0B)化）。
//
// メッセージ処理は ccid_slot.c (ccid_process_message) に委譲し、ここは bulk の出し入れに徹する。
#include <string.h>
#include "esp_log.h"
#include "ccid_device.h"
#include "usb_descriptors.h"
#include "ccid_slot.h"

static const char *TAG = "ccid";

typedef struct {
    uint8_t ep_out;
    uint8_t ep_in;
    uint8_t out_buf[CCID_EP_SIZE];   // host→device コマンド受信
    uint8_t in_buf[10 + 64];         // device→host レスポンス（ヘッダ + ATR/UID）
} ccid_state_t;

static ccid_state_t s_ccid;

// ── ドライバコールバック ──
static void ccid_init(void) {
    memset(&s_ccid, 0, sizeof(s_ccid));
    ESP_LOGI(TAG, "ccid_init (app driver registered)");
}

static bool ccid_deinit(void) {
    return true;
}

static void ccid_reset(uint8_t rhport) {
    (void)rhport;
    memset(&s_ccid, 0, sizeof(s_ccid));
}

static uint16_t ccid_open(uint8_t rhport, tusb_desc_interface_t const *itf,
                          uint16_t max_len) {
    // CCID(0x0B) のインターフェースだけ受け持つ。
    TU_VERIFY(itf->bInterfaceClass == TUSB_CLASS_SMART_CARD, 0);

    uint16_t const drv_len = sizeof(tusb_desc_interface_t) + 54 /* CCID func */ +
                             2 * sizeof(tusb_desc_endpoint_t);
    TU_VERIFY(max_len >= drv_len, 0);

    uint8_t const *p = (uint8_t const *)itf;
    p = tu_desc_next(p);  // interface を飛ばす
    // CCID functional descriptor(0x21) を飛ばす
    if (tu_desc_type(p) == CCID_DESC_TYPE_SMART_CARD) {
        p = tu_desc_next(p);
    }
    // bulk EP を 2 本開く
    for (int i = 0; i < 2; i++) {
        tusb_desc_endpoint_t const *ep = (tusb_desc_endpoint_t const *)p;
        TU_ASSERT(usbd_edpt_open(rhport, ep), 0);
        if (tu_edpt_dir(ep->bEndpointAddress) == TUSB_DIR_OUT) {
            s_ccid.ep_out = ep->bEndpointAddress;
        } else {
            s_ccid.ep_in = ep->bEndpointAddress;
        }
        p = tu_desc_next(p);
    }

    // 最初のコマンド受信を仕掛ける
    TU_ASSERT(usbd_edpt_xfer(rhport, s_ccid.ep_out, s_ccid.out_buf, CCID_EP_SIZE), 0);
    ESP_LOGI(TAG, "ccid_open ok: ep_out=0x%02x ep_in=0x%02x len=%u",
             s_ccid.ep_out, s_ccid.ep_in, (unsigned)drv_len);
    return drv_len;
}

// CCID class-specific control（ABORT 等）。最小対応: ABORT は ack、他は stall。
static bool ccid_control_xfer_cb(uint8_t rhport, uint8_t stage,
                                 tusb_control_request_t const *req) {
    if (req->bmRequestType_bit.type != TUSB_REQ_TYPE_CLASS) {
        return false;
    }
    if (stage != CONTROL_STAGE_SETUP) {
        return true;
    }
    ESP_LOGI(TAG, "ccid_control class req=0x%02x", req->bRequest);
    switch (req->bRequest) {
    case 0x01:  // ABORT
        return tud_control_status(rhport, req);
    default:
        return false;  // GET_CLOCK_FREQUENCIES(0x02)/GET_DATA_RATES(0x03) 等は stall（未使用）
    }
}

static bool ccid_xfer_cb(uint8_t rhport, uint8_t ep_addr, xfer_result_t result,
                         uint32_t xferred_bytes) {
    (void)result;
    if (ep_addr == s_ccid.ep_out) {
        // コマンド受信完了 → 処理してレスポンスを bulk-IN で返す。
        // 注: 64byte 超のチェイン受信は未対応（Get UID/Status は小さいので可）。TODO で拡張。
        ESP_LOGI(TAG, "ccid_xfer OUT %u bytes, msgtype=0x%02x",
                 (unsigned)xferred_bytes, xferred_bytes > 0 ? s_ccid.out_buf[0] : 0);
        size_t rlen = ccid_process_message(s_ccid.out_buf, xferred_bytes,
                                           s_ccid.in_buf, sizeof(s_ccid.in_buf));
        if (rlen > 0) {
            usbd_edpt_xfer(rhport, s_ccid.ep_in, s_ccid.in_buf, (uint16_t)rlen);
        } else {
            // 応答不要 → 次のコマンドを待つ
            usbd_edpt_xfer(rhport, s_ccid.ep_out, s_ccid.out_buf, CCID_EP_SIZE);
        }
    } else if (ep_addr == s_ccid.ep_in) {
        // レスポンス送信完了 → 次のコマンドを受信待ち。
        usbd_edpt_xfer(rhport, s_ccid.ep_out, s_ccid.out_buf, CCID_EP_SIZE);
    }
    return true;
}

// ── ドライバ登録 ──
static const usbd_class_driver_t s_ccid_driver = {
#if CFG_TUSB_DEBUG >= 2
    .name = "CCID",
#endif
    .init = ccid_init,
    .deinit = ccid_deinit,  // 古い tinyusb には無い → ビルドエラー時は削除
    .reset = ccid_reset,
    .open = ccid_open,
    .control_xfer_cb = ccid_control_xfer_cb,
    .xfer_cb = ccid_xfer_cb,
    .sof = NULL,
};

usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count) {
    *driver_count = 1;
    return &s_ccid_driver;
}

// main.c から呼ばれることで本 TU を強制リンクし、上の strong な usbd_app_driver_get_cb を
// 有効化する（ESP-IDF は main を whole-archive しないため。詳細は ccid_device.h のコメント）。
void ccid_force_link(void) {}
