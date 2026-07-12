/**
 * 軽量 key-value 永続化アダプタ。
 *
 * - web export（現行の配布形態）: `localStorage` — ブラウザ再読込・再訪問を跨いで保持。
 * - native（Expo Go / 将来のネイティブ配布）: in-memory fallback。プロセス生存中のみ保持し、
 *   永続化はしない（AsyncStorage への差し替えはネイティブ配布着手時にこのファイルだけ変える）。
 *
 * 保存するのは「選択した player」「ログイントークン」等の復元用途のみで、
 * 業務データのオフラインキャッシュには使わない（source of truth は常に API / core）。
 */

const memory = new Map<string, string>();

function localStorageOrNull(): Storage | null {
  try {
    const ls = (globalThis as { localStorage?: Storage }).localStorage;
    if (!ls) return null;
    // Safari プライベートモード等で書けないことがあるため書き込みを試す。
    const probe = "__phv_probe__";
    ls.setItem(probe, "1");
    ls.removeItem(probe);
    return ls;
  } catch {
    return null;
  }
}

export function storageGet(key: string): string | null {
  const ls = localStorageOrNull();
  if (ls) return ls.getItem(key);
  return memory.get(key) ?? null;
}

export function storageSet(key: string, value: string): void {
  const ls = localStorageOrNull();
  if (ls) {
    ls.setItem(key, value);
    return;
  }
  memory.set(key, value);
}

export function storageRemove(key: string): void {
  const ls = localStorageOrNull();
  if (ls) {
    ls.removeItem(key);
    return;
  }
  memory.delete(key);
}

/** JSON で保存された値を読む。壊れていたら null（保存し直しで自然回復）。 */
export function storageGetJson<T>(key: string): T | null {
  const raw = storageGet(key);
  if (raw === null) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    storageRemove(key);
    return null;
  }
}

export function storageSetJson(key: string, value: unknown): void {
  storageSet(key, JSON.stringify(value));
}

/** テスト用: in-memory fallback を空にする（localStorage there は触らない）。 */
export function clearMemoryStorageForTest(): void {
  memory.clear();
}
