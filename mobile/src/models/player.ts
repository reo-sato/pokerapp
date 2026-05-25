/**
 * Player model — derived from the shared contract, NOT from the Python core.
 *
 * Source of truth:
 *   - docs/contracts/schemas/player.schema.json (v1.0)
 *   - docs/contracts/shared-ids.md (player_id format)
 *
 * Attributes are intentionally limited to player_id + display_name + created_at
 * (Phase S1). Do not add contact / real-name / seat attributes here — those are
 * out of scope per CLAUDE.md § Player Registry.
 */

/**
 * Opaque, immutable player identifier.
 *
 * Contract: UUID4 hex (32 lowercase chars, no hyphens), pattern ^[0-9a-f]{32}$.
 * Treated as an opaque string at every boundary; never parse meaning out of it.
 */
export type PlayerId = string;

export interface Player {
  /** Stable key shared with session / ledger / settlement. Immutable. */
  readonly player_id: PlayerId;
  /** Display name. Stored stripped of surrounding whitespace by the repository. */
  readonly display_name: string;
  /** ISO 8601 creation timestamp. */
  readonly created_at: string;
}

/** Contract pattern for player_id (docs/contracts/shared-ids.md). */
export const PLAYER_ID_PATTERN = /^[0-9a-f]{32}$/;

export function isPlayerId(value: string): value is PlayerId {
  return PLAYER_ID_PATTERN.test(value);
}
