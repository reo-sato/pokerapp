/**
 * MockStaffRepository 用の fixture dataset (ADR-0037)。
 *
 * 形は docs/contracts/ の schema と api/server.py の staff endpoints に合わせた一貫データ。
 * player_id / session_id は contract fixtures（mobile/ と共有）の canonical 値を再利用する。
 * 実 persistence は持たない（mock で UI を先行させる, CLAUDE.md WS 原則）。
 */
import type {
  ActionRecord,
  HandSummary,
  LedgerEntry,
  MeasurementRow,
  MenuItem,
  OrderRequest,
  Player,
  SeatAssignment,
  StaffSession,
} from "../api/types";

/** mock の有効 staff token（HTTP では config viewer_api.staff_token に相当）。 */
export const VALID_STAFF_TOKEN = "demo-staff-token";

export const ALICE_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9";
export const BOB_ID = "00000000000000000000000000000001";
export const CAROL_ID = "00000000000000000000000000000002";

export const OPEN_SESSION_ID = "11112222333344445555666677778888";
export const CLOSED_SESSION_ID = "9f8e7d6c5b4a39281706f5e4d3c2b1a0";

export const players: Player[] = [
  { player_id: ALICE_ID, display_name: "Alice", created_at: "2026-05-22T10:00:00" },
  { player_id: BOB_ID, display_name: "Bob", created_at: "2026-05-22T10:05:00" },
  { player_id: CAROL_ID, display_name: "Carol", created_at: "2026-05-22T10:10:00" },
];

export const sessions: StaffSession[] = [
  {
    session_id: OPEN_SESSION_ID,
    label: "土曜ナイト #5",
    started_at: "2026-06-16T19:00:00",
    blinds: { sb: 100, bb: 200 },
    status: "open",
  },
  {
    session_id: CLOSED_SESSION_ID,
    label: "金曜ナイト #3",
    started_at: "2026-05-25T20:00:00",
    ended_at: "2026-05-26T01:30:00",
    blinds: { sb: 100, bb: 200 },
    status: "closed",
  },
];

export const buyinPresets: number[] = [10000, 20000, 30000];

/** 注文メニュー (menu.json master 相当, M5)。 */
export const menuItems: MenuItem[] = [
  { item_name: "ビール", unit_amount: 700 },
  { item_name: "ジントニック", unit_amount: 800 },
  { item_name: "ウーロン茶", unit_amount: 400 },
  { item_name: "コーラ", unit_amount: 400 },
  { item_name: "ミネラルウォーター", unit_amount: 300 },
];

/** ledger entries（session → entries）。append-only（取消は reversal）。 */
export const ledgerEntries: Record<string, LedgerEntry[]> = {
  [OPEN_SESSION_ID]: [
    {
      entry_id: "aa00000000000000000000000000000001",
      session_id: OPEN_SESSION_ID,
      player_id: ALICE_ID,
      kind: "buy_in",
      occurred_at: "2026-06-16T19:05:00",
      cash_amount: 10000,
      point_amount: 0,
    },
    {
      entry_id: "aa00000000000000000000000000000002",
      session_id: OPEN_SESSION_ID,
      player_id: BOB_ID,
      kind: "buy_in",
      occurred_at: "2026-06-16T19:08:00",
      cash_amount: 20000,
      point_amount: 0,
    },
    {
      entry_id: "aa00000000000000000000000000000003",
      session_id: OPEN_SESSION_ID,
      player_id: ALICE_ID,
      kind: "entry_fee",
      occurred_at: "2026-06-16T19:05:30",
      cash_amount: 500,
      point_amount: 0,
    },
  ],
  [CLOSED_SESSION_ID]: [
    {
      entry_id: "bb00000000000000000000000000000001",
      session_id: CLOSED_SESSION_ID,
      player_id: ALICE_ID,
      kind: "buy_in",
      occurred_at: "2026-05-25T20:05:00",
      cash_amount: 10000,
      point_amount: 0,
    },
    {
      entry_id: "bb00000000000000000000000000000002",
      session_id: CLOSED_SESSION_ID,
      player_id: ALICE_ID,
      kind: "order",
      occurred_at: "2026-05-25T21:30:00",
      cash_amount: 1500,
      point_amount: 0,
      note: "ジントニック x3",
      order: { item_name: "ジントニック", unit_amount: 500, quantity: 3 },
    },
  ],
};

/** hand-based seating（session → 全 hand の seat assignment）。 */
export const seatAssignments: Record<string, SeatAssignment[]> = {
  [OPEN_SESSION_ID]: [
    { session_id: OPEN_SESSION_ID, hand_id: 1, seat_no: 1, player_id: ALICE_ID },
    { session_id: OPEN_SESSION_ID, hand_id: 1, seat_no: 2, player_id: BOB_ID },
  ],
  [CLOSED_SESSION_ID]: [],
};

/**
 * 計測タブの初期 row（ADR-0043）。実 captured hand log は持たないので、
 * 「✓ 流す」/ 「✏ 修正」の UX を駆動できる最低限の値だけ用意する。
 * 1 件は needs_review=true（C-2 ガード検証用）。
 */
export const measurementRows: Record<string, MeasurementRow[]> = {
  [OPEN_SESSION_ID]: [
    {
      hand_id: 1,
      winner_seat: 1,
      winner_result: 1500,
      review_required: false,
      has_needs_review: false,
      ground_truth: null,
    },
    {
      hand_id: 2,
      winner_seat: 2,
      winner_result: 800,
      review_required: true,
      has_needs_review: true,
      ground_truth: null,
    },
    {
      hand_id: 3,
      winner_seat: 1,
      winner_result: 400,
      review_required: false,
      has_needs_review: false,
      ground_truth: null,
    },
  ],
  [CLOSED_SESSION_ID]: [],
};

function action(
  handId: number,
  street: string,
  seat: number,
  name: string,
  act: string,
  amount: number,
  potAfter: number,
  needsReview = false,
): ActionRecord {
  return {
    hand_id: handId,
    timestamp: "2026-06-16T20:00:00",
    street,
    seat,
    player_name: name,
    action: act,
    amount,
    pot_after: potAfter,
    stack_after: 10000,
    source: { camera: false, audio: true, rfid: false },
    needs_review: needsReview,
    confidence: needsReview ? 0.5 : 0.9,
  };
}

/**
 * ハンド履歴（session → 訂正適用済 HandSummary, ADR-0044）。
 * measurementRows（下）と同じ 3 ハンド構成（winner / needs_review を一致させる）。
 */
export const sessionHands: Record<string, HandSummary[]> = {
  [OPEN_SESSION_ID]: [
    {
      hand_id: 1,
      session_id: OPEN_SESSION_ID,
      started_at: "2026-06-16T19:30:00",
      ended_at: "2026-06-16T19:33:00",
      blinds: { sb: 100, bb: 200 },
      board: ["As", "Kc", "Qd", "5h", "2s"],
      players: [
        { seat: 1, name: "Alice", player_id: ALICE_ID, hole_cards: ["Ah", "Ad"],
          stack_start: 10000, stack_end: 11500, result: 1500 },
        { seat: 2, name: "Bob", player_id: BOB_ID, hole_cards: ["Ks", "Kd"],
          stack_start: 10000, stack_end: 8500, result: -1500 },
      ],
      pot_total: 3000,
      pots: [{ amount: 3000, eligible_seats: [1, 2] }],
      winner_seat: 1,
      actions: [
        action(1, "preflop", 1, "Alice", "raise", 600, 900),
        action(1, "preflop", 2, "Bob", "call", 400, 1300),
        action(1, "flop", 2, "Bob", "check", 0, 1300),
        action(1, "flop", 1, "Alice", "bet", 700, 2000),
        action(1, "flop", 2, "Bob", "call", 700, 2700),
        action(1, "turn", 2, "Bob", "check", 0, 2700),
        action(1, "turn", 1, "Alice", "check", 0, 2700),
        action(1, "river", 2, "Bob", "check", 0, 2700),
        action(1, "river", 1, "Alice", "bet", 150, 2850),
        action(1, "river", 2, "Bob", "call", 150, 3000),
      ],
    },
    {
      hand_id: 2,
      session_id: OPEN_SESSION_ID,
      started_at: "2026-06-16T19:35:00",
      ended_at: "2026-06-16T19:37:00",
      blinds: { sb: 100, bb: 200 },
      board: ["7h", "8h", "9c"],
      players: [
        { seat: 1, name: "Alice", player_id: ALICE_ID, hole_cards: null,
          stack_start: 11500, stack_end: 10700, result: -800 },
        { seat: 2, name: "Bob", player_id: BOB_ID, hole_cards: null,
          stack_start: 8500, stack_end: 9300, result: 800 },
      ],
      pot_total: 1600,
      winner_seat: 2,
      review_required: true,
      actions: [
        action(2, "preflop", 2, "Bob", "raise", 500, 800, true),
        action(2, "preflop", 1, "Alice", "call", 300, 1100),
        action(2, "flop", 1, "Alice", "check", 0, 1100),
        action(2, "flop", 2, "Bob", "bet", 500, 1600),
        action(2, "flop", 1, "Alice", "fold", 0, 1600),
      ],
    },
    {
      hand_id: 3,
      session_id: OPEN_SESSION_ID,
      started_at: "2026-06-16T19:40:00",
      ended_at: "2026-06-16T19:41:00",
      blinds: { sb: 100, bb: 200 },
      board: [],
      players: [
        { seat: 1, name: "Alice", player_id: ALICE_ID, hole_cards: null,
          stack_start: 10700, stack_end: 11100, result: 400 },
        { seat: 2, name: "Bob", player_id: BOB_ID, hole_cards: null,
          stack_start: 9300, stack_end: 8900, result: -400 },
      ],
      pot_total: 800,
      winner_seat: 1,
      actions: [
        action(3, "preflop", 1, "Alice", "raise", 400, 700),
        action(3, "preflop", 2, "Bob", "fold", 0, 700),
      ],
    },
  ],
  [CLOSED_SESSION_ID]: [],
};

/** 注文リクエスト（session → requests）。pending をスタッフが確定/却下する。 */
export const orderRequests: Record<string, OrderRequest[]> = {
  [OPEN_SESSION_ID]: [
    {
      request_id: "cc00000000000000000000000000000001",
      session_id: OPEN_SESSION_ID,
      player_id: ALICE_ID,
      item_name: "ビール",
      quantity: 2,
      status: "pending",
      requested_at: "2026-06-16T20:10:00",
    },
    {
      request_id: "cc00000000000000000000000000000002",
      session_id: OPEN_SESSION_ID,
      player_id: BOB_ID,
      item_name: "ウーロン茶",
      quantity: 1,
      note: "氷少なめ",
      status: "pending",
      requested_at: "2026-06-16T20:12:00",
    },
  ],
  [CLOSED_SESSION_ID]: [],
};
