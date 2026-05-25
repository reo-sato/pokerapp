import { MockPlayerRepository } from '../src/repositories/MockPlayerRepository';
import { RepositoryError } from '../src/repositories/errors';
import { PLAYER_ID_PATTERN } from '../src/models/player';
import { SEED_PLAYERS } from '../src/fixtures/players';

describe('MockPlayerRepository', () => {
  it('seeds from the contract fixtures', async () => {
    const repo = new MockPlayerRepository();
    const players = await repo.listPlayers();
    expect(players).toHaveLength(SEED_PLAYERS.length);
    expect(players.map((p) => p.display_name)).toEqual([
      'Alice',
      'A',
      '山田 太郎 (Yamada Taro)',
    ]);
  });

  it('lists players in creation order', async () => {
    const repo = new MockPlayerRepository();
    const players = await repo.listPlayers();
    const timestamps = players.map((p) => p.created_at);
    expect([...timestamps]).toEqual([...timestamps].sort());
  });

  it('creates a player with a contract-shaped player_id', async () => {
    const repo = new MockPlayerRepository();
    const created = await repo.createPlayer('Dave');
    expect(created.display_name).toBe('Dave');
    expect(created.player_id).toMatch(PLAYER_ID_PATTERN);
    const all = await repo.listPlayers();
    expect(all.map((p) => p.display_name)).toContain('Dave');
  });

  it('strips surrounding whitespace from display_name on create', async () => {
    const repo = new MockPlayerRepository();
    const created = await repo.createPlayer('  Spaced  ');
    expect(created.display_name).toBe('Spaced');
  });

  it('rejects a blank display_name with empty_display_name', async () => {
    const repo = new MockPlayerRepository();
    await expect(repo.createPlayer('   ')).rejects.toMatchObject({
      code: 'empty_display_name',
    } as Partial<RepositoryError>);
  });

  it('rejects an exact-match duplicate with duplicate_display_name', async () => {
    const repo = new MockPlayerRepository();
    await expect(repo.createPlayer('Alice')).rejects.toMatchObject({
      code: 'duplicate_display_name',
    } as Partial<RepositoryError>);
  });

  it('treats case / width differences as distinct (S1 scope)', async () => {
    const repo = new MockPlayerRepository();
    await expect(repo.createPlayer('alice')).resolves.toBeDefined();
  });

  it('renames a player and keeps its player_id immutable', async () => {
    const repo = new MockPlayerRepository();
    const alice = (await repo.listPlayers())[0]!;
    const renamed = await repo.renamePlayer(alice.player_id, 'Alice II');
    expect(renamed.player_id).toBe(alice.player_id);
    expect(renamed.display_name).toBe('Alice II');
  });

  it('allows a no-op rename to the same name (self-match excluded)', async () => {
    const repo = new MockPlayerRepository();
    const alice = (await repo.listPlayers())[0]!;
    await expect(repo.renamePlayer(alice.player_id, 'Alice')).resolves.toMatchObject({
      display_name: 'Alice',
    });
  });

  it('rejects renaming to another player display_name', async () => {
    const repo = new MockPlayerRepository();
    const alice = (await repo.listPlayers())[0]!;
    await expect(repo.renamePlayer(alice.player_id, 'A')).rejects.toMatchObject({
      code: 'duplicate_display_name',
    } as Partial<RepositoryError>);
  });

  it('rejects operations on an unknown player with not_found', async () => {
    const repo = new MockPlayerRepository();
    await expect(repo.getPlayer('deadbeef')).rejects.toMatchObject({ code: 'not_found' });
    await expect(repo.renamePlayer('deadbeef', 'X')).rejects.toMatchObject({
      code: 'not_found',
    });
  });

  it('supports an empty seed for the empty-state UI', async () => {
    const repo = new MockPlayerRepository([]);
    await expect(repo.listPlayers()).resolves.toEqual([]);
  });
});
