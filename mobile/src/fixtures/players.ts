/**
 * Seed data for the mock repository.
 *
 * These values are copied verbatim from the frozen contract fixtures so the
 * mock stays consistent with the schema oracle:
 *   - docs/contracts/fixtures/player/canonical.json    (Alice)
 *   - docs/contracts/fixtures/player/valid-minimal.json (A)
 *   - docs/contracts/fixtures/player/valid-rich.json    (山田 太郎 (Yamada Taro))
 *
 * Keep this in sync with those fixtures. Do NOT hand-edit the ids or names to
 * something not present in the fixtures — that would be contract drift.
 */
import { Player } from '../models/player';

export const SEED_PLAYERS: readonly Player[] = [
  {
    player_id: '0a1b2c3d4e5f60718293a4b5c6d7e8f9',
    display_name: 'Alice',
    created_at: '2026-05-22T10:00:00',
  },
  {
    player_id: '00000000000000000000000000000001',
    display_name: 'A',
    created_at: '2026-05-22T10:05:00',
  },
  {
    player_id: 'ffeeddccbbaa99887766554433221100',
    display_name: '山田 太郎 (Yamada Taro)',
    created_at: '2026-05-22T10:10:30',
  },
];
