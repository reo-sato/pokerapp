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
  // exact: 説明文（「…ログインしてください…」等）に部分一致しないようボタンだけを狙う。
  await page.getByText("ログイン", { exact: true }).click();
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
  await page.getByText("ログイン", { exact: true }).click();
  await expect(page.getByText(/トークンが無効/)).toBeVisible();
  await expect(page.getByText("セッション一覧")).toHaveCount(0);
});

test("login → open session → add & reverse a ledger entry", async ({ page }) => {
  await login(page);
  await openTable(page);

  // Alice チップ選択（exact でチップのみ。entry 行の "…　Alice　buy_in" には一致しない）。
  await page.getByText("Alice", { exact: true }).click();
  // kind は既定 buy_in。cash を入力して追加（.last() = カード見出しではなくボタン）。
  await page.getByPlaceholder("cash(円)").fill("5000");
  await page.getByText("エントリ追加", { exact: true }).last().click();
  await expect(page.getByText(/記録しました/)).toBeVisible();

  // エントリ一覧の取消（reversal, append-only。exact = 説明文の「取消は…」を除外）。
  await page.getByText("取消", { exact: true }).first().click();
  await expect(page.getByText(/取り消しました/)).toBeVisible();
});

test("orders tab: confirm a pending request (prefilled unit price)", async ({ page }) => {
  await login(page);
  await openTable(page);

  await page.getByText(/注文/).first().click();
  // pending 注文（fixture: ビール x2 / ウーロン茶 x1）が見える。
  await expect(page.getByText(/ビール x2/)).toBeVisible();
  // 単価は menu から prefill 済み → そのまま確定（exact = 説明文の「確定すると…」を除外）。
  await page.getByText("確定", { exact: true }).first().click();
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
  // 成功メッセージは「hand #N に M 席を割り当てました。」
  await expect(page.getByText(/席を割り当てました/)).toBeVisible();
});

test("hand tab: send new-hand and winner control commands", async ({ page }) => {
  await login(page);
  await openTable(page);

  await page.getByText("ハンド", { exact: true }).click();
  await page.getByText("新ハンド開始").click();
  await expect(page.getByText(/送信しました: 新ハンド/)).toBeVisible();

  await page.getByPlaceholder("席1-9").fill("3");
  await page.getByText("ウィナー確定").click();
  await expect(page.getByText(/送信しました: ウィナー 席3/)).toBeVisible();
});

test("hand tab: open hand history and replay a hand street by street", async ({ page }) => {
  await login(page);
  await openTable(page);

  // ハンド履歴（ADR-0044）: fixture 3 ハンドが hand_id 昇順で並ぶ。
  await page.getByText("ハンド", { exact: true }).click();
  await expect(page.getByText("ハンド履歴")).toBeVisible();
  await expect(page.getByText(/Hand #1/)).toBeVisible();

  // Hand #1 を開くとストリート単位リプレイ（共有コンポーネント）が表示される。
  await page.getByText(/Hand #1/).click();
  await expect(page.getByText("プリフロップ")).toBeVisible();
  await expect(page.getByText("フロップ", { exact: true })).toBeVisible();
  await expect(page.getByText("リバー", { exact: true })).toBeVisible();
  await expect(page.getByText("結果", { exact: true })).toBeVisible();
  // 記録があるホールカードは全員分表示（Alice AhAd / Bob KsKd, ADR-0044 D3）。
  await expect(page.getByText("A♥")).toBeVisible();
  await expect(page.getByText("K♠")).toBeVisible();

  // 一覧へ戻れる。
  await page.getByText("← ハンド履歴").click();
  await expect(page.getByText(/Hand #3/)).toBeVisible();
});

test("hand tab: correct a misrecognized action from the replay detail (B4)", async ({ page }) => {
  await login(page);
  await openTable(page);

  // fixture Hand #2 は要確認（action[0] needs_review）。
  await page.getByText("ハンド", { exact: true }).click();
  await page.getByText(/Hand #2/).click();
  await expect(page.getByText("✎ このハンドを訂正")).toBeVisible();

  // 先頭アクション（preflop raise 500, 要確認）を bet に訂正する。
  await page.getByText("訂正する").first().click();
  await page.getByText("bet", { exact: true }).click();
  await page.getByText("訂正を保存").click();
  await expect(page.getByText(/訂正しました（1 件）/)).toBeVisible();

  // 訂正適用済みビューが再読込され、リプレイ側に訂正済バッジ + bet が出る。
  await expect(page.getByText("訂正済").first()).toBeVisible();
  await expect(page.getByText("ベット", { exact: true }).first()).toBeVisible();
});

test("create a new session from the list", async ({ page }) => {
  await login(page);
  await page.getByPlaceholder("例: 土曜ナイト #5").fill("E2E 卓");
  await page.getByText("作成", { exact: true }).click();
  await expect(page.getByText(/セッションを作成しました/)).toBeVisible();
  // 成功メッセージと一覧行の両方に現れるため first() で一覧到達だけ確認する。
  await expect(page.getByText("E2E 卓").first()).toBeVisible();
});
