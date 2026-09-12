import React from "react";
import { FlatList, Pressable, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player, PlayerSessionSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, ErrorView, Loading, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  player: Player;
  onSelect: (session: PlayerSessionSummary) => void;
  onBack: () => void;
}

export function MySessionsScreen({ repository, player, onSelect, onBack }: Props): React.JSX.Element {
  const { data, loading, errorCode, errorMessage } = useAsync(
    () => repository.listPlayerSessions(player.player_id),
    [repository, player.player_id],
  );

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="player 選択" />
      <Text style={styles.title}>{player.display_name} のセッション</Text>
      <Text style={styles.subtitle}>参加したセッションの一覧</Text>
      {loading ? (
        <Loading />
      ) : errorCode ? (
        <ErrorView code={errorCode} message={errorMessage} />
      ) : (
        <FlatList
          data={data ?? []}
          keyExtractor={(s) => s.session_id}
          ListEmptyComponent={
            <Text style={styles.empty}>まだ記録されたセッションがありません。</Text>
          }
          renderItem={({ item }) => (
            <Pressable style={styles.card} onPress={() => onSelect(item)}>
              <Text style={styles.cardTitle}>{item.label ?? item.started_at}</Text>
              <Text style={styles.cardMeta}>
                {item.started_at}
                {item.blinds?.sb != null ? ` ・ blinds ${item.blinds.sb}/${item.blinds.bb}` : ""}
                {` ・ ${item.hands_played} hands`}
                {item.status === "open" ? " ・ 進行中" : ""}
              </Text>
            </Pressable>
          )}
        />
      )}
    </View>
  );
}
