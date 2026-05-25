/**
 * In-memory mock PlayerRepository.
 *
 * Lets the mobile UI (screens, navigation, validation feedback) be built and
 * exercised before WS1/core is reachable over an API. It satisfies the exact
 * same contract as the real backend will (docs/contracts/repository-interfaces.md)
 * so the screens can later be pointed at an API-backed implementation with no
 * UI changes.
 *
 * Business rules (blank / duplicate / not-found) are delegated to the shared
 * validation module, which mirrors the contract. The mock owns no rules of its
 * own beyond ordering and id minting.
 */
import { Player } from '../models/player';
import { newPlayerId } from '../models/ids';
import { SEED_PLAYERS } from '../fixtures/players';
import { PlayerRepository } from './PlayerRepository';
import { RepositoryError } from './errors';
import { normalizeDisplayName, messages } from '../validation/playerValidation';

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, '');
}

export class MockPlayerRepository implements PlayerRepository {
  private players: Map<string, Player>;

  constructor(seed: readonly Player[] = SEED_PLAYERS) {
    this.players = new Map(seed.map((p) => [p.player_id, { ...p }]));
  }

  async listPlayers(): Promise<Player[]> {
    return [...this.players.values()].sort((a, b) => {
      if (a.created_at !== b.created_at) {
        return a.created_at < b.created_at ? -1 : 1;
      }
      return a.display_name < b.display_name ? -1 : a.display_name > b.display_name ? 1 : 0;
    });
  }

  async getPlayer(playerId: string): Promise<Player> {
    const player = this.players.get(playerId);
    if (!player) {
      throw new RepositoryError('not_found', messages.notFound(playerId));
    }
    return { ...player };
  }

  async createPlayer(displayName: string): Promise<Player> {
    const name = normalizeDisplayName(displayName, [...this.players.values()]);
    const player: Player = {
      player_id: newPlayerId(),
      display_name: name,
      created_at: nowIso(),
    };
    this.players.set(player.player_id, player);
    return { ...player };
  }

  async renamePlayer(playerId: string, newDisplayName: string): Promise<Player> {
    const existing = this.players.get(playerId);
    if (!existing) {
      throw new RepositoryError('not_found', messages.notFound(playerId));
    }
    const name = normalizeDisplayName(
      newDisplayName,
      [...this.players.values()],
      playerId,
    );
    const updated: Player = { ...existing, display_name: name };
    this.players.set(playerId, updated);
    return { ...updated };
  }
}
