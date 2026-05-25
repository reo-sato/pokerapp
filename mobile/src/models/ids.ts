/**
 * Shared ID minting — mirrors docs/contracts/shared-ids.md.
 *
 * player_id is minted inside the app (no external system dependency) and is an
 * immutable opaque string in UUID4-hex form: 32 lowercase hex chars, no hyphens
 * (pattern ^[0-9a-f]{32}$).
 *
 * NOTE: in production the real backend (core) mints these. This generator
 * exists only so the in-memory mock can produce contract-shaped ids for newly
 * created players. It is deliberately the only place randomness enters the mock.
 */
import { PlayerId, PLAYER_ID_PATTERN } from './player';

function randomBytes(count: number): Uint8Array {
  const bytes = new Uint8Array(count);
  const g = globalThis as { crypto?: { getRandomValues?: (a: Uint8Array) => Uint8Array } };
  if (g.crypto?.getRandomValues) {
    g.crypto.getRandomValues(bytes);
    return bytes;
  }
  // Fallback for runtimes without WebCrypto (fine for a mock).
  for (let i = 0; i < count; i += 1) {
    bytes[i] = Math.floor(Math.random() * 256);
  }
  return bytes;
}

/** Mint a contract-shaped player_id (UUID4 hex, 32 lowercase chars). */
export function newPlayerId(): PlayerId {
  const bytes = randomBytes(16);
  // Set RFC 4122 version (4) and variant bits, matching uuid4().hex.
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  let hex = '';
  for (const b of bytes) {
    hex += b.toString(16).padStart(2, '0');
  }
  if (!PLAYER_ID_PATTERN.test(hex)) {
    throw new Error(`minted player_id violates contract: ${hex}`);
  }
  return hex;
}
