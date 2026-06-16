/**
 * MockStaffRepository 用の fixture dataset (ADR-0035)。
 *
 * 形は docs/contracts/ の schema と api/server.py の staff endpoints に合わせた一貫データ。
 * player_id / session_id は contract fixtures（mobile/ と共有）の canonical 値を再利用する。
 * 実 persistence は持たない（mock で UI を先行させる, CLAUDE.md WS 原則）。
 */
import type { LedgerEntry, MenuItem, OrderRequest, Player, StaffSession } from "../api/types";

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
