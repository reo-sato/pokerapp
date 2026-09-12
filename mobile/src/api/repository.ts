/**
 * ViewerRepository: viewer API 契約 (docs/contracts/viewer-api.md) に対応する
 * front-end 抽象 (docs/contracts/repository-interfaces.md の原則に従う)。
 *
 * UI はこの interface のみに依存し、mock (fixtures) と HTTP 実装を注入で差し替える。
 * not_found 等は ViewerApiError (code 分岐) で reject する。
 */
import type {
  AuthSession,
  HandCorrection,
  HandCorrectionInput,
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
  /**
   * 本人認証レイヤ (player_auth=off の既定では name-pick のまま不要)。
   * - login: L1 PIN (ADR-0027)。invalid_pin / pin_locked / player_auth_disabled で reject。
   * - oidcExchange: L2 外部 IdP (ADR-0031)。認可コードを交換。unknown_provider / invalid_idp_code。
   * 成功時はトークンを保持し、以後の self-write (createOrderRequest) に付与する。
   */
  login(playerId: string, pin: string): Promise<AuthSession>;
  oidcExchange(provider: string, code: string): Promise<AuthSession>;
  /** 現在の principal (ログイン済み player_id) / 未ログインは null。 */
  currentPrincipal(): string | null;
  /** トークンを破棄する (ログアウト / player 切替時)。 */
  clearAuth(): void;
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
  /** B4 (ADR-0036): ハンド訂正を追記する（staff 操作 = iPad）。append-only オーバーレイ。
      getHand / listPlayerHands は訂正済みビューを返す。staff token 必須。 */
  addHandCorrection(
    sessionId: string,
    handId: number,
    input: HandCorrectionInput,
  ): Promise<HandCorrection>;
}
