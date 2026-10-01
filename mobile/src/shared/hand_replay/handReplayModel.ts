/**
 * ハンドリプレイの表示モデル (ADR-0044)。
 *
 * **正本は `shared/hand_replay/` — コピー先 (mobile/staff の src/shared/hand_replay/) を
 * 直接編集しないこと。** 編集後は `python scripts/sync_shared_ui.py` で再配布する
 * (drift は `tests/test_shared_ui_sync.py` が CI で検知する)。
 *
 * hand schema 1.0 (docs/contracts/schemas/hand.schema.json) のサブセットを構造的に
 * 受け取り、GGPoker のハンドヒストリー風「ストリート単位」のセクション列に変換する。
 * 両アプリの型 (mobile HandSummary / staff hands read 応答) をそのまま渡せるよう、
 * 型はここで自前定義し、どちらのアプリの types.ts にも依存しない。
 */

export interface ReplayAction {
  street: string; // "preflop" | "flop" | "turn" | "river"
  seat: number;
  player_name?: string;
  action: string; // "check" | "call" | "bet" | "raise" | "fold" | "allin" | ...
  amount?: number;
  pot_after?: number;
  stack_after?: number;
  needs_review?: boolean;
  corrected?: boolean; // 訂正オーバーレイ痕 (ADR-0036)
  // ――― 音声テスト用の表示 (ADR-0060。showHeard のときだけ使う) ―――
  raw_text?: string; // そのアクションになった発話 (Whisper の書き起こし / CLI で打った読み上げ文)
  reason?: string; // 補正・合成の理由コード ("+" 区切り, ADR-0047 G2)
  corrected_from?: string | null; // 補正前に聞き取った action
  position?: string; // ポジション名 (BTN/SB/BB/…, ISSUE-0032)
  _original?: { action?: string; amount?: number }; // 訂正前の値 (ADR-0036)
  /** そのストリートでその人が出した合計 (表示用。buildReplayModel が stack_after から付ける)。 */
  total?: number;
}

export interface ReplayPlayer {
  seat: number;
  name: string;
  hole_cards?: string[] | null; // null = 不明 (RFID 取りこぼし / prefold)
  stack_start?: number;
  stack_end?: number;
  result?: number;
}

export interface ReplayPot {
  amount: number;
  eligible_seats?: number[];
}

/** hand schema 1.0 のリプレイに必要なサブセット (additive 互換)。 */
export interface ReplayHand {
  hand_id: number;
  started_at?: string;
  blinds?: { sb?: number; bb?: number };
  board?: string[];
  players: ReplayPlayer[];
  pot_total?: number;
  pots?: ReplayPot[]; // main/side (pokerkit backend。legacy は [])
  winner_seat?: number | null;
  /** 勝者の決まり方 (fold / cards / estimated / announced / undetermined = 札が読めず判定できない・チップは動かしていない)。 */
  winner_source?: string | null;
  actions: ReplayAction[];
  review_required?: boolean;
  position_map?: Record<string, string>; // 席 → ポジション名 (pokerkit backend)
}

export interface StreetSection {
  street: string; // "preflop" 等 (schema 上のキー)
  label: string; // 表示名 (プリフロップ 等)
  board: string[]; // この street で見えている board (先頭 3/4/5 枚スライス)
  potStart: number; // street 開始時点のポット (前 street 最終 action の pot_after)
  potEnd: number; // street 終了時点のポット
  actions: ReplayAction[];
}

export interface ReplayModel {
  handId: number;
  blinds?: { sb?: number; bb?: number };
  /** seat 昇順の参加者 (ホールカードは記録がある席は全員分そのまま)。 */
  seats: ReplayPlayer[];
  streets: StreetSection[];
  winnerSeat: number | null;
  /** 勝者が決まらずチップを動かしていない (winner_source=undetermined, hand schema 1.6)。 */
  noWinner: boolean;
  potTotal: number | null;
  pots: ReplayPot[];
}

const STREETS = ["preflop", "flop", "turn", "river"] as const;

/** street ごとに見えている board 枚数 (プリフロップ 0 / フロップ 3 / ターン 4 / リバー 5)。 */
const BOARD_VISIBLE: Record<string, number> = {
  preflop: 0,
  flop: 3,
  turn: 4,
  river: 5,
};

export const STREET_LABELS: Record<string, string> = {
  preflop: "プリフロップ",
  flop: "フロップ",
  turn: "ターン",
  river: "リバー",
};

export const ACTION_LABELS: Record<string, string> = {
  fold: "フォールド",
  check: "チェック",
  call: "コール",
  bet: "ベット",
  raise: "レイズ",
  allin: "オールイン",
  all_in: "オールイン",
  blind: "ブラインド",
};

/** アクション種別の表示名 (未知の種別は raw のまま表示する)。 */
export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

/** "As" → {rank: "A", suit: "s"}。スート 1 文字 + 残りが rank。不正形式は null。 */
export function parseCard(card: string): { rank: string; suit: string } | null {
  if (typeof card !== "string" || card.length < 2) return null;
  const suit = card[card.length - 1].toLowerCase();
  if (!"shdc".includes(suit)) return null;
  return { rank: card.slice(0, -1).toUpperCase(), suit };
}

export const SUIT_SYMBOLS: Record<string, string> = {
  s: "♠", // ♠
  h: "♥", // ♥
  d: "♦", // ♦
  c: "♣", // ♣
};

/** 4 色デッキ (♠黒 / ♥赤 / ♦青 / ♣緑)。カードチップは明色背景に載せる前提。 */
export const SUIT_COLORS: Record<string, string> = {
  s: "#1c2228",
  h: "#c62828",
  d: "#1565c0",
  c: "#2e7d32",
};

/**
 * ReplayHand → ストリート単位の表示モデル。
 *
 * - street は schema の並び (preflop→flop→turn→river) で、
 *   「アクションがある」または「board がその street まで開いている」ものを含める
 *   (all-in ランアウトはアクションが無くても board が開くため表示する)。
 * - potStart は直前 street の最終 action の pot_after (無ければ引き継ぎ)。preflop は 0。
 * - アクションは元配列の順序を保持する (street 内の時系列 = 記録順)。
 */
export function buildReplayModel(hand: ReplayHand): ReplayModel {
  const board = hand.board ?? [];
  const totals = streetTotals(hand);
  const actions = (hand.actions ?? []).map((a, i) =>
    totals[i] == null ? a : { ...a, total: totals[i] },
  );
  const byStreet: Record<string, ReplayAction[]> = {};
  for (const a of actions) {
    (byStreet[a.street] ??= []).push(a);
  }

  const streets: StreetSection[] = [];
  let pot = 0;
  for (const street of STREETS) {
    const streetActions = byStreet[street] ?? [];
    const visible = BOARD_VISIBLE[street];
    const boardOpen = street !== "preflop" && board.length >= visible;
    if (streetActions.length === 0 && !boardOpen && street !== "preflop") continue;
    const potStart = pot;
    for (const a of streetActions) {
      if (typeof a.pot_after === "number") pot = a.pot_after;
    }
    streets.push({
      street,
      label: STREET_LABELS[street] ?? street,
      board: board.slice(0, visible),
      potStart,
      potEnd: pot,
      actions: streetActions,
    });
  }

  const seats = [...(hand.players ?? [])].sort((a, b) => a.seat - b.seat);
  return {
    handId: hand.hand_id,
    blinds: hand.blinds,
    seats,
    streets,
    winnerSeat: hand.winner_seat ?? null,
    noWinner: hand.winner_seat == null && hand.winner_source === "undetermined",
    potTotal: hand.pot_total ?? null,
    pots: hand.pots ?? [],
  };
}

/** ベット・レイズ・オールインの額はトータル (記録どおり)。コールは追加額。 */
const BET_LIKE = new Set(["bet", "raise", "allin", "all_in"]);
const CALL_LIKE = new Set(["call", "allin", "all_in"]);

/** その席の、そのストリートの前に出していた額と、アクションのあとの合計 (表示用)。 */
export interface StreetFlow {
  prior?: number;
  total?: number;
}

/** プリフロップにその席が払ったブラインド (ポジションから)。分からなければ undefined。 */
function blindOf(hand: ReplayHand, a: ReplayAction): number | undefined {
  const positions = hand.position_map ?? {};
  const names = Object.values(positions);
  let pos = a.position || positions[String(a.seat)];
  if (!pos) return undefined;
  if (pos === "BTN") {
    if (names.length === 0) return undefined; // ヘッズアップ (ボタンが SB) か分からない
    pos = names.includes("SB") ? "BTN" : "SB";
  }
  if (pos === "SB" || pos === "BB") {
    const blind = pos === "SB" ? hand.blinds?.sb : hand.blinds?.bb;
    return typeof blind === "number" ? blind : undefined;
  }
  return 0;
}

/** アクションのあとの合計 (記録の額から)。コールは前に出していた額 + 追加額。 */
function afterOf(action: string, amount: number, prior: number | undefined): number | undefined {
  if (action === "call") return prior == null ? undefined : prior + amount;
  if (BET_LIKE.has(action)) return amount;
  return prior; // チェック / フォールド
}

/**
 * 各アクションの「前に出していた額」と「あとの合計」— その人がそのストリートで出した額 (表示用。
 * オーナー 2026-09-29: コールは追加額ではなくトータルで見せる。記録の amount はコールなら追加額のまま)。
 *
 * 合計 = ストリートの始めの持ち点 − stack_after。プリフロップはブラインドも入る (stack_start は払う前)。
 * 訂正した行 (_original, ADR-0036) は持ち点が訂正前のままなので、前に出していた額 + 訂正後の額から出す。
 * 分からない値は undefined。core/hand_log.py の street_flow と同じ計算。
 */
export function streetFlow(hand: ReplayHand): StreetFlow[] {
  const last = new Map<number, number>();
  for (const p of hand.players ?? []) {
    if (typeof p.stack_start === "number") last.set(p.seat, p.stack_start);
  }
  let begin = new Map<number, number>();
  let put = new Map<number, number>(); // このストリートで出した額 (記録どおり)
  let street: string | undefined;
  let first = true;
  return (hand.actions ?? []).map((a) => {
    if (first || a.street !== street) {
      first = false;
      street = a.street;
      begin = new Map(last);
      put = new Map();
    }
    const original = a._original ?? {};
    const wasAction = original.action ?? a.action;
    const wasAmount = original.amount ?? a.amount ?? 0;
    const start = begin.get(a.seat);
    const recorded =
      typeof a.stack_after === "number" && start != null ? start - a.stack_after : undefined;
    let prior = put.get(a.seat);
    if (prior == null) {
      if (a.street !== "preflop") prior = 0;
      else if (recorded != null && wasAction === "call") prior = recorded - wasAmount;
      else if (recorded != null && (wasAction === "check" || wasAction === "fold")) prior = recorded;
      else prior = blindOf(hand, a);
    }
    if (typeof a.stack_after === "number") last.set(a.seat, a.stack_after);
    let total = recorded ?? afterOf(wasAction, wasAmount, prior);
    if (total != null) put.set(a.seat, total);
    if ("action" in original || "amount" in original) total = afterOf(a.action, a.amount ?? 0, prior);
    return { prior, total };
  });
}

/** 各アクションのあと、その人がそのストリートで出した額の合計 (streetFlow の合計だけ)。 */
export function streetTotals(hand: ReplayHand): (number | undefined)[] {
  return streetFlow(hand).map((f) => f.total);
}

/**
 * 画面に出す額: コール (とオールイン) はトータル (そのストリートで出した合計)、ほかは記録の額 (ベット・レイズは
 * もともとトータル。足りないオールインのコールは記録が追加額)。トータルが分からない・合わないときは記録の額。
 */
export function shownAmount(a: ReplayAction): number | undefined {
  if (CALL_LIKE.has(a.action) && a.total != null && a.total >= (a.amount ?? 0)) return a.total;
  return a.amount;
}

/**
 * 訂正画面の金額欄 → 記録する額。金額欄はコールもトータルで入れるので、コールは前に出していた額を引いて
 * 追加額に戻す (記録のコールは追加額)。前に出していた額より少なければ undefined (入力の誤り)。
 */
export function recordedAmount(
  action: string,
  typed: number,
  prior: number | undefined,
): number | undefined {
  if (action !== "call" || prior == null) return typed;
  return typed >= prior ? typed - prior : undefined;
}

/** 補正・合成の理由コード → 短い日本語 (音声テスト用の表示, ADR-0060)。未知のコードはそのまま。 */
const REASON_LABELS: Record<string, string> = {
  synth_silent_fold: "声のないフォールド（あとで言われた席から補った）",
  actor_sensed_over_prior: "手番と違う席を言った（間の席をフォールドにした）",
  check_facing_bet: "ベットがあるのにチェック → コールにした",
  check_illegal_fold: "チェックできない場面 → フォールドにした",
  heard_call_but_check: "払う額が無いのでコールをチェックにした",
  high_conf_asr_projection: "はっきり聞こえたが場面と合わない",
  no_amount_heard: "金額が聞き取れず最小額にした",
  raise_to_vs_by_ambiguous: "レイズ額が「まで」か「追加」か曖昧",
  rounded_to_bb: "金額を BB 単位に丸めた",
  amount_snapped: "金額を出せる額に寄せた",
  no_legal_context: "手番が分からない場面だった",
  multi_action_keywords: "1 回の発話にアクションが複数あった",
  too_many_actions: "1 回の発話にアクションが多すぎた（繰り返しの聞き違いの疑い）",
  ambiguous_amount: "金額の言い方が曖昧",
  no_active_hand: "ハンドが始まっていなかった",
  low_conf_control_held: "自信の低い制御語を保留した",
  winner_seat_unresolved: "勝者の席が分からなかった",
  handler_error: "処理中のエラー",
  amount_only: "数字だけ（ベットかレイズかは場面から）",
  fuzzy_keyword: "音の近さで読んだ",
  second_ear: "Whisper が読めず、第 2 の耳で読んだ",
  sentence_after_chatter: "雑談の文のあとの文だけで読んだ",
  phonetic_amount: "意味のない語を、額の音に近いので額と読んだ",
  amount_restated: "直前の賭けの額の言い直しとして額を直した",
  legal_amount: "聞いた額がその場面で使えないので、音の近い使える額にした",
};

export function reasonLabels(reason?: string): string[] {
  if (!reason) return [];
  return reason
    .split("+")
    .filter((code) => code.length > 0)
    .map((code) => {
      const known = REASON_LABELS[code];
      if (known) return known;
      const capped = /^actor_conflict_capped\(sensed=(\d+)\)$/.exec(code);
      if (capped) return `言った席${capped[1]}は手番から遠いので手番の席にした`;
      const illegal = /^(\w+)_illegal_to_(call|fold)$/.exec(code);
      if (illegal) return `${actionLabel(illegal[1])}できない場面 → ${actionLabel(illegal[2])}にした`;
      const remap = /^(bet|raise)_to_(bet|raise)$/.exec(code);
      if (remap) return `${actionLabel(remap[1])}を${actionLabel(remap[2])}として記録した`;
      return code;
    });
}

/**
 * 音声テスト用: そのアクションが「何と聞こえて」記録されたか (ADR-0060)。
 * heard = 発話の書き起こし (無ければ null)。notes = 補正の内容と理由。
 */
export function heardDetails(a: ReplayAction): { heard: string | null; notes: string[] } {
  const notes: string[] = [];
  let reason = a.reason;
  if (a.corrected_from && a.corrected_from !== a.action) {
    notes.push(`聞き取り ${actionLabel(a.corrected_from)} → ${actionLabel(a.action)}`);
    // bet↔raise の付け替えは上の 1 行で分かるので理由からは省く
    reason = reason
      ?.split("+")
      .filter((code) => !/^(bet|raise)_to_(bet|raise)$/.test(code))
      .join("+");
  }
  notes.push(...reasonLabels(reason));
  return { heard: a.raw_text ? a.raw_text : null, notes };
}

/** 金額の桁区切り表示 (チップ額。円ではないので ¥ は付けない)。 */
export function formatChips(n: number): string {
  return n.toLocaleString("ja-JP");
}

/** 収支の符号付き表示 (+1,200 / -800 / ±0)。 */
export function formatSigned(n: number): string {
  if (n > 0) return `+${formatChips(n)}`;
  if (n < 0) return `-${formatChips(Math.abs(n))}`;
  return "±0";
}
