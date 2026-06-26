/**
 * staff API の型 (ADR-0037 / ADR-0038)。docs/contracts/ の schema と
 * api/server.py の staff endpoints (`/api/staff/...`, ADR-0021) から転記する。
 *
 * - Player:            schemas/player.schema.json (1.2)
 * - LedgerEntry:       schemas/ledger_entry.schema.json (verify-v1 ledger, ADR-0016)
 * - SessionSettlement: core/ledger.py SessionSettlement.to_dict (1.1, ADR-0023)
 * - OrderRequest:      schemas/order_request.schema.json (0.x, M5 / ADR-0018)
 * - ApiError:          docs/contracts/error-shapes.md
 *
 * validation / 業務ルールはここに複製しない (core が source of truth)。mobile/ の types.ts と
 * 重複する型は意図的に再掲する (staff app は player 用 mobile/ とは別アプリ, ADR-0037 §1)。
 */

export interface Player {
  player_id: string; // UUID4 hex (32 文字)
  display_name: string;
  created_at: string; // ISO 8601
  updated_at?: string;
  merged_into?: string; // merge 済み tombstone の survivor (ADR-0030)
  merged_at?: string;
}

export interface Blinds {
  sb?: number;
  bb?: number;
}

/**
 * staff の卓選択用 session 要約。
 * GET /api/staff/sessions（ADR-0038 §B, 未実装）/ mock fixtures。
 */
export interface StaffSession {
  session_id: string;
  label?: string;
  started_at: string;
  ended_at?: string;
  blinds?: Blinds;
  status: "open" | "closed";
}

/** core/session.py SeatAssignment.to_dict（hand-based seating, ADR-0006/0007）。 */
export interface SeatAssignment {
  session_id: string;
  hand_id: number;
  seat_no: number;
  player_id: string;
  status?: string;
}

/** GET /api/staff/sessions/{sid}/seating の応答（ADR-0038 §B）。 */
export interface StaffSeating {
  seating: SeatAssignment[];
  hand_ids: number[];
}

/** PUT .../hands/{hid}/seats の 1 割り当て（ADR-0038 §B）。 */
export interface SeatAssignInput {
  seat_no: number;
  player_id: string;
}

export interface OrderDetail {
  item_name: string;
  unit_amount: number;
  quantity: number;
}

/** schemas/ledger_entry.schema.json (verify-v1 ledger — ADR-0016)。 */
export interface LedgerEntry {
  entry_id: string;
  session_id: string;
  player_id: string;
  kind: "buy_in" | "rebuy" | "add_on" | "order" | "entry_fee" | "adjustment";
  occurred_at: string;
  cash_amount: number;
  point_amount: number;
  note?: string;
  hand_id?: number;
  reverses_entry_id?: string;
  order?: OrderDetail;
}

export type LedgerKind = LedgerEntry["kind"];

/** POST /api/staff/sessions/{sid}/ledger-entries の body (_StaffLedgerEntryBody)。 */
export interface StaffLedgerEntryBody {
  player_id: string;
  kind: string;
  cash_amount?: number;
  point_amount?: number;
  note?: string | null;
  hand_id?: number | null;
  order?: OrderDetail | null;
}

/**
 * core/ledger.py SessionSettlement.to_dict (ADR-0016 / 0023)。
 * GET .../settlement は compute_settlement 由来の **中間集計（暫定）**、
 * POST .../settlement/commit / PUT .../payment(-status) は **確定済**を返す。
 */
export interface SessionSettlement {
  session_id: string;
  player_id: string;
  cash_in_total: number;
  point_spent_total: number;
  order_total: number;
  entry_fee: number;
  point_credited_total: number;
  net_due_to_store: number; // 店への net 支払い（player→店の 1 方向）
  payment_status: string; // "paid" | "unpaid" | "partial"
  settled_at: string;
  paid_amount: number; // 受領累計（partial-paid, ADR-0023）
}

/** GET /api/menu の要素 (menu.json master, M5)。 */
export interface MenuItem {
  item_name: string;
  unit_amount: number;
}

/** schemas/order_request.schema.json (0.x, M5 — ADR-0018)。 */
export interface OrderRequest {
  request_id: string;
  session_id: string;
  player_id: string;
  item_name: string;
  quantity: number;
  note?: string;
  status: "pending" | "confirmed" | "rejected";
  requested_at: string;
  resolved_at?: string;
  ledger_entry_id?: string;
}

/** hand logger 遠隔制御コマンド（control queue, ADR-0039）。 */
export type HandControlType = "new_hand" | "winner" | "rebuy";

export interface HandControlInput {
  type: HandControlType;
  seat?: number;
  amount?: number;
}

/** POST /api/staff/sessions/{sid}/control の応答（append された 1 コマンド）。 */
export interface ControlCommand {
  command_id: string;
  type: string;
  args: { seat?: number; amount?: number };
  created_at: string;
}

/**
 * GET /api/staff/sessions/{sid}/measurement-rows の 1 行（Phase A 計測 UI, ADR-0043）。
 * UI は per row の `has_needs_review` で「✓ 流す」を無効化する（C-2 ガード）。
 */
export interface MeasurementRow {
  hand_id: number;
  winner_seat: number | null;
  winner_result: number | null; // winner の chip won（players[i].result）
  review_required: boolean;
  has_needs_review: boolean;
  ground_truth: MeasurementGroundTruth | null;
}

/** measurement row 内の GT メタ情報（GT 未記録なら null）。 */
export interface MeasurementGroundTruth {
  annotator: string;
  annotated_at: string;
  source: "captured-passthrough" | "manual-edit";
}

/**
 * ground truth hand 本体 + per-hand metadata（ADR-0043, measurement-plan §2.2）。
 * additive: 元 schema に annotator / annotated_at / source を載せている。
 */
export interface GroundTruthHand {
  hand_id: number;
  annotator: string;
  annotated_at: string;
  source: "captured-passthrough" | "manual-edit";
  board?: string[];
  actions?: Array<{ street?: string; seat?: number; action: string; amount?: number }>;
  players?: Array<{ seat: number; hole_cards?: string[] | null; showed_down?: boolean }>;
  winner_seat?: number | null;
  notes?: string;
  [key: string]: unknown;
}

/** PUT .../ground-truth/{hid} の body（manual-edit 経路）。 */
export interface GroundTruthEditPayload {
  hand_id: number;
  board?: string[];
  actions?: Array<{ street?: string; seat?: number; action: string; amount?: number }>;
  players?: Array<{ seat: number; hole_cards?: string[] | null; showed_down?: boolean }>;
  winner_seat?: number | null;
  notes?: string;
}

/** error-shapes.md の論理形。分岐は code、表示は message。 */
export interface ApiError {
  code: string;
  message: string;
  field?: string;
}

/**
 * staff API のエラー。分岐は code（error-shapes.md + staff 固有:
 * unauthorized / staff_writes_disabled / orders_unavailable / not_implemented）。
 */
export class StaffApiError extends Error {
  readonly code: string;
  readonly field?: string;

  constructor(err: ApiError) {
    super(err.message);
    this.code = err.code;
    this.field = err.field;
  }
}
