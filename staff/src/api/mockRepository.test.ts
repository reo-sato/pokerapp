/**
 * MockStaffRepository の契約挙動テスト (node:test, `npm test` = tsx --test)。
 *
 * 検査対象: StaffRepository interface の意味論 — 認可（token）/ 会計（中間集計 / エントリ追加 /
 * 精算確定 / 支払）/ 注文（確定で order entry / 却下）。error 分岐は code（error-shapes.md と一致）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { MockStaffRepository } from "./mockRepository";
import { StaffApiError } from "./types";
import {
  ALICE_ID,
  CLOSED_SESSION_ID,
  OPEN_SESSION_ID,
  VALID_STAFF_TOKEN,
} from "../mocks/fixtures";

function authed(): MockStaffRepository {
  const repo = new MockStaffRepository();
  repo.setToken(VALID_STAFF_TOKEN);
  return repo;
}

test("staff reads reject without a valid token (unauthorized)", async () => {
  const repo = new MockStaffRepository();
  await assert.rejects(repo.listSessions(), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "unauthorized");
    return true;
  });
  repo.setToken("wrong");
  await assert.rejects(repo.getSettlement(OPEN_SESSION_ID), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "unauthorized");
    return true;
  });
});

test("listSessions / listPlayers return fixtures when authed", async () => {
  const repo = authed();
  const sessions = await repo.listSessions();
  assert.deepEqual(sessions.map((s) => s.status).sort(), ["closed", "open"]);
  const players = await repo.listPlayers();
  assert.deepEqual(players.map((p) => p.display_name), ["Alice", "Bob", "Carol"]);
  assert.deepEqual(await repo.getBuyinPresets(), [10000, 20000, 30000]);
});

test("getSettlement computes intermediate aggregate per player (speculative)", async () => {
  const repo = authed();
  const rows = await repo.getSettlement(OPEN_SESSION_ID);
  const alice = rows.find((r) => r.player_id === ALICE_ID);
  assert.ok(alice);
  assert.equal(alice.cash_in_total, 10000);
  assert.equal(alice.entry_fee, 500);
  assert.equal(alice.net_due_to_store, 10500);
  assert.equal(alice.payment_status, "unpaid");
});

test("getSettlement rejects unknown session with not_found", async () => {
  const repo = authed();
  await assert.rejects(repo.getSettlement("deadbeef"), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});

test("addLedgerEntry appends and flows into settlement; entry_fee rejects point", async () => {
  const repo = authed();
  const entry = await repo.addLedgerEntry(OPEN_SESSION_ID, {
    player_id: ALICE_ID,
    kind: "rebuy",
    cash_amount: 5000,
  });
  assert.equal(entry.kind, "rebuy");
  const rows = await repo.getSettlement(OPEN_SESSION_ID);
  const alice = rows.find((r) => r.player_id === ALICE_ID);
  assert.equal(alice?.cash_in_total, 15000);

  await assert.rejects(
    repo.addLedgerEntry(OPEN_SESSION_ID, {
      player_id: ALICE_ID,
      kind: "entry_fee",
      cash_amount: 500,
      point_amount: 100,
    }),
    (err: unknown) => {
      assert.ok(err instanceof StaffApiError);
      assert.equal(err.code, "entry_fee_requires_cash");
      return true;
    },
  );
});

test("commitSettlement: closed only, once; then payment status transitions", async () => {
  const repo = authed();
  await assert.rejects(repo.commitSettlement(OPEN_SESSION_ID), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "session_not_closed");
    return true;
  });

  const rows = await repo.commitSettlement(CLOSED_SESSION_ID);
  assert.ok(rows.length > 0);
  assert.ok(rows.every((r) => r.settled_at !== ""));

  await assert.rejects(repo.commitSettlement(CLOSED_SESSION_ID), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "already_settled");
    return true;
  });

  const paid = await repo.setPaymentStatus(CLOSED_SESSION_ID, ALICE_ID, "paid");
  assert.equal(paid.payment_status, "paid");
  const partial = await repo.recordPayment(CLOSED_SESSION_ID, ALICE_ID, 1000);
  assert.equal(partial.payment_status, "partial");
  assert.equal(partial.paid_amount, 1000);
});

test("orders: confirm creates order entry, reject resolves; codes for bad input", async () => {
  const repo = authed();
  const menu = await repo.getMenu();
  assert.ok(menu.length > 0);

  const pending = await repo.listOrderRequests(OPEN_SESSION_ID, "pending");
  assert.equal(pending.length, 2);

  const confirmed = await repo.confirmOrder(pending[0].request_id, 700);
  assert.equal(confirmed.status, "confirmed");
  assert.ok(confirmed.ledger_entry_id);
  // 確定で order ledger entry が増える → 中間集計の order_total に反映。
  const rows = await repo.getSettlement(OPEN_SESSION_ID);
  const alice = rows.find((r) => r.player_id === ALICE_ID);
  assert.equal(alice?.order_total, 1400);

  const rejected = await repo.rejectOrder(pending[1].request_id);
  assert.equal(rejected.status, "rejected");

  await assert.rejects(repo.confirmOrder(pending[0].request_id, 700), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "already_resolved");
    return true;
  });
  await assert.rejects(repo.confirmOrder("nope", 700), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});

// ――― ADR-0036 §A: reversal / point grant ―――

test("reverseEntry negates cash and flows into settlement; unknown is not_found", async () => {
  const repo = authed();
  const entry = await repo.addLedgerEntry(OPEN_SESSION_ID, {
    player_id: ALICE_ID,
    kind: "rebuy",
    cash_amount: 3000,
  });
  const before = (await repo.getSettlement(OPEN_SESSION_ID)).find((r) => r.player_id === ALICE_ID);
  const rev = await repo.reverseEntry(entry.entry_id);
  assert.equal(rev.reverses_entry_id, entry.entry_id);
  assert.equal(rev.cash_amount, -3000);
  const after = (await repo.getSettlement(OPEN_SESSION_ID)).find((r) => r.player_id === ALICE_ID);
  assert.equal((before?.net_due_to_store ?? 0) - 3000, after?.net_due_to_store);

  await assert.rejects(repo.reverseEntry("nope"), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});

test("grantPoints validates player and positive amount", async () => {
  const repo = authed();
  await repo.grantPoints(ALICE_ID, 500);
  await assert.rejects(repo.grantPoints("f".repeat(32), 100), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "unknown_player");
    return true;
  });
  await assert.rejects(repo.grantPoints(ALICE_ID, 0), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "invalid_amount");
    return true;
  });
});

// ――― ADR-0036 §B: session / player / seating ―――

test("session lifecycle: create, list, close, already_closed", async () => {
  const repo = authed();
  const before = (await repo.listSessions()).length;
  const created = await repo.createSession("土曜 #9", { sb: 100, bb: 200 });
  assert.equal(created.status, "open");
  assert.equal(created.label, "土曜 #9");
  assert.equal((await repo.listSessions()).length, before + 1);

  const closed = await repo.closeSession(created.session_id);
  assert.equal(closed.status, "closed");
  assert.ok(closed.ended_at);
  await assert.rejects(repo.closeSession(created.session_id), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "already_closed");
    return true;
  });
});

test("player lifecycle: create, rename, duplicate, not_found", async () => {
  const repo = authed();
  const p = await repo.createPlayer("Dave");
  assert.equal(p.display_name, "Dave");
  const renamed = await repo.renamePlayer(p.player_id, "Dave2");
  assert.equal(renamed.display_name, "Dave2");

  await assert.rejects(repo.createPlayer("  "), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "empty_display_name");
    return true;
  });
  await assert.rejects(repo.createPlayer("Alice"), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "duplicate_display_name");
    return true;
  });
  await assert.rejects(repo.renamePlayer("f".repeat(32), "X"), (err: unknown) => {
    assert.ok(err instanceof StaffApiError);
    assert.equal(err.code, "not_found");
    return true;
  });
});

test("seating: assign batch into next hand, read current, conflict/closed codes", async () => {
  const repo = authed();
  const s = await repo.createSession("Seat test");
  const alice = await repo.createPlayer("S-Alice");
  const bob = await repo.createPlayer("S-Bob");

  const added = await repo.assignSeats(s.session_id, 1, [
    { seat_no: 1, player_id: alice.player_id },
    { seat_no: 2, player_id: bob.player_id },
  ]);
  assert.deepEqual(added.map((a) => a.seat_no), [1, 2]);

  const seating = await repo.getSeating(s.session_id);
  assert.deepEqual(seating.hand_ids, [1]);
  assert.equal(seating.seating.length, 2);

  await assert.rejects(
    repo.assignSeats(s.session_id, 2, [
      { seat_no: 1, player_id: alice.player_id },
      { seat_no: 1, player_id: bob.player_id },
    ]),
    (err: unknown) => {
      assert.ok(err instanceof StaffApiError);
      assert.equal(err.code, "seat_taken");
      return true;
    },
  );
  await assert.rejects(
    repo.assignSeats(s.session_id, 3, [{ seat_no: 99, player_id: alice.player_id }]),
    (err: unknown) => {
      assert.ok(err instanceof StaffApiError);
      assert.equal(err.code, "invalid_seat");
      return true;
    },
  );
  await assert.rejects(
    repo.assignSeats(s.session_id, 4, [{ seat_no: 1, player_id: "f".repeat(32) }]),
    (err: unknown) => {
      assert.ok(err instanceof StaffApiError);
      assert.equal(err.code, "unknown_player");
      return true;
    },
  );

  await repo.closeSession(s.session_id);
  await assert.rejects(
    repo.assignSeats(s.session_id, 5, [{ seat_no: 3, player_id: alice.player_id }]),
    (err: unknown) => {
      assert.ok(err instanceof StaffApiError);
      assert.equal(err.code, "session_closed");
      return true;
    },
  );
});
