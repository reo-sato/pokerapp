/**
 * storage / authStorage / MockRepository の永続化テスト (node:test, `npm test`)。
 *
 * node には localStorage が無いため in-memory fallback を通る（web では localStorage、
 * 意味論は同一）。「再読込」は new MockRepository() の作り直しで模擬する。
 */
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { loadStoredAuth, saveStoredAuth } from "./api/authStorage";
import { MockRepository } from "./api/mockRepository";
import { ALICE_ID } from "./mocks/fixtures";
import {
  clearMemoryStorageForTest,
  storageGet,
  storageGetJson,
  storageRemove,
  storageSet,
  storageSetJson,
} from "./storage";

beforeEach(() => clearMemoryStorageForTest());

test("storage set/get/remove roundtrip (in-memory fallback)", () => {
  assert.equal(storageGet("k"), null);
  storageSet("k", "v");
  assert.equal(storageGet("k"), "v");
  storageRemove("k");
  assert.equal(storageGet("k"), null);
});

test("storageGetJson drops broken JSON and self-heals", () => {
  storageSet("j", "{broken");
  assert.equal(storageGetJson("j"), null);
  // 壊れた値は読み出し時に削除される。
  assert.equal(storageGet("j"), null);
  storageSetJson("j", { a: 1 });
  assert.deepEqual(storageGetJson("j"), { a: 1 });
});

test("loadStoredAuth restores a session and discards expired ones", () => {
  const session = { token: "t", player_id: ALICE_ID, expires_at: 0 };
  saveStoredAuth(session);
  assert.deepEqual(loadStoredAuth(), session);

  // 期限切れ（unix 秒）は破棄される。
  saveStoredAuth({ token: "t2", player_id: ALICE_ID, expires_at: 1 });
  assert.equal(loadStoredAuth(() => 2_000), null);
  // 破棄後は消えている。
  assert.equal(loadStoredAuth(), null);
});

test("mock login persists across repository re-creation (reload) and clearAuth forgets", async () => {
  const repo1 = new MockRepository();
  await repo1.login(ALICE_ID, "1234");
  assert.equal(repo1.currentPrincipal(), ALICE_ID);

  // 「ブラウザ再読込」相当: 新しいインスタンスが保存済みログインを復元する。
  const repo2 = new MockRepository();
  assert.equal(repo2.currentPrincipal(), ALICE_ID);

  repo2.clearAuth();
  const repo3 = new MockRepository();
  assert.equal(repo3.currentPrincipal(), null);
});
