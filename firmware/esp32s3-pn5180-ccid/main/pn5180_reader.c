// pn5180_reader.c — PN5180 ×N の UID 読み取り（BUSY は CD74HC4067 MUX 経由）。
//
// SCK/MOSI/MISO/RST は共有、NSS は reader 個別。BUSY は MUX SIG(=PN5180_PIN_BUSY_SIG)に集約され、
// reader を処理する直前に MUX channel を切り替えて「選択中 reader の BUSY」を SIG に出す。
// jef-sure ドライバには busy = SIG GPIO を渡し、MUX 選択は本ファイルが面倒を見る（ドライバは MUX 非依存）。
//
// ⚠ 適合ポイント: jef-sure/pn5180 の実 API（ヘッダ名 / nfc_uids_array_t / nfc_uid_t）。
//    参照: https://github.com/jef-sure/esp32-component-pn5180 の examples。
#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_rom_sys.h"
#include "esp_log.h"

#include "app_config.h"
#include "pn5180_reader.h"
#include "pn5180.h"          // pn5180_spi_init / pn5180_init / pn5180_t / pn5180_proto_t
#include "pn5180-14443.h"    // pn5180_14443_init
#include "pn5180-15693.h"    // pn5180_15693_init

static const char *TAG = "pn5180";

// BUSY を MUX 経由で読むか直結で読むか（app_config.h の切り分けフラグ）。
#if PN5180_BUSY_VIA_MUX
#  define BUSY_PIN PN5180_PIN_BUSY_SIG
#else
#  define BUSY_PIN PN5180_PIN_BUSY_DIRECT
#endif

typedef struct {
    pn5180_t *dev;
    pn5180_proto_t *iso14443;
    pn5180_proto_t *iso15693;
    int mux_ch;
} slot_reader_t;

static slot_reader_t s_readers[CCID_SLOT_COUNT];
static pn5180_card_t s_cache[CCID_SLOT_COUNT];
static SemaphoreHandle_t s_lock;

// ── CD74HC4067 MUX（PN5180_BUSY_VIA_MUX=0 のときは no-op）──
static void mux_init(void) {
#if PN5180_BUSY_VIA_MUX
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << MUX_PIN_S0) | (1ULL << MUX_PIN_S1) |
                        (1ULL << MUX_PIN_S2) | (1ULL << MUX_PIN_S3),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
#endif
}

// channel を選択（選択中 reader の BUSY が SIG=PN5180_PIN_BUSY_SIG に出る）。
static void mux_select(int ch) {
#if PN5180_BUSY_VIA_MUX
    gpio_set_level(MUX_PIN_S0, (ch >> 0) & 1);
    gpio_set_level(MUX_PIN_S1, (ch >> 1) & 1);
    gpio_set_level(MUX_PIN_S2, (ch >> 2) & 1);
    gpio_set_level(MUX_PIN_S3, (ch >> 3) & 1);
    // 5μs では gpio_set_level 4連発の APB cycle 反映 + MUX settle + 入力サンプリングが
    // 不足し古い ch の BUSY を誤読する恐れ。50μs に拡大（poll 100ms なので無視できる）。
    esp_rom_delay_us(50);
#else
    (void)ch;
#endif
}

// ── BUSY ピン診断（テスター不要）──
// 内部プルアップ/プルダウンを切替えて読み、ピンが「フローティング(信号来てない)」か
// 「駆動されている(信号来てる)」かを判定する。PN5180 は電源投入後 idle で BUSY=Low のはず。
static void diag_busy_pin(void) {
    mux_select(PN5180_READERS[0].mux_ch);  // reader #1 の BUSY を SIG に（via_mux 時）
    const int busy = BUSY_PIN;

    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << busy,
        .mode = GPIO_MODE_INPUT,
        .intr_type = GPIO_INTR_DISABLE,
    };
    cfg.pull_up_en = GPIO_PULLUP_ENABLE;
    cfg.pull_down_en = GPIO_PULLDOWN_DISABLE;
    gpio_config(&cfg);
    esp_rom_delay_us(300);
    int pu = gpio_get_level(busy);

    cfg.pull_up_en = GPIO_PULLUP_DISABLE;
    cfg.pull_down_en = GPIO_PULLDOWN_ENABLE;
    gpio_config(&cfg);
    esp_rom_delay_us(300);
    int pd = gpio_get_level(busy);

    const char *verdict;
    if (pu == 1 && pd == 0)
        verdict = "FLOATING=信号が来ていない（MUX EN/VCC/SIG配線/ch対応 を疑う）";
    else if (pu == 0 && pd == 0)
        verdict = "LOW駆動=Lowに固定（PN5180 BUSY idle かも→MUXは届いている公算）";
    else if (pu == 1 && pd == 1)
        verdict = "HIGH駆動=Highに固定（常時busy/結線ミス/短絡 を疑う）";
    else
        verdict = "不定";
    ESP_LOGW(TAG, "BUSY診断: pin=%d via_mux=%d pull-up読み=%d pull-down読み=%d => %s",
             busy, PN5180_BUSY_VIA_MUX, pu, pd, verdict);
}

// ── MUX 全 channel 走査（テスター不要）──
// 16 ch を順に選択し SIG(pull-up) を読む。'0'=Low駆動(信号有=その ch に通電中の reader),
// '1'=floating/High。全 '1' なら MUX 不通（EN/VCC/SIG）or 全 reader 未通電。一部 '0' なら
// MUX は生きており、'1' の ch だけ reader 未接続/未通電。
static void diag_mux_scan(void) {
#if PN5180_BUSY_VIA_MUX
    const int busy = PN5180_PIN_BUSY_SIG;
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << busy, .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    char buf[20];
    for (int ch = 0; ch < 16; ch++) {
        mux_select(ch);
        esp_rom_delay_us(200);
        buf[ch] = gpio_get_level(busy) ? '1' : '0';
    }
    buf[16] = '\0';
    ESP_LOGW(TAG, "MUX scan ch0..15 (pull-up): %s  [0=Low駆動(信号有) 1=floating]", buf);
#endif
}

bool pn5180_reader_init(void) {
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return false;
    memset(s_cache, 0, sizeof(s_cache));
    mux_init();
    diag_busy_pin();  // テスター無しで BUSY ピンの素性を診断（ログに出す）
    diag_mux_scan();  // 全 ch 走査で MUX 不通 か reader 個別 かを切り分け
    // MUX(CD74HC4067)のSIG出力はアナログスイッチ経由で駆動が弱め。FLOATING だと ESP32-S3 の
    // 入力バッファが弱駆動を読み損ねて常時 High/Low に張り付くことがある。pull-up を当てると
    // High が安定し、PN5180 が BUSY を Low に引いた瞬間にだけ Low に落ちる（push-pull と
    // 内部 pull-up の併用は干渉しない）。
    gpio_set_direction(BUSY_PIN, GPIO_MODE_INPUT);  // 方向を明示（IDF 実装依存を避ける）
    gpio_set_pull_mode(BUSY_PIN, GPIO_PULLUP_ONLY);

    // ── RST/NSS 駆動 → BUSY 応答の手動診断 ──
    // PN5180 は RST=Low(>10us)→High→数 ms 後 BUSY=Low(idle)。SPI 無しでこれが起こるかを観る。
    // RST が物理的に届いているなら、Low→High の前後で BUSY が High↘Low と変化するはず。
    const int rst = PN5180_PIN_RST;
    const int nss = PN5180_READERS[0].nss;
    gpio_set_direction(rst, GPIO_MODE_OUTPUT);
    gpio_set_direction(nss, GPIO_MODE_OUTPUT);
    gpio_set_level(nss, 1);  // deselect
    mux_select(PN5180_READERS[0].mux_ch);

    // 内部 pull-up を当てた状態で読むのが要点:
    //  - PN5180 が生きていて出力駆動していれば、その値（High/Low）が読める
    //  - PN5180 が無反応(Hi-Z)なら pull-up に引かれて常に High に見える
    // → 「pull-up なのに Low に見える」= ちゃんと PN5180 が Low に引いている = チップ生存
    // 　「pull-up でも 0/1 が変わらない」= 配線/MUX/電源 で PN5180 まで届いていない
    gpio_set_pull_mode(BUSY_PIN, GPIO_PULLUP_ONLY);

    gpio_set_level(rst, 0);                      // RST 押す（PN5180 リセット中 BUSY=High 期待）
    esp_rom_delay_us(2000);
    int busy_during_rst = gpio_get_level(BUSY_PIN);
    gpio_set_level(rst, 1);                      // RST 離す
    esp_rom_delay_us(50);
    int busy_just_after = gpio_get_level(BUSY_PIN);
    vTaskDelay(pdMS_TO_TICKS(10));               // PN5180 ブート待ち
    int busy_after_boot = gpio_get_level(BUSY_PIN);  // ブート完了で BUSY=Low 期待

    ESP_LOGW(TAG, "RST診断(pull-up有): BUSY during_rst=%d  just_after=%d  after_10ms=%d",
             busy_during_rst, busy_just_after, busy_after_boot);
    ESP_LOGW(TAG, "  正常な PN5180: during_rst=1(High) → after_boot=0(Low,チップが Low に引く)");
    ESP_LOGW(TAG, "  全部 0 = pull-up が負けるほど強く Low → MUX が常時 Low 駆動 / GND 短絡 を疑う");
    ESP_LOGW(TAG, "  全部 1 = チップ無反応(Hi-Z) → PN5180 が電源/RST/物理接続不良");

    gpio_set_pull_mode(BUSY_PIN, GPIO_PULLUP_ONLY);  // MUX 弱駆動でも level 確定（driver にも有効）

    // SPI バスは全 reader 共有（pn5180_spi_init の引数順は host, SCK, MISO, MOSI, freq）。
    pn5180_spi_t *spi = pn5180_spi_init(PN5180_SPI_HOST, PN5180_PIN_SCK,
                                        PN5180_PIN_MISO, PN5180_PIN_MOSI,
                                        PN5180_SPI_HZ);
    if (!spi) {
        ESP_LOGE(TAG, "pn5180_spi_init failed");
        return false;
    }

    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        const pn5180_reader_cfg_t *cfg = &PN5180_READERS[i];
        s_readers[i].mux_ch = cfg->mux_ch;
        mux_select(cfg->mux_ch);  // この reader の BUSY を SIG に出してから init

        // busy = BUSY_PIN（MUX SIG or 直結, 共有）, rst = 共有, nss = reader 個別。
        s_readers[i].dev = pn5180_init(spi, cfg->nss, BUSY_PIN, PN5180_PIN_RST);
        if (!s_readers[i].dev && i == 0) {
            // ── 低レベル NSS スキャン（pn5180_init を再呼出しせず、add/remove で安全に）──
            // 通常 init 失敗時、ch=cfg->mux_ch に生きているチップに対し、どの NSS GPIO が
            // SPI 応答するかを spi_bus_add_device + 1byte 送信 + BUSY 監視 + remove で総当り。
            // pn5180_init は内部で spi_bus_add_device するため複数回呼ぶと leak/crash する。
            // ── SPI 配線の生存確認（NSS 関係なし、SCK/MOSI/MISO 経路の通電チェック）──
            // PN5180 が SPI を全く受け付けない場合、まず SPI バス自体が壊れていないか確かめる。
            // MISO を内部 pull-up し、SPI 送信中に MISO レベルが揺らぐかで線が生きているかを推定。
            ESP_LOGW(TAG, "SPI 配線生存確認 (SCK=%d MOSI=%d MISO=%d):",
                     PN5180_PIN_SCK, PN5180_PIN_MOSI, PN5180_PIN_MISO);
            gpio_set_pull_mode(PN5180_PIN_MISO, GPIO_PULLUP_ONLY);
            int miso_before = gpio_get_level(PN5180_PIN_MISO);
            ESP_LOGW(TAG, "  MISO (pull-up時, SPI 通信前): %d (1=line idle/floating, 0=chip が Low に引いている)",
                     miso_before);

            // ── BUSY 直接サンプリング（生存確認）──
            // ch12 を選択して BUSY の生波形を 200μs サンプリングし、何 % の時間で High だったかを見る。
            // PN5180 が完全に死んでいるなら 0% (常時 Low)、生きていて活動中なら数十% 変動。
            mux_select(cfg->mux_ch);
            gpio_set_pull_mode(BUSY_PIN, GPIO_PULLUP_ONLY);
            int high_count = 0;
            for (int j = 0; j < 200; j++) {
                if (gpio_get_level(BUSY_PIN)) high_count++;
                esp_rom_delay_us(1);
            }
            ESP_LOGW(TAG, "  BUSY サンプル (200μs/pull-up): High=%d%%  (0%%=常時Low/100%%=常時High が問題)",
                     high_count / 2);

            ESP_LOGW(TAG, "NSS スキャン: ch%d のチップが応答する NSS を低レベル SPI で探索", cfg->mux_ch);
            static const int nss_candidates[] = {1, 2, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 18};
            const int n_cands = sizeof(nss_candidates) / sizeof(nss_candidates[0]);
            mux_select(cfg->mux_ch);
            int found_nss = -1;
            for (int k = 0; k < n_cands; k++) {
                int try_nss = nss_candidates[k];
                // 以前は cfg->nss をスキップしていたが、SPI 側の問題で通常 init が失敗した可能性が
                // あるため、全候補を毎回試す（GPIO18 含む）。
                // 共有 RST を一度叩いて PN5180 をリセット状態にする
                gpio_set_direction(PN5180_PIN_RST, GPIO_MODE_OUTPUT);
                gpio_set_level(PN5180_PIN_RST, 0);
                esp_rom_delay_us(1000);
                gpio_set_level(PN5180_PIN_RST, 1);
                vTaskDelay(pdMS_TO_TICKS(5));  // ブート待ち

                spi_device_interface_config_t devcfg = {
                    .clock_speed_hz = 1000000,
                    .mode = 0,
                    .spics_io_num = try_nss,
                    .queue_size = 1,
                };
                spi_device_handle_t dev = NULL;
                if (spi_bus_add_device(PN5180_SPI_HOST, &devcfg, &dev) != ESP_OK) {
                    ESP_LOGW(TAG, "  [%d/%d] add_device(NSS=GPIO%d) 失敗", k + 1, n_cands, try_nss);
                    continue;
                }
                // PN5180 の READ_REGISTER コマンド (0x04) + 1 byte reg addr + 4 byte dummy read。
                // 正しい NSS のチップに当たれば SPI を受理して BUSY を High に立て、後続に応答する。
                uint8_t tx[6] = {0x04, 0x00, 0x00, 0x00, 0x00, 0x00};
                uint8_t rx[6] = {0};
                spi_transaction_t t = {
                    .length = 48, .tx_buffer = tx, .rx_buffer = rx,
                };
                spi_device_polling_transmit(dev, &t);
                // BUSY 監視: 数百 μs 以内に High に立つか
                bool went_high = false;
                for (int j = 0; j < 500; j++) {
                    if (gpio_get_level(BUSY_PIN)) { went_high = true; break; }
                    esp_rom_delay_us(2);
                }
                spi_bus_remove_device(dev);
                // MISO に非ゼロが返ってきていれば、SPI 経路は生きていてチップが何か出している。
                bool miso_active = (rx[2] != 0 || rx[3] != 0 || rx[4] != 0 || rx[5] != 0);
                ESP_LOGW(TAG, "  [%d/%d] NSS=GPIO%d -> BUSY:%s  MISO:%02X %02X %02X %02X %s",
                         k + 1, n_cands, try_nss,
                         went_high ? "YES" : "no ",
                         rx[2], rx[3], rx[4], rx[5],
                         miso_active ? "(non-zero!)" : "");
                if (went_high) { found_nss = try_nss; break; }
            }
            if (found_nss > 0) {
                ESP_LOGW(TAG, "✅ ch%d のチップは NSS=GPIO%d で応答。本番 init に渡す",
                         cfg->mux_ch, found_nss);
                ESP_LOGW(TAG, "   → app_config.h で PN5180_READERS[0]={.nss=%d, .mux_ch=%d} に修正してください",
                         found_nss, cfg->mux_ch);
                s_readers[i].dev = pn5180_init(spi, found_nss, BUSY_PIN, PN5180_PIN_RST);
            } else {
                ESP_LOGE(TAG, "❌ NSS スキャン: 全 %d 候補で BUSY 反応なし", n_cands);
                ESP_LOGE(TAG, "   → SPI 配線(SCK=%d/MOSI=%d/MISO=%d) or RST(GPIO%d) or PN5180 自体の問題",
                         PN5180_PIN_SCK, PN5180_PIN_MOSI, PN5180_PIN_MISO, PN5180_PIN_RST);
            }
        }
        if (!s_readers[i].dev) {
            ESP_LOGE(TAG, "pn5180_init reader %d failed (nss=%d busy=%d rst=%d mux_ch=%d via_mux=%d)",
                     i, cfg->nss, BUSY_PIN, PN5180_PIN_RST, cfg->mux_ch, PN5180_BUSY_VIA_MUX);
            return false;
        }
        s_readers[i].iso14443 = pn5180_14443_init(s_readers[i].dev);
        // TODO(実機): 第2引数 modulation はコンポーネント enum に合わせる（0=既定想定）。
        s_readers[i].iso15693 = pn5180_15693_init(s_readers[i].dev, 0);
        ESP_LOGI(TAG, "PN5180 reader %d ready (nss=%d mux_ch=%d)", i, cfg->nss, cfg->mux_ch);
    }
    return true;
}

// 1 つの proto から最初の UID を取り出す。取れたら true。
// 注: get_all_uids() は内部で setupRF + inventory を行う（pn5180-15693.c:687）。
//     ここで別途 setup_rf を呼ぶと二重設定でカード状態が乱れるため、get_all_uids のみ呼ぶ。
static bool read_uid_from_proto(pn5180_proto_t *proto, uint8_t *uid, uint8_t *uid_len) {
    if (!proto || !proto->get_all_uids) return false;
    nfc_uids_array_t *uids = proto->get_all_uids(proto);
    if (!uids) return false;
    // 一時診断: get_all_uids が非 NULL を返した = 何か見つけた。count と uid_length を出す。
    ESP_LOGI(TAG, "get_all_uids 戻り: count=%d uid_length=%d",
             uids->uids_count, uids->uids_count > 0 ? uids->uids[0].uid_length : -1);

    bool found = false;
    // nfc_uids_array_t { int uids_count; nfc_uid_t uids[]; }
    // nfc_uid_t { int8_t uid_length; ...; uint8_t uid[10]; }
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

// カード presence の保持サイクル数（debounce）。ISO15693 の inventory は単発で取りこぼすことが
// あり、保持が無いと host(pyscard)の IccPowerOn がちょうど取りこぼしポーリングに当たった瞬間に
// connect 失敗し、UID が一切取れない（probe_pcsc watch が 0 件になる主因）。一度検出したら
// この回数だけは present を維持し、連続 miss が超えたときだけ離脱と判定する。
#define PRESENCE_HOLD_MISSES 3
static int s_miss[CCID_SLOT_COUNT];

void pn5180_reader_poll_once(void) {
    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        mux_select(s_readers[i].mux_ch);  // この reader の BUSY を SIG に

        uint8_t uid[16];
        uint8_t len = 0;

        // ISO15693（8B）→ だめなら ISO14443A（4/7B）の順。
        bool detected = read_uid_from_proto(s_readers[i].iso15693, uid, &len) ||
                        read_uid_from_proto(s_readers[i].iso14443, uid, &len);

        bool was_present = s_cache[i].present;
        pn5180_card_t c = {0};
        if (detected) {
            c.present = true;
            c.uid_len = len;
            memcpy(c.uid, uid, len);
            s_miss[i] = 0;
        } else if (was_present && s_miss[i] < PRESENCE_HOLD_MISSES) {
            // 取りこぼし: 直近の present + UID を数サイクル保持（host の connect 失敗を防ぐ）。
            c = s_cache[i];
            s_miss[i]++;
        } else {
            // 連続 miss が hold を超えた → カード離脱と判定。
            c.present = false;
            s_miss[i] = 0;
        }

        // 状態が変化した時だけログ（毎ポーリングのスパムを避ける）。カード読み取りの可視化。
        if (c.present && !was_present) {
            char hex[3 * 16 + 1];
            int p = 0;
            for (int b = 0; b < c.uid_len && b < 16; b++) {
                p += snprintf(hex + p, sizeof(hex) - p, "%02X%s", c.uid[b], b + 1 < c.uid_len ? ":" : "");
            }
            ESP_LOGI(TAG, "🎴 カード検出! reader %d: UID=%s (%dB)", i, hex, c.uid_len);
        } else if (!c.present && was_present) {
            ESP_LOGI(TAG, "   カード離脱 reader %d", i);
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
