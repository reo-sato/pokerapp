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
#include <stdlib.h>          // free（ドライバ経路の get_all_uids が返す heap 配列の解放）
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"       // esp_timer_get_time（poll 1 周の所要時間計測）
#include "esp_log.h"

#include "app_config.h"
#include "pn5180_reader.h"
#include "pn5180.h"          // pn5180_spi_init / pn5180_init / pn5180_t / pn5180_proto_t
#include "pn5180-14443.h"    // pn5180_14443_init
#include "pn5180-15693.h"    // pn5180_15693_init

static const char *TAG = "pn5180";

#if PN5180_FAST_INVENTORY
// ── ISO/IEC 15693 のフラグ / コマンド ──
// ドライバの public ヘッダには出ていない（src/pn5180-15693.c の file-local）ので自前定義する。
// 値は ISO/IEC 15693-3 とドライバ内部の定義に一致。
#define ISO15693_FLAG_DATA_RATE_HIGH 0x02  // 応答を high data rate(26.48kbps) で返させる
#define ISO15693_FLAG_INVENTORY      0x04  // inventory フラグ（bit5 の意味が slot 数に変わる）
#define ISO15693_FLAG_SLOT_ONE       0x20  // 1 slot（応答は 1 枚だけ。16 slot は使わない）
// bit5 は inventory=0 のとき Address_flag（UID 8B を続けて 1 枚だけに宛てる）になる。
#define ISO15693_FLAG_ADDRESS        0x20
#define ISO15693_CMD_INVENTORY       0x01
#define ISO15693_CMD_STAY_QUIET      0x02  // 宛先タグを黙らせる（応答なし。RF off で解除）

// fast 経路の間だけ pn5180_t.timeout_ms を絞る値。ドライバ既定は 500ms で、これは
// **SPI の BUSY 待ち・transceive 状態待ち・RF off 待ちすべての上限**なので、1 台の不調が
// 1 周を 0.5 秒伸ばしてしまう（11 台なら 5.5 秒）。ドライバ自身も get_all_uids の間だけ
// 40ms に落としているので、それに合わせる（scan 後に元へ戻す）。
#define PN5180_FAST_OP_TIMEOUT_MS 40

// DFS スタックの深さ。DFS は root の 2 子から始まり、1 回の probe で pop 1 / push 最大 2 =
// 正味 +1 なので、probe 上限 + 2 あれば溢れない（push 前に空き 2 を確認もしている）。
#define FAST_DFS_STACK (PN5180_FAST_MAX_PROBES + 2)
#endif

// BUSY を MUX 経由で読むか直結で読むか（app_config.h の切り分けフラグ）。
#if PN5180_BUSY_VIA_MUX
#  define BUSY_PIN PN5180_PIN_BUSY_SIG
#else
#  define BUSY_PIN PN5180_PIN_BUSY_DIRECT
#endif

// slot 数は配線表 PN5180_READERS の要素数を超えられない（超えると配列外参照）。
// メッセージは ASCII 固定（gcc の診断が非 ASCII を 8 進エスケープして読めなくなるため）。
_Static_assert(CCID_SLOT_COUNT >= 1 &&
                   CCID_SLOT_COUNT <= (int)(sizeof(PN5180_READERS) / sizeof(PN5180_READERS[0])),
               "CCID_SLOT_COUNT must be 1..N of PN5180_READERS: extend the wiring table in app_config.h first");

typedef struct {
    pn5180_t *dev;               // NULL = 未通電などで init を飛ばした slot（常にカード無し扱い）
    pn5180_proto_t *iso14443;
    pn5180_proto_t *iso15693;
    int mux_ch;
    bool rf_loaded;              // fast 経路: LOAD_RF_CONFIG 済み（false なら poll で再試行）
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

#if CCID_SLOT_COUNT > 1
// ── 配線チェック（複数 slot 時、init の前に 1 回）──
// MUX scan の「通電マスク」と app_config.h の設定（PN5180_READERS[0..CCID_SLOT_COUNT-1].mux_ch）を
// 突き合わせ、食い違いをログに出す。実機で「挿し忘れ / コネクタ違い / slot 数不足」を即座に見分ける。
//   (1) 設定 ch なのに floating   → その reader は未通電/未接続。init を飛ばして skip する。
//   (2) 通電しているのに設定範囲外 → 繋いだのに CCID_SLOT_COUNT が足りない、または配線表とのズレ。
static void diag_wiring_map(uint16_t low_mask) {
    const int n_table = (int)(sizeof(PN5180_READERS) / sizeof(PN5180_READERS[0]));
    uint16_t cfg_mask = 0;

    // (1) 設定した ch のうち floating のもの。
    char missing[128];
    missing[0] = '\0';
    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        const int ch = PN5180_READERS[i].mux_ch;
        cfg_mask |= (uint16_t)(1u << ch);
        if (low_mask & (uint16_t)(1u << ch)) continue;
        const size_t used = strlen(missing);
        snprintf(missing + used, sizeof(missing) - used, "%s#%d(ch%d)",
                 used ? ", " : "", i + 1, ch);
    }
    if (missing[0]) {
        ESP_LOGW(TAG, "配線チェック: 設定 ch なのに floating（未通電/未接続）= %s → この slot は skip",
                 missing);
    }

    // (2) 通電しているのに設定範囲外の ch（表に載っていれば reader 番号も出す）。
    char extra[128];
    extra[0] = '\0';
    for (int ch = 0; ch < 16; ch++) {
        if (!(low_mask & (uint16_t)(1u << ch))) continue;
        if (cfg_mask & (uint16_t)(1u << ch)) continue;
        int idx = -1;
        for (int k = 0; k < n_table; k++) {
            if (PN5180_READERS[k].mux_ch == ch) { idx = k; break; }
        }
        const size_t used = strlen(extra);
        if (idx >= 0) {
            snprintf(extra + used, sizeof(extra) - used, "%sch%d(=#%d)", used ? ", " : "", ch, idx + 1);
        } else {
            snprintf(extra + used, sizeof(extra) - used, "%sch%d(表に無い)", used ? ", " : "", ch);
        }
    }
    if (extra[0]) {
        ESP_LOGW(TAG, "配線チェック: 通電しているが設定範囲外の ch = %s → CCID_SLOT_COUNT(%d) を増やすか配線表を確認",
                 extra, CCID_SLOT_COUNT);
    }

    if (!missing[0] && !extra[0]) {
        ESP_LOGI(TAG, "配線 OK: 設定 %d ch すべて通電（設定範囲外の通電 ch も無し）", CCID_SLOT_COUNT);
    }
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
    // spi_bus_add_device + READ_EEPROM 1 発 + BUSY 監視 + remove で総当り（候補は配線表の 13 本）。
    //
    // **BUSY が壊れていても必ず SPI を送る**のが要点。実機で「MUX 全 ch floating / RST 診断
    // 1,1,1 / NSS スキャンは全候補『送信前から BUSY=High』で SPI を一度も送らず終了」という
    // 状態になり、PN5180 が死んでいるのか BUSY(MUX)経路だけが壊れているのか切り分けられなかった。
    // BUSY ハンドシェイクの代わりに **固定待ち 1ms** を置き、EEPROM の firmware version を
    // 読んで MISO に意味のある値が返るかで「チップ生存」を判定する。
    ESP_LOGW(TAG, "NSS スキャン: ch%d のチップが応答する NSS を低レベル SPI で探索（診断のみ、再 init なし）",
             cfg->mux_ch);
    ESP_LOGW(TAG, "  READ_EEPROM(0x07) addr=0x12(FIRMWARE_VERSION) len=2 を送信 → 受信 2 byte を FW= で表示");
    const int n_cands = (int)(sizeof(PN5180_READERS) / sizeof(PN5180_READERS[0]));
    int found_nss = -1;      // BUSY が Low→High に動いた候補（最も強い証拠）
    int spi_alive_nss = -1;  // BUSY は不明だが MISO に意味のある値が返った候補
    uint8_t spi_alive_fw[2] = {0, 0};
    for (int k = 0; k < n_cands; k++) {
        const int try_nss = PN5180_READERS[k].nss;
        // 共有 RST を叩いてリセット → ブート完了（BUSY=Low）を待つ。
        gpio_set_direction(PN5180_PIN_RST, GPIO_MODE_OUTPUT);
        gpio_set_level(PN5180_PIN_RST, 0);
        esp_rom_delay_us(1000);
        gpio_set_level(PN5180_PIN_RST, 1);
        vTaskDelay(pdMS_TO_TICKS(10));
        mux_select(cfg->mux_ch);

        // 送信前の BUSY。High（floating/stuck）なら「送信で High に立った」と区別できないので、
        // この候補では BUSY 判定を諦める（= 誤検出して再 init しない。実機で ch12 が浮いていて
        // GPIO1 を誤検出しクラッシュした反省）。SPI 自体は送る。
        const int busy_before = gpio_get_level(BUSY_PIN);

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

        // PN5180 の SPI は 2 フェーズ:「送信 = NSS↓ コマンド NSS↑」→「受信 = NSS↓ 読み出し NSS↑」。
        // hardware CS では 1 トランザクション = 1 フェーズなので 2 回に分ける。
        // 送信フェーズ: READ_EEPROM(0x07) + addr 0x12(FIRMWARE_VERSION) + len 2。
        uint8_t tx_cmd[3] = {0x07, 0x12, 0x02};
        spi_transaction_t t_cmd = {.length = 8 * sizeof(tx_cmd), .tx_buffer = tx_cmd};
        spi_device_polling_transmit(dev, &t_cmd);

        // 固定待ち 1ms（BUSY ハンドシェイクの代わり）。ついでに BUSY の立ち上がりも観る
        // （送信前が Low だった候補だけ意味がある）。
        bool went_high = false;
        for (int j = 0; j < 500; j++) {
            if (!busy_before && gpio_get_level(BUSY_PIN)) went_high = true;
            esp_rom_delay_us(2);
        }

        // 受信フェーズ: 2 byte 読み出し（PN5180 は MOSI を無視して MISO に載せる）。
        uint8_t tx_dummy[2] = {0xFF, 0xFF};
        uint8_t fw[2] = {0xFF, 0xFF};
        spi_transaction_t t_rd = {.length = 16, .tx_buffer = tx_dummy, .rx_buffer = fw};
        spi_device_polling_transmit(dev, &t_rd);
        esp_rom_delay_us(1000);
        spi_bus_remove_device(dev);

        // 全 FF = pull-up で浮いている / 全 00 = Low 固定。どちらも「応答ではない」。
        const bool miso_floating = (fw[0] == 0xFF && fw[1] == 0xFF);
        const bool miso_zero = (fw[0] == 0x00 && fw[1] == 0x00);
        const bool miso_active = !miso_floating && !miso_zero;
        ESP_LOGW(TAG, "  [%d/%d] NSS=GPIO%d (ch%d) -> 送信前BUSY=%d  BUSY Low→High:%s  FW=%02X %02X %s",
                 k + 1, n_cands, try_nss, PN5180_READERS[k].mux_ch, busy_before,
                 busy_before ? "判定不能" : (went_high ? "YES" : "no "),
                 fw[0], fw[1],
                 miso_active ? "(SPI 応答あり)" : miso_floating ? "(FF=floating)" : "(00=Low 固定)");
        if (miso_active && spi_alive_nss < 0) {
            spi_alive_nss = try_nss;
            spi_alive_fw[0] = fw[0];
            spi_alive_fw[1] = fw[1];
        }
        if (!busy_before && went_high) { found_nss = try_nss; break; }
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
    } else if (spi_alive_nss > 0) {
        // BUSY は動かなかったが SPI には答えた = チップは生きていて BUSY 経路だけが死んでいる。
        ESP_LOGW(TAG, "✅ SPI 応答あり (NSS=GPIO%d, FW=%02X %02X) = **PN5180 は生きている**",
                 spi_alive_nss, spi_alive_fw[0], spi_alive_fw[1]);
        ESP_LOGW(TAG, "   → BUSY/MUX 経路を疑う: MUX VCC(3.3V)/EN(GND)/SIG(GPIO%d) の配線、"
                      "ch%d の BUSY 線、PN5180_BUSY_VIA_MUX=%d の設定",
                 PN5180_PIN_BUSY_SIG, cfg->mux_ch, PN5180_BUSY_VIA_MUX);
        ESP_LOGW(TAG, "   → 切り分け: PN5180_BUSY_VIA_MUX=0 + reader #1 の BUSY を GPIO%d に直結して再ビルド",
                 PN5180_PIN_BUSY_DIRECT);
    } else {
        ESP_LOGE(TAG, "❌ NSS スキャン: 全 %d 候補で SPI 無応答（FF FF / 00 00 のみ）", n_cands);
        ESP_LOGE(TAG, "   → PN5180 の電源(3.3V/5V)・RST(GPIO%d)・SPI 配線(SCK=%d/MOSI=%d/MISO=%d) を疑う"
                      "（BUSY/MUX ではなくチップに届いていない）",
                 PN5180_PIN_RST, PN5180_PIN_SCK, PN5180_PIN_MOSI, PN5180_PIN_MISO);
    }
}

bool pn5180_reader_init(void) {
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return false;
    memset(s_cache, 0, sizeof(s_cache));
    mux_init();

    // ── scan の前に共有 RST を 1 回叩いて全 PN5180 を idle(BUSY=Low) に揃える ──
    // 電源投入直後や前回稼働の途中状態では BUSY が High のままのチップがあり、そのまま scan すると
    // 通電中なのに '1'(floating 扱い) と読んで自動選択を外す（実機 2026-09-10: ch0 に挿した reader が
    // scan では全 '1' で、設定既定が ch0 だったから偶然 init できた）。reset → 10ms でブート完了。
    gpio_set_direction(PN5180_PIN_RST, GPIO_MODE_OUTPUT);
    gpio_set_level(PN5180_PIN_RST, 0);
    esp_rom_delay_us(2000);
    gpio_set_level(PN5180_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(10));

    // ── MUX 全 ch 走査（MUX 不通 か reader 個別 かの切り分け）→ bring-up は通電 ch から自動選択 ──
    char scan[17];
    const uint16_t low_mask = mux_scan_low_mask(scan);
    const pn5180_reader_cfg_t *cfg0 = &PN5180_READERS[0];
#if CCID_SLOT_COUNT == 1
    cfg0 = select_bringup_reader(low_mask, cfg0);
#endif
    // CCID_SLOT_COUNT > 1 では自動選択せず配列順 = slot 順。low_mask は下の配線チェックと
    // 「未通電 reader の skip」で使う。

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

#if CCID_SLOT_COUNT > 1
    diag_wiring_map(low_mask);  // 設定 ch と通電 ch の食い違いを先に見せる
#endif

    int ready_count = 0;
    char skipped[128];  // 未通電で飛ばした reader の一覧（起動要約に出す）
    skipped[0] = '\0';

    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        // [0] は bring-up 自動選択の結果（本番 CCID_SLOT_COUNT=13 では = PN5180_READERS[0]）。
        const pn5180_reader_cfg_t *cfg = (i == 0) ? cfg0 : &PN5180_READERS[i];
        s_readers[i].mux_ch = cfg->mux_ch;
        s_readers[i].dev = NULL;

#if CCID_SLOT_COUNT > 1
        // 未通電/未接続（BUSY が floating）の reader は pn5180_init を呼ばずに飛ばす。
        // 呼んでしまうと失敗経路のドライバ deinit が **共有 SPI device を解放** し、他の生きている
        // reader まで巻き添えで死ぬ（後述）。飛ばした slot は dev=NULL のまま = 常にカード無し。
        if (!(low_mask & (uint16_t)(1u << cfg->mux_ch))) {
            ESP_LOGW(TAG,
                     "reader #%d (slot %d, nss=GPIO%d, ch%d) は未通電/未接続 → skip"
                     "（host には slot は見えるが Get UID は常に SW=6A81）",
                     i + 1, i, cfg->nss, cfg->mux_ch);
            const size_t used = strlen(skipped);
            snprintf(skipped + used, sizeof(skipped) - used, "%s#%d", used ? ", " : "", i + 1);
            continue;
        }
#endif

        mux_select(cfg->mux_ch);  // この reader の BUSY を SIG に出してから init

        // busy = BUSY_PIN（MUX SIG or 直結, 共有）, rst = 共有, nss = reader 個別。
        s_readers[i].dev = pn5180_init(spi, cfg->nss, BUSY_PIN, PN5180_PIN_RST);
        if (!s_readers[i].dev) {
            // 通電しているのに応答しない = 配線/NSS ミス・個体不良の類。
            ESP_LOGE(TAG, "pn5180_init reader %d failed (nss=%d busy=%d rst=%d mux_ch=%d via_mux=%d)",
                     i, cfg->nss, BUSY_PIN, PN5180_PIN_RST, cfg->mux_ch, PN5180_BUSY_VIA_MUX);
            ESP_LOGE(TAG, "  → ドライバの失敗経路が pn5180_deinit で **共有 SPI device** を解放したため、"
                          "以降の reader 初期化も poll も不可 → 全 reader 停止（この 1 台だけ skip はできない）");
            // まだ 1 台も ready でないときだけ深掘り診断（NSS スキャン等）を出す。
            if (ready_count == 0) diag_after_init_failure(cfg);
            // pn5180_init は再呼出ししない（失敗時のドライバ deinit で assert → 再起動ループ）。
            // USB CCID は main.c が上げたままにするので host からは reader が見え続ける。
            return false;
        }
        s_readers[i].iso14443 = pn5180_14443_init(s_readers[i].dev);
        // 第2引数は pn5180_15693_rf_config_t（pn5180->rf_config の初期値になるだけで、
        // ドライバ経路では get_all_uids が毎回上書きする）。fast 経路と同じ値を入れておく。
        s_readers[i].iso15693 = pn5180_15693_init(s_readers[i].dev, PN5180_FAST_RF_CONFIG);
        ready_count++;
        ESP_LOGI(TAG, "PN5180 reader %d ready (nss=%d mux_ch=%d)", i, cfg->nss, cfg->mux_ch);
    }

    if (ready_count == 0) {
        ESP_LOGE(TAG, "PN5180 ready 0 台（全 slot が未通電/未接続）— 電源・MUX・配線を確認");
        return false;
    }

#if PN5180_FAST_INVENTORY
    // ── fast 経路の RF 設定を全 reader にロード（init ループの「後」で行うのが要点）──
    // pn5180_init() は内部で **共有 RST を pulse する**ので、reader k の init 中に reader k+1 の
    // チップもリセットされ、レジスタ（= LOAD_RF_CONFIG の内容）が消える。init ループの中で
    // ロードしても後続の init で無効になるため、全台の init が終わってから別ループでロードする。
    // 以降 RF を on/off しても設定は残る（リセットしない限り）。
    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        if (!s_readers[i].dev) continue;
        mux_select(s_readers[i].mux_ch);
        s_readers[i].rf_loaded = pn5180_loadRFConfig(s_readers[i].dev, PN5180_FAST_RF_CONFIG);
        if (!s_readers[i].rf_loaded) {
            // init 自体は成功しているので slot は生かしたまま、poll 側で毎回ロードを再試行する。
            ESP_LOGE(TAG, "reader %d: LOAD_RF_CONFIG(0x%02X) 失敗 — poll で再試行する",
                     i, (unsigned)PN5180_FAST_RF_CONFIG);
        }
    }
    ESP_LOGI(TAG, "fast inventory 有効 (RF config=0x%02X, RX timeout=%d ms, max %d 枚/probe 上限 %d)",
             (unsigned)PN5180_FAST_RF_CONFIG, PN5180_FAST_RX_TIMEOUT_MS,
             PN5180_MAX_CARDS_PER_READER, PN5180_FAST_MAX_PROBES);
#endif

    ESP_LOGI(TAG, "PN5180 ready: %d/%d slot%s%s%s", ready_count, CCID_SLOT_COUNT,
             skipped[0] ? "（skip: " : "", skipped, skipped[0] ? "）" : "");
    return true;
}

#if !PN5180_FAST_INVENTORY
// 1 つの proto から UID を **最大 PN5180_MAX_CARDS_PER_READER 枚** 取り出す。戻り値 = 枚数。
// 注: get_all_uids() は内部で setupRF + inventory を行う。ここで別途 setup_rf を呼ぶと
//     二重設定でカード状態が乱れるため、get_all_uids のみ呼ぶ。
// cache は uid_len を 1 つしか持たない（重ね置きは同種カード前提）ので、先頭と長さが違う
// エントリは捨てる。
static uint8_t read_uids_from_proto(pn5180_proto_t *proto, uint8_t uids[][16], uint8_t *uid_len) {
    if (!proto || !proto->get_all_uids) return 0;
    nfc_uids_array_t *arr = proto->get_all_uids(proto);
    if (!arr) return 0;
    // get_all_uids が非 NULL を返した = 何か見つけた。毎 poll 出るので DEBUG（検出/離脱は poll 側が INFO）。
    ESP_LOGD(TAG, "get_all_uids 戻り: count=%d uid_length=%d",
             arr->uids_count, arr->uids_count > 0 ? arr->uids[0].uid_length : -1);

    // nfc_uids_array_t { int uids_count; nfc_uid_t uids[]; }
    // nfc_uid_t { int8_t uid_length; ...; uint8_t uid[10]; }
    uint8_t count = 0;
    for (int k = 0; k < arr->uids_count && count < PN5180_MAX_CARDS_PER_READER; k++) {
        int n = arr->uids[k].uid_length;
        if (n > 16) n = 16;
        if (n <= 0) continue;
        if (count == 0) {
            *uid_len = (uint8_t)n;
        } else if ((uint8_t)n != *uid_len) {
            continue;
        }
        memcpy(uids[count], arr->uids[k].uid, (size_t)n);
        count++;
    }
    free(arr);  // README: heap 配列は free 必須
    return count;
}
#endif  // !PN5180_FAST_INVENTORY

#if PN5180_FAST_INVENTORY
// ── 1 回の INVENTORY probe（mask 付き）の結果 ──
typedef enum {
    PROBE_NONE = 0,   // 応答なし（この mask に合致するカードは無い）
    PROBE_UID,        // ちょうど 1 枚が応答した → uid_out に LSB-first の 8 byte
    PROBE_COLLISION,  // 複数枚が同時に応答した → mask を 1 bit 伸ばして分割する
} probe_result_t;

// ISO15693 INVENTORY を 1 回だけ送り、応答を 3 値で返す（ドライバの総当たりをしない）。
// フレーム: flags(0x26 = high rate | inventory | 1 slot), INVENTORY(0x01), mask_len,
//           mask 値（ceil(mask_len/8) byte, **LSB-first**）
// mask は「UID の下位 mask_len ビット」と比較される。ISO15693 は UID を LSB から送るので、
// mask bit i = 受信生バイト rx[2] の bit0 から数えて i 番目のビット。
static probe_result_t fast_probe_15693(pn5180_t *dev, uint64_t mask, uint8_t mask_len,
                                       uint8_t *uid_out) {
    const uint8_t nbytes = (uint8_t)((mask_len + 7) / 8);  // mask_len<=64 なので <=8
    uint8_t buf[3 + 8];
    buf[0] = ISO15693_FLAG_DATA_RATE_HIGH | ISO15693_FLAG_INVENTORY | ISO15693_FLAG_SLOT_ONE;
    buf[1] = ISO15693_CMD_INVENTORY;
    buf[2] = mask_len;
    for (uint8_t i = 0; i < nbytes; i++) {
        buf[3 + i] = (uint8_t)((mask >> (8 * i)) & 0xFF);
    }

    // ドライバも送信のたびに CRC を有効化している（LOAD_RF_CONFIG や idle 遷移で落ちても
    // 拾えるように）。レジスタ 2 本の write なので費用は小さい。
    pn5180_enable_crc(dev);
    // idle → transceive 遷移と全 IRQ clear は pn5180_sendData の内部で行われる。
    if (!pn5180_sendData(dev, buf, 3 + nbytes, 0)) return PROBE_NONE;

    // 応答待ち（自前ループ）。pn5180_wait_for_irq() は timeout のたびに ESP_LOGE を出すので
    // 使えない（カード無しの reader が毎 poll ログを吐いて UART が埋まる）。
    uint32_t irq = 0;
    const int64_t deadline = esp_timer_get_time() + (int64_t)PN5180_FAST_RX_TIMEOUT_MS * 1000;
    for (;;) {
        irq = pn5180_getIRQStatus(dev);
        if (irq & (RX_IRQ_STAT | TIMER2_IRQ_STAT | GENERAL_ERROR_IRQ_STAT)) break;
        if (esp_timer_get_time() > deadline) break;
        esp_rom_delay_us(100);
    }
    if (!(irq & RX_IRQ_STAT)) return PROBE_NONE;  // 無応答 / RX timeout(TIMER2) / general error

    uint32_t rs = 0;
    if (!pn5180_readRegister(dev, RX_STATUS, &rs)) return PROBE_NONE;
    const uint32_t n = (rs >> RX_BYTES_RECEIVED_START) & RX_BYTES_RECEIVED_MASK;
    const bool collision = (rs & RX_COLLISION_DETECTED) != 0;

    // **衝突フラグが立っていれば受信バイト数に依らず分割する**。SOF で衝突すると
    // 「collision=1 / 受信 0 byte」で返ることがあり、下の n==0 を先に見ると衝突を
    // 「カード無し」に倒してしまう（= 重ね置きが永久に分離できない）。
    if (collision) return PROBE_COLLISION;

    // 受信バイト 0 で protocol/integrity error だけ = 応答の実体が無いノイズ（ドライバも
    // 0 byte + protocol error を noise と呼んでいる）→「カード無し」。
    if (n == 0) return PROBE_NONE;

    // 応答は flags(1) + DSFID(1) + UID(8) = 10 byte（CRC は PN5180 が検証して外す）。
    // 10 byte 以外 / **バイトはあるが CRC・protocol error** は、いずれも「複数枚の応答が
    // 重なった」可能性があるので mask を伸ばして分割する側に倒す。
    // （CRC 崩れを「無し」に倒すと、2 枚の応答が衝突フラグ無しで重なり続ける限り両方とも
    //   永久に読めない。ノイズだった場合は子 2 枝が NONE で終わり 2 probe 損するだけ。）
    if (n != 10 || (rs & (RX_PROTOCOL_ERROR | RX_DATA_INTEGRITY_ERROR))) {
        return PROBE_COLLISION;
    }

    uint8_t rx[10];
    if (!pn5180_readData(dev, 10, rx)) return PROBE_NONE;
    if (rx[0] & 0x01) return PROBE_NONE;  // ISO15693 応答 flags bit0 = Error_flag
    memcpy(uid_out, rx + 2, 8);           // LSB-first のまま返す（反転は呼び側 = poll）
    return PROBE_UID;
}

// ── 見つけたタグを黙らせる（STAY QUIET, ISO/IEC 15693-3 §10.3）──
// フレーム: flags(0x22 = high rate | Address_flag), STAY_QUIET(0x02), UID 8 byte（**LSB-first** =
// probe で受信した生バイト順のまま）。**応答は無い**ので RX は待たない。
// これを送らないと、2 枚同時応答を PN5180 が衝突と認識せず強い方だけ復号する（capture effect）
// ケースで、弱い方が永久に分離できない（実機 2026-09-10: 3 枚重ねで 3 枚目が一度も出なかった）。
// quiet 状態は **RF を切ると解除**される（呼び側が inventory の最後に必ず RF off する）。
// 失敗はログを出さずに無視する（黙らせ損ねたタグは次の probe でまた応答し、再送される）。
static void fast_stay_quiet_15693(pn5180_t *dev, const uint8_t *uid_lsb_first) {
    uint8_t buf[2 + 8];
    buf[0] = ISO15693_FLAG_DATA_RATE_HIGH | ISO15693_FLAG_ADDRESS;
    buf[1] = ISO15693_CMD_STAY_QUIET;
    memcpy(buf + 2, uid_lsb_first, 8);

    pn5180_enable_crc(dev);
    if (!pn5180_sendData(dev, buf, (int)sizeof(buf), 0)) return;

    // 送信完了（TX_IRQ）を待つ。フレームは 12 byte（10 + CRC 2）× 8 bit / 26.48 kbps ≈ 3.7 ms
    // かかるので上限は 10 ms。**待ち切れずに次の pn5180_sendData（idle→transceive）へ進むと、送信中の
    // フレームが途中で打ち切られてタグに Stay Quiet が届かない**（実機 2026-09-10: 上限 3 ms だった
    // とき、3 枚重ねで quiet が効かず毎 poll probe 上限 16 回まで空回り → 1 周 ≈170 ms）。
    // RX は待たない（応答の無いコマンド）。
    const int64_t deadline = esp_timer_get_time() + 10000;
    for (;;) {
        if (pn5180_getIRQStatus(dev) & TX_IRQ_STAT) break;
        if (esp_timer_get_time() > deadline) break;
        esp_rom_delay_us(100);
    }
    // タグ側の処理時間（ISO/IEC 15693-3 の t1 ≈ 320 µs）を空けてから次の要求を送る。
    esp_rom_delay_us(500);
}

// 見つけた UID を集合に積む（重複は無視、満杯も無視）。
static void fast_add_uid(uint8_t uids[][16], uint8_t *count, const uint8_t *uid) {
    for (uint8_t k = 0; k < *count; k++) {
        if (memcmp(uids[k], uid, 8) == 0) return;
    }
    if (*count >= PN5180_MAX_CARDS_PER_READER) return;
    memcpy(uids[*count], uid, 8);
    (*count)++;
}

// 直近に読んだ reader 1 台の probe（INVENTORY 送信）回数。poll 統計で「1 周の最大」を出すために持つ
// （Stay Quiet が効かず probe 上限まで空回りしているか、をログで見えるようにする）。
static int s_fast_last_probes;

// ── 高速 inventory 本体（Stay Quiet + mask ベースの anti-collision DFS）──
// RF はこの関数の間ずっと ON（reader ごと 1 回だけ立てる）。**最後に必ず RF off**（quiet 解除）。
// 戻り値 = 見つかった枚数（0..PN5180_MAX_CARDS_PER_READER）。uids は **LSB-first のまま**。
//
// ループ構造:
//   root(mask 0) を probe → 応答なし = 全部拾った / 1 枚 = 記録して Stay Quiet /
//   衝突 = mask DFS で分離（見つけるたび Stay Quiet）→ DFS が尽きたら **root を再 probe**。
// 再 probe が要る理由: 1 slot inventory は本来「mask に合致するのが 1 枚のときだけ応答が成立」
// するが、実機では 2 枚が同時応答しても PN5180 が衝突を検出せず強い方だけを正しく復号する
// （capture effect）。この枝は PROBE_UID で終わってしまい、弱い方は DFS では現れない。
// 見つけた札を黙らせてから root をやり直すと、隠れていた札が応答してくる。
static uint8_t fast_inventory_15693(slot_reader_t *r, uint8_t uids[][16], uint8_t *uid_len) {
    pn5180_t *dev = r->dev;
    *uid_len = 8;
    if (!r->rf_loaded) {
        if (!pn5180_loadRFConfig(dev, PN5180_FAST_RF_CONFIG)) return 0;
        r->rf_loaded = true;
    }
    if (!pn5180_setRF_on(dev)) return 0;  // is_rf_on はドライバが持つので二重 ON にはならない
    esp_rom_delay_us(PN5180_FAST_FIELD_SETTLE_US);

    typedef struct {
        uint64_t mask;
        uint8_t len;
    } dfs_node_t;
    dfs_node_t stack[FAST_DFS_STACK];

    uint8_t count = 0;
    int probes = 0;
    int stale_rounds = 0;  // 新しい UID が 1 枚も増えなかったラウンドの連続数
    uint8_t uid[8];
    while (probes < PN5180_FAST_MAX_PROBES && count < PN5180_MAX_CARDS_PER_READER) {
        const uint8_t before = count;
        probes++;
        const probe_result_t root = fast_probe_15693(dev, 0, 0, uid);
        if (root == PROBE_NONE) break;  // 誰も応答しない = 残りは居ない（正常終了）
        if (root == PROBE_UID) {
            fast_add_uid(uids, &count, uid);
            fast_stay_quiet_15693(dev, uid);  // dup でも送る（黙らせ損ねの再送になる）
        } else {
            // 衝突: root の 2 子（bit0 = 0 / 1）から mask DFS。
            int sp = 0;
            stack[sp].mask = 1ULL;
            stack[sp].len = 1;
            sp++;
            stack[sp].mask = 0ULL;
            stack[sp].len = 1;
            sp++;
            while (sp > 0 && probes < PN5180_FAST_MAX_PROBES &&
                   count < PN5180_MAX_CARDS_PER_READER) {
                const dfs_node_t cur = stack[--sp];
                probes++;
                switch (fast_probe_15693(dev, cur.mask, cur.len, uid)) {
                case PROBE_UID:
                    fast_add_uid(uids, &count, uid);
                    fast_stay_quiet_15693(dev, uid);
                    break;
                case PROBE_COLLISION:
                    // mask を 1 bit 伸ばして 2 分割（bit cur.len が 0 の枝 / 1 の枝）。
                    if (cur.len < 64 && sp + 2 <= (int)FAST_DFS_STACK) {
                        stack[sp].mask = cur.mask | (1ULL << cur.len);
                        stack[sp].len = (uint8_t)(cur.len + 1);
                        sp++;
                        stack[sp].mask = cur.mask;
                        stack[sp].len = (uint8_t)(cur.len + 1);
                        sp++;
                    }
                    break;
                case PROBE_NONE:
                default:
                    break;
                }
            }
        }
        // ラウンドで 1 枚も増えなかった = Stay Quiet が効いていない（黙らない札 / 送信失敗）。
        // 同じ探索を繰り返しても進まないので、1 回だけ再試行して打ち切る（probe 上限まで
        // 空回りすると reader 1 台で 80ms 以上を食う）。増えたなら再試行回数をリセット。
        if (count == before) {
            if (++stale_rounds >= 2) {
                ESP_LOGD(TAG, "fast inventory: Stay Quiet が効かず進捗なし（%u 枚で打ち切り）",
                         (unsigned)count);
                break;
            }
        } else {
            stale_rounds = 0;
        }
        // 次のラウンドで root を再 probe（capture で隠れていた札を拾う）。
    }
    if (probes >= PN5180_FAST_MAX_PROBES && count < PN5180_MAX_CARDS_PER_READER) {
        // 打ち切り（ノイズ or 想定より多い枚数）。取れた分だけ返し、残りは次の poll に任せる。
        ESP_LOGD(TAG, "fast inventory: probe 上限 %d に到達（%u 枚取得、未探索が残っている可能性）",
                 PN5180_FAST_MAX_PROBES, (unsigned)count);
    }

    // ── quiet 解除のため必ず RF を落とす ──
    // Stay Quiet で黙った札は磁界が消えるまで黙ったまま（ISO/IEC 15693-3: quiet state は RF off で
    // reset）。次の poll でまた全枚数を数えるには、この reader を読み終えた時点で必ず off が要る。
    // よって PN5180_RF_OFF_BETWEEN_READERS=0（RF 時分割 off = A/B 用）でも fast 経路は off する。
    // poll 側の rf_off_after_read() と二重になり得るが、ドライバの setRF_off は RF_STATUS が
    // 既に off なら即 true を返すので無害。
    pn5180_setRF_off(dev);
    s_fast_last_probes = probes;
    return count;
}
#endif  // PN5180_FAST_INVENTORY

// カード presence の保持サイクル数（debounce）。ISO15693 の inventory は単発で取りこぼすことが
// あり、保持が無いと host(pyscard)の IccPowerOn がちょうど取りこぼしポーリングに当たった瞬間に
// connect 失敗し、UID が一切取れない（probe_pcsc watch が 0 件になる主因）。一度検出したら
// この回数だけは present を維持し、連続 miss が超えたときだけ離脱と判定する。
//
// hold は **UID 単位**（slot 単位ではない）。slot 単位だと「検出 0 枚のときだけ前回集合を保つ」
// ことしかできず、2 枚中 1 枚を 1 回取りこぼしただけで集合が丸ごと 1 枚に置き換わる。実機
// （2026-09-10, 2 枚重ね）で host に届く枚数が 2↔1 と数百 ms 周期で揺れ、`watch` が同じ札を
// 何度も再発火した。UID ごとに miss を数えれば、欠けた 1 枚だけを数サイクル保持できる。
#define PRESENCE_HOLD_MISSES 3
// s_cache[i].uids[k] と添字が対応する連続 miss 数（0 = 今回検出）。
static uint8_t s_uid_miss[CCID_SLOT_COUNT][PN5180_MAX_CARDS_PER_READER];
// 「検出枚数 > PN5180_MAX_CARDS_PER_READER」の WARN を slot ごと 1 回に絞るフラグ。
static bool s_overflow_warned[CCID_SLOT_COUNT];

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

// ── 検出した UID 群の並びを正規化（memcmp 昇順）──
// DFS の探索順や AGC 順のままだと、同じ 2 枚でも poll ごとに並びが入れ替わり得る。CCID の
// Get UID は UID を連結して返すので、並びが揺れると host 側が「別の組み合わせ」と誤認する。
// 枚数は最大 PN5180_MAX_CARDS_PER_READER（=4）なので挿入ソートで十分。
// miss が非 NULL なら miss[] も同じ置換で並べ替える（UID 単位 hold のカウンタを連れて動かす）。
static void sort_uids(uint8_t uids[][16], uint8_t *miss, uint8_t count, uint8_t uid_len) {
    for (uint8_t a = 1; a < count; a++) {
        uint8_t tmp[16];
        memcpy(tmp, uids[a], sizeof(tmp));
        const uint8_t tmp_miss = miss ? miss[a] : 0;
        int b = (int)a - 1;
        while (b >= 0 && memcmp(uids[b], tmp, uid_len) > 0) {
            memcpy(uids[b + 1], uids[b], sizeof(tmp));
            if (miss) miss[b + 1] = miss[b];
            b--;
        }
        memcpy(uids[b + 1], tmp, sizeof(tmp));
        if (miss) miss[b + 1] = tmp_miss;
    }
}

// キャッシュ 2 つが「同じカード集合」か（枚数・長さ・UID がすべて一致）。ログの発火判定に使う。
static bool cards_equal(const pn5180_card_t *a, const pn5180_card_t *b) {
    if (a->present != b->present || a->count != b->count || a->uid_len != b->uid_len) return false;
    for (uint8_t k = 0; k < a->count; k++) {
        if (memcmp(a->uids[k], b->uids[k], a->uid_len) != 0) return false;
    }
    return true;
}

// UID 集合を "E0:04:…, E0:04:…" の 1 行に整形する（ログ用）。
static void format_uids(const pn5180_card_t *c, char *buf, size_t buf_size) {
    size_t p = 0;
    buf[0] = '\0';
    for (uint8_t k = 0; k < c->count; k++) {
        if (k > 0 && p + 2 < buf_size) {
            p += (size_t)snprintf(buf + p, buf_size - p, ", ");
        }
        for (uint8_t b = 0; b < c->uid_len && b < 16; b++) {
            if (p + 4 >= buf_size) return;  // 溢れたら打ち切り（snprintf の戻り値を足さない）
            p += (size_t)snprintf(buf + p, buf_size - p, "%02X%s",
                                  c->uids[k][b], (b + 1 < c->uid_len) ? ":" : "");
        }
    }
}

// ── 検出集合と前回集合を UID 単位でマージする（presence hold）──
// prev（前回 cache）と det（今回の検出、反転・ソート済み）を突き合わせ、
//   - prev にあり det にもある UID → miss=0 で残す
//   - prev にあり det に無い UID   → miss++。PRESENCE_HOLD_MISSES 未満なら残す（hold）
//   - det にあり prev に無い UID   → miss=0 で追加（満杯なら hold 中の最古を追い出す）
// を行い、結果を out に書く。戻り値 = hold している（今回検出されなかった）枚数。
// s_uid_miss[slot] は out の並びに合わせて書き換える。
static uint8_t merge_presence(int slot, const pn5180_card_t *prev,
                              const uint8_t det[][16], uint8_t det_count, uint8_t det_len,
                              pn5180_card_t *out) {
    uint8_t *miss = s_uid_miss[slot];
    uint8_t nu[PN5180_MAX_CARDS_PER_READER][16];
    uint8_t nm[PN5180_MAX_CARDS_PER_READER];
    uint8_t n = 0;
    uint8_t held = 0;

    // UID 長が変わった（15693 8B ↔ 14443 4/7B）ときは cache が長さを 1 つしか持てないので、
    // 前回集合は引き継がずに今回の検出だけにする。
    const bool keep_prev = (prev->count == 0) || (det_count == 0) || (prev->uid_len == det_len);
    const uint8_t len = det_count ? det_len : prev->uid_len;

    if (keep_prev) {
        for (uint8_t k = 0; k < prev->count; k++) {
            bool seen = false;
            for (uint8_t j = 0; j < det_count; j++) {
                if (memcmp(det[j], prev->uids[k], len) == 0) { seen = true; break; }
            }
            uint8_t m = 0;
            if (!seen) {
                m = (uint8_t)(miss[k] + 1);
                if (m >= PRESENCE_HOLD_MISSES) continue;  // hold 切れ = この 1 枚だけ離脱
                held++;
            }
            memcpy(nu[n], prev->uids[k], sizeof(nu[n]));
            nm[n] = m;
            n++;
        }
    }

    for (uint8_t j = 0; j < det_count; j++) {
        bool dup = false;
        for (uint8_t k = 0; k < n; k++) {
            if (memcmp(nu[k], det[j], len) == 0) { dup = true; break; }
        }
        if (dup) continue;
        if (n < PN5180_MAX_CARDS_PER_READER) {
            memcpy(nu[n], det[j], sizeof(nu[n]));
            nm[n] = 0;
            n++;
            continue;
        }
        // 満杯: hold 中（miss>0）で最も古いものを追い出して「今ある札」を優先する。
        int victim = -1;
        uint8_t worst = 0;
        for (uint8_t k = 0; k < n; k++) {
            if (nm[k] > worst) { worst = nm[k]; victim = (int)k; }
        }
        if (victim < 0) {
            // 全部が「今回検出」= 重ね置きが上限を超えている（運用/設定の問題）。
            if (!s_overflow_warned[slot]) {
                s_overflow_warned[slot] = true;
                ESP_LOGW(TAG, "reader %d: 検出 %u 枚が上限 %d 枚を超過 — 超過分は無視"
                              "（PN5180_MAX_CARDS_PER_READER を見直す）",
                         slot, (unsigned)det_count, PN5180_MAX_CARDS_PER_READER);
            }
            continue;
        }
        memcpy(nu[victim], det[j], sizeof(nu[victim]));
        nm[victim] = 0;
        held--;  // 追い出したのは hold 中の 1 枚
    }

    sort_uids(nu, nm, n, len);

    memset(out, 0, sizeof(*out));
    out->present = (n > 0);
    out->count = n;
    out->uid_len = n ? len : 0;
    for (uint8_t k = 0; k < n; k++) memcpy(out->uids[k], nu[k], sizeof(out->uids[k]));
    for (uint8_t k = 0; k < PN5180_MAX_CARDS_PER_READER; k++) miss[k] = (k < n) ? nm[k] : 0;
    return held;
}

#if PN5180_RF_OFF_BETWEEN_READERS
// ── RF 時分割: inventory 直後に磁界を落とす（同時 RF ON は 1 台だけ）──
// ドライバの get_all_uids() は RF を ON のまま戻るため、明示的に切らないと 11 台ぶんの磁界が
// 重なる（干渉 + 電流）。理由の詳細は app_config.h の PN5180_RF_OFF_BETWEEN_READERS。
// 失敗（SPI/BUSY 不調）は毎 poll 出すと UART を埋めるので reader ごと最初の 3 回だけ WARN。
static uint8_t s_rf_off_fail[CCID_SLOT_COUNT];

static void rf_off_after_read(int i) {
    if (pn5180_setRF_off(s_readers[i].dev)) return;
    if (s_rf_off_fail[i] < 3) {
        s_rf_off_fail[i]++;
        ESP_LOGW(TAG, "pn5180_setRF_off 失敗 reader %d (%u 回目) — 磁界が ON のまま次の reader へ",
                 i, (unsigned)s_rf_off_fail[i]);
    } else {
        ESP_LOGD(TAG, "pn5180_setRF_off 失敗 reader %d", i);
    }
}
#endif

#if POLL_STATS_INTERVAL_MS > 0
// ── poll 周期の統計（13 台化したときの 1 周時間を実測する）──
// 窓（POLL_STATS_INTERVAL_MS）ごとに min/avg/max と「最長 reader」を出してリセットする。
static int64_t s_stats_window_start_us;  // 窓の開始時刻（0 = 未初期化）
static int64_t s_stats_min_us;
static int64_t s_stats_max_us;
static int64_t s_stats_sum_us;
static int64_t s_stats_worst_us;         // 窓内で最も遅かった 1 reader の所要時間
static int s_stats_worst_idx;
static int s_stats_max_probes;           // 窓内で reader 1 台が使った probe 回数の最大（fast 経路）
static int s_stats_cycles;
#endif

void pn5180_reader_poll_once(void) {
#if POLL_STATS_INTERVAL_MS > 0
    const int64_t cycle_start_us = esp_timer_get_time();
    int64_t cycle_worst_us = -1;
    int cycle_worst_idx = -1;
    int ready_slots = 0;
    int cycle_max_probes = 0;  // この周で最も probe を使った reader の回数（fast 経路のみ。0 = ドライバ経路）
#endif

    for (int i = 0; i < CCID_SLOT_COUNT; i++) {
        // 未通電で init を飛ばした slot（dev=NULL）は触らない。cache は present=false のままなので
        // host には「カード無し（Get UID → 6A 81）」に見える。
        if (!s_readers[i].dev) continue;
#if POLL_STATS_INTERVAL_MS > 0
        const int64_t reader_start_us = esp_timer_get_time();
        ready_slots++;
#endif
        mux_select(s_readers[i].mux_ch);  // この reader の BUSY を SIG に

        uint8_t uids[PN5180_MAX_CARDS_PER_READER][16];
        uint8_t len = 0;
        uint8_t count = 0;
        bool from_iso15693 = false;

        // ドライバ既定の timeout_ms=500 は SPI BUSY 待ち / transceive 状態待ち / RF off 待ちの
        // すべてに効くため、1 台の不調が 1 周を 0.5 秒伸ばす。読み取りの間だけ短くする
        // （ドライバの get_all_uids も内部で 40ms に落としている）。
        const int64_t saved_timeout_ms = s_readers[i].dev->timeout_ms;
#if PN5180_FAST_INVENTORY
        s_readers[i].dev->timeout_ms = PN5180_FAST_OP_TIMEOUT_MS;
        // 自前の mask DFS（ISO15693 専用。PN5180_TRY_ISO14443 はこの経路では無視）。
        count = fast_inventory_15693(&s_readers[i], uids, &len);
        from_iso15693 = (count > 0);
#if POLL_STATS_INTERVAL_MS > 0
        if (s_fast_last_probes > cycle_max_probes) cycle_max_probes = s_fast_last_probes;
#endif
#else
        // ISO15693（8B）→（PN5180_TRY_ISO14443=1 のときだけ）ISO14443A（4/7B）の順。
        // proto を分けて試すのは、ISO15693 のときだけ MSB-first に反転するため。
        count = read_uids_from_proto(s_readers[i].iso15693, uids, &len);
        from_iso15693 = (count > 0);
#if PN5180_TRY_ISO14443
        if (count == 0) {
            count = read_uids_from_proto(s_readers[i].iso14443, uids, &len);
        }
#endif
#endif

#if PN5180_RF_OFF_BETWEEN_READERS
        // inventory 直後に磁界を落とす（次の reader を読む前に = 同時 RF ON は 1 台だけ）。
        rf_off_after_read(i);
#endif
        s_readers[i].dev->timeout_ms = saved_timeout_ms;

        // 契約 v1.1 §7: ISO15693 の生バイトは LSB-first なので MSB-first に反転する。
        if (from_iso15693 && len > 1) {
            for (uint8_t k = 0; k < count; k++) reverse_bytes(uids[k], len);
        }
        // 反転後に並びを正規化（同じ組み合わせなら毎回同じ順序 = host の差分判定が安定する）。
        sort_uids(uids, NULL, count, len);

        // 前回集合と UID 単位でマージ（欠けた 1 枚だけを数サイクル hold する）。
        const pn5180_card_t prev = s_cache[i];  // 書き手はこの poll task だけなので lock 不要
        pn5180_card_t c;
        const uint8_t held = merge_presence(i, &prev, (const uint8_t (*)[16])uids, count, len, &c);

        // カード集合が変化した時だけログ（毎ポーリングのスパムを避ける）。枚数や UID の
        // 差し替え（1 枚 → 2 枚、flop の追加など）も「変化」として出す。
        if (!cards_equal(&prev, &c)) {
            if (c.present) {
                char list[PN5180_MAX_CARDS_PER_READER * (3 * 16 + 2) + 1];
                format_uids(&c, list, sizeof(list));
                char hold_note[16];
                hold_note[0] = '\0';
                if (held) snprintf(hold_note, sizeof(hold_note), " (hold %u)", (unsigned)held);
                ESP_LOGI(TAG, "🎴 reader %d: %u 枚 [%s] (%uB/枚)%s",
                         i, (unsigned)c.count, list, (unsigned)c.uid_len, hold_note);
            } else {
                ESP_LOGI(TAG, "   カード離脱 reader %d", i);
            }
        }

        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_cache[i] = c;
        xSemaphoreGive(s_lock);

#if POLL_STATS_INTERVAL_MS > 0
        const int64_t reader_us = esp_timer_get_time() - reader_start_us;
        if (reader_us > cycle_worst_us) {
            cycle_worst_us = reader_us;
            cycle_worst_idx = i;
        }
#endif
    }

#if POLL_STATS_INTERVAL_MS > 0
    const int64_t cycle_us = esp_timer_get_time() - cycle_start_us;
    if (s_stats_cycles == 0) {  // 窓の最初の 1 周で初期化（min/max の種）
        s_stats_min_us = cycle_us;
        s_stats_max_us = cycle_us;
        s_stats_sum_us = 0;
        s_stats_worst_us = -1;
        s_stats_worst_idx = -1;
        s_stats_max_probes = 0;
    }
    if (cycle_us < s_stats_min_us) s_stats_min_us = cycle_us;
    if (cycle_us > s_stats_max_us) s_stats_max_us = cycle_us;
    if (cycle_worst_us > s_stats_worst_us) {
        s_stats_worst_us = cycle_worst_us;
        s_stats_worst_idx = cycle_worst_idx;
    }
    s_stats_sum_us += cycle_us;
    s_stats_cycles++;
    if (cycle_max_probes > s_stats_max_probes) s_stats_max_probes = cycle_max_probes;

    const int64_t now_us = esp_timer_get_time();
    if (s_stats_window_start_us == 0) s_stats_window_start_us = now_us;  // 初回だけ窓を開始
    if (now_us - s_stats_window_start_us >= (int64_t)POLL_STATS_INTERVAL_MS * 1000) {
        const int64_t avg_us = s_stats_sum_us / s_stats_cycles;  // cycles >= 1
        ESP_LOGI(TAG,
                 "poll 統計(直近 %d 周): 1 周 min/avg/max = %lld/%lld/%lld ms, "
                 "最長 reader #%d (slot %d) = %lld ms, probe 最大 %d 回/reader, ready %d slot",
                 s_stats_cycles,
                 (long long)(s_stats_min_us / 1000), (long long)(avg_us / 1000),
                 (long long)(s_stats_max_us / 1000),
                 s_stats_worst_idx + 1, s_stats_worst_idx,
                 (long long)(s_stats_worst_us / 1000), s_stats_max_probes, ready_slots);
        s_stats_cycles = 0;             // 次の窓へ（min/max/sum は次の 1 周で初期化）
        s_stats_window_start_us = now_us;
    }
#endif
}

bool pn5180_reader_get_card(uint8_t slot, pn5180_card_t *out) {
    if (slot >= CCID_SLOT_COUNT || !out) return false;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    *out = s_cache[slot];
    xSemaphoreGive(s_lock);
    return true;
}
