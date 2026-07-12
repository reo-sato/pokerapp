/**
 * MockRepository: fixtures を読むだけの in-memory 実装 (M2, WS3 mock-first)。
 *
 * 契約 (viewer-api.md / error-shapes.md) と同じ意味論で振る舞う:
 * unknown player / session / hand は code="not_found" の ViewerApiError で reject。
 * WS1/API 完成を待たずに画面遷移・状態管理を作るための実装で、HttpRepository と
 * 同じ ViewerRepository interface を満たす。
 */
import type { ViewerRepository } from "./repository";
import type {
  AuthSession,
  HandCorrection,
  HandCorrectionInput,
  HandSummary,
  LedgerEntry,
  MenuItem,
  OrderRequest,
  OrderRequestBody,
  Player,
  PlayerSessionLedger,
  PlayerSessionSummary,
} from "./types";
import { ViewerApiError } from "./types";
import { clearStoredAuth, loadStoredAuth, saveStoredAuth } from "./authStorage";
import * as fx from "../mocks/fixtures";

function notFound(message: string): ViewerApiError {
  return new ViewerApiError({ code: "not_found", message });
}

// mock のデモ用 credential（UI 動作確認用。実認証は core / viewer API）。
const MOCK_PIN = "1234";
const MOCK_OIDC_CODE = "demo-good";

export class MockRepository implements ViewerRepository {
  // 注文リクエストは mock 内の in-memory 状態（pending のまま。確定はスタッフ desktop の責務）
  private orderRequests: OrderRequest[] = [];
  private orderSeq = 0;
  // ハンド訂正 (B4/ADR-0036) の in-memory 状態。getHand/listPlayerHands で overlay 適用。
  private corrections: HandCorrection[] = [];
  private corrSeq = 0;
  // 本人認証 (L1/L2) の in-memory 状態。OIDC サインアップで作った player も保持する。
  private principal: string | null = null;
  private signedUp: Player[] = [];

  constructor() {
    // 保存済みログインを復元する（HttpRepository と同じ意味論。mock token は期限なし）。
    const stored = loadStoredAuth();
    if (stored) this.principal = stored.player_id;
  }

  async health(): Promise<{ status: string; version: string }> {
    return { status: "ok", version: "mock" };
  }

  async listPlayers(): Promise<Player[]> {
    return [...fx.players, ...this.signedUp];
  }

  async getPlayer(playerId: string): Promise<Player> {
    const player =
      fx.players.find((p) => p.player_id === playerId) ??
      this.signedUp.find((p) => p.player_id === playerId);
    if (!player) throw notFound(`player_id=${playerId} は存在しません。`);
    return player;
  }

  async login(playerId: string, pin: string): Promise<AuthSession> {
    await this.getPlayer(playerId); // not_found
    if ((pin ?? "").length < 4) {
      throw new ViewerApiError({ code: "pin_too_short", message: "PIN は 4 桁以上必要です。" });
    }
    if (pin !== MOCK_PIN) {
      throw new ViewerApiError({ code: "invalid_pin", message: "PIN が違います。" });
    }
    this.principal = playerId;
    const session = { token: `mock-token-${playerId}`, player_id: playerId, expires_at: 0 };
    saveStoredAuth(session);
    return session;
  }

  async oidcExchange(provider: string, code: string): Promise<AuthSession> {
    if (code !== MOCK_OIDC_CODE) {
      throw new ViewerApiError({ code: "invalid_idp_code", message: "認可コードが無効です。" });
    }
    // 初回 link 相当: (provider) ごとに 1 player を作る（再交換は同一 player に解決）。
    const playerId = `face${provider.slice(0, 4).padEnd(4, "0")}`.padEnd(32, "0").slice(0, 32);
    let player = this.signedUp.find((p) => p.player_id === playerId);
    if (!player) {
      const now = new Date().toISOString().slice(0, 19);
      player = {
        player_id: playerId, display_name: `${provider} ユーザー`,
        created_at: now, updated_at: now,
      };
      this.signedUp.push(player);
    }
    this.principal = player.player_id;
    const session = {
      token: `mock-token-${player.player_id}`, player_id: player.player_id, expires_at: 0,
    };
    saveStoredAuth(session);
    return session;
  }

  currentPrincipal(): string | null {
    return this.principal;
  }

  clearAuth(): void {
    this.principal = null;
    clearStoredAuth();
  }

  async listPlayerSessions(playerId: string): Promise<PlayerSessionSummary[]> {
    await this.getPlayer(playerId);
    return fx.sessionsByPlayer[playerId] ?? [];
  }

  async listPlayerHands(playerId: string, sessionId: string): Promise<HandSummary[]> {
    await this.getPlayer(playerId);
    const seated = fx.seatedHandIds[playerId]?.[sessionId];
    if (seated === undefined) {
      throw notFound(`session_id=${sessionId} は存在しません。`);
    }
    const hands = fx.handsBySession[sessionId] ?? [];
    return hands
      .filter((h) => seated.includes(h.hand_id))
      .map((h) => this.applyCorrections(sessionId, h));
  }

  async getHand(sessionId: string, handId: number): Promise<HandSummary> {
    const hand = (fx.handsBySession[sessionId] ?? []).find((h) => h.hand_id === handId);
    if (!hand) {
      throw notFound(`hand_id=${handId} は session_id=${sessionId} に存在しません。`);
    }
    return this.applyCorrections(sessionId, hand);
  }

  async getPlayerLedger(playerId: string, sessionId: string): Promise<PlayerSessionLedger> {
    await this.getPlayer(playerId);
    if (fx.seatedHandIds[playerId]?.[sessionId] === undefined) {
      throw notFound(`session_id=${sessionId} は存在しません。`);
    }
    const entries: LedgerEntry[] = fx.ledgerEntries[playerId]?.[sessionId] ?? [];
    // SessionSettlement を 1 player に限定した中間集計 (ADR-0016)。mock はデータから素朴に畳む。
    const sumCash = (kinds: LedgerEntry["kind"][]) =>
      entries.filter((e) => kinds.includes(e.kind)).reduce((a, e) => a + e.cash_amount, 0);
    const sumPoint = (kinds: LedgerEntry["kind"][]) =>
      entries.filter((e) => kinds.includes(e.kind)).reduce((a, e) => a + e.point_amount, 0);
    return {
      entries,
      summary: {
        cash_in_total: sumCash(["buy_in", "rebuy", "add_on"]),
        order_total: sumCash(["order"]),
        entry_fee: sumCash(["entry_fee"]),
        point_spent_total: sumPoint(["buy_in", "rebuy", "add_on", "order"]),
        point_credited_total: 0, // mock は point grant を持たない
        net_due_to_store: entries.reduce((a, e) => a + e.cash_amount, 0),
        settled: false, // mock は未確定（確定フローはスタッフ desktop）
        payment_status: null,
        settled_at: null,
        paid_amount: 0,
      },
    };
  }

  async getMenu(): Promise<MenuItem[]> {
    return fx.menuItems;
  }

  async listOrderRequests(playerId: string, sessionId: string): Promise<OrderRequest[]> {
    await this.getPlayer(playerId);
    return this.orderRequests.filter(
      (r) => r.player_id === playerId && r.session_id === sessionId,
    );
  }

  async createOrderRequest(
    playerId: string,
    sessionId: string,
    body: OrderRequestBody,
  ): Promise<OrderRequest> {
    await this.getPlayer(playerId);
    if (!fx.menuItems.some((i) => i.item_name === body.item_name)) {
      throw new ViewerApiError({
        code: "unknown_item",
        message: `item_name=${body.item_name} はメニューにありません。`,
      });
    }
    if (!Number.isInteger(body.quantity) || body.quantity < 1 || body.quantity > 99) {
      throw new ViewerApiError({
        code: "invalid_quantity",
        message: "quantity は 1..99 の整数が必要です。",
      });
    }
    this.orderSeq += 1;
    const request: OrderRequest = {
      request_id: this.orderSeq.toString(16).padStart(32, "0"),
      session_id: sessionId,
      player_id: playerId,
      item_name: body.item_name,
      quantity: body.quantity,
      note: body.note,
      status: "pending",
      requested_at: new Date().toISOString().slice(0, 19),
    };
    this.orderRequests.push(request);
    return request;
  }

  async addHandCorrection(
    sessionId: string,
    handId: number,
    input: HandCorrectionInput,
  ): Promise<HandCorrection> {
    const idx = input.action_index ?? null;
    const actionFields = ["action", "amount"];
    const handFields = ["winner_seat"];
    const ok = idx === null ? handFields.includes(input.field) : actionFields.includes(input.field);
    if (!ok) {
      throw new ViewerApiError({
        code: "invalid_correction",
        message: `field=${input.field} はこの対象では訂正できません。`,
      });
    }
    this.corrSeq += 1;
    const correction: HandCorrection = {
      correction_id: this.corrSeq.toString(16).padStart(32, "0"),
      session_id: sessionId,
      hand_id: handId,
      action_index: idx,
      field: input.field,
      new_value: input.new_value,
      corrected_by: input.corrected_by ?? "staff",
      corrected_at: new Date().toISOString().slice(0, 19),
      note: input.note,
    };
    this.corrections.push(correction);
    return correction;
  }

  /** 元ハンドに訂正を重ねた訂正済みビューを返す（ADR-0036, python 版の overlay と対称）。 */
  private applyCorrections(sessionId: string, hand: HandSummary): HandSummary {
    const cs = this.corrections
      .filter((c) => c.session_id === sessionId && c.hand_id === hand.hand_id)
      .sort((a, b) => (a.corrected_at + a.correction_id).localeCompare(b.corrected_at + b.correction_id));
    if (cs.length === 0) return hand;
    const h = JSON.parse(JSON.stringify(hand)) as HandSummary & Record<string, unknown>;
    for (const c of cs) {
      if (c.action_index === null) {
        (h as Record<string, unknown>)[c.field] = c.new_value;
        continue;
      }
      const actions = h.actions ?? [];
      if (c.action_index < 0 || c.action_index >= actions.length) continue;
      const a = actions[c.action_index] as unknown as Record<string, unknown>;
      const orig = (a._original as Record<string, unknown>) ?? {};
      if (!(c.field in orig)) orig[c.field] = a[c.field];
      a._original = orig;
      a[c.field] = c.new_value;
      a.corrected = true;
      a.needs_review = false;
    }
    if (!(h.actions ?? []).some((a) => a.needs_review)) h.review_required = false;
    return h;
  }
}
