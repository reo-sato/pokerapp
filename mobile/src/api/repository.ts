/**
 * ViewerRepository: viewer API 契約 (docs/contracts/viewer-api.md) に対応する
 * front-end 抽象 (docs/contracts/repository-interfaces.md の原則に従う)。
 *
 * UI はこの interface のみに依存し、mock (fixtures) と HTTP 実装を注入で差し替える。
 * not_found 等は ViewerApiError (code 分岐) で reject する。
 */
import type { HandSummary, Player, PlayerSessionSummary } from "./types";

export interface ViewerRepository {
  health(): Promise<{ status: string; version: string }>;
  listPlayers(): Promise<Player[]>;
  getPlayer(playerId: string): Promise<Player>;
  listPlayerSessions(playerId: string): Promise<PlayerSessionSummary[]>;
  listPlayerHands(playerId: string, sessionId: string): Promise<HandSummary[]>;
  getHand(sessionId: string, handId: number): Promise<HandSummary>;
}
