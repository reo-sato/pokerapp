/**
 * MockRepository 用の fixture dataset (M2)。
 *
 * 形は docs/contracts/ の schema / fixtures (player canonical 等) に合わせた一貫データ。
 * ID は contract fixtures の canonical 値を再利用している。実 persistence は持たない
 * (CLAUDE.md § mobile が mock で先行できる範囲)。
 */
import type {
  HandSummary,
  LedgerEntry,
  MenuItem,
  Player,
  PlayerSessionSummary,
} from "../api/types";

export const ALICE_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9";
export const BOB_ID = "00000000000000000000000000000001";
export const SESSION_ID = "9f8e7d6c5b4a39281706f5e4d3c2b1a0";

export const players: Player[] = [
  { player_id: ALICE_ID, display_name: "Alice", created_at: "2026-05-22T10:00:00" },
  { player_id: BOB_ID, display_name: "Bob", created_at: "2026-05-22T10:05:00" },
];

export const sessionsByPlayer: Record<string, PlayerSessionSummary[]> = {
  [ALICE_ID]: [
    {
      session_id: SESSION_ID,
      label: "金曜ナイト #3",
      started_at: "2026-05-25T20:00:00",
      ended_at: "2026-05-26T01:30:00",
      blinds: { sb: 100, bb: 200 },
      status: "closed",
      hands_played: 2,
    },
  ],
  [BOB_ID]: [
    {
      session_id: SESSION_ID,
      label: "金曜ナイト #3",
      started_at: "2026-05-25T20:00:00",
      ended_at: "2026-05-26T01:30:00",
      blinds: { sb: 100, bb: 200 },
      status: "closed",
      hands_played: 1,
    },
  ],
};

const hand1: HandSummary = {
  hand_id: 1,
  session_id: SESSION_ID,
  started_at: "2026-05-25T20:15:30",
  ended_at: "2026-05-25T20:17:10",
  blinds: { sb: 100, bb: 200 },
  board: ["As", "Ks", "Qs"],
  board_source: "rfid",
  players: [
    {
      seat: 1, name: "Alice", player_id: ALICE_ID, hole_cards: ["Ah", "Kd"],
      hole_cards_source: "rfid", stack_start: 10000, stack_end: 11100, result: 1100,
    },
    {
      seat: 2, name: "Bob", player_id: BOB_ID, hole_cards: null,
      hole_cards_source: "", stack_start: 10000, stack_end: 8900, result: -1100,
    },
  ],
  pot_total: 2200,
  pots: [{ amount: 2200, eligible_seats: [1, 2] }],
  winner_seat: 1,
  actions: [
    {
      hand_id: 1, timestamp: "2026-05-25T20:15:40", street: "preflop", seat: 2,
      player_name: "Bob", action: "raise", amount: 600, pot_after: 900,
      stack_after: 9400, source: { camera: false, audio: true, rfid: false },
      needs_review: false, confidence: 0.5,
    },
    {
      hand_id: 1, timestamp: "2026-05-25T20:15:55", street: "preflop", seat: 1,
      player_name: "Alice", action: "call", amount: 600, pot_after: 1300,
      stack_after: 9400, source: { camera: false, audio: true, rfid: true },
      needs_review: false, confidence: 0.95,
    },
  ],
  review_required: false,
};

const hand2: HandSummary = {
  ...hand1,
  hand_id: 2,
  started_at: "2026-05-25T20:18:00",
  ended_at: "2026-05-25T20:19:30",
  board: [],
  board_source: "",
  players: [hand1.players[0]],
  pot_total: 300,
  pots: [],
  winner_seat: 1,
  actions: [],
};

export const handsBySession: Record<string, HandSummary[]> = {
  [SESSION_ID]: [hand1, hand2],
};

/** player が着席している hand のみ (seat_assignment 相当の帰属)。 */
export const seatedHandIds: Record<string, Record<string, number[]>> = {
  [ALICE_ID]: { [SESSION_ID]: [1, 2] },
  [BOB_ID]: { [SESSION_ID]: [1] },
};

/** 注文メニュー (menu.json master 相当, M5)。 */
export const menuItems: MenuItem[] = [
  { item_name: "ビール", unit_amount: 700 },
  { item_name: "ジントニック", unit_amount: 800 },
  { item_name: "ウーロン茶", unit_amount: 400 },
  { item_name: "コーラ", unit_amount: 400 },
  { item_name: "枝豆", unit_amount: 300, sold_out: true },
];

/** ledger entries (verify-v1 ledger, ADR-0016)。player → session → entries。 */
export const ledgerEntries: Record<string, Record<string, LedgerEntry[]>> = {
  [ALICE_ID]: {
    [SESSION_ID]: [
      {
        entry_id: "1f2e3d4c5b6a79880917263544536271",
        session_id: SESSION_ID,
        player_id: ALICE_ID,
        kind: "buy_in",
        occurred_at: "2026-05-25T20:05:00",
        cash_amount: 10000,
        point_amount: 0,
      },
      {
        entry_id: "1f2e3d4c5b6a79880917263544536272",
        session_id: SESSION_ID,
        player_id: ALICE_ID,
        kind: "order",
        occurred_at: "2026-05-25T21:30:00",
        cash_amount: 1500,
        point_amount: 0,
        note: "ジントニック x3",
        order: { item_name: "ジントニック", unit_amount: 500, quantity: 3 },
      },
      {
        entry_id: "1f2e3d4c5b6a79880917263544536273",
        session_id: SESSION_ID,
        player_id: ALICE_ID,
        kind: "adjustment",
        occurred_at: "2026-05-25T23:00:00",
        cash_amount: -500,
        point_amount: 0,
        note: "返金",
      },
    ],
  },
  [BOB_ID]: {
    [SESSION_ID]: [
      {
        entry_id: "1f2e3d4c5b6a79880917263544536274",
        session_id: SESSION_ID,
        player_id: BOB_ID,
        kind: "buy_in",
        occurred_at: "2026-05-25T20:10:00",
        cash_amount: 20000,
        point_amount: 0,
      },
    ],
  },
};
