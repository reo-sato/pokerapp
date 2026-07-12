/**
 * ハンドリプレイ view (ADR-0044) — GGPoker ハンドヒストリー風のストリート単位表示。
 *
 * **正本は `shared/hand_replay/` — コピー先 (mobile/staff の src/shared/hand_replay/) を
 * 直接編集しないこと。** 編集後は `python scripts/sync_shared_ui.py` で再配布する。
 *
 * mobile / staff 両アプリから使うため、各アプリの common.tsx / types.ts に依存しない
 * 自己完結コンポーネント (両アプリ共通のダークトーンに合わせた自前スタイル)。
 * ロジックは handReplayModel.ts の純関数に置き、ここは描画のみ。
 */
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import {
  actionLabel,
  buildReplayModel,
  formatChips,
  formatSigned,
  parseCard,
  SUIT_COLORS,
  SUIT_SYMBOLS,
  type ReplayAction,
  type ReplayHand,
  type ReplayPlayer,
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
};

/** 1 枚のカードチップ ("As" 等)。不正形式は raw 文字列で灰色表示。 */
export function CardChip(props: { card: string; size?: "sm" | "md" }): React.JSX.Element {
  const parsed = parseCard(props.card);
  const sm = props.size === "sm";
  if (!parsed) {
    return (
      <View style={[s.cardChip, sm && s.cardChipSm, { backgroundColor: c.cardAlt }]}>
        <Text style={[s.cardChipText, sm && s.cardChipTextSm, { color: c.muted }]}>
          {props.card}
        </Text>
      </View>
    );
  }
  const color = SUIT_COLORS[parsed.suit];
  return (
    <View style={[s.cardChip, sm && s.cardChipSm]}>
      <Text style={[s.cardChipText, sm && s.cardChipTextSm, { color }]}>
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
        <View key={i} style={[s.cardChip, s.cardChipSm, s.cardBack]}>
          <Text style={[s.cardChipTextSm, { color: c.muted }]}>?</Text>
        </View>
      ))}
    </View>
  );
}

function SeatRow(props: { player: ReplayPlayer; isWinner: boolean }): React.JSX.Element {
  const { player, isWinner } = props;
  const result = player.result;
  return (
    <View style={s.seatRow}>
      <Text style={s.seatNo}>席{player.seat}</Text>
      <Text style={s.seatName} numberOfLines={1}>
        {isWinner ? "🏆 " : ""}
        {player.name}
      </Text>
      {player.hole_cards?.length ? (
        <View style={s.cardRow}>
          {player.hole_cards.map((card, i) => (
            <CardChip key={i} card={card} size="sm" />
          ))}
        </View>
      ) : (
        <HiddenCards />
      )}
      <View style={s.seatRight}>
        {player.stack_start != null && player.stack_end != null ? (
          <Text style={s.metaText}>
            {formatChips(player.stack_start)} → {formatChips(player.stack_end)}
          </Text>
        ) : null}
        {result != null ? (
          <Text style={[s.resultText, { color: result >= 0 ? c.pos : c.neg }]}>
            {formatSigned(result)}
          </Text>
        ) : null}
      </View>
    </View>
  );
}

function ActionRow(props: {
  action: ReplayAction;
  resolveName: (seat: number) => string;
}): React.JSX.Element {
  const a = props.action;
  const name = a.player_name || props.resolveName(a.seat);
  return (
    <View style={s.actionRow}>
      <Text style={s.actionSeat}>席{a.seat}</Text>
      <Text style={s.actionText} numberOfLines={1}>
        <Text style={{ color: c.text }}>{name} </Text>
        <Text style={{ color: c.accent, fontWeight: "700" }}>{actionLabel(a.action)}</Text>
        {a.amount ? <Text style={{ color: c.text }}> {formatChips(a.amount)}</Text> : null}
      </Text>
      {a.needs_review ? <Text style={[s.badge, { color: c.warn }]}>要確認</Text> : null}
      {a.corrected ? <Text style={[s.badge, { color: c.accent }]}>訂正済</Text> : null}
      {a.pot_after != null ? (
        <Text style={s.metaText}>pot {formatChips(a.pot_after)}</Text>
      ) : null}
    </View>
  );
}

/**
 * ハンドリプレイ本体。`hand` は viewer API / staff API の HandSummary dict をそのまま渡せる
 * (訂正オーバーレイ適用済みビューを渡すこと — ADR-0036)。
 */
export function HandReplay(props: { hand: ReplayHand }): React.JSX.Element {
  const model = buildReplayModel(props.hand);
  const nameBySeat = new Map(model.seats.map((p) => [p.seat, p.name]));
  const resolveName = (seat: number): string => nameBySeat.get(seat) ?? `席${seat}`;
  const winner = model.winnerSeat != null ? nameBySeat.get(model.winnerSeat) : undefined;

  return (
    <View>
      {/* 参加者 (ホールカードは記録がある席は全員分表示 — ADR-0044 D3) */}
      <View style={s.section}>
        {model.blinds?.sb != null ? (
          <Text style={s.metaText}>
            ブラインド {formatChips(model.blinds.sb)}/{formatChips(model.blinds.bb ?? 0)}
          </Text>
        ) : null}
        {model.seats.map((p) => (
          <SeatRow key={p.seat} player={p} isWinner={p.seat === model.winnerSeat} />
        ))}
      </View>

      {/* ストリート単位のセクション */}
      {model.streets.map((st) => (
        <View key={st.street} style={s.section}>
          <View style={s.streetHeader}>
            <Text style={s.streetLabel}>{st.label}</Text>
            {st.board.length > 0 ? (
              <View style={s.cardRow}>
                {st.board.map((card, i) => (
                  <CardChip key={i} card={card} />
                ))}
              </View>
            ) : null}
            <Text style={[s.metaText, s.streetPot]}>ポット {formatChips(st.potStart)}</Text>
          </View>
          {st.actions.length === 0 ? (
            <Text style={s.metaText}>（アクションなし）</Text>
          ) : (
            st.actions.map((a, i) => <ActionRow key={i} action={a} resolveName={resolveName} />)
          )}
        </View>
      ))}

      {/* 結果 */}
      <View style={s.section}>
        <View style={s.streetHeader}>
          <Text style={s.streetLabel}>結果</Text>
          {model.potTotal != null ? (
            <Text style={[s.metaText, s.streetPot]}>ポット合計 {formatChips(model.potTotal)}</Text>
          ) : null}
        </View>
        {model.winnerSeat != null ? (
          <Text style={s.resultLine}>
            🏆 席{model.winnerSeat} {winner ?? ""}
          </Text>
        ) : (
          <Text style={s.metaText}>勝者記録なし</Text>
        )}
        {model.pots.map((pot, i) => (
          <Text key={i} style={s.metaText}>
            {i === 0 ? "メインポット" : `サイドポット${i}`} {formatChips(pot.amount)}
            {pot.eligible_seats?.length
              ? ` ・ 対象 ${pot.eligible_seats.map((n) => `席${n}`).join(" ")}`
              : ""}
          </Text>
        ))}
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  section: {
    backgroundColor: c.card,
    borderRadius: 10,
    padding: 12,
    marginBottom: 10,
  },
  streetHeader: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    marginBottom: 6,
  },
  streetLabel: { color: c.text, fontSize: 15, fontWeight: "700", marginRight: 10 },
  streetPot: { marginLeft: "auto" },
  cardRow: { flexDirection: "row", alignItems: "center" },
  cardChip: {
    backgroundColor: c.cardFace,
    borderRadius: 4,
    paddingHorizontal: 5,
    paddingVertical: 2,
    marginRight: 4,
    minWidth: 30,
    alignItems: "center",
  },
  cardChipSm: { paddingHorizontal: 3, paddingVertical: 1, minWidth: 26 },
  cardBack: { backgroundColor: c.cardAlt, borderWidth: 1, borderColor: c.border },
  cardChipText: { fontSize: 15, fontWeight: "700" },
  cardChipTextSm: { fontSize: 12, fontWeight: "700" },
  seatRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 4,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: c.border,
  },
  seatNo: { color: c.muted, fontSize: 12, width: 34 },
  seatName: { color: c.text, fontSize: 14, fontWeight: "600", flexShrink: 1, marginRight: 8 },
  seatRight: { marginLeft: "auto", alignItems: "flex-end" },
  actionRow: { flexDirection: "row", alignItems: "center", paddingVertical: 3 },
  actionSeat: { color: c.muted, fontSize: 12, width: 34 },
  actionText: { fontSize: 14, flexShrink: 1, marginRight: 8 },
  badge: { fontSize: 11, fontWeight: "700", marginRight: 8 },
  metaText: { color: c.muted, fontSize: 12 },
  resultText: { fontSize: 13, fontWeight: "700" },
  resultLine: { color: c.text, fontSize: 14, fontWeight: "600", marginBottom: 4 },
});
