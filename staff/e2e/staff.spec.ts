import { expect, test } from "@playwright/test";

/**
 * staff アプリ E2E（ADR-0037 / WS4）。web export + MockRepository をブラウザで開き、
 * 実フローをタップ駆動で検証する（token = demo-staff-token, fixtures = staff/src/mocks）。
 *
 * RN Web は Pressable を <div role=button>、TextInput を <input> に描画する。selector は
 * 表示テキスト（exact でチップを一意化）と placeholder を使う（testID 非依存）。
 */

const TOKEN = "demo-staff-token";

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByText("店舗スタッフ")).toBeVisible();
  await page.getByPlaceholder("viewer_api.staff_token").fill(TOKEN);
  await page.getByText("ログイン").click();
  await expect(page.getByText("セッション一覧")).toBeVisible();
}

async function openTable(page: import("@playwright/test").Page): Promise<void> {
  // OPEN fixture session を開く（会計タブが既定）。
  await page.getByText("土曜ナイト #5").first().click();
  await expect(page.getByText("会計", { exact: true })).toBeVisible();
}

test("invalid token shows auth error and stays on login", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder("viewer_api.staff_token").fill("wrong-token");
  await page.getByText("ログイン").click();
  await expect(page.getByText(/トークンが無効/)).toBeVisible();
  await expect(page.getByText("セッション一覧")).toHaveCount(0);
});

test("login → open session → add & reverse a ledger entry", async ({ page }) => {
  await login(page);
  await openTable(page);

  // Alice チップ選択（exact でチップのみ。entry 行の "…　Alice　buy_in" には一致しない）。
  await page.getByText("Alice", { exact: true }).click();
  // kind は既定 buy_in。cash を入力して追加。
  await page.getByPlaceholder("cash(円)").fill("5000");
  await page.getByText("エントリ追加").click();
  await expect(page.getByText(/記録しました/)).toBeVisible();

  // エントリ一覧の取消（reversal, append-only）。
  await page.getByText("取消").first().click();
  await expect(page.getByText(/取り消しました/)).toBeVisible();
});

test("orders tab: confirm a pending request (prefilled unit price)", async ({ page }) => {
  await login(page);
  await openTable(page);

  await page.getByText(/注文/).first().click();
  // pending 注文（fixture: ビール x2 / ウーロン茶 x1）が見える。
  await expect(page.getByText(/ビール x2/)).toBeVisible();
  // 単価は menu から prefill 済み → そのまま確定。
  await page.getByText("確定").first().click();
  await expect(page.getByText(/確定しました/)).toBeVisible();
});

test("seating tab: shows current seating and assigns next hand", async ({ page }) => {
  await login(page);
  await openTable(page);

  await page.getByText("座席").click();
  await expect(page.getByText("現在の座席（最新 hand 由来）")).toBeVisible();

  // 未着席の Carol を席3 に割り当て（次 hand）。
  await page.getByText("Carol", { exact: true }).click();
  await page.getByPlaceholder("席1-9").fill("3");
  await page.getByText("席に追加").click();
  await expect(page.getByText("割り当て予定")).toBeVisible();
  await page.getByText(/に割り当て$/).click();
  await expect(page.getByText(/に割り当てました/)).toBeVisible();
});

test("hand tab: send new-hand and winner control commands", async ({ page }) => {
  await login(page);
  await openTable(page);

  await page.getByText("ハンド").click();
  await page.getByText("新ハンド開始").click();
  await expect(page.getByText(/送信しました: 新ハンド/)).toBeVisible();

  await page.getByPlaceholder("席1-9").fill("3");
  await page.getByText("ウィナー確定").click();
  await expect(page.getByText(/送信しました: ウィナー 席3/)).toBeVisible();
});

test("create a new session from the list", async ({ page }) => {
  await login(page);
  await page.getByPlaceholder("例: 土曜ナイト #5").fill("E2E 卓");
  await page.getByText("作成", { exact: true }).click();
  await expect(page.getByText(/セッションを作成しました/)).toBeVisible();
  await expect(page.getByText("E2E 卓")).toBeVisible();
});
