import { normalizeDisplayName, messages } from '../src/validation/playerValidation';
import { Player } from '../src/models/player';

const existing: Player[] = [
  { player_id: '0a1b2c3d4e5f60718293a4b5c6d7e8f9', display_name: 'Alice', created_at: '2026-05-22T10:00:00' },
  { player_id: '00000000000000000000000000000001', display_name: 'A', created_at: '2026-05-22T10:05:00' },
];

describe('normalizeDisplayName', () => {
  it('strips surrounding whitespace', () => {
    expect(normalizeDisplayName('  Bob  ', existing)).toBe('Bob');
  });

  it('rejects empty and whitespace-only names', () => {
    expect(() => normalizeDisplayName('', existing)).toThrow();
    expect(() => normalizeDisplayName('   ', existing)).toThrow();
    try {
      normalizeDisplayName('   ', existing);
    } catch (e: any) {
      expect(e.code).toBe('empty_display_name');
      expect(e.field).toBe('display_name');
    }
  });

  it('rejects exact-match duplicates after strip', () => {
    try {
      normalizeDisplayName('  Alice  ', existing);
      throw new Error('expected rejection');
    } catch (e: any) {
      expect(e.code).toBe('duplicate_display_name');
      expect(e.message).toBe(messages.duplicate('Alice'));
    }
  });

  it('allows self-match when excludeId is the owner (rename no-op)', () => {
    expect(
      normalizeDisplayName('Alice', existing, '0a1b2c3d4e5f60718293a4b5c6d7e8f9'),
    ).toBe('Alice');
  });

  it('does not fold case or width (ISSUE-0002 out of scope)', () => {
    expect(normalizeDisplayName('alice', existing)).toBe('alice');
  });
});
