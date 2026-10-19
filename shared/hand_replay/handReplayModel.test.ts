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
  heardDetails,
  parseCard,
  preflopFoldedSeats,
  reasonLabels,
  recordedAmount,
  seatRingLayout,
  shownAmount,
  streetFlow,
  streetTotals,
  TABLE_FRONT,
  tableSeats,
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

// ――― 表示の額・音声テスト用の表示（ADR-0060 / 2026-09-29〜）―――

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
  assert.deepEqual(reasonLabels("amount_only+second_ear"), [
    "数字だけ（ベットかレイズかは場面から）",
    "Whisper が読めず、第 2 の耳で読んだ",
  ]);
  assert.deepEqual(reasonLabels("amount_only+sentence_after_chatter"), [
    "数字だけ（ベットかレイズかは場面から）",
    "雑談の文のあとの文だけで読んだ",
  ]);
  assert.deepEqual(reasonLabels("amount_only+phonetic_amount+second_ear"), [
    "数字だけ（ベットかレイズかは場面から）",
    "意味のない語を、額の音に近いので額と読んだ",
    "Whisper が読めず、第 2 の耳で読んだ",
  ]);
  assert.deepEqual(reasonLabels("amount_only+amount_restated"), [
    "数字だけ（ベットかレイズかは場面から）",
    "直前の賭けの額の言い直しとして額を直した",
  ]);
  assert.deepEqual(reasonLabels("amount_only+legal_amount+ambiguous_amount"), [
    "数字だけ（ベットかレイズかは場面から）",
    "聞いた額がその場面で使えないので、音の近い使える額にした",
    "金額の言い方が曖昧",
  ]);
});

test("streetTotals / shownAmount: a call is shown as what the player put in on that street", () => {
  const hand: ReplayHand = {
    hand_id: 8,
    blinds: { sb: 100, bb: 200 },
    players: [
      { seat: 4, name: "A", stack_start: 10500 },
      { seat: 5, name: "B", stack_start: 13800 },
      { seat: 6, name: "C", stack_start: 5700 },
    ],
    actions: [
      { street: "preflop", seat: 4, action: "raise", amount: 900, stack_after: 9600 },
      { street: "preflop", seat: 5, action: "call", amount: 800, stack_after: 12900 }, // SB: 100 + 800
      { street: "preflop", seat: 6, action: "allin", amount: 5700, stack_after: 0 },
      { street: "preflop", seat: 4, action: "call", amount: 4800, stack_after: 4800 },
      { street: "preflop", seat: 5, action: "allin", amount: 13800, stack_after: 0 },
      { street: "preflop", seat: 4, action: "call", amount: 4800, stack_after: 0 }, // 足りないオールインのコール
      { street: "flop", seat: 4, action: "check", amount: 0 }, // stack_after が無い行 (記録の額から: チェック = 0)
    ],
  };
  assert.deepEqual(streetTotals(hand), [900, 900, 5700, 5700, 13800, 10500, 0]);
  const shown = buildReplayModel(hand).streets[0].actions.map(shownAmount);
  assert.deepEqual(shown, [900, 900, 5700, 5700, 13800, 10500]);
  // 持ち点が分からなければ記録の額
  assert.equal(shownAmount({ street: "preflop", seat: 1, action: "call", amount: 400 }), 400);
  // 合わない (訂正で額だけ変わった) ときも記録の額
  assert.equal(shownAmount({ street: "preflop", seat: 1, action: "call", amount: 500, total: 300 }), 500);
});

test("streetFlow: corrected rows use what the player had in before + the corrected amount", () => {
  const base: ReplayHand = {
    hand_id: 8,
    blinds: { sb: 100, bb: 200 },
    position_map: { "4": "BTN", "5": "SB", "6": "BB" },
    players: [
      { seat: 4, name: "A", stack_start: 10500 },
      { seat: 5, name: "B", stack_start: 13800 },
      { seat: 6, name: "C", stack_start: 5700 },
    ],
    actions: [
      { street: "preflop", seat: 4, action: "raise", amount: 900, stack_after: 9600 },
      { street: "preflop", seat: 5, action: "call", amount: 800, stack_after: 12900 },
      { street: "preflop", seat: 6, action: "fold", amount: 0, stack_after: 5500 },
    ],
  };
  assert.deepEqual(streetFlow(base), [
    { prior: 0, total: 900 },
    { prior: 100, total: 900 },
    { prior: 200, total: 200 },
  ]);
  // SB のコール (追加 800) をレイズ 2700 に訂正: 合計はレイズの額
  const raised = structuredClone(base);
  Object.assign(raised.actions[1], { action: "raise", amount: 2700, _original: { action: "call", amount: 800 } });
  assert.equal(streetFlow(raised)[1].total, 2700);
  // BTN のレイズ 900 をコール (追加 200) に訂正: 前に出していた額は 0 (ブラインドではない)
  const called = structuredClone(base);
  Object.assign(called.actions[0], { action: "call", amount: 200, _original: { action: "raise", amount: 900 } });
  assert.deepEqual(streetFlow(called)[0], { prior: 0, total: 200 });
  // SB のコールの額だけ訂正 (800 → 700): SB の 100 + 700
  const amended = structuredClone(base);
  Object.assign(amended.actions[1], { amount: 700, _original: { amount: 800 } });
  assert.deepEqual(streetFlow(amended)[1], { prior: 100, total: 800 });
  // SB のレイズを訂正でコールに: ブラインドはポジションから (SB 100)
  const sbRaise = structuredClone(base);
  sbRaise.actions[1] = {
    street: "preflop", seat: 5, action: "call", amount: 800, stack_after: 11100,
    _original: { action: "raise", amount: 2700 },
  };
  assert.deepEqual(streetFlow(sbRaise)[1], { prior: 100, total: 900 });
  // ヘッズアップはボタンが SB
  const headsUp: ReplayHand = {
    hand_id: 9,
    blinds: { sb: 100, bb: 200 },
    position_map: { "1": "BTN", "2": "BB" },
    players: [
      { seat: 1, name: "A", stack_start: 5000 },
      { seat: 2, name: "B", stack_start: 5000 },
    ],
    actions: [{ street: "preflop", seat: 1, action: "call", amount: 100, _original: { action: "raise", amount: 600 } }],
  };
  assert.deepEqual(streetFlow(headsUp)[0], { prior: 100, total: 200 });
});

test("shownAmount: a short all-in call recorded as the added chips is shown as the total", () => {
  const hand: ReplayHand = {
    hand_id: 3,
    players: [
      { seat: 1, name: "A", stack_start: 10000 },
      { seat: 2, name: "B", stack_start: 3000 },
    ],
    actions: [
      { street: "flop", seat: 1, action: "bet", amount: 1000, stack_after: 9000 },
      { street: "flop", seat: 2, action: "call", amount: 1000, stack_after: 2000 },
      { street: "turn", seat: 1, action: "bet", amount: 5000, stack_after: 4000 },
      { street: "turn", seat: 2, action: "allin", amount: 2000, stack_after: 0 },
    ],
  };
  const shown = buildReplayModel(hand).streets.flatMap((st) => st.actions.map(shownAmount));
  assert.deepEqual(shown, [1000, 1000, 5000, 2000]);
});

test("recordedAmount: a call typed as the street total is stored as the added chips", () => {
  assert.equal(recordedAmount("call", 900, 100), 800); // SB が 900 にコール → 追加 800
  assert.equal(recordedAmount("call", 50, 100), undefined); // 前に出していた額より少ない
  assert.equal(recordedAmount("call", 400, undefined), 400); // 分からなければそのまま
  assert.equal(recordedAmount("raise", 1800, 200), 1800); // レイズはトータルのまま
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

test("tableSeats: 8 fixed seats by seat number, missing seats are empty", () => {
  const p = (seat: number) => ({ seat, name: `P${seat}` });
  const seats = tableSeats([p(5), p(2), p(7)]);
  assert.deepEqual(seats.map((s) => s.seat), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(seats.filter((s) => s.player).map((s) => s.seat), [2, 5, 7]);
  assert.equal(seats[0].player, null);
  // 8 を超える席番号があれば、その番号まで広げる
  assert.equal(tableSeats([p(1), p(9)]).length, 9);
  assert.equal(tableSeats([]).length, 8);
});

test("seatRingLayout front: the spot between seats 4 and 5 is at the bottom center", () => {
  const slots = seatRingLayout(8, TABLE_FRONT);
  // 席 4（index 3）が右下、席 5（index 4）が左下で、下中央をはさんで左右対称
  assert.equal(slots[3].top, slots[4].top);
  assert.ok(slots[3].top > 80 && slots[3].left > 50 && slots[4].left < 50);
  assert.equal(Math.round(slots[3].left + slots[4].left), 100);
  // 奥（上中央）をはさむのは席 8 と席 1
  assert.equal(slots[7].top, slots[0].top);
  assert.ok(slots[0].top < 20 && slots[0].left > 50 && slots[7].left < 50);
  // 既定（front = 0）は先頭の席が下中央のまま
  assert.deepEqual(seatRingLayout(4)[0], { top: 88, left: 50 });
});
