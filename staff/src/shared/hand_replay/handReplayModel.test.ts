/**
 * handReplayModel の純関数テスト (node:test, `npm test` = tsx --test)。
 *
 * **正本は `shared/hand_replay/`** — 編集後は `python scripts/sync_shared_ui.py` で再配布。
 *
 * 検査対象: ストリート分割 / board スライス (3/4/5) / ポット境界 (potStart = 前 street の
 * 最終 pot_after) / all-in ランアウト (アクション無し street の表示) / カードパース /
 * 席リング配置と短縮金額表記 (テーブル UI, ADR-0051)。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  actionColor,
  actionLabel,
  ALL_STREETS,
  buildReplayModel,
  compactActionLabel,
  formatChips,
  formatChipsCompact,
  formatSigned,
  formatSignedCompact,
  parseCard,
  preflopFoldedSeats,
  seatRingLayout,
  visibleColumnActions,
  type ReplayHand,
} from "./handReplayModel";

function makeHand(overrides: Partial<ReplayHand> = {}): ReplayHand {
  return {
    hand_id: 1,
    blinds: { sb: 100, bb: 200 },
    board: ["As", "Kc", "Qd", "5h", "2s"],
    players: [
      { seat: 2, name: "Bob", hole_cards: null, stack_start: 10000, stack_end: 9200, result: -800 },
      { seat: 1, name: "Alice", hole_cards: ["Ah", "Ad"], stack_start: 10000, stack_end: 10800, result: 800 },
    ],
    pot_total: 1600,
    pots: [{ amount: 1600, eligible_seats: [1, 2] }],
    winner_seat: 1,
    actions: [
      { street: "preflop", seat: 1, action: "raise", amount: 200, pot_after: 300 },
      { street: "preflop", seat: 2, action: "call", amount: 200, pot_after: 500 },
      { street: "flop", seat: 2, action: "check", amount: 0, pot_after: 500 },
      { street: "flop", seat: 1, action: "bet", amount: 300, pot_after: 800 },
      { street: "flop", seat: 2, action: "call", amount: 300, pot_after: 1100 },
      { street: "turn", seat: 2, action: "check", amount: 0, pot_after: 1100 },
      { street: "turn", seat: 1, action: "check", amount: 0, pot_after: 1100 },
      { street: "river", seat: 2, action: "bet", amount: 500, pot_after: 1600 },
      { street: "river", seat: 1, action: "fold", amount: 0, pot_after: 1600 },
    ],
    ...overrides,
  };
}

test("streets are split in preflop→flop→turn→river order with board slices 0/3/4/5", () => {
  const model = buildReplayModel(makeHand());
  assert.deepEqual(model.streets.map((s) => s.street), ["preflop", "flop", "turn", "river"]);
  assert.deepEqual(model.streets[0].board, []);
  assert.deepEqual(model.streets[1].board, ["As", "Kc", "Qd"]);
  assert.deepEqual(model.streets[2].board, ["As", "Kc", "Qd", "5h"]);
  assert.deepEqual(model.streets[3].board, ["As", "Kc", "Qd", "5h", "2s"]);
});

test("potStart carries the previous street's final pot_after (preflop starts at 0)", () => {
  const model = buildReplayModel(makeHand());
  const [pre, flop, turn, river] = model.streets;
  assert.equal(pre.potStart, 0);
  assert.equal(pre.potEnd, 500);
  assert.equal(flop.potStart, 500);
  assert.equal(flop.potEnd, 1100);
  assert.equal(turn.potStart, 1100);
  assert.equal(river.potStart, 1100);
  assert.equal(river.potEnd, 1600);
});

test("preflop-only hand shows only preflop (no board opened)", () => {
  const model = buildReplayModel(
    makeHand({
      board: [],
      actions: [
        { street: "preflop", seat: 1, action: "raise", amount: 600, pot_after: 900 },
        { street: "preflop", seat: 2, action: "fold", amount: 0, pot_after: 900 },
      ],
    }),
  );
  assert.deepEqual(model.streets.map((s) => s.street), ["preflop"]);
});

test("all-in runout shows postflop streets with no actions (board is open)", () => {
  const model = buildReplayModel(
    makeHand({
      actions: [
        { street: "preflop", seat: 1, action: "allin", amount: 10000, pot_after: 10100 },
        { street: "preflop", seat: 2, action: "call", amount: 10000, pot_after: 20000 },
      ],
    }),
  );
  assert.deepEqual(model.streets.map((s) => s.street), ["preflop", "flop", "turn", "river"]);
  const flop = model.streets[1];
  assert.deepEqual(flop.actions, []);
  // アクションの無い street はポットを引き継ぐ。
  assert.equal(flop.potStart, 20000);
  assert.equal(flop.potEnd, 20000);
  assert.deepEqual(model.streets[3].board, ["As", "Kc", "Qd", "5h", "2s"]);
});

test("seats are sorted by seat number and hole cards pass through (null = unknown)", () => {
  const model = buildReplayModel(makeHand());
  assert.deepEqual(model.seats.map((p) => p.seat), [1, 2]);
  assert.deepEqual(model.seats[0].hole_cards, ["Ah", "Ad"]);
  assert.equal(model.seats[1].hole_cards, null);
});

test("winner / pots / pot_total pass through (legacy backend: pots=[])", () => {
  const model = buildReplayModel(makeHand());
  assert.equal(model.winnerSeat, 1);
  assert.equal(model.potTotal, 1600);
  assert.equal(model.pots[0].amount, 1600);

  const legacy = buildReplayModel(makeHand({ pots: undefined, winner_seat: undefined }));
  assert.deepEqual(legacy.pots, []);
  assert.equal(legacy.winnerSeat, null);
});

test("parseCard parses rank + suit and rejects garbage", () => {
  assert.deepEqual(parseCard("As"), { rank: "A", suit: "s" });
  assert.deepEqual(parseCard("Th"), { rank: "T", suit: "h" });
  assert.deepEqual(parseCard("10d"), { rank: "10", suit: "d" });
  assert.equal(parseCard("A"), null);
  assert.equal(parseCard("Ax"), null);
  assert.equal(parseCard(""), null);
});

test("labels and formatters", () => {
  assert.equal(actionLabel("raise"), "レイズ");
  assert.equal(actionLabel("all_in"), "オールイン");
  assert.equal(actionLabel("limp?"), "limp?"); // 未知は raw のまま
  assert.equal(formatChips(12000), "12,000");
  assert.equal(formatSigned(800), "+800");
  assert.equal(formatSigned(-1600), "-1,600");
  assert.equal(formatSigned(0), "±0");
});

test("seatRingLayout puts the first seat at bottom center and walks clockwise", () => {
  const slots = seatRingLayout(4);
  assert.equal(slots.length, 4);
  // 先頭は下中央（見ている人の手前）。
  assert.deepEqual(slots[0], { top: 88, left: 50 });
  // 下 → 左 → 上 → 右 の順（画面座標なので top が小さいほど上）。
  assert.deepEqual(slots[1], { top: 50, left: 15 });
  assert.deepEqual(slots[2], { top: 12, left: 50 });
  assert.deepEqual(slots[3], { top: 50, left: 85 });
});

test("seatRingLayout keeps every seat inside the container for 2..9 seats", () => {
  for (let n = 2; n <= 9; n += 1) {
    const slots = seatRingLayout(n);
    assert.equal(slots.length, n);
    for (const slot of slots) {
      // 中心が 12%..88% に収まっていれば、チップ半分をずらしても外へ出ない。
      assert.ok(slot.top >= 12 && slot.top <= 88, `top out of range: ${slot.top} (n=${n})`);
      assert.ok(slot.left >= 15 && slot.left <= 85, `left out of range: ${slot.left} (n=${n})`);
    }
  }
  // 不正な席数は空（描画側で落ちない）。
  assert.deepEqual(seatRingLayout(0), []);
  assert.deepEqual(seatRingLayout(-1), []);
});

test("ALL_STREETS is the fixed four-column order", () => {
  assert.deepEqual([...ALL_STREETS], ["preflop", "flop", "turn", "river"]);
});

test("compact formatters shorten amounts for narrow columns", () => {
  assert.equal(formatChipsCompact(600), "600");
  assert.equal(formatChipsCompact(1000), "1k");
  assert.equal(formatChipsCompact(1500), "1.5k");
  assert.equal(formatChipsCompact(12200), "12.2k");
  assert.equal(formatChipsCompact(120000), "120k");
  assert.equal(formatSignedCompact(8200), "+8.2k");
  assert.equal(formatSignedCompact(-600), "-600");
  assert.equal(formatSignedCompact(0), "±0");
});

test("compactActionLabel uses the narrow poker vernacular for the 4 columns", () => {
  assert.equal(compactActionLabel("raise"), "Raise");
  assert.equal(compactActionLabel("allin"), "All-in");
  assert.equal(compactActionLabel("all_in"), "All-in");
  assert.equal(compactActionLabel("limp?"), "limp?"); // 未知は raw のまま
  // 日本語ラベル（共有テキスト・詳細表示側）は据え置き。
  assert.equal(actionLabel("allin"), "オールイン");
});

test("preflopFoldedSeats lists only seats that folded preflop", () => {
  const actions = [
    { street: "preflop", seat: 3, action: "fold" },
    { street: "preflop", seat: 4, action: "raise", amount: 600 },
    { street: "preflop", seat: 1, action: "call", amount: 600 },
    { street: "flop", seat: 4, action: "fold" }, // ポストフロップ fold は対象外
  ];
  assert.deepEqual(preflopFoldedSeats(actions), [3]);
  assert.deepEqual(preflopFoldedSeats([]), []);
});

test("visibleColumnActions hides folds in the preflop column only", () => {
  const preflop = [
    { street: "preflop", seat: 3, action: "fold" },
    { street: "preflop", seat: 4, action: "raise", amount: 600 },
  ];
  assert.deepEqual(
    visibleColumnActions("preflop", preflop).map((a) => a.seat),
    [4],
  );
  // 他ストリートは素通し（フロップ以降の fold は残す）。
  const flop = [
    { street: "flop", seat: 4, action: "fold" },
    { street: "flop", seat: 1, action: "call", amount: 1000 },
  ];
  assert.deepEqual(visibleColumnActions("flop", flop), flop);
});

test("hiding preflop folds does not change the pot the column shows", () => {
  // 描画専用フィルタなので、fold を隠しても potEnd は全アクション由来のまま。
  const hand = makeHand({
    actions: [
      { street: "preflop", seat: 3, action: "fold", amount: 0, pot_after: 300 },
      { street: "preflop", seat: 1, action: "raise", amount: 600, pot_after: 900 },
      { street: "preflop", seat: 2, action: "call", amount: 600, pot_after: 1500 },
    ],
  });
  const preflop = buildReplayModel(hand).streets.find((st) => st.street === "preflop");
  assert.ok(preflop);
  assert.equal(preflop.potEnd, 1500);
  assert.deepEqual(
    visibleColumnActions("preflop", preflop.actions).map((a) => a.action),
    ["raise", "call"],
  );
});

test("actionColor maps each action to its own hue and falls back for unknown", () => {
  const colors = ["fold", "check", "call", "bet", "raise", "allin"].map(actionColor);
  // 6 種別がすべて異なる色（色で種別が読み分けられる）。
  assert.equal(new Set(colors).size, 6);
  assert.equal(actionColor("all_in"), actionColor("allin"));
  assert.equal(actionColor("limp?"), "#f2f5f7"); // 未知は通常テキスト色
});
