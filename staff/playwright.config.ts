import { defineConfig, devices } from "@playwright/test";

/**
 * staff アプリの E2E（ADR-0037 / WS4）。web export（`dist/`）+ MockRepository を
 * ヘッドレス Chromium で開き、ログイン→会計→注文→座席の実フローをタップ駆動で検証する。
 *
 * 前提: `npm run export:web` で `dist/` を生成済み（`npm run e2e` が build→serve→test を一括実行）。
 * iPad 配布形態（web export を Safari で開く）に最も近い自動テスト。実機タッチ/レイアウトは
 * 手動 QA を別途行う（環境制約: Linux に iOS シミュレータなし）。
 */
const PORT = 4173;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      // iPad 相当のビューポート（landscape）+ touch。device preset の後に置かないと
      // Desktop Chrome の viewport(1280×720)/hasTouch(false) に上書きされて実効しない。
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1180, height: 820 },
        hasTouch: true,
      },
    },
  ],
  webServer: {
    command: `node e2e/serve.mjs ${PORT}`,
    url: `http://127.0.0.1:${PORT}`,
    timeout: 30_000,
    reuseExistingServer: !process.env.CI,
  },
});
