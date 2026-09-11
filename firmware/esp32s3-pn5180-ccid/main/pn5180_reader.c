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

// RX_STATUS(0x13) の衝突位置フィールド（bits 25:19 = 受信フレーム内で最初に衝突したビット位置
// 0..127）。ドライバの public ヘッダに入っているが、版によっては無いことがあるので保険で定義する
// （値は PN5180 データシート。ヘッダ側にあればそちらが優先される）。
#ifndef RX_COLL_POS_START
#  define RX_COLL_POS_START 19
#endif
#ifndef RX_COLL_POS_MASK
#  define RX_COLL_POS_MASK 0x7F
#endif

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

// 物理 reader 数は配線表 PN5180_READERS の要素数を超えられない（超えると配列外参照）。
// USB 上の CCID slot は常に 1（ADR-0041）で、物理 reader は Get UID の P2 index で選ぶ。
// メッセージは ASCII 固定（gcc の診断が非 ASCII を 8 進エスケープして読めなくなるため）。
_Static_assert(PN5180_READER_COUNT >= 1 &&
                   PN5180_READER_COUNT <= (int)(sizeof(PN5180_READERS) / sizeof(PN5180_READERS[0])),
               "PN5180_READER_COUNT must be 1..N of PN5180_READERS: extend the wiring table in app_config.h first");

typedef struct {
    pn5180_t *dev;               // NULL = 未通電などで init を飛ばした reader（常にカード無し扱い）
    pn5180_proto_t *iso14443;
    pn5180_proto_t *iso15693;
    int mux_ch;
    bool rf_loaded;              // fast 経路: LOAD_RF_CONFIG 済み（false なら poll で再試行）
} slot_reader_t;

static slot_reader_t s_readers[PN5180_READER_COUNT];
static pn5180_card_t s_cache[PN5180_READER_COUNT];
static SemaphoreHandle_t s_lock;
// 起動時の RST 診断（reader 0）で「RST 中に BUSY=High」が観測できたか。false なら RST がその chip に
// 届いていない疑い（init 失敗診断の判定文に使う, ISSUE-0023）。
static bool s_rst_seen_high;

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

#if PN5180_READER_COUNT == 1
// ── bring-up 用: 通電中の ch から使う reader を自動選択 ──
// 1 台だけ繋いで検証するとき、どのコネクタ（= MUX ch）に挿さっているかは日によって変わる
// （実機で ch12 → ch7 に変わり、設定固定だと init 失敗 → 再ビルドが必要だった）。MUX scan で
// Low 駆動＝通電 idle の PN5180 がいる ch を見つけ、テーブルからその ch の nss を引いて使う。
//   - 設定 [0] の ch が通電中ならそのまま。
//   - 通電 ch が別にあればそれ（複数なら最小番号）。
//   - 全 floating なら [0] にフォールバック（init は失敗するが診断ログは出る）。
// 本番（PN5180_READER_COUNT=11）ではテーブル順 = reader index 順なので自動選択はしない。
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

#if PN5180_READER_COUNT > 1
// ── 配線チェック（複数 reader 時、init の前に 1 回）──
// MUX scan の「通電マスク」と app_config.h の設定（PN5180_READERS[0..PN5180_READER_COUNT-1].mux_ch）を
// 突き合わせ、食い違いをログに出す。実機で「挿し忘れ / コネクタ違い / 台数不足」を即座に見分ける。
//   (1) 設定 ch なのに floating   → その reader は未通電/未接続。init を飛ばして skip する。
//   (2) 通電しているのに設定範囲外 → 繋いだのに PN5180_READER_COUNT が足りない、または配線表とのズレ。
static void diag_wiring_map(uint16_t low_mask) {
    const int n_table = (int)(sizeof(PN5180_READERS) / sizeof(PN5180_READERS[0]));
    uint16_t cfg_mask = 0;

    // (1) 設定した ch のうち floating のもの。
    char missing[128];
    missing[0] = '\0';
    for (int i = 0; i < PN5180_READER_COUNT; i++) {
        const int ch = PN5180_READERS[i].mux_ch;
        cfg_mask |= (uint16_t)(1u << ch);
        if (low_mask & (uint16_t)(1u << ch)) continue;
        const size_t used = strlen(missing);
        snprintf(missing + used, sizeof(missing) - used, "%s#%d(ch%d)",
                 used ? ", " : "", i + 1, ch);
    }
    if (missing[0]) {
        ESP_LOGW(TAG, "配線チェック: 設定 ch なのに floating（未通電/未接続）= %s → この reader は skip",
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
        ESP_LOGW(TAG, "配線チェック: 通電しているが設定範囲外の ch = %s → PN5180_READER_COUNT(%d) を増やすか配線表を確認",
                 extra, PN5180_READER_COUNT);
    }

    if (!missing[0] && !extra[0]) {
        ESP_LOGI(TAG, "配線 OK: 設定 %d ch すべて通電（設定範囲外の通電 ch も無し）", PN5180_READER_COUNT);
    }
}
#endif

// ── 1 本の NSS に低レベル SPI で READ_EEPROM(FIRMWARE_VERSION) を送る（BUSY 非依存）──
// 共有 SPI とは別に device を一時 add/remove する。BUSY ハンドシェイクの代わりに **固定待ち 1ms** を
// 置き、MISO に意味のある値（`FF FF`=floating / `00 00`=Low 固定 のいずれでもない）が返るかで
// 「chip が生きているか」を判定する。BUSY が壊れていても必ず SPI を送るのが要点。
// 共有 RST を叩くので **全 chip がリセットされる** → 呼ぶのは「init ループの後・RF config ロードの前」
// （または init 失敗直後）に限る。
// 戻り値: MISO に意味のある値が返った（= chip 生存）。fw_out に受信 2 byte。
static bool spi_probe_nss(int nss, int mux_ch, uint8_t fw_out[2],
                          int *busy_before_out, bool *went_high_out) {
    gpio_set_direction(PN5180_PIN_RST, GPIO_MODE_OUTPUT);
    gpio_set_level(PN5180_PIN_RST, 0);
    esp_rom_delay_us(1000);
    gpio_set_level(PN5180_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    mux_select(mux_ch);

    // 送信前の BUSY。High（floating/stuck）なら「送信で High に立った」と区別できないので、
    // その場合は BUSY 判定を諦める（= 誤検出して再 init しない。実機で ch12 が浮いていて
    // GPIO1 を誤検出しクラッシュした反省）。SPI 自体は送る。
    const int busy_before = gpio_get_level(BUSY_PIN);
    bool went_high = false;
    fw_out[0] = 0xFF;
    fw_out[1] = 0xFF;

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 1000000,
        .mode = 0,
        .spics_io_num = nss,
        .queue_size = 1,
    };
    spi_device_handle_t dev = NULL;
    if (spi_bus_add_device(PN5180_SPI_HOST, &devcfg, &dev) != ESP_OK) {
        if (busy_before_out) *busy_before_out = busy_before;
        if (went_high_out) *went_high_out = false;
        return false;
    }

    // PN5180 の SPI は 2 フェーズ:「送信 = NSS↓ コマンド NSS↑」→「受信 = NSS↓ 読み出し NSS↑」。
    // hardware CS では 1 トランザクション = 1 フェーズなので 2 回に分ける。
    // 送信フェーズ: READ_EEPROM(0x07) + addr 0x12(FIRMWARE_VERSION) + len 2。
    uint8_t tx_cmd[3] = {0x07, 0x12, 0x02};
    spi_transaction_t t_cmd = {.length = 8 * sizeof(tx_cmd), .tx_buffer = tx_cmd};
    spi_device_polling_transmit(dev, &t_cmd);

    // 固定待ち 1ms（BUSY ハンドシェイクの代わり）。ついでに BUSY の立ち上がりも観る
    // （送信前が Low だった場合だけ意味がある）。
    for (int j = 0; j < 500; j++) {
        if (!busy_before && gpio_get_level(BUSY_PIN)) went_high = true;
        esp_rom_delay_us(2);
    }

    // 受信フェーズ: 2 byte 読み出し（PN5180 は MOSI を無視して MISO に載せる）。
    uint8_t tx_dummy[2] = {0xFF, 0xFF};
    spi_transaction_t t_rd = {.length = 16, .tx_buffer = tx_dummy, .rx_buffer = fw_out};
    spi_device_polling_transmit(dev, &t_rd);
    esp_rom_delay_us(1000);
    spi_bus_remove_device(dev);

    if (busy_before_out) *busy_before_out = busy_before;
    if (went_high_out) *went_high_out = went_high;
    // 全 FF = pull-up で浮いている / 全 00 = Low 固定。どちらも「応答ではない」。
    const bool miso_floating = (fw_out[0] == 0xFF && fw_out[1] == 0xFF);
    const bool miso_zero = (fw_out[0] == 0x00 && fw_out[1] == 0x00);
    return !miso_floating && !miso_zero;
}

// ── skip した reader の chip 生存確認（BUSY 非依存, ISSUE-0023）──
// BUSY が floating で skip した reader について「**電源が来ていない**のか **BUSY 線だけが切れている**のか」
// を切り分ける。実機 2026-09-11 で reader を別コネクタに挿し替えたら floating が付いてきた
// （= reader 側の故障）が、そこから先（電源か BUSY 線か）は手で当たるしかなかったので自動化する。
// 呼ぶのは init ループの後・RF config ロードの前（spi_probe_nss が共有 RST を叩くため）。
static void diag_skipped_reader(int idx, const pn5180_reader_cfg_t *cfg) {
    uint8_t fw[2];
    int busy_before = 0;
    bool went_high = false;
    const bool alive = spi_probe_nss(cfg->nss, cfg->mux_ch, fw, &busy_before, &went_high);
    if (alive) {
        ESP_LOGW(TAG,
                 "reader #%d (ch%d) の chip 生存確認: FW=%02X %02X = **chip は生きている** → "
                 "BUSY 線だけが不通（この reader の BUSY ピン/圧着/コネクタ ch%d の BUSY を確認）",
                 idx + 1, cfg->mux_ch, fw[0], fw[1], cfg->mux_ch);
    } else {
        ESP_LOGW(TAG,
                 "reader #%d (ch%d) の chip 生存確認: FW=%02X %02X = SPI 無応答 → "
                 "この reader の電源(3.3V/5V)/GND か SPI 線(SCK/MOSI/MISO)/chip 個体を疑う",
                 idx + 1, cfg->mux_ch, fw[0], fw[1]);
    }
}

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
        uint8_t fw[2];
        int busy_before = 0;
        bool went_high = false;
        // 対象 chip の ch を選んだまま、候補 NSS で SPI を 1 発送る（BUSY 非依存）。
        const bool miso_active = spi_probe_nss(try_nss, cfg->mux_ch, fw, &busy_before, &went_high);
        const bool miso_floating = (fw[0] == 0xFF && fw[1] == 0xFF);
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
            if (!s_rst_seen_high) {
                // chip は SPI に答える（生きている）のに RST 中に BUSY が High にならなかった =
                // RST がこの chip に届いていない可能性が高い（コネクタの RST ピン / 配線, ISSUE-0023）。
                ESP_LOGW(TAG, "   → RST 診断(reader 0) が during_rst=0: chip がリセットに反応していない。"
                              "この reader のコネクタの RST ピン/配線の不通を疑う（ISSUE-0023）");
            }
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

// ── 配線表の全 NSS（13 本）を High に固定する（init の前に 1 回）──
// SPI バスは全 reader 共有なので、通電しているのに init していない chip の NSS が floating だと、
// その chip が「選択された」と解釈して MISO を駆動し、init 中の reader の応答と衝突し得る。
// 該当するのは (a) まだ init 順が来ていない reader、(b) 未通電判定で skip した reader、
// (c) 配線表にあるが PN5180_READER_COUNT の範囲外の予備（#12/#13）に挿された reader。
// 実機 2026-09-11（10 台接続、#12 にも 1 台）で reader 0 の FIRMWARE_VERSION 読みが FF FF になり
// init 失敗した（ISSUE-0023）。原因の確定はしていないが、NSS を全部 High にしておけば
// この経路の衝突は起きないので、範囲外の予備も含めて先に deselect する。
static void nss_deselect_all(void) {
    const int n = (int)(sizeof(PN5180_READERS) / sizeof(PN5180_READERS[0]));
    for (int i = 0; i < n; i++) {
        gpio_set_direction(PN5180_READERS[i].nss, GPIO_MODE_OUTPUT);
        gpio_set_level(PN5180_READERS[i].nss, 1);
    }
}

// ── 失敗した pn5180_init の後始末: 共有 SPI を作り直す ──
// ドライバの失敗経路 pn5180_deinit(ret, false) は **共有 SPI device を外し、pn5180_spi_t も free**
// する（バス自体は残る）。そのままでは、すでに ready の reader（dev->spi が dangling）も
// 次の reader も使えない。バスを解放して pn5180_spi_init をやり直し、ready 済み reader の
// dev->spi を新しい構造体に差し替える（pn5180_t は公開 struct）。これで
// 「失敗した 1 台だけ skip して他は続行」が可能になる。
static pn5180_spi_t *spi_recreate_after_init_failure(void) {
    spi_bus_free(PN5180_SPI_HOST);  // 共有 device は deinit が外し済みなので解放できる
    pn5180_spi_t *spi = pn5180_spi_init(PN5180_SPI_HOST, PN5180_PIN_SCK,
                                        PN5180_PIN_MISO, PN5180_PIN_MOSI,
                                        PN5180_SPI_HZ);
    if (!spi) {
        ESP_LOGE(TAG, "共有 SPI の作り直しに失敗（spi_bus_initialize / add_device）");
        return NULL;
    }
    for (int j = 0; j < PN5180_READER_COUNT; j++) {
        if (s_readers[j].dev) s_readers[j].dev->spi = spi;
    }
    return spi;
}

bool pn5180_reader_init(void) {
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return false;
    memset(s_cache, 0, sizeof(s_cache));
    mux_init();
    nss_deselect_all();  // 全 chip を deselect してから SPI/RST に触る（ISSUE-0023）

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
#if PN5180_READER_COUNT == 1
    cfg0 = select_bringup_reader(low_mask, cfg0);
#endif
    // PN5180_READER_COUNT > 1 では自動選択せず配列順 = reader index 順。low_mask は下の配線チェックと
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
    s_rst_seen_high = (busy_during_rst == 1);
    if (busy_during_rst == 0 && busy_after_boot == 0) {
        // BUSY が Low に引かれている（chip は通電）のに RST 中に High にならない = RST が届いていない
        // 可能性。後の NSS スキャンで「BUSY Low→High:YES」なら chip 自体は生きている（ISSUE-0023）。
        ESP_LOGW(TAG, "  → during_rst=0 かつ after=0: RST がこの chip に届いていない可能性"
                      "（コネクタの RST ピン/配線）。SPI に応答するなら chip は生きている（ISSUE-0023）");
    }

    gpio_set_pull_mode(BUSY_PIN, GPIO_PULLUP_ONLY);  // MUX 弱駆動でも level 確定（driver にも有効）

    // SPI バスは全 reader 共有（pn5180_spi_init の引数順は host, SCK, MISO, MOSI, freq）。
    pn5180_spi_t *spi = pn5180_spi_init(PN5180_SPI_HOST, PN5180_PIN_SCK,
                                        PN5180_PIN_MISO, PN5180_PIN_MOSI,
                                        PN5180_SPI_HZ);
    if (!spi) {
        ESP_LOGE(TAG, "pn5180_spi_init failed");
        return false;
    }

#if PN5180_READER_COUNT > 1
    diag_wiring_map(low_mask);  // 設定 ch と通電 ch の食い違いを先に見せる
#endif

    int ready_count = 0;
    char skipped[160];  // 未通電 / init 失敗で飛ばした reader の一覧（起動要約に出す）
    skipped[0] = '\0';
    bool init_diag_done = false;  // 深掘り診断は起動につき 1 回だけ

    for (int i = 0; i < PN5180_READER_COUNT; i++) {
        // [0] は bring-up 自動選択の結果（本番 PN5180_READER_COUNT=11 では = PN5180_READERS[0]）。
        const pn5180_reader_cfg_t *cfg = (i == 0) ? cfg0 : &PN5180_READERS[i];
        s_readers[i].mux_ch = cfg->mux_ch;
        s_readers[i].dev = NULL;

#if PN5180_READER_COUNT > 1
        // 未通電/未接続（BUSY が floating）の reader は pn5180_init を呼ばずに飛ばす。
        // 呼んでしまうと失敗経路のドライバ deinit が **共有 SPI device を解放** し、他の生きている
        // reader まで巻き添えで死ぬ（後述）。飛ばした reader は dev=NULL のまま = 常にカード無し。
        if (!(low_mask & (uint16_t)(1u << cfg->mux_ch))) {
            ESP_LOGW(TAG,
                     "reader #%d (index %d, nss=GPIO%d, ch%d) は未通電/未接続 → skip"
                     "（host の Get UID P2=%d は常に SW=6A81）",
                     i + 1, i, cfg->nss, cfg->mux_ch, i);
            const size_t used = strlen(skipped);
            // `#k` = index+1（席番号 / board）、`chN` = コネクタ（配線表は #4 を飛ばしているので別物）。
            snprintf(skipped + used, sizeof(skipped) - used, "%s#%d(ch%d)", used ? ", " : "", i + 1,
                     cfg->mux_ch);
            continue;
        }
#endif

        mux_select(cfg->mux_ch);  // この reader の BUSY を SIG に出してから init

        // busy = BUSY_PIN（MUX SIG or 直結, 共有）, rst = 共有, nss = reader 個別。
        s_readers[i].dev = pn5180_init(spi, cfg->nss, BUSY_PIN, PN5180_PIN_RST);
        if (!s_readers[i].dev) {
            // 通電しているのに応答しない = 配線/NSS ミス・個体不良・RST 不通の類。
            // ドライバの失敗経路は共有 SPI（device + pn5180_spi_t）を壊すので、作り直してから
            // **1 回だけ再試行**する。RST が届いていない reader は ESP32 の再起動を跨いで前回の
            // 途中状態（応答待ち）のまま残り、最初の 1 発だけ噛み合わないことがある
            // （実機 2026-09-11: init の 1 発目は FF FF、直後の診断の READ_EEPROM には応答, ISSUE-0023）。
            ESP_LOGW(TAG, "pn5180_init reader %d failed (nss=%d busy=%d rst=%d mux_ch=%d via_mux=%d)"
                          " → 共有 SPI を作り直して 1 回だけ再試行",
                     i, cfg->nss, BUSY_PIN, PN5180_PIN_RST, cfg->mux_ch, PN5180_BUSY_VIA_MUX);
            spi = spi_recreate_after_init_failure();
            if (!spi) return false;
            vTaskDelay(pdMS_TO_TICKS(20));
            mux_select(cfg->mux_ch);
            s_readers[i].dev = pn5180_init(spi, cfg->nss, BUSY_PIN, PN5180_PIN_RST);
        }
        if (!s_readers[i].dev) {
            // 再試行も失敗: この reader だけ skip して他は続行する（dev=NULL = 常にカード無し）。
            // 共有 SPI をもう一度作り直さないと ready 済み reader と後続 reader が使えない。
            ESP_LOGE(TAG, "pn5180_init reader %d は再試行も失敗 → この reader は skip"
                          "（host の Get UID P2=%d は常に SW=6A81）。他の reader は続行",
                     i, i);
            spi = spi_recreate_after_init_failure();
            if (!spi) return false;
            // 深掘り診断（NSS スキャン等）は「まだ 1 台も ready でない」ときに 1 回だけ出す
            // （全台失敗のときに 11 回出して UART を埋めない）。診断は RST を叩き、自前の SPI device
            // を add/remove するだけなので、作り直した共有 SPI には影響しない。
            if (ready_count == 0 && !init_diag_done) {
                init_diag_done = true;
                diag_after_init_failure(cfg);
            }
            const size_t used = strlen(skipped);
            snprintf(skipped + used, sizeof(skipped) - used, "%s#%d(ch%d,init失敗)", used ? ", " : "",
                     i + 1, cfg->mux_ch);
            continue;
        }
        s_readers[i].iso14443 = pn5180_14443_init(s_readers[i].dev);
        // 第2引数は pn5180_15693_rf_config_t（pn5180->rf_config の初期値になるだけで、
        // ドライバ経路では get_all_uids が毎回上書きする）。fast 経路と同じ値を入れておく。
        s_readers[i].iso15693 = pn5180_15693_init(s_readers[i].dev, PN5180_FAST_RF_CONFIG);
        ready_count++;
        ESP_LOGI(TAG, "PN5180 reader %d ready (nss=%d mux_ch=%d)", i, cfg->nss, cfg->mux_ch);
    }

    if (ready_count == 0) {
        ESP_LOGE(TAG, "PN5180 ready 0 台（全 reader が未通電/未接続/init 失敗）— 電源・MUX・配線を確認");
        // 深掘り診断（BUSY 非依存の NSS スキャン）を出す。PN5180_READER_COUNT > 1 では全 reader が
        // MUX scan で skip されて pn5180_init を 1 度も呼ばないことがあり、ここで呼ばないと
        // 「チップが死んでいるのか BUSY/MUX 経路だけが壊れているのか」を切り分けられない。
        if (!init_diag_done) diag_after_init_failure(cfg0);
        return false;
    }

    // ── skip した reader の chip 生存確認（ISSUE-0023）──
    // 「電源が来ていない」のか「BUSY 線だけが切れている」のかを起動ログで切り分けられるようにする。
    // **RF config ロードの前**に置くのが要点（spi_probe_nss は共有 RST を叩くのでレジスタが消える）。
    for (int i = 0; i < PN5180_READER_COUNT; i++) {
        if (s_readers[i].dev) continue;
        const pn5180_reader_cfg_t *cfg = (i == 0) ? cfg0 : &PN5180_READERS[i];
        diag_skipped_reader(i, cfg);
    }

#if PN5180_FAST_INVENTORY
    // ── fast 経路の RF 設定を全 reader にロード（init ループの「後」で行うのが要点）──
    // pn5180_init() は内部で **共有 RST を pulse する**ので、reader k の init 中に reader k+1 の
    // チップもリセットされ、レジスタ（= LOAD_RF_CONFIG の内容）が消える。init ループの中で
    // ロードしても後続の init で無効になるため、全台の init が終わってから別ループでロードする。
    // 以降 RF を on/off しても設定は残る（リセットしない限り）。
    for (int i = 0; i < PN5180_READER_COUNT; i++) {
        if (!s_readers[i].dev) continue;
        mux_select(s_readers[i].mux_ch);
        s_readers[i].rf_loaded = pn5180_loadRFConfig(s_readers[i].dev, PN5180_FAST_RF_CONFIG);
        if (!s_readers[i].rf_loaded) {
            // init 自体は成功しているので reader は生かしたまま、poll 側で毎回ロードを再試行する。
            ESP_LOGE(TAG, "reader %d: LOAD_RF_CONFIG(0x%02X) 失敗 — poll で再試行する",
                     i, (unsigned)PN5180_FAST_RF_CONFIG);
        }
    }
    ESP_LOGI(TAG, "fast inventory 有効 (RF config=0x%02X, RX timeout=%d ms, max %d 枚/probe 上限 %d)",
             (unsigned)PN5180_FAST_RF_CONFIG, PN5180_FAST_RX_TIMEOUT_MS,
             PN5180_MAX_CARDS_PER_READER, PN5180_FAST_MAX_PROBES);
#endif

    ESP_LOGI(TAG, "PN5180 ready: %d/%d reader%s%s%s", ready_count, PN5180_READER_COUNT,
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
    PROBE_COLLISION,  // 衝突フラグあり = 複数枚が同時に応答した → mask を伸ばして分割する
    PROBE_NOISE,      // 衝突フラグ無しで壊れた受信（磁界の縁のノイズ）→ 同じ node を 1 回だけ再 probe
} probe_result_t;

// ── PROBE_COLLISION のときに拾う「衝突位置」情報（ISSUE-0021 実装 A）──
// ISO15693 の応答フレームは flags(8bit) DSFID(8bit) UID(64bit, **LSB-first**) なので
// **UID の bit i = フレームの bit 16+i**。衝突ビットより手前のビットは正しく受信できているため、
// mask を 1 bit ずつ伸ばさずに **衝突位置まで一気に伸ばせる**（空の兄弟枝を probe しなくなる）。
typedef struct {
    uint8_t pos;        // UID 内の衝突ビット位置（= RX_COLL_POS - 16）。**0xFF = 無効**
    uint8_t uid[8];     // 衝突ビットより前の UID 先頭部分（LSB-first、それ以降のビットは 0）
    uint16_t raw_pos;   // RX_COLL_POS の生値（ログ用。基準がフレーム先頭かを実機で見るため）
    uint16_t rx_bytes;  // 受信バイト数（ログ用）
} fast_coll_t;

// 衝突した応答から RX_COLL_POS と「衝突ビットより前の UID 先頭部分」を取り出す。
// 取り出せない（衝突が flags/DSFID 内 / 受信バイトが衝突ビットに届いていない / readData 失敗）
// ときは `coll->pos` を 0xFF のままにし、呼び側は従来どおり 1 bit だけ mask を伸ばす。
// 1 周のうち「応答が返ってきた probe の待ち時間」の最大値（µs, poll 統計用）。
// PN5180_FAST_RX_TIMEOUT_MS を下げる余地は「実機で応答が来るまでの最長」が分からないと決められない
// ので実測する。無応答の probe は timeout いっぱいなので数えない（ISSUE-0021）。
static int s_fast_rx_wait_max_us;

static void fast_fill_coll(pn5180_t *dev, uint32_t rs, uint32_t n, fast_coll_t *coll) {
    const uint32_t raw = (rs >> RX_COLL_POS_START) & RX_COLL_POS_MASK;
    coll->raw_pos = (uint16_t)raw;
    coll->rx_bytes = (uint16_t)n;
    if (raw < 16 || (raw - 16) >= 64) return;  // UID の外（flags/DSFID）で衝突 = 分割には使えない
    const uint32_t pos = raw - 16;
    // rx[0]=flags, rx[1]=DSFID, rx[2+]=UID。衝突ビットを含むバイトまで受信できていること。
    if (n < 2 + pos / 8 + 1) return;
    const uint32_t rd = (n > 10) ? 10 : n;  // 部分バイトも RX_STATUS の byte 数に含まれる
    uint8_t rx[10];
    if (!pn5180_readData(dev, (int)rd, rx)) return;
    memcpy(coll->uid, rx + 2, rd - 2);  // n >= 3 なので rd-2 は 1..8
    coll->pos = (uint8_t)pos;
}

// ISO15693 INVENTORY を 1 回だけ送り、応答を 3 値で返す（ドライバの総当たりをしない）。
// フレーム: flags(0x26 = high rate | inventory | 1 slot), INVENTORY(0x01), mask_len,
//           mask 値（ceil(mask_len/8) byte, **LSB-first**）
// mask は「UID の下位 mask_len ビット」と比較される。ISO15693 は UID を LSB から送るので、
// mask bit i = 受信生バイト rx[2] の bit0 から数えて i 番目のビット。
// coll（非 NULL）には PROBE_COLLISION のときだけ衝突位置情報を書く（無効なら pos=0xFF）。
static probe_result_t fast_probe_15693(pn5180_t *dev, uint64_t mask, uint8_t mask_len,
                                       uint8_t *uid_out, fast_coll_t *coll) {
    if (coll) {
        coll->pos = 0xFF;
        coll->raw_pos = 0;
        coll->rx_bytes = 0;
        memset(coll->uid, 0, sizeof(coll->uid));
    }
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
    const int64_t wait_start = esp_timer_get_time();
    // 上限 = **フレームの送信時間** + PN5180_FAST_RX_TIMEOUT_MS（応答ぶんの予算）。
    // 送信が終わるまで応答は来ないので、mask を長くした probe に固定値を使うと応答前に打ち切る。
    // 26.48 kbps・1 byte ≈ 0.30 ms なので、mask 0 の 5 byte(3+CRC2) ≈ 1.5 ms に対し
    // mask 64 bit の 13 byte ≈ 3.9 ms。**狙い撃ち probe が常に外れる**という罠になるため連動させる
    // （Stay Quiet 側も同じ 26.48 kbps でフレーム時間を見積もっている, ISSUE-0021）。
    const int tx_us = (int)((3 + nbytes + 2) * 8 * 1000000 / 26480);  // +2 = CRC
    const int64_t deadline = wait_start + tx_us + (int64_t)PN5180_FAST_RX_TIMEOUT_MS * 1000;
    for (;;) {
        irq = pn5180_getIRQStatus(dev);
        if (irq & (RX_IRQ_STAT | TIMER2_IRQ_STAT | GENERAL_ERROR_IRQ_STAT)) break;
        if (esp_timer_get_time() > deadline) break;
        esp_rom_delay_us(100);
    }
    if (!(irq & RX_IRQ_STAT)) return PROBE_NONE;  // 無応答 / RX timeout(TIMER2) / general error
    // **応答が来たときの待ち時間**を記録する（poll 統計に出す）。PN5180_FAST_RX_TIMEOUT_MS を
    // どこまで下げられるかは「実機で応答が来るまでの最長」が分からないと決められない。
    // 無応答（上の return）は timeout いっぱいなので測る意味がなく、除外する。
    {
        const int64_t waited = esp_timer_get_time() - wait_start;
        if (waited > s_fast_rx_wait_max_us) s_fast_rx_wait_max_us = (int)waited;
    }

    uint32_t rs = 0;
    if (!pn5180_readRegister(dev, RX_STATUS, &rs)) return PROBE_NONE;
    const uint32_t n = (rs >> RX_BYTES_RECEIVED_START) & RX_BYTES_RECEIVED_MASK;
    const bool collision = (rs & RX_COLLISION_DETECTED) != 0;

    // **衝突フラグが立っていれば受信バイト数に依らず分割する**。SOF で衝突すると
    // 「collision=1 / 受信 0 byte」で返ることがあり、下の n==0 を先に見ると衝突を
    // 「カード無し」に倒してしまう（= 重ね置きが永久に分離できない）。
    if (collision) {
        // 衝突位置まで mask を伸ばすための情報を取る（RX_COLL_POS が有効なときだけ）。
        if (coll) fast_fill_coll(dev, rs, n, coll);
        return PROBE_COLLISION;
    }

    // 受信バイト 0 で protocol/integrity error だけ = 応答の実体が無いノイズ（ドライバも
    // 0 byte + protocol error を noise と呼んでいる）→「カード無し」。
    if (n == 0) return PROBE_NONE;

    // 応答は flags(1) + DSFID(1) + UID(8) = 10 byte（CRC は PN5180 が検証して外す）。
    // 10 byte 以外 / **バイトはあるが CRC・protocol error** = 壊れた受信。**衝突フラグが
    // 立っていない**ので分割はしない（PROBE_NOISE = 呼び側が同じ node を 1 回だけ再 probe する）。
    //
    // ⚠ 以前はこれも COLLISION に倒していたが、実機（2026-09-10, commit 73ecd29, 1 枚だけ載せて
    //   カードを動かす）で **probe 最大 16 = 上限**・1 周 max 160 ms になった。カードが磁界の縁に
    //   あるとノイズ受信が続き、DFS が 2 分木を上限まで展開する（**ノイズは子枝でもノイズ**なので
    //   分割しても消えない）ため。本物の同時応答は実機では衝突フラグが立つ（2 枚・3 枚とも読めて
    //   いる）。稀に「フラグ無しで 2 枚が重なる」ケースがあっても、次の poll + UID 単位 hold +
    //   PN5180_FAST_CONFIRM_EVERY の再確認で回収する。
    if (n != 10 || (rs & (RX_PROTOCOL_ERROR | RX_DATA_INTEGRITY_ERROR))) {
        return PROBE_NOISE;
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
// 直近に読んだ reader 1 台で「RX_COLL_POS を使えず 1 bit 伸ばしに落ちた」回数（poll 統計用）。
static int s_fast_last_fallbacks;
// 直近に読んだ reader 1 台で「壊れた受信を同じ node で再 probe した」回数（poll 統計用）。
static int s_fast_last_noise_retries;
// reader ごとの coll_pos ログ出力回数（最初の 3 回だけ INFO、以降 DEBUG）。
static uint8_t s_collpos_logs[PN5180_READER_COUNT];
// 安全弁: RX_COLL_POS の基準が想定と違うと分かったら以後は 1 bit 伸ばしに固定する（全 reader 共通）。
//
// 「採用した分割で札が 1 枚も応答しなかった」ラウンドが **この回数だけ連続**したときに無効化する。
// 1 回で切ると誤発火する: hole card 2 枚を同時に持ち上げる過渡では、root probe の時点では
// まだ場にあって COLLISION → 子枝を probe する頃には 2 枚とも場外 → 両子枝 NONE、という並びが
// 普通に起こる（基準は正しいのに 0 枚になる）。連続回数で見れば過渡は 1〜2 で途切れ、
// 基準が本当に違う場合だけ連続して積み上がる。`PRESENCE_HOLD_MISSES` と同じく「連続回数」判定。
#define PN5180_COLLPOS_DISABLE_STREAK 3
static bool s_collpos_disabled;
// 「採用分割あり かつ 0 枚」だったラウンドの連続数（採用して札が取れたら 0 に戻す）。
static uint8_t s_collpos_empty_streak;
// 現ラウンドで「衝突位置を採用した分割」を何回したか + 最後に採用した生値（安全弁の判定・ログ用）。
static int s_fast_round_adopted;
static uint16_t s_fast_round_adopt_raw;
// reader ごとの「確認 probe を省略した連続回数」（実装 B）。
static uint8_t s_confirm_skips[PN5180_READER_COUNT];

typedef struct {
    uint64_t mask;
    uint8_t len;
} dfs_node_t;

// ── 衝突した枝を 2 分割して DFS スタックに積む（戻り値 = 新しい sp）──
// RX_COLL_POS が有効なら **衝突位置 pos まで mask を一気に伸ばす**（実装 A）。pos より手前の
// ビットは全応答で一致しているので、子は `(prefix|1<<pos, pos+1)` と `(prefix, pos+1)` の 2 つ
// で、**どちらにも必ず札がいる**（＝空の兄弟枝を RX timeout いっぱい待つ probe が消える）。
// 無効・不整合なら従来どおり 1 bit だけ伸ばす。
static int fast_push_children(int slot, dfs_node_t *stack, int sp, const dfs_node_t *cur,
                              const fast_coll_t *coll) {
    if (sp + 2 > (int)FAST_DFS_STACK) return sp;  // 溢れ防止（probe 上限があるので通常来ない）

    uint64_t mask = cur->mask;
    uint8_t len = cur->len;
    bool adopted = false;
    if (!s_collpos_disabled && coll->pos != 0xFF && coll->pos >= cur->len && coll->pos < 64) {
        // 受信できた UID 先頭部分の bit[0..pos) を prefix にする（bit i = uid[i/8] の bit i%8）。
        uint64_t prefix = 0;
        for (uint8_t b = 0; b < coll->pos; b++) {
            prefix |= (uint64_t)((coll->uid[b / 8] >> (b % 8)) & 1U) << b;
        }
        // 既知ビット（cur.mask の下位 cur.len bit）と一致するはず。ズレたら信用せず fallback。
        const uint64_t known = (cur->len == 0) ? 0ULL : (~0ULL >> (64 - cur->len));
        if (((prefix ^ cur->mask) & known) == 0) {
            mask = prefix;
            len = coll->pos;
            adopted = true;
            s_fast_round_adopted++;
            s_fast_round_adopt_raw = coll->raw_pos;
        }
    }

    // 実機で RX_COLL_POS の基準（フレーム先頭 or UID 先頭）を確認できるよう、reader ごと最初の
    // 3 回だけ INFO で出す（毎 poll 出すと UART が埋まるので以降は DEBUG）。
    const int uid_bit = (coll->pos == 0xFF) ? -1 : (int)coll->pos;
    const char *verdict = adopted ? "採用" : "fallback(1bit)";
    if (slot >= 0 && slot < PN5180_READER_COUNT && s_collpos_logs[slot] < 3) {
        s_collpos_logs[slot]++;
        ESP_LOGI(TAG, "reader %d: coll_pos=%u（UID bit %d）, 受信 %u byte, cur.len=%u → %s",
                 slot, (unsigned)coll->raw_pos, uid_bit, (unsigned)coll->rx_bytes,
                 (unsigned)cur->len, verdict);
    } else {
        ESP_LOGD(TAG, "reader %d: coll_pos=%u（UID bit %d）, 受信 %u byte, cur.len=%u → %s",
                 slot, (unsigned)coll->raw_pos, uid_bit, (unsigned)coll->rx_bytes,
                 (unsigned)cur->len, verdict);
    }
    if (!adopted) s_fast_last_fallbacks++;

    if (len >= 64) return sp;  // 64 bit すべて一致 = 同一 UID が 2 枚（これ以上は割れない）
    stack[sp].mask = mask | (1ULL << len);
    stack[sp].len = (uint8_t)(len + 1);
    sp++;
    stack[sp].mask = mask;
    stack[sp].len = (uint8_t)(len + 1);
    sp++;
    return sp;
}

// ── probe 1 回 + ノイズ時の 1 回だけの再試行 ──
// 「衝突フラグ無しで壊れた受信」（PROBE_NOISE）は分割しても消えないので、**同じ node をその場で
// 1 回だけ再 probe** し、それでも壊れていたら NONE 扱いにする（DFS を広げない）。
// `*probes` は実際に送った INVENTORY の回数だけ増える（= PN5180_FAST_MAX_PROBES が時間の上限）。
static probe_result_t fast_probe_retry(pn5180_t *dev, uint64_t mask, uint8_t mask_len,
                                       uint8_t *uid_out, fast_coll_t *coll, int *probes) {
    (*probes)++;
    probe_result_t rc = fast_probe_15693(dev, mask, mask_len, uid_out, coll);
    if (rc != PROBE_NOISE) return rc;
    (*probes)++;
    s_fast_last_noise_retries++;
    rc = fast_probe_15693(dev, mask, mask_len, uid_out, coll);
    return (rc == PROBE_NOISE) ? PROBE_NONE : rc;
}

#if PN5180_FAST_TARGETED_PROBE
// mask は byte 単位で送るので 8 の倍数。0 は「狙い撃ちでない」= 意味がないので 8 以上。
// メッセージは ASCII 固定（gcc の診断が非 ASCII を 8 進エスケープして読めなくなるため）。
_Static_assert(PN5180_FAST_TARGETED_MASK_BITS >= 8 && PN5180_FAST_TARGETED_MASK_BITS <= 64 &&
                   PN5180_FAST_TARGETED_MASK_BITS % 8 == 0,
               "PN5180_FAST_TARGETED_MASK_BITS must be a multiple of 8 in 8..64");

// MSB-first の UID から inventory の mask 値（uint64, 下位 PN5180_FAST_TARGETED_MASK_BITS bit）を作る。
// mask bit i は「生（LSB-first）バイト列の bit i」= raw[i/8] の bit i%8。ISO15693 は UID を
// LSB から送るので raw[j] = MSB-first の [7-j]（`fast_same_as_prev` の比較と同じ対応）。
// 32 bit なら raw[0..3] = MSB-first の [7..4] = ICODE の**シリアル 4 byte 全部**を含む。
static uint64_t fast_mask_from_msb_uid(const uint8_t *msb) {
    const int nb = PN5180_FAST_TARGETED_MASK_BITS / 8;
    uint64_t m = 0;
    for (int j = 0; j < nb; j++) m |= (uint64_t)msb[7 - j] << (8 * j);
    return m;
}
#endif

// 直近に読んだ reader 1 台の狙い撃ち probe 数 / 当たった数（poll 統計用。無効時は常に 0）。
static int s_fast_targeted_probes;
static int s_fast_targeted_hits;

// 直近に読んだ reader 1 台が「簡略サイクル」だったか（1 = 狙い撃ちだけで終えた。poll 統計用）。
static int s_fast_last_cheap;

#if !PN5180_FAST_TARGETED_PROBE
// 1 ラウンド目で見つけた集合が前回 cache と同じか（実装 B の判定）。
// prev は **MSB-first**（反転・ソート済みで hold 中の UID も含む）、det は probe が返した
// **LSB-first** の生バイト。hold 中の札が混ざっていれば「同じではない」= 確認 probe を省かない。
static bool fast_same_as_prev(const pn5180_card_t *prev, const uint8_t det[][16], uint8_t count) {
    if (count == 0 || !prev->present || prev->count != count || prev->uid_len != 8) return false;
    for (uint8_t k = 0; k < count; k++) {
        bool found = false;
        for (uint8_t j = 0; j < prev->count && !found; j++) {
            found = true;
            for (uint8_t b = 0; b < 8; b++) {
                if (prev->uids[j][b] != det[k][7 - b]) { found = false; break; }
            }
        }
        if (!found) return false;
    }
    return true;
}
#endif  // !PN5180_FAST_TARGETED_PROBE

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
//
// 定常状態（前回と同じ札が載ったまま）では最後の「もう居ない」確認 probe を
// PN5180_FAST_CONFIRM_EVERY 回に 1 回だけにする（実装 B）。11 台に札が載っていると確認だけで
// RX timeout × 11 ≈ 90 ms/周かかるため。capture で隠れた札は最大 CONFIRM_EVERY poll 後に見つかる。
static uint8_t fast_inventory_15693(int slot, slot_reader_t *r, const pn5180_card_t *prev,
                                    uint8_t uids[][16], uint8_t *uid_len) {
    pn5180_t *dev = r->dev;
    *uid_len = 8;
    s_fast_last_fallbacks = 0;
    s_fast_last_noise_retries = 0;
    s_fast_targeted_probes = 0;
    s_fast_targeted_hits = 0;
    s_fast_rx_wait_max_us = 0;
    s_fast_last_cheap = 0;
    if (!r->rf_loaded) {
        if (!pn5180_loadRFConfig(dev, PN5180_FAST_RF_CONFIG)) return 0;
        r->rf_loaded = true;
    }
    if (!pn5180_setRF_on(dev)) return 0;  // is_rf_on はドライバが持つので二重 ON にはならない
    esp_rom_delay_us(PN5180_FAST_FIELD_SETTLE_US);

    dfs_node_t stack[FAST_DFS_STACK];

    uint8_t count = 0;
    int probes = 0;
    int rounds = 0;
    int stale_rounds = 0;  // 新しい UID が 1 枚も増えなかったラウンドの連続数
    uint8_t uid[8];
    fast_coll_t coll;

#if PN5180_FAST_TARGETED_PROBE
    // ── 前段: 前回見えていた UID を「完全一致 mask」で直接呼ぶ（衝突しない・即答）──
    // 載ったままの札はここで確定する。外れた札（もう無い UID）は RX timeout ぶん待つが、
    // UID 単位 hold で数 poll で消える。
    //
    // **簡略サイクル（cheap）**: 前回 1 枚以上あった reader では、CONFIRM_EVERY 回に
    // (CONFIRM_EVERY-1) 回を「狙い撃ちだけで終わる」サイクルにする。Stay Quiet も root probe も
    // 送らない（Stay Quiet は **root probe で新しい札だけを見るための下準備**なので、root を
    // 送らないなら不要）。実機 2026-09-11 の内訳（reader 1 台・2 枚）:
    //   狙い撃ち 7.4 ms × 2 + Stay Quiet 4.2 ms × 2 + root(空) 9.5 ms ≈ 35 ms
    //   → 簡略サイクルは 2 枚で ≈ 17 ms（半分以下）。
    // 代償: **既に札がある reader に増えた札**の発見が最大 CONFIRM_EVERY poll 遅れる。
    // 空の reader（prev 0 枚）はこの経路に入らないので、**配られた瞬間は毎 poll 検出できる**
    // （席の 1 枚目 / flop / turn / river はすべて空の reader に載るので影響を受けない。
    //  影響するのは「席の 2 枚目」だけで、ディーラーが 2 周目を配る間隔より十分速い）。
    const bool cheap = (PN5180_FAST_CONFIRM_EVERY > 0) && prev->present && prev->uid_len == 8 &&
                       prev->count > 0 &&
                       (s_confirm_skips[slot] + 1 < PN5180_FAST_CONFIRM_EVERY);
    if (prev->present && prev->uid_len == 8) {
        for (uint8_t k = 0; k < prev->count && count < PN5180_MAX_CARDS_PER_READER &&
                            probes < PN5180_FAST_MAX_PROBES; k++) {
            s_fast_targeted_probes++;
            const uint64_t m = fast_mask_from_msb_uid(prev->uids[k]);
            if (fast_probe_retry(dev, m, PN5180_FAST_TARGETED_MASK_BITS, uid, &coll, &probes)
                != PROBE_UID) {
                // 外れ（もう無い）/ 衝突（この prefix を共有する別の札も居る）。どちらも
                // 下の root probe + DFS に任せる（衝突した札はそこで分離される）。
                continue;
            }
            s_fast_targeted_hits++;
            fast_add_uid(uids, &count, uid);
            // root probe で「新しい札」だけを見るために、確定した札は黙らせる。
            // 簡略サイクルは root を送らないので Stay Quiet も要らない（完全一致 mask の probe は
            // 他の札が応答しないので、黙らせなくても狙い撃ち自体は成立する）。
            if (!cheap) fast_stay_quiet_15693(dev, uid);
        }
    }
    if (cheap) {
        s_confirm_skips[slot]++;
        s_fast_last_cheap = 1;
        pn5180_setRF_off(dev);  // quiet 解除（送っていないが RF は落とす）
        s_fast_last_probes = probes;
        return count;
    }
    s_confirm_skips[slot] = 0;  // このサイクルは root まで確認する（カウンタを畳む）
#endif

    while (probes < PN5180_FAST_MAX_PROBES && count < PN5180_MAX_CARDS_PER_READER) {
        const uint8_t before = count;
        int round_uids = 0;  // このラウンドで応答した札の数（重複込み。安全弁の判定用）
        rounds++;
        s_fast_round_adopted = 0;
        const probe_result_t root = fast_probe_retry(dev, 0, 0, uid, &coll, &probes);
        if (root == PROBE_NONE) break;  // 誰も応答しない = 残りは居ない（正常終了）
        if (root == PROBE_UID) {
            round_uids++;
            fast_add_uid(uids, &count, uid);
            fast_stay_quiet_15693(dev, uid);  // dup でも送る（黙らせ損ねの再送になる）
        } else {
            // 衝突: root（mask 0 / len 0）を分割して mask DFS。RX_COLL_POS が使えれば
            // 衝突位置まで一気に伸びる（使えなければ bit0 で 2 分割 = 従来どおり）。
            const dfs_node_t root_node = {.mask = 0, .len = 0};
            int sp = fast_push_children(slot, stack, 0, &root_node, &coll);
            while (sp > 0 && probes < PN5180_FAST_MAX_PROBES &&
                   count < PN5180_MAX_CARDS_PER_READER) {
                const dfs_node_t cur = stack[--sp];
                switch (fast_probe_retry(dev, cur.mask, cur.len, uid, &coll, &probes)) {
                case PROBE_UID:
                    round_uids++;
                    fast_add_uid(uids, &count, uid);
                    fast_stay_quiet_15693(dev, uid);
                    break;
                case PROBE_COLLISION:
                    sp = fast_push_children(slot, stack, sp, &cur, &coll);
                    break;
                case PROBE_NOISE:  // fast_probe_retry が NONE に畳むのでここには来ない
                case PROBE_NONE:
                default:
                    break;
                }
            }
        }
        // ── coll_pos の安全弁 ──
        // 「衝突位置を採用して分割したのに、そのラウンドで札が 1 枚も応答しなかった」= 分けた
        // 両子枝が空。RX_COLL_POS の基準（フレーム先頭 or UID 先頭）が想定と違う疑いがあるが、
        // 札を持ち上げる過渡でも 1 回だけなら起こるので、**PN5180_COLLPOS_DISABLE_STREAK 回
        // 連続**したときだけ以後 1 bit 伸ばしに固定する（遅くなるが正しさは保てる）。WARN は 1 回。
        if (s_fast_round_adopted > 0 && !s_collpos_disabled) {
            if (round_uids > 0) {
                s_collpos_empty_streak = 0;  // 採用した分割で札が取れた = 基準は合っている
            } else if (++s_collpos_empty_streak >= PN5180_COLLPOS_DISABLE_STREAK) {
                s_collpos_disabled = true;
                ESP_LOGW(TAG,
                         "reader %d: coll_pos を採用した分割で札が取れないラウンドが %u 回連続"
                         "（最後の coll_pos=%u）→ coll_pos を無効化（以後は 1 bit ずつ伸ばす DFS）。"
                         "RX_COLL_POS の基準を確認",
                         slot, (unsigned)s_collpos_empty_streak,
                         (unsigned)s_fast_round_adopt_raw);
            }
        }

#if !PN5180_FAST_TARGETED_PROBE
        // ── 定常状態なら「もう居ない」確認 probe（次ラウンドの root）を間引く（実装 B）──
        // 1 ラウンド目で前回 cache と同じ集合が揃ったときだけ。集合が変わった / 前回 0 枚 /
        // 2 ラウンド目以降（= capture で隠れた札を掘っている最中）は必ず確認する。
        // **実装 C（狙い撃ち）が有効なときはこちらを使わない**: 間引きの判定は前段（狙い撃ち）で
        // 行い、Stay Quiet と root probe をまとめて省く（そちらが安く効く）。
        if (PN5180_FAST_CONFIRM_EVERY > 0 && rounds == 1 &&
            fast_same_as_prev(prev, (const uint8_t (*)[16])uids, count)) {
            if (++s_confirm_skips[slot] < PN5180_FAST_CONFIRM_EVERY) break;
        }
        s_confirm_skips[slot] = 0;  // 集合が変わった / N 回目 = 確認する（カウンタを畳む）
#endif
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
// hold は **UID 単位**（reader 単位ではない）。reader 単位だと「検出 0 枚のときだけ前回集合を保つ」
// ことしかできず、2 枚中 1 枚を 1 回取りこぼしただけで集合が丸ごと 1 枚に置き換わる。実機
// （2026-09-10, 2 枚重ね）で host に届く枚数が 2↔1 と数百 ms 周期で揺れ、`watch` が同じ札を
// 何度も再発火した。UID ごとに miss を数えれば、欠けた 1 枚だけを数サイクル保持できる。
#define PRESENCE_HOLD_MISSES 3
// s_cache[i].uids[k] と添字が対応する連続 miss 数（0 = 今回検出）。
static uint8_t s_uid_miss[PN5180_READER_COUNT][PN5180_MAX_CARDS_PER_READER];
// 「検出枚数 > PN5180_MAX_CARDS_PER_READER」の WARN を reader ごと 1 回に絞るフラグ。
static bool s_overflow_warned[PN5180_READER_COUNT];

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
static uint8_t s_rf_off_fail[PN5180_READER_COUNT];

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
static int s_stats_fallbacks;            // 窓内の coll_pos fallback 回数（fast 経路）
static int s_stats_noise_retries;        // 窓内のノイズ再試行回数（fast 経路）
static int s_stats_targeted_probes;      // 窓内の狙い撃ち probe 数（fast 経路）
static int s_stats_targeted_hits;        // 窓内の狙い撃ち命中数（fast 経路）
static int s_stats_rx_wait_max_us;       // 窓内で「応答が返った probe」の最長待ち（µs, fast 経路）
static int s_stats_cheap_readers;        // 窓内の簡略サイクル数（reader 単位, fast 経路）
static int s_stats_cycles;
#endif

void pn5180_reader_poll_once(void) {
#if POLL_STATS_INTERVAL_MS > 0
    const int64_t cycle_start_us = esp_timer_get_time();
    int64_t cycle_worst_us = -1;
    int cycle_worst_idx = -1;
    int ready_readers = 0;
    int cycle_max_probes = 0;  // この周で最も probe を使った reader の回数（fast 経路のみ。0 = ドライバ経路）
    int cycle_fallbacks = 0;      // この周で RX_COLL_POS を使えず 1 bit 伸ばしに落ちた回数
    int cycle_noise_retries = 0;  // この周で壊れた受信を再 probe した回数
    int cycle_targeted_probes = 0;  // この周の狙い撃ち probe 数（前回 UID の完全一致 probe）
    int cycle_targeted_hits = 0;    // うち当たった数（= 載ったままだった札）
    int cycle_rx_wait_max_us = 0;   // この周で「応答が返った probe」の最長待ち時間（µs）
    int cycle_cheap_readers = 0;    // この周で「簡略サイクル」（狙い撃ちだけ）で終えた reader 数
#endif

    for (int i = 0; i < PN5180_READER_COUNT; i++) {
        // 未通電で init を飛ばした reader（dev=NULL）は触らない。cache は present=false のままなので
        // host には「カード無し（Get UID P2=i → 6A 81）」に見える。
        if (!s_readers[i].dev) continue;
#if POLL_STATS_INTERVAL_MS > 0
        const int64_t reader_start_us = esp_timer_get_time();
        ready_readers++;
#endif
        mux_select(s_readers[i].mux_ch);  // この reader の BUSY を SIG に

        uint8_t uids[PN5180_MAX_CARDS_PER_READER][16];
        uint8_t len = 0;
        uint8_t count = 0;
        bool from_iso15693 = false;

        // 前回集合（書き手はこの poll task だけなので lock 不要）。下の merge_presence と、
        // fast 経路の「確認 probe 間引き」（実装 B）の両方が使うので inventory の前に取る。
        const pn5180_card_t prev = s_cache[i];

        // ドライバ既定の timeout_ms=500 は SPI BUSY 待ち / transceive 状態待ち / RF off 待ちの
        // すべてに効くため、1 台の不調が 1 周を 0.5 秒伸ばす。読み取りの間だけ短くする
        // （ドライバの get_all_uids も内部で 40ms に落としている）。
        const int64_t saved_timeout_ms = s_readers[i].dev->timeout_ms;
#if PN5180_FAST_INVENTORY
        s_readers[i].dev->timeout_ms = PN5180_FAST_OP_TIMEOUT_MS;
        // 自前の mask DFS（ISO15693 専用。PN5180_TRY_ISO14443 はこの経路では無視）。
        count = fast_inventory_15693(i, &s_readers[i], &prev, uids, &len);
        from_iso15693 = (count > 0);
#if POLL_STATS_INTERVAL_MS > 0
        if (s_fast_last_probes > cycle_max_probes) cycle_max_probes = s_fast_last_probes;
        cycle_fallbacks += s_fast_last_fallbacks;
        cycle_noise_retries += s_fast_last_noise_retries;
        cycle_targeted_probes += s_fast_targeted_probes;
        cycle_targeted_hits += s_fast_targeted_hits;
        cycle_cheap_readers += s_fast_last_cheap;
        if (s_fast_rx_wait_max_us > cycle_rx_wait_max_us) cycle_rx_wait_max_us = s_fast_rx_wait_max_us;
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
        s_stats_fallbacks = 0;
        s_stats_noise_retries = 0;
        s_stats_targeted_probes = 0;
        s_stats_targeted_hits = 0;
        s_stats_rx_wait_max_us = 0;
        s_stats_cheap_readers = 0;
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
    s_stats_fallbacks += cycle_fallbacks;
    s_stats_noise_retries += cycle_noise_retries;
    s_stats_targeted_probes += cycle_targeted_probes;
    s_stats_targeted_hits += cycle_targeted_hits;
    s_stats_cheap_readers += cycle_cheap_readers;
    if (cycle_rx_wait_max_us > s_stats_rx_wait_max_us) s_stats_rx_wait_max_us = cycle_rx_wait_max_us;

    const int64_t now_us = esp_timer_get_time();
    if (s_stats_window_start_us == 0) s_stats_window_start_us = now_us;  // 初回だけ窓を開始
    if (now_us - s_stats_window_start_us >= (int64_t)POLL_STATS_INTERVAL_MS * 1000) {
        const int64_t avg_us = s_stats_sum_us / s_stats_cycles;  // cycles >= 1
        ESP_LOGI(TAG,
                 "poll 統計(直近 %d 周): 1 周 min/avg/max = %lld/%lld/%lld ms, "
                 "最長 reader #%d (index %d) = %lld ms, probe 最大 %d 回/reader, "
                 "狙い撃ち %d/%d 命中, 簡略 %d/%d reader周, 応答待ち最長 %d.%01d ms, "
                 "coll_pos fallback %d, ノイズ再試行 %d, ready %d reader",
                 s_stats_cycles,
                 (long long)(s_stats_min_us / 1000), (long long)(avg_us / 1000),
                 (long long)(s_stats_max_us / 1000),
                 s_stats_worst_idx + 1, s_stats_worst_idx,
                 (long long)(s_stats_worst_us / 1000), s_stats_max_probes,
                 s_stats_targeted_hits, s_stats_targeted_probes,
                 s_stats_cheap_readers, s_stats_cycles * ready_readers,
                 s_stats_rx_wait_max_us / 1000, (s_stats_rx_wait_max_us % 1000) / 100,
                 s_stats_fallbacks, s_stats_noise_retries, ready_readers);
        s_stats_cycles = 0;             // 次の窓へ（min/max/sum は次の 1 周で初期化）
        s_stats_window_start_us = now_us;
    }
#endif
}

bool pn5180_reader_get_card(uint8_t reader_index, pn5180_card_t *out) {
    if (reader_index >= PN5180_READER_COUNT || !out) return false;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    *out = s_cache[reader_index];
    xSemaphoreGive(s_lock);
    return true;
}
