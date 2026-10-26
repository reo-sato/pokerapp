import assert from "node:assert/strict";
import { test } from "node:test";

import { newestHandsFirst, newestSessionsFirst } from "./ordering";

test("新しいセッションが上（始まった時刻の新しい順、同じなら後に作ったほう）", () => {
  const got = newestSessionsFirst([
    { session_id: "a", started_at: "2026-10-19T18:00:00" },
    { session_id: "b", started_at: "2026-10-26T18:30:00" },
    { session_id: "c", started_at: null },
    { session_id: "d", started_at: "2026-10-26T18:30:00" },
  ]);
  assert.deepEqual(got.map((s) => s.session_id), ["d", "b", "a", "c"]);
});

test("新しいハンドが上（ハンド番号の大きい順）。元の並びは変えない", () => {
  const hands = [{ hand_id: 1 }, { hand_id: 3 }, { hand_id: 2 }];
  assert.deepEqual(newestHandsFirst(hands).map((h) => h.hand_id), [3, 2, 1]);
  assert.deepEqual(hands.map((h) => h.hand_id), [1, 3, 2]);
});
