/**
 * buildHandText の純関数テスト (node:test, `npm test` = tsx --test)。
 *
 * **正本は `shared/hand_replay/`** — 編集後は `python scripts/sync_shared_ui.py` で再配布。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { buildHandText } from "./handReplayText";
import type { ReplayHand } from "./handReplayModel";

const HAND: ReplayHand = {
  hand_id: 7,
  started_at: "2026-07-12T20:00:00",
  blinds: { sb: 100, bb: 200 },
  board: ["As", "Kc", "Qd"],
  players: [
    { seat: 2, name: "Bob", hole_cards: null, stack_start: 10000, stack_end: 9200, result: -800 },
    { seat: 1, name: "Alice", hole_cards: ["Ah", "Ad"], stack_start: 10000, stack_end: 10800, result: 800 },
  ],
  pot_total: 1600,
  winner_seat: 1,
  actions: [
    { street: "preflop", seat: 1, action: "raise", amount: 600, pot_after: 900 },
    { street: "preflop", seat: 2, action: "call", amount: 400, pot_after: 1300, needs_review: true },
    { street: "flop", seat: 2, action: "fold", amount: 0, pot_after: 1300 },
  ],
};

test("buildHandText renders header, seats, streets and result", () => {
  const text = buildHandText(HAND);
  const lines = text.split("\n");

  assert.equal(lines[0], "Hand #7 ・ 2026-07-12T20:00:00 ・ ブラインド 100/200");
  // seats は seat 昇順。勝者に 🏆、記録が無いホールカードは "?? ??"。
  assert.equal(lines[1], "席1 Alice 🏆 [Ah Ad] 10,000→10,800 (+800)");
  assert.equal(lines[2], "席2 Bob [?? ??] 10,000→9,200 (-800)");
  // ストリート見出しに board スライスと開始時ポット。
  assert.ok(lines.includes("--- プリフロップ (ポット 0)"));
  assert.ok(lines.includes("--- フロップ [As Kc Qd] (ポット 1,300)"));
  // アクション行（日本語ラベル + 要確認マーク）。
  assert.ok(lines.includes("席1 Alice レイズ 600"));
  assert.ok(lines.includes("席2 Bob コール 400（要確認）"));
  assert.equal(lines[lines.length - 1], "結果: 🏆 席1 Alice ・ ポット合計 1,600");
});

test("buildHandText handles missing winner and empty streets", () => {
  const text = buildHandText({
    ...HAND,
    winner_seat: undefined,
    board: ["As", "Kc", "Qd", "5h", "2s"],
    actions: [
      { street: "preflop", seat: 1, action: "allin", amount: 10000, pot_after: 20000 },
    ],
  });
  // all-in ランアウト: アクションの無い street も board が開いていれば載る。
  assert.ok(text.includes("--- リバー [As Kc Qd 5h 2s] (ポット 20,000)"));
  assert.ok(text.includes("（アクションなし）"));
  assert.ok(text.endsWith("結果: ポット合計 1,600"));
});
