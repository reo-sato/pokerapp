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
  actions: ReplayAction[];
  review_required?: boolean;
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
  potTotal: number | null;
  pots: ReplayPot[];
}

/** schema 上のストリート順。テーブル UI が 4 列を固定で描くために公開する。 */
export const ALL_STREETS = ["preflop", "flop", "turn", "river"] as const;
const STREETS = ALL_STREETS;

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
  const actions = hand.actions ?? [];
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
    potTotal: hand.pot_total ?? null,
    pots: hand.pots ?? [],
  };
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

/**
 * 狭い列に収めるための短縮アクション表記（ADR-0051）。
 *
 * 日本語ラベル（`actionLabel`）は 1 文字が広く、4 列レイアウトでは金額と並ぶと見切れる。
 * 列の中だけはポーカーの原語表記を使う（卓上で通じる語彙そのままで、かつ幅が半分以下）。
 * 未知の action は raw のまま返す（silent failure を作らない）。
 */
export const COMPACT_ACTION_LABELS: Record<string, string> = {
  fold: "Fold",
  check: "Check",
  call: "Call",
  bet: "Bet",
  raise: "Raise",
  allin: "All-in",
  all_in: "All-in",
  blind: "Blind",
};

export function compactActionLabel(action: string): string {
  return COMPACT_ACTION_LABELS[action] ?? action;
}

/** テーブル外周に置く 1 席の位置（親コンテナに対する中心の %）。 */
export interface SeatSlot {
  top: number;
  left: number;
}

const RING_RX = 35; // 横半径 (%)
const RING_RY = 38; // 縦半径 (%)

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}

/**
 * 席をテーブル外周（楕円）に等間隔で配置する（ADR-0051）。
 *
 * 先頭の席を**下中央**（見ている人の手前）に置き、そこから時計回り
 * （下 → 左 → 上 → 右）に並べる。返すのは席チップの**中心**位置なので、
 * 描画側はチップの半分だけ負の margin でずらす。
 *
 * ボタン位置はデータに無いため（ADR-0044 D3 で推定は scope 外）、並びは
 * **席番号順**であってポジション順ではない。
 */
export function seatRingLayout(count: number): SeatSlot[] {
  if (!Number.isFinite(count) || count <= 0) return [];
  const slots: SeatSlot[] = [];
  for (let i = 0; i < count; i += 1) {
    const rad = ((90 + (360 / count) * i) * Math.PI) / 180;
    slots.push({
      top: round1(50 + RING_RY * Math.sin(rad)),
      left: round1(50 + RING_RX * Math.cos(rad)),
    });
  }
  return slots;
}

/**
 * プリフロップで fold した席（ADR-0051 追記）。
 *
 * そのハンドに実質関与していない席を UI で沈めるための判定。ポストフロップの fold は
 * 含めない（フロップまで参加した席は通常表示）。
 */
export function preflopFoldedSeats(actions: ReplayAction[]): number[] {
  const seats = new Set<number>();
  for (const a of actions ?? []) {
    if (a.street === "preflop" && a.action === "fold") seats.add(a.seat);
  }
  return [...seats].sort((x, y) => x - y);
}

/**
 * 列に描くアクション（ADR-0051 追記）。プリフロップの fold は出さない。
 *
 * 大半の席が fold するプリフロップ列で実質的な攻防が埋もれるため。降りた席はテーブル図側で
 * 減光して示す。**描画専用のフィルタ**で、ポット計算（`buildReplayModel`）も
 * テキスト書き出し（`handReplayText.ts`）も全アクションを見たままなので数値は変わらない。
 */
export function visibleColumnActions(street: string, actions: ReplayAction[]): ReplayAction[] {
  const all = actions ?? [];
  if (street !== "preflop") return all;
  return all.filter((a) => a.action !== "fold");
}

/** 狭い列に収めるための短縮金額表記 (600 / 1.5k / 12.2k / 120k)。 */
export function formatChipsCompact(n: number): string {
  if (!Number.isFinite(n)) return "0";
  if (Math.abs(n) < 1000) return formatChips(n);
  const k = n / 1000;
  const text = k.toFixed(Math.abs(k) < 100 ? 1 : 0).replace(/\.0$/, "");
  return `${text}k`;
}

/** 収支の短縮表記 (+8.2k / -600 / ±0)。 */
export function formatSignedCompact(n: number): string {
  if (n > 0) return `+${formatChipsCompact(n)}`;
  if (n < 0) return `-${formatChipsCompact(Math.abs(n))}`;
  return "±0";
}
