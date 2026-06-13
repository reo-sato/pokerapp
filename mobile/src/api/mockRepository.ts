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
import * as fx from "../mocks/fixtures";

function notFound(message: string): ViewerApiError {
  return new ViewerApiError({ code: "not_found", message });
}

export class MockRepository implements ViewerRepository {
  // 注文リクエストは mock 内の in-memory 状態（pending のまま。確定はスタッフ desktop の責務）
  private orderRequests: OrderRequest[] = [];
  private orderSeq = 0;

  async health(): Promise<{ status: string; version: string }> {
    return { status: "ok", version: "mock" };
  }

  async listPlayers(): Promise<Player[]> {
    return fx.players;
  }

  async getPlayer(playerId: string): Promise<Player> {
    const player = fx.players.find((p) => p.player_id === playerId);
    if (!player) throw notFound(`player_id=${playerId} は存在しません。`);
    return player;
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
    return hands.filter((h) => seated.includes(h.hand_id));
  }

  async getHand(sessionId: string, handId: number): Promise<HandSummary> {
    const hand = (fx.handsBySession[sessionId] ?? []).find((h) => h.hand_id === handId);
    if (!hand) {
      throw notFound(`hand_id=${handId} は session_id=${sessionId} に存在しません。`);
    }
    return hand;
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
}
