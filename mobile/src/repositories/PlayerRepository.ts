/**
 * PlayerRepository interface — mirrors docs/contracts/repository-interfaces.md.
 *
 * The UI depends ONLY on this interface. The concrete implementation (in-memory
 * mock today, a local or API-backed client later) is injected via React context
 * so screens never know where the data lives. Swapping the implementation must
 * not require touching any screen (the future-API boundary from CLAUDE.md).
 *
 * All rejections are RepositoryError with a stable `code` (see errors.ts).
 */
import { Player } from '../models/player';

export interface PlayerRepository {
  /** All players in creation order. */
  listPlayers(): Promise<Player[]>;

  /** One player. Rejects RepositoryError(not_found). */
  getPlayer(playerId: string): Promise<Player>;

  /**
   * Create a player with a freshly minted player_id.
   * Rejects RepositoryError(empty_display_name | duplicate_display_name).
   */
  createPlayer(displayName: string): Promise<Player>;

  /**
   * Rename an existing player (player_id is immutable).
   * Rejects RepositoryError(not_found | empty_display_name | duplicate_display_name).
   */
  renamePlayer(playerId: string, newDisplayName: string): Promise<Player>;
}
