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
static void diag_busy_pin(int mux_ch) {
    mux_select(mux_ch);  // 対象 reader の BUSY を SIG に（via_mux 時）
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
    ESP_LOGW(TAG, "BUSY診断(ch%d): pin=%d via_mux=%d pull-up読み=%d pull-down読み=%d => %s",
             mux_ch, busy, PN5180_BUSY_VIA_MUX, pu, pd, verdict);
}

// ── MUX 全 channel 走査（テスター不要）──
// 16 ch を順に選択し SIG(pull-up) を読む。'0'=Low駆動(信号有=その ch に通電中 idle の PN5180),
// '1'=floating/High。全 '1' なら MUX 不通（EN/VCC/SIG）or 全 reader 未通電。一部 '0' なら
// MUX は生きており、'1' の ch だけ reader 未接続/未通電。
// 戻り値: Low だった ch のビットマスク（bit n = ch n）。buf には 17 文字以上の領域を渡す。
static uint16_t mux_scan_low_mask(char *buf) {
    uint16_t mask = 0;
#if PN5180_BUSY_VIA_MUX
    const int busy = PN5180_PIN_BUSY_SIG;
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << busy, .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    for (int ch = 0; ch < 16; ch++) {
        mux_select(ch);
        esp_rom_delay_us(200);
        int lvl = gpio_get_level(busy);
        buf[ch] = lvl ? '1' : '0';
        if (!lvl) mask |= (uint16_t)(1u << ch);
    }
    buf[16] = '\0';
    ESP_LOGW(TAG, "MUX scan ch0..15 (pull-up): %s  [0=Low駆動(信号有=通電中 reader) 1=floating]", buf);
#else
    buf[0] = '\0';
#endif
    return mask;
}

#if CCID_SLOT_COUNT == 1
// ── bring-up 用: 通電中の ch から使う reader を自動選択 ──
// 1 台だけ繋いで検証するとき、どのコネクタ（= MUX ch）に挿さっているかは日によって変わる
// （実機で ch12 → ch7 に変わり、設定固定だと init 失敗 → 再ビルドが必要だった）。MUX scan で
// Low 駆動＝通電 idle の PN5180 がいる ch を見つけ、テーブルからその ch の nss を引いて使う。
//   - 設定 [0] の ch が通電中ならそのまま。
//   - 通電 ch が別にあればそれ（複数なら最小番号）。
//   - 全 floating なら [0] にフォールバック（init は失敗するが診断ログは出る）。
// 本番（CCID_SLOT_COUNT=13）ではテーブル順 = slot 順なので自動選択はしない。
static const pn5180_reader_cfg_t *select_bringup_reader(uint16_t low_mask,
                                                        const pn5180_reader_cfg_t *fallback) {
    const int n = sizeof(PN5180_READERS) / sizeof(PN5180_READERS[0]);
    if (low_mask & (1u << fallback->mux_ch)) {
        ESP_LOGI(TAG, "bring-up: 設定 reader (ch%d, nss=GPIO%d) が通電中 → そのまま使用",
                 fallback->mux_ch, fallback->nss);
        return fallback;
    }
    if (low_mask == 0) {
        ESP_LOGW(TAG, "bring-up: 通電中の ch なし（全 floating）→ 設定 reader (ch%d, nss=GPIO%d) で試行",
                 fallback->mux_ch, fallback->nss);
        return fallback;
    }
    const int n_low = __builtin_popcount(low_mask);
    for (int ch = 0; ch < 16; ch++) {
        if (!(low_mask & (1u << ch))) continue;
        for (int k = 0; k < n; k++) {
            if (PN5180_READERS[k].mux_ch != ch) continue;
            ESP_LOGW(TAG, "bring-up: 設定 ch%d は floating。通電中は ch%d → reader #%d (nss=GPIO%d) を自動選択%s",
                     fallback->mux_ch, ch, ch + 1, PN5180_READERS[k].nss,
                     n_low > 1 ? "（複数 ch 通電中: 最小番号を採用）" : "");
            return &PN5180_READERS[k];
        }
        ESP_LOGW(TAG, "bring-up: ch%d が通電中だが PN5180_READERS に該当なし（配線表を確認）", ch);
    }
    return fallback;
}
#endif

// ── init 失敗時の診断（ログのみ。pn5180_init は再呼出ししない）──
// 以前はここで見つけた NSS で pn5180_init を 2 回目に呼んでいたが、2 回目も失敗すると
// ドライバ内の deinit → spi_bus_remove_device で assert（xQueue NULL）→ 再起動ループになり
// USB CCID まで落ちた。診断はログに留め、修正は app_config.h + 再ビルドで行う。
static void diag_after_init_failure(const pn5180_reader_cfg_t *cfg) {
    // SPI 配線の生存確認（NSS 関係なし、SCK/MOSI/MISO 経路の通電チェック）。
    ESP_LOGW(TAG, "SPI 配線生存確認 (SCK=%d MOSI=%d MISO=%d):",
             PN5180_PIN_SCK, PN5180_PIN_MOSI, PN5180_PIN_MISO);
    gpio_set_pull_mode(PN5180_PIN_MISO, GPIO_PULLUP_ONLY);
    int miso_before = gpio_get_level(PN5180_PIN_MISO);
    ESP_LOGW(TAG, "  MISO (pull-up時, SPI 通信前): %d (1=line idle/floating, 0=chip が Low に引いている)",
             miso_before);

    // BUSY 直接サンプリング（対象 ch の生波形を 200μs 見る）。
    mux_select(cfg->mux_ch);
    gpio_set_pull_mode(BUSY_PIN, GPIO_PULLUP_ONLY);
    int high_count = 0;
    for (int j = 0; j < 200; j++) {
        if (gpio_get_level(BUSY_PIN)) high_count++;
        esp_rom_delay_us(1);
    }
    ESP_LOGW(TAG, "  BUSY サンプル (200μs/pull-up): High=%d%%  (0%%=常時Low/100%%=常時High が問題)",
             high_count / 2);

    // NSS スキャン: ch=cfg->mux_ch のチップに対し、どの NSS GPIO が SPI 応答するかを
    // spi_bus_add_device + READ_REGISTER 1 発 + BUSY 監視 + remove で総当り。
    ESP_LOGW(TAG, "NSS スキャン: ch%d のチップが応答する NSS を低レベル SPI で探索（診断のみ、再 init なし）",
             cfg->mux_ch);
    static const int nss_candidates[] = {1, 2, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 18};
    const int n_cands = sizeof(nss_candidates) / sizeof(nss_candidates[0]);
    int found_nss = -1;
    for (int k = 0; k < n_cands; k++) {
        const int try_nss = nss_candidates[k];
        // 共有 RST を叩いてリセット → ブート完了（BUSY=Low）を待つ。
        gpio_set_direction(PN5180_PIN_RST, GPIO_MODE_OUTPUT);
        gpio_set_level(PN5180_PIN_RST, 0);
        esp_rom_delay_us(1000);
        gpio_set_level(PN5180_PIN_RST, 1);
        vTaskDelay(pdMS_TO_TICKS(10));
        mux_select(cfg->mux_ch);

        // 偽陽性対策: 送信前に BUSY が Low(idle) であることを要求する。既に High なら
        // floating/stuck で「送信で High に立った」と区別できない（実機で ch12 が浮いていて
        // 最初の候補 GPIO1 を誤検出し、その NSS で再 init → クラッシュした）。
        if (gpio_get_level(BUSY_PIN)) {
            ESP_LOGW(TAG, "  [%d/%d] NSS=GPIO%d -> 送信前から BUSY=High（floating/stuck: 判定不能）",
                     k + 1, n_cands, try_nss);
            continue;
        }

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
        // READ_REGISTER (0x04) + reg addr + 4 byte dummy。正しい NSS のチップなら BUSY が High に立つ。
        uint8_t tx[6] = {0x04, 0x00, 0x00, 0x00, 0x00, 0x00};
        uint8_t rx[6] = {0};
        spi_transaction_t t = {.length = 48, .tx_buffer = tx, .rx_buffer = rx};
        spi_device_polling_transmit(dev, &t);
        bool went_high = false;
        for (int j = 0; j < 500; j++) {
            if (gpio_get_level(BUSY_PIN)) { went_high = true; break; }
            esp_rom_delay_us(2);
        }
        spi_bus_remove_device(dev);
        // MISO 全 FF は pull-up で浮いている値（応答ではない）。
        const bool miso_floating = (rx[2] == 0xFF && rx[3] == 0xFF && rx[4] == 0xFF && rx[5] == 0xFF);
        const bool miso_active = !miso_floating && (rx[2] | rx[3] | rx[4] | rx[5]) != 0;
        ESP_LOGW(TAG, "  [%d/%d] NSS=GPIO%d -> BUSY Low→High:%s  MISO:%02X %02X %02X %02X %s",
                 k + 1, n_cands, try_nss, went_high ? "YES" : "no ",
                 rx[2], rx[3], rx[4], rx[5],
                 miso_active ? "(応答あり)" : miso_floating ? "(FF=floating)" : "");
        if (went_high) { found_nss = try_nss; break; }
    }
    if (found_nss > 0) {
        ESP_LOGW(TAG, "✅ ch%d のチップは NSS=GPIO%d で応答（設定は GPIO%d）",
                 cfg->mux_ch, found_nss, cfg->nss);
        if (found_nss != cfg->nss) {
            ESP_LOGW(TAG, "   → app_config.h の PN5180_READERS で ch%d の nss を %d に修正して再ビルド",
                     cfg->mux_ch, found_nss);
        } else {
            ESP_LOGW(TAG, "   → NSS は設定どおり応答。RST(GPIO%d)/電源/SPI 配線 or PN5180 個体を疑う",
                     PN5180_PIN_RST);
        }
    } else {
        ESP_LOGE(TAG, "❌ NSS スキャン: 全 %d 候補で反応なし", n_cands);
        ESP_LOGE(TAG, "   → SPI 配線(SCK=%d/MOSI=%d/MISO=%d) or RST(GPIO%d) or PN5180/電源 の問題",
                 PN5180_PIN_SCK, PN5180_PIN_MOSI, PN5180_PIN_MISO, PN5180_PIN_RST);
    }
}

bool pn5180_reader_init(void) {
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return false;
    memset(s_cache, 0, sizeof(s_cache));
    mux_init();

    // ── MUX 全 ch 走査（MUX 不通 か reader 個別 かの切り分け）→ bring-up は通電 ch から自動選択 ──
    char scan[17];
    const uint16_t low_mask = mux_scan_low_mask(scan);
    const pn5180_reader_cfg_t *cfg0 = &PN5180_READERS[0];
#if CCID_SLOT_COUNT == 1
    cfg0 = select_bringup_reader(low_mask, cfg0);
#else
    (void)low_mask;
#endif

    diag_busy_pin(cfg0->mux_ch);  // 対象 reader の BUSY ピンの素性を診断（ログに出す）
    // MUX(CD74HC4067)のSIG出力はアナログスイッチ経由で駆動が弱め。FLOATING だと ESP32-S3 の
    // 入力バッファが弱駆動を読み損ねて常時 High/Low に張り付くことがある。pull-up を当てると
    // High が安定し、PN5180 が BUSY を Low に引いた瞬間にだけ Low に落ちる（push-pull と
    // 内部 pull-up の併用は干渉しない）。
    gpio_set_direction(BUSY_PIN, GPIO_MODE_INPUT);  // 方向を明示（IDF 実装依存を避ける）
    gpio_set_pull_mode(BUSY_PIN, GPIO_PULLUP_ONLY);

    // ── RST/NSS 駆動 → BUSY 応答の手動診断（対象 reader）──
    // PN5180 は RST=Low(>10us)→High→数 ms 後 BUSY=Low(idle)。SPI 無しでこれが起こるかを観る。
    // RST が物理的に届いているなら、Low→High の前後で BUSY が High↘Low と変化するはず。
    const int rst = PN5180_PIN_RST;
    const int nss = cfg0->nss;
    gpio_set_direction(rst, GPIO_MODE_OUTPUT);
    gpio_set_direction(nss, GPIO_MODE_OUTPUT);
    gpio_set_level(nss, 1);  // deselect
    mux_select(cfg0->mux_ch);

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

    ESP_LOGW(TAG, "RST診断(ch%d, pull-up有): BUSY during_rst=%d  just_after=%d  after_10ms=%d",
             cfg0->mux_ch, busy_during_rst, busy_just_after, busy_after_boot);
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
        // [0] は bring-up 自動選択の結果（本番 CCID_SLOT_COUNT=13 では = PN5180_READERS[0]）。
        const pn5180_reader_cfg_t *cfg = (i == 0) ? cfg0 : &PN5180_READERS[i];
        s_readers[i].mux_ch = cfg->mux_ch;
        mux_select(cfg->mux_ch);  // この reader の BUSY を SIG に出してから init

        // busy = BUSY_PIN（MUX SIG or 直結, 共有）, rst = 共有, nss = reader 個別。
        s_readers[i].dev = pn5180_init(spi, cfg->nss, BUSY_PIN, PN5180_PIN_RST);
        if (!s_readers[i].dev) {
            ESP_LOGE(TAG, "pn5180_init reader %d failed (nss=%d busy=%d rst=%d mux_ch=%d via_mux=%d)",
                     i, cfg->nss, BUSY_PIN, PN5180_PIN_RST, cfg->mux_ch, PN5180_BUSY_VIA_MUX);
            if (i == 0) diag_after_init_failure(cfg);
            // pn5180_init は再呼出ししない（失敗時のドライバ deinit で assert → 再起動ループ）。
            // USB CCID は main.c が上げたままにするので host からは reader が見え続ける。
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

// 契約 v1.1 §7 (rfid-usb-ccid.md): UID は **MSB-first** で返す MUST。
// PN5180 + jef-sure ドライバの ISO/IEC 15693 INVENTORY 生レスポンスは **LSB-first**（PN5180 が
// LE で読み戻す。実機 ICODE SLIX で `0D B7 2A 1C 53 01 04 E0` ＝末尾が `04 E0` で逆順）。
// これを正さないと `rfid_cards.json`（MSB-first 期待）と照合が外れる。host の `bytes_to_tag_id`
// は受信バイトをそのまま hex 化するだけなので、ここで反転する。
// ISO14443A の UID は元から MSB-first（manufacturer code が先頭）なので反転しない。
static void reverse_bytes(uint8_t *p, size_t n) {
    for (size_t a = 0, b = n - 1; a < b; a++, b--) {
        uint8_t t = p[a];
        p[a] = p[b];
        p[b] = t;
    }
}

void pn5180_reader_poll_once(void) {
    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        mux_select(s_readers[i].mux_ch);  // この reader の BUSY を SIG に

        uint8_t uid[16];
        uint8_t len = 0;

        // ISO15693（8B）→（PN5180_TRY_ISO14443=1 のときだけ）ISO14443A（4/7B）の順。
        // proto を分けて試すのは、ISO15693 のときだけ MSB-first に反転するため。
        bool detected = false;
        bool from_iso15693 = false;
        if (read_uid_from_proto(s_readers[i].iso15693, uid, &len)) {
            detected = true;
            from_iso15693 = true;
        }
#if PN5180_TRY_ISO14443
        else if (read_uid_from_proto(s_readers[i].iso14443, uid, &len)) {
            detected = true;
        }
#endif

        // 契約 v1.1 §7: ISO15693 の生バイトは LSB-first なので MSB-first に反転する。
        if (detected && from_iso15693 && len > 1) {
            reverse_bytes(uid, len);
        }

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
