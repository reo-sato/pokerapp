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

/** 店外で見るための閲覧リンク（ADR-0067 D5）。url の `#link=` の後ろが本人の鍵（人に渡さない）。 */
export interface ViewerLink {
  player_id: string; // merge 済みなら統合先
  url: string;
  created_at?: string | null;
  qr_image?: string | null; // QR コード（画像の data URI）。無ければ URL だけを出す
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
  /** お客さんの画面に出す手札の範囲（ADR-0067 D6）。無ければ店の設定に従う。 */
  hole_card_visibility?: HoleCardVisibility;
}

export type HoleCardVisibility = "all" | "own_and_shown";

export interface HoleCardVisibilityResult {
  session: StaffSession;
  store_default: HoleCardVisibility;
  effective: HoleCardVisibility;
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

/** GET /api/menu の要素 (menu.json master, M5。sold_out は additive = ADR-0046)。 */
export interface MenuItem {
  item_name: string;
  unit_amount: number;
  sold_out?: boolean;
}

/** schemas/order_request.schema.json (0.x, M5 — ADR-0018)。 */
export interface OrderRequest {
  request_id: string;
  session_id: string;
  player_id: string;
  item_name: string;
  quantity: number;
  note?: string;
  status: "pending" | "confirmed" | "rejected" | "cancelled"; // cancelled = 本人取り下げ (1.1, ADR-0045)
  requested_at: string;
  resolved_at?: string;
  ledger_entry_id?: string;
}

/** schemas/action.schema.json (1.0)。staff hands read（ADR-0044）用。 */
export interface ActionRecord {
  hand_id: number;
  timestamp: string;
  street: string; // "preflop" | "flop" | "turn" | "river"
  seat: number;
  player_name: string;
  action: string; // "check" | "call" | "bet" | "raise" | "fold" | "allin" | ...
  amount: number;
  pot_after: number;
  stack_after: number;
  source: { camera: boolean; audio: boolean; rfid: boolean };
  needs_review: boolean;
  confidence: number;
  corrected?: boolean; // 訂正オーバーレイ痕 (ADR-0036, additive)
}

export interface HandPlayer {
  seat: number;
  name: string;
  player_id?: string | null;
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

/** schemas/hand.schema.json (1.0)。GET /api/staff/sessions/{sid}/hands の要素（訂正適用済）。 */
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

/** ハンド訂正レコード (ADR-0036)。append-only オーバーレイ。 */
export interface HandCorrection {
  correction_id: string;
  session_id: string;
  hand_id: number;
  action_index: number | null; // null = hand レベル (winner_seat 等)
  field: string; // "action" | "amount" | "winner_seat"
  new_value: string | number;
  corrected_by: string;
  corrected_at: string;
  note?: string;
}

/** ハンド訂正の入力 (staff 操作, ADR-0036)。 */
export interface HandCorrectionInput {
  field: string; // "action" | "amount"（action_index あり） | "winner_seat"（hand レベル）
  new_value: string | number;
  action_index?: number | null;
  corrected_by?: string;
  note?: string;
}

/** hand logger 遠隔制御コマンド（control queue, ADR-0039）。 */
export type HandControlType = "new_hand" | "winner" | "rebuy" | "set_dealer";

export interface HandControlInput {
  type: HandControlType;
  seat?: number;
  amount?: number;
  /** set_dealer: 配っているディーラーの名前（空 = 消す, ADR-0074）。 */
  name?: string;
}

/** POST /api/staff/sessions/{sid}/control の応答（append された 1 コマンド）。 */
export interface ControlCommand {
  command_id: string;
  type: string;
  args: { seat?: number; amount?: number; name?: string };
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
  /** 項目ごとの誤り（卓の設定 = 400 invalid_setup, ADR-0073 段階 C）。 */
  errors?: FieldError[];
}

export interface FieldError {
  field: string;
  message: string;
}

/**
 * staff API のエラー。分岐は code（error-shapes.md + staff 固有:
 * unauthorized / staff_writes_disabled / orders_unavailable / not_implemented）。
 */
export class StaffApiError extends Error {
  readonly code: string;
  readonly field?: string;
  readonly errors?: FieldError[];

  constructor(err: ApiError) {
    super(err.message);
    this.code = err.code;
    this.field = err.field;
    this.errors = err.errors;
  }
}

// ――― ロガーの様子（ADR-0073 D3。GET /api/staff/logger, /api/staff/logger/notices）―――

/** ロガーのプロセス: running = 動作中 / unresponsive = 心拍が古い / crashed = 終わらずに止まった / stopped。 */
export type LoggerRunStatus = "running" | "unresponsive" | "crashed" | "stopped";

/** ロガーが書く状態（`logs/logger_state.json`）。段階は starting / setup / running / stopping / stopped。 */
export interface LoggerState {
  phase: string;
  mode: string;
  pid?: number;
  session_id: string | null;
  started_at?: string;
  updated_at?: string;
  ended_at?: string;
  table?: { mode?: string | null; seats?: number[]; sb?: number | null; bb?: number | null };
  mic?: { state?: string; level?: number; device_name?: string | null; error?: string };
  rfid?: { state?: string; connected?: number; configured?: number; error?: string };
  speech_backlog?: number;
  transcriber?: string;
  /** 書き起こしの様子（ADR-0072）: 方式の名前（openai-gpt-transcribe など）・API か・API につながらない間か・送り直し待ち。 */
  asr?: { name?: string | null; remote?: boolean; down?: boolean; waiting?: number };
  /** Windows のセッション（ADR-0073 段階 0）: console / remote（RDP）/ state（disconnected = RDP を閉じたまま）。 */
  win_session?: { session?: number; console?: boolean; remote?: boolean; state?: string | null; platform?: string };
  notice_seq?: number;
}

export interface LoggerStatusResponse {
  status: LoggerRunStatus;
  state: LoggerState | null;
  heartbeat_age_sec: number | null;
  holder_text: string | null;
  /** 店舗サーバーから起動できるか（見張り役がある = ADR-0073 段階 C）。 */
  can_start?: boolean;
  /** 落ちたロガーの記録（status が crashed のとき）。 */
  crash?: { pid?: number | null; session_id?: string | null; hand_id?: number | null; at?: string } | null;
}

// ――― iPad から起動（ADR-0073 段階 C。GET /api/staff/logger/setup, POST /api/staff/logger/start）―――

/** 卓の設定（PC の設定画面と同じ形 = tools/session_setup_ui.DEFAULT_SETUP）。 */
export interface LoggerSetup {
  mode: "ring" | "tournament";
  seats: number[];
  names: Record<string, string>;
  stacks: Record<string, number>;
  default_stack: number;
  sb: number;
  bb: number;
  structure: string;
  level: number;
  elapsed_min: number;
  start_time: string | null;
  button: number | null;
  stackless: boolean;
  /** 配っているディーラー（任意, ADR-0074）。 */
  dealer?: string;
}

export interface StructureLevel {
  sb?: number;
  bb?: number;
  ante?: number;
  minutes: number;
  break?: boolean;
}

export interface BlindStructureInfo {
  name: string;
  levels: StructureLevel[];
  starting_stack?: number;
  start_time?: string;
}

export interface LoggerSetupResponse {
  last: Partial<LoggerSetup>;
  structures: BlindStructureInfo[];
  /** 前回の時計（16 時間以内）: 続きから始めるときの値。 */
  resume: { structure: string; level: number; elapsed_min: number; text?: string } | null;
  can_start: boolean;
}

export interface LoggerStartResponse {
  pid: number | null;
  started_at: string;
}

/** 端末に出たお知らせ 1 行（kind = notice / unapplied / action / heard / cards）。 */
export interface LoggerNotice {
  seq: number;
  t: number;
  kind: string;
  text: string;
  session_id: string | null;
  hand_id?: number | null;
}

export interface LoggerNoticesResponse {
  notices: LoggerNotice[];
  last_seq: number;
  /** ロガーが起動し直した（番号が 1 から）: 手元のお知らせを捨てて読み直す。 */
  reset: boolean;
}
