/**
 * AuthSession（L1 PIN / L2 OIDC の principal トークン, ADR-0027/0031）の永続化。
 *
 * web export ではブラウザ再読込のたびにログインし直しになるのを防ぐ。保存先は
 * `src/storage.ts`（web = localStorage / native = in-memory fallback）。期限切れは
 * 読み出し時に破棄する（サーバ側でも検証されるため、ここは UX のための先回りに過ぎない）。
 */
import { storageGetJson, storageRemove, storageSetJson } from "../storage";
import type { AuthSession } from "./types";

const KEY = "phv.auth";

export function loadStoredAuth(now: () => number = Date.now): AuthSession | null {
  const s = storageGetJson<AuthSession>(KEY);
  if (!s || typeof s.token !== "string" || typeof s.player_id !== "string") {
    return null;
  }
  // expires_at は unix 秒（0 = 期限なし = mock）。
  if (typeof s.expires_at === "number" && s.expires_at > 0 && s.expires_at * 1000 <= now()) {
    storageRemove(KEY);
    return null;
  }
  return s;
}

export function saveStoredAuth(session: AuthSession): void {
  storageSetJson(KEY, session);
}

export function clearStoredAuth(): void {
  storageRemove(KEY);
}
