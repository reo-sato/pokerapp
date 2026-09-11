/**
 * ハンドリプレイ view (ADR-0051) — GGPoker のハンドログ風「1 画面」表示。
 *
 * **正本は `shared/hand_replay/` — コピー先 (mobile/staff の src/shared/hand_replay/) を
 * 直接編集しないこと。** 編集後は `python scripts/sync_shared_ui.py` で再配布する。
 *
 * 画面を上下に二分する:
 *   上半分 = テーブル図（席を外周にリング配置 + 中央にボードとポット。**最終状態で固定**）
 *   下半分 = プリフロップ / フロップ / ターン / リバーの **4 列**にアクションを並べる
 *
 * スマホ縦 1 画面にスクロールなしで収めるため、自身は ScrollView を持たず `flex: 1` で
 * 親の高さを埋める（埋め込み側が高さを与える）。列が溢れた極端なハンドだけ、列の中だけが
 * スクロールする（画面自体はスクロールしない）。
 *
 * mobile / staff 両アプリから使うため、各アプリの common.tsx / types.ts に依存しない
 * 自己完結コンポーネント。ロジックは handReplayModel.ts の純関数に置き、ここは描画のみ。
 */
import React from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";

import {
  actionColor,
  ALL_STREETS,
  buildReplayModel,
  compactActionLabel,
  formatChips,
  formatChipsCompact,
  formatSignedCompact,
  parseCard,
  preflopFoldedSeats,
  seatRingLayout,
  STREET_LABELS,
  SUIT_COLORS,
  SUIT_SYMBOLS,
  type ReplayAction,
  type ReplayHand,
  type ReplayPlayer,
  type StreetSection,
  visibleColumnActions,
} from "./handReplayModel";

const c = {
  text: "#f2f5f7",
  muted: "#8a949e",
  card: "#1b2026",
  cardAlt: "#222a31",
  border: "#2c343c",
  pos: "#7fd48a",
  neg: "#f08080",
  warn: "#ffb74d",
  accent: "#5ab0f0",
  cardFace: "#f4f1e8",
  // テーブルのフェルト。ダーク基調から浮かない程度に彩度を落とした深緑。
  felt: "#14332a",
  feltEdge: "#2b5a49",
  seatFill: "#151a1f",
  // 本人の強調。ダーク基調になじむ琥珀（彩度を上げるとホールカードの可読性が落ちる）。
  selfFill: "#3a2e12",
  selfEdge: "#c9a227",
  selfRow: "#2b2415",
};

/** プリフロップで降りた席の減光率（ADR-0051 追記）。 */
const FOLDED_OPACITY = 0.4;

const SEAT_W = 78;
const SEAT_H = 58;

/** 1 枚のカードチップ ("As" 等)。不正形式は raw 文字列で灰色表示。 */
export function CardChip(props: { card: string; size?: "xs" | "sm" | "md" }): React.JSX.Element {
  const size = props.size ?? "md";
  const box = [s.cardChip, size === "sm" && s.cardChipSm, size === "xs" && s.cardChipXs];
  const label = [s.cardChipText, size === "sm" && s.cardChipTextSm, size === "xs" && s.cardChipTextXs];
  const parsed = parseCard(props.card);
  if (!parsed) {
    return (
      <View style={[...box, { backgroundColor: c.cardAlt }]}>
        <Text style={[...label, { color: c.muted }]} numberOfLines={1}>
          {props.card}
        </Text>
      </View>
    );
  }
  return (
    <View style={box}>
      <Text style={[...label, { color: SUIT_COLORS[parsed.suit] }]} numberOfLines={1}>
        {parsed.rank}
        {SUIT_SYMBOLS[parsed.suit]}
      </Text>
    </View>
  );
}

/** 不明ホールカード (裏向き 2 枚)。 */
function HiddenCards(): React.JSX.Element {
  return (
    <View style={s.cardRow}>
      {[0, 1].map((i) => (
        <View key={i} style={[s.cardChip, s.cardChipXs, s.cardBack]}>
          <Text style={[s.cardChipTextXs, { color: c.muted }]}>?</Text>
        </View>
      ))}
    </View>
  );
}

/**
 * テーブル外周に置く 1 席。
 *
 * - 勝者は縁を緑にして 🏆 と緑の名前を添える
 * - **本人**（`isSelf`）は地を琥珀に。勝者でもあるときは地は琥珀のまま縁だけ勝者の緑を優先する
 * - **プリフロップで降りた席**（`dimmed`）はチップごと減光してハンドから沈める
 */
function SeatChip(props: {
  player: ReplayPlayer;
  isWinner: boolean;
  isSelf: boolean;
  dimmed: boolean;
  top: number;
  left: number;
}): React.JSX.Element {
  const { player, isWinner, isSelf, dimmed } = props;
  const result = player.result;
  return (
    <View
      style={[
        s.seat,
        isSelf && s.seatSelf,
        isWinner && s.seatWinner,
        dimmed && { opacity: FOLDED_OPACITY },
        { top: `${props.top}%`, left: `${props.left}%` },
      ]}
    >
      <Text style={[s.seatName, isWinner && { color: c.pos }]} numberOfLines={1}>
        {isWinner ? "🏆" : ""}
        {player.seat} {player.name}
      </Text>
      {player.hole_cards?.length ? (
        <View style={s.cardRow}>
          {player.hole_cards.map((card, i) => (
            <CardChip key={i} card={card} size="xs" />
          ))}
        </View>
      ) : (
        <HiddenCards />
      )}
      {result != null ? (
        <Text style={[s.seatResult, { color: result >= 0 ? c.pos : c.neg }]} numberOfLines={1}>
          {formatSignedCompact(result)}
        </Text>
      ) : player.stack_end != null ? (
        <Text style={s.seatResult} numberOfLines={1}>
          {formatChipsCompact(player.stack_end)}
        </Text>
      ) : null}
    </View>
  );
}

/**
 * 4 列の 1 行。列幅が狭い (約 88px) ので **2 段**に分ける:
 *   1 段目 = 席番号 + プレーヤー名（誰のアクションか）
 *   2 段目 = アクション名（**種別ごとの色** = ADR-0051 追記 D8）+ 短縮金額
 * 要確認 / 訂正済は左の色ストライプ + 記号で幅を使わずに示す。
 */
function ActionLine(props: {
  action: ReplayAction;
  isSelf: boolean;
  name: string;
}): React.JSX.Element {
  const a = props.action;
  const stripe = a.needs_review ? c.warn : a.corrected ? c.accent : "transparent";
  return (
    <View
      style={[s.actionLine, props.isSelf && s.actionLineSelf, { borderLeftColor: stripe }]}
    >
      <Text style={[s.actionWho, props.isSelf && { color: c.selfEdge }]} numberOfLines={1}>
        {a.seat} {props.name}
      </Text>
      <View style={s.actionMain}>
        <Text style={[s.actionLabel, { color: actionColor(a.action) }]} numberOfLines={1}>
          {compactActionLabel(a.action)}
        </Text>
        {a.amount ? (
          <Text style={s.actionAmount} numberOfLines={1}>
            {formatChipsCompact(a.amount)}
          </Text>
        ) : null}
        {a.needs_review ? <Text style={[s.mark, { color: c.warn }]}>!</Text> : null}
        {a.corrected ? <Text style={[s.mark, { color: c.accent }]}>✎</Text> : null}
      </View>
    </View>
  );
}

/** ストリート 1 列。そのストリートで**新しく開いた**ボードだけを見出しに出す。 */
function StreetColumn(props: {
  street: string;
  section?: StreetSection;
  newCards: string[];
  selfSeat?: number;
  nameOf: (action: ReplayAction) => string;
}): React.JSX.Element {
  const { section, newCards } = props;
  // プリフロップの fold は出さない（降りた席はテーブル図側で減光して示す）。
  const actions = visibleColumnActions(props.street, section?.actions ?? []);
  return (
    <View style={s.column}>
      <View style={s.columnHead}>
        <Text style={s.columnTitle} numberOfLines={1}>
          {STREET_LABELS[props.street] ?? props.street}
        </Text>
        <View style={s.columnCards}>
          {newCards.map((card, i) => (
            <CardChip key={i} card={card} size="xs" />
          ))}
        </View>
        <Text style={s.columnPot} numberOfLines={1}>
          {section ? `pot ${formatChipsCompact(section.potEnd)}` : "—"}
        </Text>
      </View>
      <ScrollView
        style={s.columnBody}
        contentContainerStyle={s.columnBodyContent}
        showsVerticalScrollIndicator={false}
      >
        {actions.length === 0 ? (
          <Text style={s.columnEmpty}>—</Text>
        ) : (
          actions.map((a, i) => (
            <ActionLine
              key={i}
              action={a}
              isSelf={a.seat === props.selfSeat}
              name={props.nameOf(a)}
            />
          ))
        )}
      </ScrollView>
    </View>
  );
}

/** street ごとに「新しく開いたボード」を切り出す (flop=3 枚 / turn=1 枚 / river=1 枚)。 */
function newCardsFor(street: string, board: string[]): string[] {
  if (street === "flop") return board.slice(0, 3);
  if (street === "turn") return board.slice(3, 4);
  if (street === "river") return board.slice(4, 5);
  return [];
}

/**
 * ハンドリプレイ本体。`hand` は viewer API / staff API の HandSummary dict をそのまま渡せる
 * (訂正オーバーレイ適用済みビューを渡すこと — ADR-0036)。
 */
export function HandReplay(props: {
  hand: ReplayHand;
  /** 見ている本人の席（mobile のみ。staff は「本人」が居ないので未指定 = 強調なし）。 */
  selfSeat?: number;
}): React.JSX.Element {
  const model = buildReplayModel(props.hand);
  const board = props.hand.board ?? [];
  const foldedPreflop = new Set(preflopFoldedSeats(props.hand.actions ?? []));
  const slots = seatRingLayout(model.seats.length);
  const sectionByStreet = new Map(model.streets.map((st) => [st.street, st]));
  // アクションに player_name が無い記録もあるので、席→参加者名で補う（最後は「席N」）。
  const nameBySeat = new Map(model.seats.map((p) => [p.seat, p.name]));
  const nameOf = (a: ReplayAction): string =>
    a.player_name ?? nameBySeat.get(a.seat) ?? `席${a.seat}`;
  const winnerName =
    model.winnerSeat != null
      ? model.seats.find((p) => p.seat === model.winnerSeat)?.name
      : undefined;
  const sidePots = model.pots.length > 1 ? model.pots : [];

  return (
    <View style={s.root}>
      {/* 上半分: テーブル図（最終状態） */}
      <View style={s.table}>
        <View style={s.felt} />

        <View style={s.center} pointerEvents="none">
          {board.length > 0 ? (
            <View style={s.cardRow}>
              {board.map((card, i) => (
                <CardChip key={i} card={card} />
              ))}
            </View>
          ) : (
            <Text style={s.centerMuted}>ボード記録なし</Text>
          )}
          <Text style={s.pot}>
            {model.potTotal != null ? `ポット ${formatChips(model.potTotal)}` : "ポット —"}
          </Text>
          {sidePots.length > 0 ? (
            <Text style={s.centerMuted} numberOfLines={1}>
              {sidePots
                .map((pot, i) =>
                  i === 0
                    ? `メイン ${formatChipsCompact(pot.amount)}`
                    : `サイド${i} ${formatChipsCompact(pot.amount)}`,
                )
                .join(" ・ ")}
            </Text>
          ) : null}
          {model.winnerSeat != null ? (
            <Text style={s.winner} numberOfLines={1}>
              🏆 席{model.winnerSeat} {winnerName ?? ""}
            </Text>
          ) : null}
          {model.blinds?.sb != null ? (
            <Text style={s.blinds}>
              {formatChips(model.blinds.sb)}/{formatChips(model.blinds.bb ?? 0)}
            </Text>
          ) : null}
        </View>

        {model.seats.map((p, i) => (
          <SeatChip
            key={p.seat}
            player={p}
            isWinner={p.seat === model.winnerSeat}
            isSelf={p.seat === props.selfSeat}
            dimmed={foldedPreflop.has(p.seat)}
            top={slots[i]?.top ?? 50}
            left={slots[i]?.left ?? 50}
          />
        ))}
      </View>

      {/* 下半分: 4 ストリートを 4 列で */}
      <View style={s.streets}>
        {ALL_STREETS.map((street) => (
          <StreetColumn
            key={street}
            street={street}
            section={sectionByStreet.get(street)}
            newCards={newCardsFor(street, board)}
            selfSeat={props.selfSeat}
            nameOf={nameOf}
          />
        ))}
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  root: { flex: 1 },

  // ――― テーブル図 ―――
  table: { flex: 1, minHeight: 250, position: "relative" },
  felt: {
    position: "absolute",
    left: "15%",
    right: "15%",
    top: "12%",
    bottom: "12%",
    borderRadius: 999,
    backgroundColor: c.felt,
    borderWidth: 2,
    borderColor: c.feltEdge,
  },
  center: {
    position: "absolute",
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
  },
  centerMuted: { color: c.muted, fontSize: 11, marginTop: 2 },
  pot: { color: c.text, fontSize: 14, fontWeight: "700", marginTop: 6 },
  winner: { color: c.pos, fontSize: 12, fontWeight: "700", marginTop: 2 },
  blinds: { color: c.muted, fontSize: 10, marginTop: 2 },

  seat: {
    position: "absolute",
    width: SEAT_W,
    height: SEAT_H,
    marginLeft: -SEAT_W / 2,
    marginTop: -SEAT_H / 2,
    backgroundColor: c.seatFill,
    borderWidth: 1,
    borderColor: c.border,
    borderRadius: 8,
    paddingVertical: 3,
    alignItems: "center",
    justifyContent: "center",
  },
  seatWinner: { borderColor: c.pos },
  seatSelf: { backgroundColor: c.selfFill, borderColor: c.selfEdge },
  seatName: { color: c.text, fontSize: 11, fontWeight: "600", maxWidth: SEAT_W - 8 },
  seatResult: { color: c.muted, fontSize: 10, fontWeight: "700", marginTop: 1 },

  // ――― カード ―――
  cardRow: { flexDirection: "row", alignItems: "center" },
  cardChip: {
    backgroundColor: c.cardFace,
    borderRadius: 4,
    paddingHorizontal: 5,
    paddingVertical: 2,
    marginHorizontal: 1,
    minWidth: 28,
    alignItems: "center",
  },
  cardChipSm: { paddingHorizontal: 3, paddingVertical: 1, minWidth: 24 },
  cardChipXs: { paddingHorizontal: 2, paddingVertical: 0, minWidth: 21 },
  cardBack: { backgroundColor: c.cardAlt, borderWidth: 1, borderColor: c.border },
  cardChipText: { fontSize: 14, fontWeight: "700" },
  cardChipTextSm: { fontSize: 12, fontWeight: "700" },
  cardChipTextXs: { fontSize: 10, fontWeight: "700" },

  // ――― 4 列 ―――
  streets: { flex: 1, flexDirection: "row", marginTop: 6 },
  column: {
    flex: 1,
    backgroundColor: c.card,
    borderRadius: 8,
    marginHorizontal: 2,
    paddingHorizontal: 4,
    paddingTop: 5,
    paddingBottom: 3,
  },
  columnHead: {
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: c.border,
    paddingBottom: 4,
    marginBottom: 3,
  },
  columnTitle: { color: c.text, fontSize: 11, fontWeight: "700" },
  columnCards: { flexDirection: "row", alignItems: "center", minHeight: 14, marginTop: 3 },
  columnPot: { color: c.muted, fontSize: 10, marginTop: 2 },
  columnBody: { flex: 1 },
  columnBodyContent: { paddingBottom: 2 },
  columnEmpty: { color: c.border, fontSize: 11, marginTop: 2 },
  actionLine: {
    paddingVertical: 2,
    paddingLeft: 3,
    marginBottom: 2,
    borderLeftWidth: 2,
  },
  actionLineSelf: { backgroundColor: c.selfRow, borderRadius: 3 },
  actionMain: { flexDirection: "row", alignItems: "center" },
  actionWho: { color: c.muted, fontSize: 9 },
  actionLabel: { fontSize: 10, fontWeight: "700", flexShrink: 1 },
  actionAmount: { color: c.text, fontSize: 10, fontWeight: "700", marginLeft: 3 },
  mark: { fontSize: 10, fontWeight: "700", marginLeft: 2 },
});
