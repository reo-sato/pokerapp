import React from "react";
import { FlatList, Pressable, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { HandSummary, Player, PlayerSessionSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, ErrorView, Loading, formatResult, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  player: Player;
  session: PlayerSessionSummary;
  onSelect: (hand: HandSummary) => void;
  onBack: () => void;
  onOpenLedger: () => void;
}

/** hand.players から自分の行を探す。player_id は additive (E3 前は absent) なので name に fallback。 */
export function findOwnRow(hand: HandSummary, player: Player) {
  return (
    hand.players.find((p) => p.player_id === player.player_id) ??
    hand.players.find((p) => p.name === player.display_name)
  );
}

export function MyHandsScreen({
  repository, player, session, onSelect, onBack, onOpenLedger,
}: Props): React.JSX.Element {
  const { data, loading, errorCode, errorMessage } = useAsync(
    () => repository.listPlayerHands(player.player_id, session.session_id),
    [repository, player.player_id, session.session_id],
  );

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="セッション一覧" />
      <Text style={styles.title}>{session.label ?? session.started_at}</Text>
      <Text style={styles.subtitle}>{player.display_name} が参加したハンド</Text>
      <Pressable onPress={onOpenLedger}>
        <Text style={styles.back}>会計を見る（バイイン・注文） →</Text>
      </Pressable>
      {loading ? (
        <Loading />
      ) : errorCode ? (
        <ErrorView code={errorCode} message={errorMessage} />
      ) : (
        <FlatList
          data={data ?? []}
          keyExtractor={(h) => String(h.hand_id)}
          ListEmptyComponent={
            <Text style={styles.empty}>
              ハンドログがまだありません（記録が始まると表示されます）。
            </Text>
          }
          renderItem={({ item }) => {
            const own = findOwnRow(item, player);
            return (
              <Pressable style={styles.card} onPress={() => onSelect(item)}>
                <Text style={styles.cardTitle}>
                  Hand #{item.hand_id}
                  {own ? (
                    <Text style={own.result >= 0 ? styles.pos : styles.neg}>
                      {`  ${formatResult(own.result)}`}
                    </Text>
                  ) : null}
                </Text>
                <Text style={styles.cardMeta}>
                  {item.started_at}
                  {item.board?.length ? ` ・ board ${item.board.join(" ")}` : ""}
                  {item.pot_total != null ? ` ・ pot ${item.pot_total}` : ""}
                </Text>
              </Pressable>
            );
          }}
        />
      )}
    </View>
  );
}
