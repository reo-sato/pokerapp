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
    // 中間集計は core (validation-rules.md) と同じ定義。mock はデータから素朴に畳む。
    const sum = (kinds: LedgerEntry["kind"][]) =>
      entries.filter((e) => kinds.includes(e.kind)).reduce((a, e) => a + e.cash_amount, 0);
    const buyIn = sum(["buy_in", "rebuy", "add_on"]);
    const order = sum(["order"]);
    const adjustment = sum(["adjustment"]);
    return {
      entries,
      summary: {
        buy_in_total: buyIn,
        order_total: order,
        adjustment_total: adjustment,
        total_due: buyIn + order + adjustment,
      },
    };
  }
}
