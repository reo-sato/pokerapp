/**
 * handReplayModel の純関数テスト (node:test, `npm test` = tsx --test)。
 *
 * **正本は `shared/hand_replay/`** — 編集後は `python scripts/sync_shared_ui.py` で再配布。
 *
 * 検査対象: ストリート分割 / board スライス (3/4/5) / ポット境界 (potStart = 前 street の
 * 最終 pot_after) / all-in ランアウト (アクション無し street の表示) / カードパース。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  actionLabel,
  buildReplayModel,
  formatChips,
  formatSigned,
  heardDetails,
  parseCard,
  reasonLabels,
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

// ――― 音声テスト用の表示 (ADR-0060) ―――

test("reasonLabels: known codes, patterns, and unknown codes", () => {
  assert.deepEqual(reasonLabels(undefined), []);
  assert.deepEqual(reasonLabels("check_facing_bet+amount_snapped"), [
    "ベットがあるのにチェック → コールにした",
    "金額を出せる額に寄せた",
  ]);
  assert.deepEqual(reasonLabels("actor_conflict_capped(sensed=6)"), [
    "言った席6は手番から遠いので手番の席にした",
  ]);
  assert.deepEqual(reasonLabels("raise_illegal_to_call"), ["レイズできない場面 → コールにした"]);
  assert.deepEqual(reasonLabels("something_new"), ["something_new"]);
});

test("heardDetails: heard text, correction, and synthesized fold", () => {
  const spoken = heardDetails({
    street: "flop", seat: 3, action: "call", amount: 200,
    raw_text: "シート3 チェック", corrected_from: "check", reason: "check_facing_bet",
  });
  assert.equal(spoken.heard, "シート3 チェック");
  assert.deepEqual(spoken.notes, ["聞き取り チェック → コール", "ベットがあるのにチェック → コールにした"]);

  const synth = heardDetails({ street: "preflop", seat: 2, action: "fold", reason: "synth_silent_fold" });
  assert.equal(synth.heard, null);
  assert.deepEqual(synth.notes, ["声のないフォールド（あとで言われた席から補った）"]);

  const remapped = heardDetails({
    street: "flop", seat: 1, action: "raise", amount: 600,
    raw_text: "シート1 ベット 600", corrected_from: "bet", reason: "bet_to_raise",
  });
  assert.deepEqual(remapped.notes, ["聞き取り ベット → レイズ"]);   // 付け替えは 1 行で足りる

  const plain = heardDetails({ street: "river", seat: 1, action: "check", raw_text: "チェック" });
  assert.deepEqual(plain, { heard: "チェック", notes: [] });
});
