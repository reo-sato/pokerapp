/**
 * docs/contracts/ の schema から転記した viewer API の型 (M2, ADR-0013)。
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
