/**
 * display_name validation — mirrors docs/contracts/validation-rules.md.
 *
 * IMPORTANT: core (core/player_repository.py) is the source of truth for these
 * business rules. This module reproduces them ONLY so the mock repository can
 * behave like the real backend before WS1 is wired up over an API. The UI layer
 * must NOT call this directly or re-implement these checks — screens call the
 * repository and render the RepositoryError they get back (branch on `code`).
 *
 * Keep this aligned with the contract; do not invent extra rules, magic numbers,
 * or normalization (no casefold / full-width folding — that is ISSUE-0002, out
 * of scope for S1).
 */
import { Player } from '../models/player';
import { RepositoryError } from '../repositories/errors';

const FIELD = 'display_name';

/** Centralized, display-facing messages (the only place strings live). */
export const messages = {
  empty: () => 'display_name が空です。',
  duplicate: (name: string) => `「${name}」は既に存在します。`,
  notFound: (playerId: string) => `player_id=${playerId} は存在しません。`,
};

/**
 * Strip surrounding whitespace and enforce the S1 rules.
 *
 * @param raw         the proposed display_name
 * @param existing    current players to check for exact-match duplicates
 * @param excludeId   player_id to skip when checking duplicates (rename: self
 *                    match is allowed / no-op)
 * @returns the normalized (stripped) display_name
 * @throws RepositoryError with code empty_display_name | duplicate_display_name
 */
export function normalizeDisplayName(
  raw: string,
  existing: readonly Player[],
  excludeId?: string,
): string {
  const name = (raw ?? '').trim();
  if (name.length === 0) {
    throw new RepositoryError('empty_display_name', messages.empty(), FIELD);
  }
  for (const player of existing) {
    if (player.player_id === excludeId) {
      continue;
    }
    // Exact match after strip. Case / width are intentionally NOT folded.
    if (player.display_name === name) {
      throw new RepositoryError(
        'duplicate_display_name',
        messages.duplicate(name),
        FIELD,
      );
    }
  }
  return name;
}
