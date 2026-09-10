// ccid_slot.c — CCID メッセージ処理の中核（契約 §5 ATR / §6 Get UID / §7 UID）。
//
// 対応する PC_to_RDR（host→reader）:
//   0x62 IccPowerOn   → 0x80 DataBlock(ATR)              … connect 成立(§5)
//   0x63 IccPowerOff  → 0x81 SlotStatus
//   0x65 GetSlotStatus→ 0x81 SlotStatus                  … カード有無
//   0x6F XfrBlock     → 0x80 DataBlock(APDU 応答)         … Get UID(§6)
//   0x61/0x6C/0x6D Parameters 系 → 0x82 Parameters(最小)
//   その他           → 0x81 SlotStatus(command failed)
// reader→host の非同期通知:
//   0x50 NotifySlotChange（interrupt-IN, カード挿抜）… ccid_slot_build_notify で組み立て
//
// host(rfid/bridge.py)が依存するのは「XfrBlock に FF CA 00 00 00 → UID + 90 00」だけ（§6）。
#include <string.h>
#include "esp_log.h"
#include "ccid_slot.h"
#include "app_config.h"
#include "pn5180_reader.h"

// bring-up 診断: host(pyscard)が実際に送ってくる CCID コマンドと、こちらの応答を UART に出す。
// CONFIG_ESP_CONSOLE_UART_DEFAULT=y なので CP2102N 側に出る（native USB は CCID 専有）。
static const char *TAG = "ccid-msg";

// ── CCID message types ──
#define PC_TO_RDR_ICC_POWER_ON   0x62
#define PC_TO_RDR_ICC_POWER_OFF  0x63
#define PC_TO_RDR_GET_SLOT_STAT  0x65
#define PC_TO_RDR_XFR_BLOCK      0x6F
#define PC_TO_RDR_GET_PARAMS     0x6C
#define PC_TO_RDR_RESET_PARAMS   0x6D
#define PC_TO_RDR_SET_PARAMS     0x61

#define RDR_TO_PC_DATA_BLOCK     0x80
#define RDR_TO_PC_SLOT_STATUS    0x81
#define RDR_TO_PC_PARAMETERS     0x82
#define RDR_TO_PC_NOTIFY_SLOT_CHANGE 0x50  // interrupt-IN

// bStatus: bmICCStatus(bit1..0) | bmCommandStatus(bit7..6)
#define ICC_PRESENT_ACTIVE   0x00
#define ICC_PRESENT_INACTIVE 0x01
#define ICC_ABSENT           0x02
#define CMD_OK               0x00
#define CMD_FAILED           0x40

// PC/SC v2.01 Part3 storage-card proxy ATR（host は中身を解釈しない, §5）。
// 末尾 TCK 含め整合した固定値。種別が変わっても connect さえ通ればよい。
static const uint8_t ATR[] = {
    0x3B, 0x8F, 0x80, 0x01, 0x80, 0x4F, 0x0C, 0xA0, 0x00, 0x00,
    0x03, 0x06, 0x03, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x6A,
};

// ── slot 状態 ──
// CCID の bmICCStatus は 0=present&active / 1=present&inactive / 2=absent の 3 値。
// 「active」= host が IccPowerOn した状態。通電前から active と偽ったり、IccPowerOff の
// 応答で absent を返したりすると host(usbccid) の状態機械が狂うので、powered を別に持つ。
static bool s_powered[CCID_SLOT_COUNT];            // IccPowerOn 済み（PowerOff / 離脱でクリア）
static bool s_notified_present[CCID_SLOT_COUNT];   // 最後に host へ通知（commit）した present
static bool s_snapshot_present[CCID_SLOT_COUNT];   // build_notify 時点の present（commit で反映）

static bool slot_present(uint8_t slot) {
    pn5180_card_t c;
    return slot < CCID_SLOT_COUNT && pn5180_reader_get_card(slot, &c) && c.present;
}

// slot の現在状態（pn5180_reader が更新したキャッシュ + powered）。
static uint8_t icc_status(uint8_t slot) {
    if (slot >= CCID_SLOT_COUNT) return ICC_ABSENT;
    if (!slot_present(slot)) {
        s_powered[slot] = false;
        return ICC_ABSENT;
    }
    return s_powered[slot] ? ICC_PRESENT_ACTIVE : ICC_PRESENT_INACTIVE;
}

static const char *st_name(uint8_t st) {
    switch (st) {
    case ICC_PRESENT_ACTIVE:   return "PRESENT(active)";
    case ICC_PRESENT_INACTIVE: return "PRESENT(inactive)";
    default:                   return "ABSENT";
    }
}

// ── ヘルパ: bulk-IN ヘッダを書く ──
static size_t put_header(uint8_t *out, uint8_t type, uint32_t data_len,
                         uint8_t slot, uint8_t seq, uint8_t status,
                         uint8_t error, uint8_t param) {
    out[0] = type;
    out[1] = (uint8_t)(data_len & 0xFF);
    out[2] = (uint8_t)((data_len >> 8) & 0xFF);
    out[3] = (uint8_t)((data_len >> 16) & 0xFF);
    out[4] = (uint8_t)((data_len >> 24) & 0xFF);
    out[5] = slot;
    out[6] = seq;
    out[7] = status;
    out[8] = error;
    out[9] = param;
    return 10;
}

// XfrBlock 内の APDU を処理して応答(データ+SW)を resp に書き、長さを返す。
// Get UID(FF CA 00 00 00)のみ実装。他は未対応 SW を返す。
static size_t handle_apdu(uint8_t slot, const uint8_t *apdu, size_t apdu_len,
                          uint8_t *resp, size_t resp_max) {
    // Get UID: CLA=FF INS=CA P1=00 P2=00 Le=00（末尾 Le は省略され 4 byte のこともある）
    bool is_get_uid = apdu_len >= 4 && apdu[0] == 0xFF && apdu[1] == 0xCA &&
                      apdu[2] == 0x00 && apdu[3] == 0x00;
    if (is_get_uid) {
        pn5180_card_t c;
        if (pn5180_reader_get_card(slot, &c) && c.present && c.uid_len > 0 &&
            (size_t)c.uid_len + 2 <= resp_max) {
            memcpy(resp, c.uid, c.uid_len);     // UID（生バイト MSB-first, 4/7/8B, §7）
            resp[c.uid_len] = 0x90;             // SW1
            resp[c.uid_len + 1] = 0x00;         // SW2 = 90 00（成功）
            return (size_t)c.uid_len + 2;
        }
        // カード無し/読取り失敗 → 6A 81（host は 非90 00 を None 扱い, §6）
        resp[0] = 0x6A;
        resp[1] = 0x81;
        return 2;
    }
    // 未対応 INS
    resp[0] = 0x6D;
    resp[1] = 0x00;
    return 2;
}

size_t ccid_process_message(const uint8_t *in, size_t in_len,
                            uint8_t *out, size_t out_max) {
    if (in_len < 10 || out_max < 10) {
        return 0;
    }
    const uint8_t type = in[0];
    const uint32_t dlen = (uint32_t)in[1] | ((uint32_t)in[2] << 8) |
                          ((uint32_t)in[3] << 16) | ((uint32_t)in[4] << 24);
    const uint8_t slot = in[5];
    const uint8_t seq = in[6];
    const uint8_t *data = in + 10;
    const size_t data_len = (in_len - 10 < dlen) ? (in_len - 10) : dlen;

    const uint8_t st = icc_status(slot);  // slot 範囲外は ABSENT

    switch (type) {
    case PC_TO_RDR_ICC_POWER_ON: {
        if (st == ICC_ABSENT) {
            // カード無し → 失敗 + ICC_MUTE(0xFE)。host は毎 poll でここを叩くため DEBUG（既定非表示）。
            ESP_LOGD(TAG, "IccPowerOn slot=%u: カード無し(ABSENT) → MUTE", (unsigned)slot);
            return put_header(out, RDR_TO_PC_DATA_BLOCK, 0, slot, seq,
                              (uint8_t)(CMD_FAILED | ICC_ABSENT), 0xFE, 0x00);
        }
        s_powered[slot] = true;
        ESP_LOGI(TAG, "IccPowerOn slot=%u: present → ATR(%uB) 返却（connect 成立）",
                 (unsigned)slot, (unsigned)sizeof(ATR));
        size_t n = put_header(out, RDR_TO_PC_DATA_BLOCK, sizeof(ATR), slot, seq,
                              CMD_OK | ICC_PRESENT_ACTIVE, 0x00, 0x00);
        if (n + sizeof(ATR) <= out_max) {
            memcpy(out + n, ATR, sizeof(ATR));
            return n + sizeof(ATR);
        }
        return n;
    }
    case PC_TO_RDR_ICC_POWER_OFF: {
        // 通電解除。カードが乗ったままなら present&inactive（absent を返すと「抜かれた」扱いになる）。
        if (slot < CCID_SLOT_COUNT) s_powered[slot] = false;
        ESP_LOGD(TAG, "IccPowerOff slot=%u", (unsigned)slot);
        return put_header(out, RDR_TO_PC_SLOT_STATUS, 0, slot, seq,
                          (uint8_t)(CMD_OK | (st == ICC_ABSENT ? ICC_ABSENT : ICC_PRESENT_INACTIVE)),
                          0x00, 0x00);
    }
    case PC_TO_RDR_GET_SLOT_STAT: {
        // host の polling を観測: 最初の数回と、報告する状態が変わった時だけ INFO に出す。
        static uint8_t s_last[CCID_SLOT_COUNT];
        static bool s_seen[CCID_SLOT_COUNT];
        static int s_logged = 0;
        if (slot < CCID_SLOT_COUNT && (!s_seen[slot] || s_last[slot] != st || s_logged < 3)) {
            ESP_LOGI(TAG, "GetSlotStatus slot=%u → %s", (unsigned)slot, st_name(st));
            s_seen[slot] = true;
            s_last[slot] = st;
            s_logged++;
        }
        return put_header(out, RDR_TO_PC_SLOT_STATUS, 0, slot, seq,
                          CMD_OK | st, 0x00, 0x00);
    }
    case PC_TO_RDR_XFR_BLOCK: {
        uint8_t resp[64];
        size_t rn = handle_apdu(slot, data, data_len, resp, sizeof(resp));
        // APDU 先頭（Get UID なら FF CA 00 00 00）と、返した SW を出す。
        // 期待: apdu=FF CA 00 00 00 → resp_len=uid+2, sw=90 00。sw=6A 81 ならカード無し判定。
        ESP_LOGI(TAG,
                 "XfrBlock slot=%u apdu_len=%u apdu=%02X %02X %02X %02X %02X → resp_len=%u sw=%02X %02X",
                 (unsigned)slot, (unsigned)data_len,
                 data_len > 0 ? data[0] : 0, data_len > 1 ? data[1] : 0,
                 data_len > 2 ? data[2] : 0, data_len > 3 ? data[3] : 0,
                 data_len > 4 ? data[4] : 0, (unsigned)rn,
                 rn >= 2 ? resp[rn - 2] : 0, rn >= 1 ? resp[rn - 1] : 0);
        size_t n = put_header(out, RDR_TO_PC_DATA_BLOCK, (uint32_t)rn, slot, seq,
                              CMD_OK | (st == ICC_ABSENT ? ICC_ABSENT : ICC_PRESENT_ACTIVE),
                              0x00, 0x00);
        if (n + rn <= out_max) {
            memcpy(out + n, resp, rn);
            return n + rn;
        }
        return n;
    }

    case PC_TO_RDR_GET_PARAMS:
    case PC_TO_RDR_SET_PARAMS:
    case PC_TO_RDR_RESET_PARAMS: {
        // 最小の T=1 Parameters を返す（5 byte abProtocolDataStructure）。
        static const uint8_t params[5] = {0x11, 0x10, 0x00, 0x15, 0x00};
        size_t n = put_header(out, RDR_TO_PC_PARAMETERS, sizeof(params), slot, seq,
                              CMD_OK | st, 0x00, 0x01 /* bProtocolNum=T=1 */);
        if (n + sizeof(params) <= out_max) {
            memcpy(out + n, params, sizeof(params));
            return n + sizeof(params);
        }
        return n;
    }

    default:
        // 未対応コマンド → command failed
        return put_header(out, RDR_TO_PC_SLOT_STATUS, 0, slot, seq,
                          (uint8_t)(CMD_FAILED | st), 0x00, 0x00);
    }
}

// ── カード挿抜通知（RDR_to_PC_NotifySlotChange）──
// bmSlotICCState: slot ごとに 2bit。bit0 = 現在 present、bit1 = 前回通知から変化あり。
// slot s のビット位置 = byte (s/4), bit ((s%4)*2)。
size_t ccid_slot_build_notify(uint8_t *out, size_t out_max) {
    const size_t nbytes = (2 * CCID_SLOT_COUNT + 7) / 8;
    if (!out || out_max < 1 + nbytes) return 0;
    memset(out, 0, 1 + nbytes);
    out[0] = RDR_TO_PC_NOTIFY_SLOT_CHANGE;
    bool changed = false;
    for (uint8_t s = 0; s < CCID_SLOT_COUNT; s++) {
        const bool present = slot_present(s);
        if (!present) s_powered[s] = false;  // 離脱したら通電状態も解除
        s_snapshot_present[s] = present;
        uint8_t bits = present ? 0x01 : 0x00;
        if (present != s_notified_present[s]) {
            bits |= 0x02;
            changed = true;
        }
        out[1 + s / 4] |= (uint8_t)(bits << ((s % 4) * 2));
    }
    return changed ? (1 + nbytes) : 0;
}

void ccid_slot_notify_committed(void) {
    memcpy(s_notified_present, s_snapshot_present, sizeof(s_notified_present));
}

void ccid_slot_reset_notify(void) {
    memset(s_notified_present, 0, sizeof(s_notified_present));
    memset(s_snapshot_present, 0, sizeof(s_snapshot_present));
    memset(s_powered, 0, sizeof(s_powered));
}
