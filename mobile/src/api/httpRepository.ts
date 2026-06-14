/**
 * HttpRepository: viewer API (api/server.py) を fetch する実装 (M2, ADR-0017)。
 *
 * baseUrl 例: "http://192.168.1.10:8788"（運営 PC の LAN IP + viewer_api.bind_port）。
 * 非 2xx は body の {code, message} を ViewerApiError として throw する。
 */
import type { ViewerRepository } from "./repository";
import type {
  AuthSession,
  HandSummary,
  MenuItem,
  OrderRequest,
  OrderRequestBody,
  Player,
  PlayerSessionLedger,
  PlayerSessionSummary,
} from "./types";
import { ViewerApiError } from "./types";

export class HttpRepository implements ViewerRepository {
  private readonly baseUrl: string;
  // player principal トークン (L1/L2)。self-write (注文 POST) に Bearer で付与する。
  private playerToken: string | null = null;
  private principal: string | null = null;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  private async get<T>(path: string): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`);
    const body = await res.json();
    if (!res.ok) {
      throw new ViewerApiError({
        code: body?.code ?? "unknown_error",
        message: body?.message ?? `HTTP ${res.status}`,
      });
    }
    return body as T;
  }

  private async post<T>(path: string, payload: unknown, withAuth = false): Promise<T> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (withAuth && this.playerToken) {
      headers.Authorization = `Bearer ${this.playerToken}`;
    }
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    const body = await res.json();
    if (!res.ok) {
      throw new ViewerApiError({
        code: body?.code ?? "unknown_error",
        message: body?.message ?? `HTTP ${res.status}`,
      });
    }
    return body as T;
  }

  health(): Promise<{ status: string; version: string }> {
    return this.get("/api/health");
  }

  async listPlayers(): Promise<Player[]> {
    const body = await this.get<{ players: Player[] }>("/api/players");
    return body.players;
  }

  getPlayer(playerId: string): Promise<Player> {
    return this.get(`/api/players/${encodeURIComponent(playerId)}`);
  }

  async login(playerId: string, pin: string): Promise<AuthSession> {
    const session = await this.post<AuthSession>(
      "/api/auth/login", { player_id: playerId, pin }, false,
    );
    this.playerToken = session.token;
    this.principal = session.player_id;
    return session;
  }

  async oidcExchange(provider: string, code: string): Promise<AuthSession> {
    const session = await this.post<AuthSession>(
      `/api/auth/${encodeURIComponent(provider)}/exchange`, { code }, false,
    );
    this.playerToken = session.token;
    this.principal = session.player_id;
    return session;
  }

  currentPrincipal(): string | null {
    return this.principal;
  }

  clearAuth(): void {
    this.playerToken = null;
    this.principal = null;
  }

  async listPlayerSessions(playerId: string): Promise<PlayerSessionSummary[]> {
    const body = await this.get<{ sessions: PlayerSessionSummary[] }>(
      `/api/players/${encodeURIComponent(playerId)}/sessions`,
    );
    return body.sessions;
  }

  async listPlayerHands(playerId: string, sessionId: string): Promise<HandSummary[]> {
    const body = await this.get<{ hands: HandSummary[] }>(
      `/api/players/${encodeURIComponent(playerId)}/sessions/${encodeURIComponent(sessionId)}/hands`,
    );
    return body.hands;
  }

  getHand(sessionId: string, handId: number): Promise<HandSummary> {
    return this.get(`/api/sessions/${encodeURIComponent(sessionId)}/hands/${handId}`);
  }

  getPlayerLedger(playerId: string, sessionId: string): Promise<PlayerSessionLedger> {
    return this.get(
      `/api/players/${encodeURIComponent(playerId)}/sessions/${encodeURIComponent(sessionId)}/ledger`,
    );
  }

  async getMenu(): Promise<MenuItem[]> {
    const body = await this.get<{ items: MenuItem[] }>("/api/menu");
    return body.items;
  }

  async listOrderRequests(playerId: string, sessionId: string): Promise<OrderRequest[]> {
    const body = await this.get<{ requests: OrderRequest[] }>(
      `/api/players/${encodeURIComponent(playerId)}/sessions/${encodeURIComponent(sessionId)}/order-requests`,
    );
    return body.requests;
  }

  createOrderRequest(
    playerId: string,
    sessionId: string,
    body: OrderRequestBody,
  ): Promise<OrderRequest> {
    // self-write: principal トークンがあれば付与する (player_auth=optional/required, ADR-0027/0031)。
    return this.post(
      `/api/players/${encodeURIComponent(playerId)}/sessions/${encodeURIComponent(sessionId)}/order-requests`,
      body,
      true,
    );
  }
}
