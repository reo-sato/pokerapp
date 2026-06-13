/**
 * ViewerRepository: viewer API 契約 (docs/contracts/viewer-api.md) に対応する
 * front-end 抽象 (docs/contracts/repository-interfaces.md の原則に従う)。
 *
 * UI はこの interface のみに依存し、mock (fixtures) と HTTP 実装を注入で差し替える。
 * not_found 等は ViewerApiError (code 分岐) で reject する。
 */
import type {
  HandSummary,
  MenuItem,
  OrderRequest,
  OrderRequestBody,
  Player,
  PlayerSessionLedger,
  PlayerSessionSummary,
} from "./types";

export interface ViewerRepository {
  health(): Promise<{ status: string; version: string }>;
  listPlayers(): Promise<Player[]>;
  getPlayer(playerId: string): Promise<Player>;
  listPlayerSessions(playerId: string): Promise<PlayerSessionSummary[]>;
  listPlayerHands(playerId: string, sessionId: string): Promise<HandSummary[]>;
  getHand(sessionId: string, handId: number): Promise<HandSummary>;
  /** 会計参照 (ADR-0016 ledger): 自分の会計参照（entries + 中間集計, read-only）。 */
  getPlayerLedger(playerId: string, sessionId: string): Promise<PlayerSessionLedger>;
  /** M5 (ADR-0018): 注文メニュー / 自分のリクエスト一覧 / 注文リクエスト送信。
      送信は pending を作るだけで、会計への記帳はスタッフ確定後。 */
  getMenu(): Promise<MenuItem[]>;
  listOrderRequests(playerId: string, sessionId: string): Promise<OrderRequest[]>;
  createOrderRequest(
    playerId: string,
    sessionId: string,
    body: OrderRequestBody,
  ): Promise<OrderRequest>;
}
