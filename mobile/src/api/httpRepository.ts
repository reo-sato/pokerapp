/**
 * HttpRepository: viewer API (api/server.py) を fetch する実装 (M2, ADR-0013)。
 *
 * baseUrl 例: "http://192.168.1.10:8788"（運営 PC の LAN IP + viewer_api.bind_port）。
 * 非 2xx は body の {code, message} を ViewerApiError として throw する。
 */
import type { ViewerRepository } from "./repository";
import type { HandSummary, Player, PlayerSessionSummary } from "./types";
import { ViewerApiError } from "./types";

export class HttpRepository implements ViewerRepository {
  private readonly baseUrl: string;

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
}
