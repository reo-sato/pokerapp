/**
 * StaffRepository: staff API 契約 (api/server.py `/api/staff/...`, ADR-0021 / 0035 / 0036) に
 * 対応する front-end 抽象。UI はこの interface のみに依存し、mock (fixtures) と HTTP 実装を
 * 注入で差し替える (contract-first, ADR-0037 §6)。
 *
 * scope = 会計 (Ledger/精算) + 注文リクエスト捌き（ADR-0037 の最優先 2 機能。staff API が実装済）。
 * 座席 / ハンドロガー遠隔制御は ADR-0038（API 追加）後に additive 拡張する。
 *
 * 認可は staff shared token（`Authorization: Bearer <viewer_api.staff_token>`, ADR-0021）。
 * token 未設定/不一致は staff_writes_disabled(403) / unauthorized(401)、read-only プロセスへの
 * write は orders_unavailable(503) で reject する（StaffApiError の code で分岐）。
 */
import type {
  ControlCommand,
  HandControlInput,
  LedgerEntry,
  MenuItem,
  OrderRequest,
  Player,
  SeatAssignInput,
  SeatAssignment,
  SessionSettlement,
  StaffLedgerEntryBody,
  StaffSeating,
  StaffSession,
} from "./types";

export interface StaffRepository {
  // ――― 認可（staff shared token を保持・付与する） ―――
  /** staff token をセットする（端末ログイン）。 */
  setToken(token: string): void;
  /** 現在の token（未設定は null）。 */
  getToken(): string | null;
  /** token を破棄する（ログアウト）。 */
  clearToken(): void;

  // ――― 接続 / 認可確認 ―――
  /** API 死活。token 不要。 */
  health(): Promise<{ status: string; version: string }>;
  /**
   * token の有効性を確認する（任意の staff read を叩く）。
   * unauthorized / staff_writes_disabled で reject。ログイン画面で使う。
   */
  verifyToken(): Promise<void>;

  // ――― セッション（卓選択 / ライフサイクル, ADR-0038 §B）―――
  /** 全 session 一覧（staff の卓選択用, GET /api/staff/sessions）。 */
  listSessions(): Promise<StaffSession[]>;
  /** session を作成する（UUID4 採番, ADR-0007）。 */
  createSession(label?: string, blinds?: { sb?: number; bb?: number }): Promise<StaffSession>;
  /** session を close する（精算確定の前提）。 */
  closeSession(sessionId: string): Promise<StaffSession>;

  // ――― player（registry, ADR-0038 §B）―――
  /** registry の全 player（会計エントリ / 座席割当の player 選択用）。 */
  listPlayers(): Promise<Player[]>;
  /** player を作成する（座席タブのその場登録）。 */
  createPlayer(displayName: string): Promise<Player>;
  /** player の display_name をリネームする。 */
  renamePlayer(playerId: string, displayName: string): Promise<Player>;

  // ――― 座席（hand-based seating, ADR-0038 §B）―――
  /** 現在の seating（最新 hand 由来）+ 記録済 hand_id 一覧。 */
  getSeating(sessionId: string): Promise<StaffSeating>;
  /** 指定 hand に seat→player を割り当てる（append。conflict は error）。 */
  assignSeats(
    sessionId: string,
    handId: number,
    assignments: SeatAssignInput[],
  ): Promise<SeatAssignment[]>;

  // ――― 会計（Ledger / 精算）―――
  /** buy-in 金額プリセット（config 由来, ADR-0026）。 */
  getBuyinPresets(): Promise<number[]>;
  /** session の中間集計（compute_settlement, 暫定 = speculative）。 */
  getSettlement(sessionId: string): Promise<SessionSettlement[]>;
  /** ledger entry を追加する（buy_in/rebuy/add_on/order/entry_fee/adjustment, cash+point）。 */
  addLedgerEntry(sessionId: string, body: StaffLedgerEntryBody): Promise<LedgerEntry>;
  /** session の ledger entry 一覧（reversal UI 用 read, ADR-0038 §A）。 */
  listLedgerEntries(sessionId: string): Promise<LedgerEntry[]>;
  /** ledger entry を reversal で取り消す（append-only, ADR-0038 §A）。 */
  reverseEntry(entryId: string): Promise<LedgerEntry>;
  /** point を付与する（manual_grant, ADR-0038 §A）。 */
  grantPoints(playerId: string, deltaPoints: number): Promise<void>;
  /** session を精算確定する（closed session のみ。確定済 settlement 行を返す）。 */
  commitSettlement(sessionId: string): Promise<SessionSettlement[]>;
  /** 確定済 settlement の支払状態を paid/unpaid に切り替える。 */
  setPaymentStatus(
    sessionId: string,
    playerId: string,
    status: "paid" | "unpaid",
  ): Promise<SessionSettlement>;
  /** 受領額を記録し payment_status を導出する（partial-paid, ADR-0023）。 */
  recordPayment(
    sessionId: string,
    playerId: string,
    paidAmount: number,
  ): Promise<SessionSettlement>;

  // ――― 注文リクエスト捌き（M5 / ADR-0018）―――
  /** 注文メニュー（menu.json master）。確定時の単価 prefill に使う。 */
  getMenu(): Promise<MenuItem[]>;
  /** session の注文 queue（全 player。status で絞り込み可）。 */
  listOrderRequests(sessionId: string, status?: string): Promise<OrderRequest[]>;
  /** 注文を確定する（単価を確定し order ledger entry を起こす）。 */
  confirmOrder(requestId: string, unitAmount: number): Promise<OrderRequest>;
  /** 注文を却下する。 */
  rejectOrder(requestId: string): Promise<OrderRequest>;

  // ――― ハンド（hand logger 遠隔制御, ADR-0039 §C）―――
  /**
   * hand logger に制御コマンド（新ハンド / ウィナー / リバイ）を送る。録音は PC 常駐のまま、
   * iPad はコマンドを control queue に積むだけ（適用は hand logger プロセス）。
   */
  sendControl(sessionId: string, input: HandControlInput): Promise<ControlCommand>;
}
