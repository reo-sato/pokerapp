import React from "react";
import { ScrollView, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player, PlayerSessionSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, ErrorView, Loading, formatResult, styles } from "./common";
import { findOwnRow } from "./MyHandsScreen";

interface Props {
  repository: ViewerRepository;
  player: Player;
  session: PlayerSessionSummary;
  handId: number;
  onBack: () => void;
}

export function HandDetailScreen({
  repository, player, session, handId, onBack,
}: Props): React.JSX.Element {
  const { data: hand, loading, errorCode, errorMessage } = useAsync(
    () => repository.getHand(session.session_id, handId),
    [repository, session.session_id, handId],
  );

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="ハンド一覧" />
      <Text style={styles.title}>Hand #{handId}</Text>
      {loading ? (
        <Loading />
      ) : errorCode || !hand ? (
        <ErrorView code={errorCode} message={errorMessage} />
      ) : (
        <ScrollView>
          <Text style={styles.subtitle}>
            {hand.started_at}
            {hand.blinds?.sb != null ? ` ・ blinds ${hand.blinds.sb}/${hand.blinds.bb}` : ""}
          </Text>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>
              Board: {hand.board?.length ? hand.board.join(" ") : "—"}
            </Text>
            <Text style={styles.cardMeta}>
              pot {hand.pot_total ?? "—"}
              {hand.winner_seat != null ? ` ・ winner 席${hand.winner_seat}` : ""}
            </Text>
            {(() => {
              const own = findOwnRow(hand, player);
              if (!own) return null;
              return (
                <Text style={styles.cardMeta}>
                  自分: 席{own.seat}
                  {own.hole_cards?.length ? ` ・ ${own.hole_cards.join(" ")}` : ""}
                  {" ・ 収支 "}
                  <Text style={own.result >= 0 ? styles.pos : styles.neg}>
                    {formatResult(own.result)}
                  </Text>
                </Text>
              );
            })()}
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>プレイヤー</Text>
            {hand.players.map((p) => (
              <Text key={p.seat} style={styles.cardMeta}>
                席{p.seat} {p.name} stack {p.stack_start}→{p.stack_end}{" "}
                <Text style={p.result >= 0 ? styles.pos : styles.neg}>
                  {formatResult(p.result)}
                </Text>
              </Text>
            ))}
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>アクション</Text>
            {hand.actions.length === 0 ? (
              <Text style={styles.cardMeta}>記録なし</Text>
            ) : (
              hand.actions.map((a, i) => (
                <Text key={i} style={styles.cardMeta}>
                  [{a.street}] 席{a.seat} {a.player_name} {a.action}
                  {a.amount ? ` ${a.amount}` : ""} ・ pot {a.pot_after}
                  {a.needs_review ? " ・ 要確認" : ""}
                </Text>
              ))
            )}
          </View>
        </ScrollView>
      )}
    </View>
  );
}
