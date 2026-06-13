/**
 * docs/contracts/ の schema から転記した viewer API の型 (M2, ADR-0017)。
 *
 * - Player:               schemas/player.schema.json (1.0)
 * - PlayerSessionSummary: schemas/player_session_summary.schema.json (0.x)
 * - HandSummary/Action:   schemas/hand.schema.json / action.schema.json (1.0)
 * - ApiError:             docs/contracts/error-shapes.md
 *
 * validation ロジックはここに複製しない (core が source of truth)。
 */

export interface Player {
  player_id: string; // UUID4 hex (32 文字)
  display_name: string;
  created_at: string; // ISO 8601
}

export interface Blinds {
  sb?: number;
  bb?: number;
}

export interface PlayerSessionSummary {
  session_id: string;
  label?: string;
  started_at: string;
  ended_at?: string;
  blinds?: Blinds;
  status: "open" | "closed";
  hands_played: number;
}

export interface ActionRecord {
  hand_id: number;
  timestamp: string;
  street: string; // "preflop" | "flop" | "turn" | "river"
  seat: number;
  player_name: string;
  action: string; // "check" | "call" | "bet" | "raise" | "fold" | "all_in" | ...
  amount: number;
  pot_after: number;
  stack_after: number;
  source: { camera: boolean; audio: boolean; rfid: boolean };
  needs_review: boolean;
  confidence: number;
}

export interface HandPlayer {
  seat: number;
  name: string;
  player_id?: string | null; // additive (S2.x)。E3 前の legacy ログでは absent
  hole_cards?: string[] | null;
  hole_cards_source?: string;
  stack_start: number;
  stack_end: number;
  result: number;
  committed?: number;
}

export interface Pot {
  amount: number;
  eligible_seats: number[];
}

export interface HandSummary {
  hand_id: number;
  session_id: string;
  started_at: string;
  ended_at: string;
  blinds?: Blinds;
  board?: string[];
  board_source?: string;
  players: HandPlayer[];
  pot_total?: number;
  pots?: Pot[];
  winner_seat?: number;
  actions: ActionRecord[];
  review_required?: boolean;
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
  point_amount: number; // point 払いがあれば >0
  note?: string;
  hand_id?: number;
  reverses_entry_id?: string;
  order?: OrderDetail;
}

/**
 * SessionSettlement の field を 1 player に限定した中間集計 (ADR-0016)。
 * 確定値ではない（確定は S4 settlement）。
 */
export interface LedgerSummary {
  cash_in_total: number; // buy_in/rebuy/add_on の cash_amount 合計
  order_total: number; // kind=order の cash_amount 合計
  entry_fee: number; // kind=entry_fee の cash_amount 合計
  point_spent_total: number; // 使用 point 合計
  point_credited_total: number; // 付与 point 合計
  net_due_to_store: number; // 全 kind の cash_amount 合計（店への net 支払い）
  // S4: 精算の確定状態（committed のときのみ payment_status/settled_at が意味を持つ）。
  settled: boolean;
  payment_status?: string | null; // "paid" | "unpaid" | "partial"（settled=true のとき, ADR-0023）
  settled_at?: string | null;
  paid_amount?: number; // 受領累計額（partial-paid, ADR-0023。未確定は 0）
}

export interface PlayerSessionLedger {
  entries: LedgerEntry[];
  summary: LedgerSummary;
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

export interface OrderRequestBody {
  item_name: string;
  quantity: number;
  note?: string;
}

/** error-shapes.md の論理形。分岐は code、表示は message。 */
export interface ApiError {
  code: string;
  message: string;
  field?: string;
}

export class ViewerApiError extends Error {
  readonly code: string;
  readonly field?: string;

  constructor(err: ApiError) {
    super(err.message);
    this.code = err.code;
    this.field = err.field;
  }
}
