/**
 * MockStaffRepository: 契約 fixtures 相当の in-memory 実装 (ADR-0035 §6)。
 *
 * API なしで staff app の画面遷移・状態管理・validation 表示を先行開発するための実装。
 * 業務ルールは core (api/server.py + core/ledger_repository.py) を **近似** する（source of
 * truth は core。ここでは UI 開発に必要な範囲の意味論だけを再現する）。error は code 分岐を
 * core / error-shapes.md と一致させる（not_found / invalid_amount / entry_fee_requires_cash /
 * session_not_closed / already_settled / session_closed / already_resolved / unauthorized）。
 */
import type { StaffRepository } from "./repository";
import {
  type LedgerEntry,
  type LedgerKind,
  type MenuItem,
  type OrderRequest,
  type Player,
  type SessionSettlement,
  StaffApiError,
  type StaffLedgerEntryBody,
  type StaffSession,
} from "./types";
import {
  buyinPresets,
  ledgerEntries,
  menuItems,
  orderRequests,
  players,
  sessions,
  VALID_STAFF_TOKEN,
} from "../mocks/fixtures";

const LEDGER_KINDS: LedgerKind[] = [
  "buy_in",
  "rebuy",
  "add_on",
  "order",
  "entry_fee",
  "adjustment",
];
const CASH_IN_KINDS: LedgerKind[] = ["buy_in", "rebuy", "add_on"];

/** net と受領累計から payment_status を導出する（ADR-0023 と同じ規則）。 */
function derivePaymentStatus(net: number, paid: number): string {
  if (net <= 0) return "paid";
  if (paid <= 0) return "unpaid";
  if (paid >= net) return "paid";
  return "partial";
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export class MockStaffRepository implements StaffRepository {
  private token: string | null = null;
  private seq = 0;
  private readonly sessions: StaffSession[];
  private readonly players: Player[];
  private readonly presets: number[];
  private readonly menu: MenuItem[];
  private readonly ledger: Record<string, LedgerEntry[]>;
  private readonly orders: Record<string, OrderRequest[]>;
  // 確定済 settlement（session_id → player_id → row）。commit でのみ生成。
  private readonly committed: Record<string, Record<string, SessionSettlement>> = {};

  constructor() {
    this.sessions = clone(sessions);
    this.players = clone(players);
    this.presets = clone(buyinPresets);
    this.menu = clone(menuItems);
    this.ledger = clone(ledgerEntries);
    this.orders = clone(orderRequests);
  }

  // ――― 認可 ―――

  setToken(token: string): void {
    this.token = token;
  }

  getToken(): string | null {
    return this.token;
  }

  clearToken(): void {
    this.token = null;
  }

  private requireAuth(): void {
    // server の _staff_guard 近似: token 未設定/不一致は unauthorized で弾く
    // （server は token 未設定=403 staff_writes_disabled / 不一致=401 unauthorized を区別）。
    if (!this.token || this.token !== VALID_STAFF_TOKEN) {
      throw new StaffApiError({
        code: "unauthorized",
        message: "スタッフトークンが無効です。",
      });
    }
  }

  private nextId(prefix: string): string {
    this.seq += 1;
    return `${prefix}${String(this.seq).padStart(30, "0")}`;
  }

  private requireSession(sessionId: string): StaffSession {
    const s = this.sessions.find((x) => x.session_id === sessionId);
    if (!s) {
      throw new StaffApiError({ code: "not_found", message: "session が見つかりません。" });
    }
    return s;
  }

  // ――― 接続 / 認可確認 ―――

  async health(): Promise<{ status: string; version: string }> {
    return { status: "ok", version: "staff-mock" };
  }

  async verifyToken(): Promise<void> {
    this.requireAuth();
  }

  // ――― セッション / player ―――

  async listSessions(): Promise<StaffSession[]> {
    this.requireAuth();
    return clone(this.sessions);
  }

  async listPlayers(): Promise<Player[]> {
    this.requireAuth();
    return clone(this.players);
  }

  // ――― 会計 ―――

  async getBuyinPresets(): Promise<number[]> {
    this.requireAuth();
    return [...this.presets];
  }

  private computeSettlement(sessionId: string): SessionSettlement[] {
    const entries = this.ledger[sessionId] ?? [];
    const order: string[] = [];
    const byPlayer: Record<string, LedgerEntry[]> = {};
    for (const e of entries) {
      if (!(e.player_id in byPlayer)) {
        byPlayer[e.player_id] = [];
        order.push(e.player_id);
      }
      byPlayer[e.player_id].push(e);
    }
    return order.map((pid) => {
      const es = byPlayer[pid];
      const sum = (pred: (e: LedgerEntry) => boolean): number =>
        es.filter(pred).reduce((acc, e) => acc + e.cash_amount, 0);
      const cash_in_total = sum((e) => CASH_IN_KINDS.includes(e.kind));
      const order_total = sum((e) => e.kind === "order");
      const entry_fee = sum((e) => e.kind === "entry_fee");
      const point_spent_total = es.reduce(
        (acc, e) => acc + Math.max(0, e.point_amount),
        0,
      );
      const net_due_to_store = es.reduce((acc, e) => acc + e.cash_amount, 0);
      return {
        session_id: sessionId,
        player_id: pid,
        cash_in_total,
        point_spent_total,
        order_total,
        entry_fee,
        point_credited_total: 0,
        net_due_to_store,
        payment_status: "unpaid",
        settled_at: "",
        paid_amount: 0,
      };
    });
  }

  async getSettlement(sessionId: string): Promise<SessionSettlement[]> {
    this.requireAuth();
    this.requireSession(sessionId);
    // GET .../settlement = compute_settlement = 中間集計（暫定 / speculative）。
    return this.computeSettlement(sessionId);
  }

  async addLedgerEntry(
    sessionId: string,
    body: StaffLedgerEntryBody,
  ): Promise<LedgerEntry> {
    this.requireAuth();
    this.requireSession(sessionId);
    const kind = body.kind as LedgerKind;
    if (!LEDGER_KINDS.includes(kind)) {
      throw new StaffApiError({ code: "invalid_amount", message: `不正な kind: ${body.kind}` });
    }
    const cash = body.cash_amount ?? 0;
    const point = body.point_amount ?? 0;
    if (!Number.isInteger(cash) || !Number.isInteger(point)) {
      throw new StaffApiError({ code: "invalid_amount", message: "金額は整数で入力してください。" });
    }
    if (kind === "entry_fee" && point !== 0) {
      throw new StaffApiError({
        code: "entry_fee_requires_cash",
        message: "entry fee は cash のみで支払えます。",
      });
    }
    const entry: LedgerEntry = {
      entry_id: this.nextId("e"),
      session_id: sessionId,
      player_id: body.player_id,
      kind,
      occurred_at: new Date().toISOString(),
      cash_amount: cash,
      point_amount: point,
      ...(body.note ? { note: body.note } : {}),
      ...(body.order ? { order: body.order } : {}),
    };
    (this.ledger[sessionId] ??= []).push(entry);
    return clone(entry);
  }

  async commitSettlement(sessionId: string): Promise<SessionSettlement[]> {
    this.requireAuth();
    const session = this.requireSession(sessionId);
    if (session.status !== "closed") {
      throw new StaffApiError({
        code: "session_not_closed",
        message: "open session は確定できません（先に session を close してください）。",
      });
    }
    if (this.committed[sessionId]) {
      throw new StaffApiError({ code: "already_settled", message: "すでに確定済みです。" });
    }
    const now = new Date().toISOString();
    const rows = this.computeSettlement(sessionId).map((r) => ({
      ...r,
      settled_at: now,
      payment_status: derivePaymentStatus(r.net_due_to_store, 0),
    }));
    this.committed[sessionId] = {};
    for (const r of rows) this.committed[sessionId][r.player_id] = r;
    return clone(rows);
  }

  private requireCommitted(sessionId: string, playerId: string): SessionSettlement {
    const row = this.committed[sessionId]?.[playerId];
    if (!row) {
      throw new StaffApiError({ code: "not_found", message: "確定済 settlement が見つかりません。" });
    }
    return row;
  }

  async setPaymentStatus(
    sessionId: string,
    playerId: string,
    status: "paid" | "unpaid",
  ): Promise<SessionSettlement> {
    this.requireAuth();
    const row = this.requireCommitted(sessionId, playerId);
    row.paid_amount = status === "paid" ? Math.max(0, row.net_due_to_store) : 0;
    row.payment_status = derivePaymentStatus(row.net_due_to_store, row.paid_amount);
    return clone(row);
  }

  async recordPayment(
    sessionId: string,
    playerId: string,
    paidAmount: number,
  ): Promise<SessionSettlement> {
    this.requireAuth();
    if (!Number.isInteger(paidAmount) || paidAmount < 0) {
      throw new StaffApiError({ code: "invalid_amount", message: "受領額は 0 以上の整数です。" });
    }
    const row = this.requireCommitted(sessionId, playerId);
    row.paid_amount = paidAmount;
    row.payment_status = derivePaymentStatus(row.net_due_to_store, paidAmount);
    return clone(row);
  }

  // ――― 注文 ―――

  async getMenu(): Promise<MenuItem[]> {
    return clone(this.menu);
  }

  async listOrderRequests(sessionId: string, status?: string): Promise<OrderRequest[]> {
    this.requireAuth();
    this.requireSession(sessionId);
    const all = this.orders[sessionId] ?? [];
    return clone(status ? all.filter((r) => r.status === status) : all);
  }

  private findOrder(requestId: string): { req: OrderRequest; sessionId: string } {
    for (const [sessionId, reqs] of Object.entries(this.orders)) {
      const req = reqs.find((r) => r.request_id === requestId);
      if (req) return { req, sessionId };
    }
    throw new StaffApiError({ code: "not_found", message: "注文リクエストが見つかりません。" });
  }

  async confirmOrder(requestId: string, unitAmount: number): Promise<OrderRequest> {
    this.requireAuth();
    if (!Number.isInteger(unitAmount) || unitAmount < 0) {
      throw new StaffApiError({ code: "invalid_amount", message: "単価は 0 以上の整数です。" });
    }
    const { req, sessionId } = this.findOrder(requestId);
    if (req.status !== "pending") {
      throw new StaffApiError({ code: "already_resolved", message: "この注文は処理済みです。" });
    }
    const session = this.requireSession(sessionId);
    if (session.status === "closed") {
      throw new StaffApiError({
        code: "session_closed",
        message: "closed session には注文を確定できません。",
      });
    }
    // staff-in-the-loop: 確定で order ledger entry を起こす（ADR-0018）。
    const entry = await this.addLedgerEntry(sessionId, {
      player_id: req.player_id,
      kind: "order",
      cash_amount: unitAmount * req.quantity,
      point_amount: 0,
      order: { item_name: req.item_name, unit_amount: unitAmount, quantity: req.quantity },
    });
    req.status = "confirmed";
    req.resolved_at = new Date().toISOString();
    req.ledger_entry_id = entry.entry_id;
    return clone(req);
  }

  async rejectOrder(requestId: string): Promise<OrderRequest> {
    this.requireAuth();
    const { req } = this.findOrder(requestId);
    if (req.status !== "pending") {
      throw new StaffApiError({ code: "already_resolved", message: "この注文は処理済みです。" });
    }
    req.status = "rejected";
    req.resolved_at = new Date().toISOString();
    return clone(req);
  }
}
