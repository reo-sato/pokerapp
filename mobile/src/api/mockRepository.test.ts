/**
 * MockRepository の契約挙動テスト (node:test, `npm test` = tsx --test)。
 *
 * 検査対象: ViewerRepository interface の意味論 — 一覧 / player 別フィルタ /
 * not_found code での reject (error-shapes.md と同じ分岐キー)。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { MockRepository } from "./mockRepository";
import { ViewerApiError } from "./types";
import { ALICE_ID, BOB_ID, SESSION_ID } from "../mocks/fixtures";

const MISSING_ID = "f".repeat(32);

test("listPlayers returns fixture players", async () => {
  const repo = new MockRepository();
  const players = await repo.listPlayers();
  assert.deepEqual(players.map((p) => p.display_name), ["Alice", "Bob"]);
});

test("getPlayer rejects unknown player with not_found", async () => {
  const repo = new MockRepository();
  await assert.rejects(repo.getPlayer(MISSING_ID), (err: unknown) => {
    assert.ok(err instanceof ViewerApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});

test("listPlayerSessions filters by player", async () => {
  const repo = new MockRepository();
  const alice = await repo.listPlayerSessions(ALICE_ID);
  assert.equal(alice.length, 1);
  assert.equal(alice[0].hands_played, 2);
  const bob = await repo.listPlayerSessions(BOB_ID);
  assert.equal(bob[0].hands_played, 1);
});

test("listPlayerHands returns only seated hands", async () => {
  const repo = new MockRepository();
  const aliceHands = await repo.listPlayerHands(ALICE_ID, SESSION_ID);
  assert.deepEqual(aliceHands.map((h) => h.hand_id), [1, 2]);
  const bobHands = await repo.listPlayerHands(BOB_ID, SESSION_ID);
  assert.deepEqual(bobHands.map((h) => h.hand_id), [1]);
});

test("listPlayerHands rejects unknown session with not_found", async () => {
  const repo = new MockRepository();
  await assert.rejects(repo.listPlayerHands(ALICE_ID, "deadbeef"), (err: unknown) => {
    assert.ok(err instanceof ViewerApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});

test("getHand returns hand or rejects with not_found", async () => {
  const repo = new MockRepository();
  const hand = await repo.getHand(SESSION_ID, 1);
  assert.equal(hand.hand_id, 1);
  assert.ok(hand.players.some((p) => p.player_id === ALICE_ID));
  await assert.rejects(repo.getHand(SESSION_ID, 99), (err: unknown) => {
    assert.ok(err instanceof ViewerApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});
