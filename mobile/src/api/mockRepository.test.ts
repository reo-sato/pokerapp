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

test("getPlayerLedger returns entries and summary consistent with core rules", async () => {
  const repo = new MockRepository();
  const alice = await repo.getPlayerLedger(ALICE_ID, SESSION_ID);
  assert.deepEqual(alice.entries.map((e) => e.kind), ["buy_in", "order", "adjustment"]);
  assert.deepEqual(alice.summary, {
    cash_in_total: 10000,
    order_total: 1500,
    entry_fee: 0,
    point_spent_total: 0,
    point_credited_total: 0,
    net_due_to_store: 11000,
    settled: false,
    payment_status: null,
    settled_at: null,
    paid_amount: 0,
  });
  await assert.rejects(repo.getPlayerLedger(ALICE_ID, "deadbeef"), (err: unknown) => {
    assert.ok(err instanceof ViewerApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});

test("order requests: create pending, list own, validate menu and quantity", async () => {
  const repo = new MockRepository();
  assert.ok((await repo.getMenu()).length > 0);

  const req = await repo.createOrderRequest(ALICE_ID, SESSION_ID, {
    item_name: "ビール",
    quantity: 2,
  });
  assert.equal(req.status, "pending");
  assert.equal(req.ledger_entry_id, undefined);

  const mine = await repo.listOrderRequests(ALICE_ID, SESSION_ID);
  assert.deepEqual(mine.map((r) => r.item_name), ["ビール"]);
  assert.deepEqual(await repo.listOrderRequests(BOB_ID, SESSION_ID), []);

  await assert.rejects(
    repo.createOrderRequest(ALICE_ID, SESSION_ID, { item_name: "存在しない品", quantity: 1 }),
    (err: unknown) => {
      assert.ok(err instanceof ViewerApiError);
      assert.equal(err.code, "unknown_item");
      return true;
    },
  );
  await assert.rejects(
    repo.createOrderRequest(ALICE_ID, SESSION_ID, { item_name: "ビール", quantity: 0 }),
    (err: unknown) => {
      assert.ok(err instanceof ViewerApiError);
      assert.equal(err.code, "invalid_quantity");
      return true;
    },
  );
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
