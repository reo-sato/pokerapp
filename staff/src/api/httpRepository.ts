/**
 * HttpStaffRepository: staff API (api/server.py の `/api/staff/...`, ADR-0021) を fetch する実装。
 *
 * baseUrl 例: "http://192.168.1.10:8788"（運営 PC の LAN IP + viewer_api.bind_port）。staff token を
 * `Authorization: Bearer <token>` で付与する。非 2xx は body の {code,message} を StaffApiError に。
 *
 * write は **write 所有プロセス**（`python main.py --ledger` + viewer_api.enabled + staff_token）の
 * み有効（単独 `--viewer-api` は read-only → orders_unavailable(503)）。ADR-0020/0021。
 *
 * 未実装 endpoint（ADR-0038 で追加予定）は not_implemented で reject し、mock 先行との差分を明示する。
 */
import type { StaffRepository } from "./repository";
import {
  type ControlCommand,
  type GroundTruthEditPayload,
  type GroundTruthHand,
  type HandControlInput,
  type LedgerEntry,
  type MeasurementRow,
  type MenuItem,
  type OrderRequest,
  type Player,
  type SeatAssignInput,
  type SeatAssignment,
  type SessionSettlement,
  StaffApiError,
  type StaffLedgerEntryBody,
  type StaffSeating,
  type StaffSession,
} from "./types";

const E = encodeURIComponent;

export class HttpStaffRepository implements StaffRepository {
  private readonly baseUrl: string;
  private token: string | null = null;

  constructor(baseUrl: string, token?: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.token = token ?? null;
  }

  setToken(token: string): void {
    this.token = token;
  }

  getToken(): string | null {
    return this.token;
  }

  clearToken(): void {
    this.token = null;
  }

  private authHeaders(): Record<string, string> {
    return this.token ? { Authorization: `Bearer ${this.token}` } : {};
  }

  private async parse<T>(res: Response): Promise<T> {
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new StaffApiError({
        code: (body as { code?: string })?.code ?? "unknown_error",
        message: (body as { message?: string })?.message ?? `HTTP ${res.status}`,
      });
    }
    return body as T;
  }

  private async get<T>(path: string, auth = true): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      headers: auth ? this.authHeaders() : {},
    });
    return this.parse<T>(res);
  }

  private async send<T>(method: string, path: string, payload?: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: { "Content-Type": "application/json", ...this.authHeaders() },
      body: JSON.stringify(payload ?? {}),
    });
    return this.parse<T>(res);
  }

  health(): Promise<{ status: string; version: string }> {
    return this.get("/api/health", false);
  }

  async verifyToken(): Promise<void> {
    // 安価な staff read を叩いて token を検証する（unauthorized/staff_writes_disabled が propagate）。
    await this.get("/api/staff/buyin-presets");
  }

  async listSessions(): Promise<StaffSession[]> {
    const body = await this.get<{ sessions: StaffSession[] }>("/api/staff/sessions");
    return body.sessions;
  }

  createSession(label?: string, blinds?: { sb?: number; bb?: number }): Promise<StaffSession> {
    const payload: { label?: string; blinds?: { sb?: number; bb?: number } } = {};
    if (label) payload.label = label;
    if (blinds) payload.blinds = blinds;
    return this.send("POST", "/api/staff/sessions", payload);
  }

  closeSession(sessionId: string): Promise<StaffSession> {
    return this.send("POST", `/api/staff/sessions/${E(sessionId)}/close`);
  }

  async listPlayers(): Promise<Player[]> {
    const body = await this.get<{ players: Player[] }>("/api/staff/players");
    return body.players;
  }

  createPlayer(displayName: string): Promise<Player> {
    return this.send("POST", "/api/staff/players", { display_name: displayName });
  }

  renamePlayer(playerId: string, displayName: string): Promise<Player> {
    return this.send("PUT", `/api/staff/players/${E(playerId)}`, { display_name: displayName });
  }

  getSeating(sessionId: string): Promise<StaffSeating> {
    return this.get(`/api/staff/sessions/${E(sessionId)}/seating`);
  }

  async assignSeats(
    sessionId: string,
    handId: number,
    assignments: SeatAssignInput[],
  ): Promise<SeatAssignment[]> {
    const body = await this.send<{ assignments: SeatAssignment[] }>(
      "PUT",
      `/api/staff/sessions/${E(sessionId)}/hands/${handId}/seats`,
      { assignments },
    );
    return body.assignments;
  }

  async getBuyinPresets(): Promise<number[]> {
    const body = await this.get<{ presets: number[] }>("/api/staff/buyin-presets");
    return body.presets;
  }

  async getSettlement(sessionId: string): Promise<SessionSettlement[]> {
    const body = await this.get<{ settlements: SessionSettlement[] }>(
      `/api/staff/sessions/${E(sessionId)}/settlement`,
    );
    return body.settlements;
  }

  addLedgerEntry(sessionId: string, body: StaffLedgerEntryBody): Promise<LedgerEntry> {
    return this.send("POST", `/api/staff/sessions/${E(sessionId)}/ledger-entries`, body);
  }

  async listLedgerEntries(sessionId: string): Promise<LedgerEntry[]> {
    const body = await this.get<{ entries: LedgerEntry[] }>(
      `/api/staff/sessions/${E(sessionId)}/ledger-entries`,
    );
    return body.entries;
  }

  reverseEntry(entryId: string): Promise<LedgerEntry> {
    return this.send("POST", `/api/staff/ledger-entries/${E(entryId)}/reverse`);
  }

  async grantPoints(playerId: string, deltaPoints: number): Promise<void> {
    await this.send("POST", `/api/staff/players/${E(playerId)}/point-grants`, {
      delta_points: deltaPoints,
    });
  }

  async commitSettlement(sessionId: string): Promise<SessionSettlement[]> {
    const body = await this.send<{ settlements: SessionSettlement[] }>(
      "POST",
      `/api/staff/sessions/${E(sessionId)}/settlement/commit`,
    );
    return body.settlements;
  }

  setPaymentStatus(
    sessionId: string,
    playerId: string,
    status: "paid" | "unpaid",
  ): Promise<SessionSettlement> {
    return this.send(
      "PUT",
      `/api/staff/sessions/${E(sessionId)}/players/${E(playerId)}/payment-status`,
      { status },
    );
  }

  recordPayment(
    sessionId: string,
    playerId: string,
    paidAmount: number,
  ): Promise<SessionSettlement> {
    return this.send(
      "PUT",
      `/api/staff/sessions/${E(sessionId)}/players/${E(playerId)}/payment`,
      { paid_amount: paidAmount },
    );
  }

  async getMenu(): Promise<MenuItem[]> {
    const body = await this.get<{ items: MenuItem[] }>("/api/menu", false);
    return body.items;
  }

  async listOrderRequests(sessionId: string, status?: string): Promise<OrderRequest[]> {
    const q = status ? `?status=${E(status)}` : "";
    const body = await this.get<{ requests: OrderRequest[] }>(
      `/api/staff/sessions/${E(sessionId)}/order-requests${q}`,
    );
    return body.requests;
  }

  confirmOrder(requestId: string, unitAmount: number): Promise<OrderRequest> {
    return this.send("POST", `/api/staff/order-requests/${E(requestId)}/confirm`, {
      unit_amount: unitAmount,
    });
  }

  rejectOrder(requestId: string): Promise<OrderRequest> {
    return this.send("POST", `/api/staff/order-requests/${E(requestId)}/reject`);
  }

  sendControl(sessionId: string, input: HandControlInput): Promise<ControlCommand> {
    const payload: { type: string; seat?: number; amount?: number } = { type: input.type };
    if (input.seat !== undefined) payload.seat = input.seat;
    if (input.amount !== undefined) payload.amount = input.amount;
    return this.send("POST", `/api/staff/sessions/${E(sessionId)}/control`, payload);
  }

  // ――― Phase A 計測 / ground truth（ADR-0043）―――

  async listMeasurementRows(sessionId: string): Promise<MeasurementRow[]> {
    const body = await this.get<{ rows: MeasurementRow[] }>(
      `/api/staff/sessions/${E(sessionId)}/measurement-rows`,
    );
    return body.rows;
  }

  passThroughGroundTruth(
    sessionId: string,
    handId: number,
    annotator = "staff",
  ): Promise<GroundTruthHand> {
    return this.send(
      "PUT",
      `/api/staff/sessions/${E(sessionId)}/ground-truth/${handId}`,
      { source: "captured-passthrough", annotator },
    );
  }

  submitGroundTruthEdit(
    sessionId: string,
    handId: number,
    payload: GroundTruthEditPayload,
    annotator = "staff",
  ): Promise<GroundTruthHand> {
    return this.send(
      "PUT",
      `/api/staff/sessions/${E(sessionId)}/ground-truth/${handId}`,
      { source: "manual-edit", annotator, hand: payload },
    );
  }

  getGroundTruth(sessionId: string, handId: number): Promise<GroundTruthHand> {
    return this.get(`/api/staff/sessions/${E(sessionId)}/ground-truth/${handId}`);
  }
}
