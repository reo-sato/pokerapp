import React from "react";
import { FlatList, Pressable, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorView, Loading, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  onSelect: (player: Player) => void;
}

/** 一覧から「自分」を選ぶ（M1/M2 のプライバシーモデル = name-pick, ISSUE-0019）。 */
export function PlayerSelectScreen({ repository, onSelect }: Props): React.JSX.Element {
  const { data, loading, errorCode, errorMessage } = useAsync(
    () => repository.listPlayers(), [repository],
  );

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>Poker Hand Viewer</Text>
      <Text style={styles.subtitle}>あなたの名前を選んでください</Text>
      {loading ? (
        <Loading />
      ) : errorCode ? (
        <ErrorView code={errorCode} message={errorMessage} />
      ) : (
        <FlatList
          data={data ?? []}
          keyExtractor={(p) => p.player_id}
          ListEmptyComponent={<Text style={styles.empty}>player が未登録です。</Text>}
          renderItem={({ item }) => (
            <Pressable style={styles.card} onPress={() => onSelect(item)}>
              <Text style={styles.cardTitle}>{item.display_name}</Text>
            </Pressable>
          )}
        />
      )}
    </View>
  );
}
