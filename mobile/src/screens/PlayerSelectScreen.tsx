import React from "react";
import { FlatList, Pressable, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorView, Loading, ReloadLink, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  onSelect: (player: Player) => void;
  /** L1 PIN ログイン (ADR-0027): 選んだ player の本人確認へ。 */
  onLogin: (player: Player) => void;
  /** L2 外部 IdP サインアップ (ADR-0031)。 */
  onSignup: () => void;
}

/**
 * 一覧から「自分」を選ぶ（既定 = name-pick, ISSUE-0019）。
 * 本人確認が要る会場向けに、各 player の「PIN」ログイン (L1) と
 * 「LINE / Google でサインアップ」(L2) も提供する（player_auth=off の会場では name-pick のまま）。
 */
export function PlayerSelectScreen({
  repository, onSelect, onLogin, onSignup,
}: Props): React.JSX.Element {
  const { data, loading, errorCode, errorMessage, reload } = useAsync(
    () => repository.listPlayers(), [repository],
  );

  return (
    <View style={styles.screen}>
      <View style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center" }}>
        <Text style={styles.title}>Poker Hand Viewer</Text>
        <ReloadLink onPress={reload} />
      </View>
      <Text style={styles.subtitle}>あなたの名前を選んでください</Text>
      {loading ? (
        <Loading />
      ) : errorCode ? (
        <ErrorView code={errorCode} message={errorMessage} onRetry={reload} />
      ) : (
        <FlatList
          data={data ?? []}
          keyExtractor={(p) => p.player_id}
          ListEmptyComponent={<Text style={styles.empty}>player が未登録です。</Text>}
          renderItem={({ item }) => (
            <View style={styles.card}>
              <Pressable onPress={() => onSelect(item)}>
                <Text style={styles.cardTitle}>{item.display_name}</Text>
              </Pressable>
              <Pressable onPress={() => onLogin(item)}>
                <Text style={[styles.cardMeta, { color: "#5ab0f0" }]}>🔒 PIN でログイン</Text>
              </Pressable>
            </View>
          )}
          ListFooterComponent={
            <Pressable onPress={onSignup}>
              <Text style={[styles.back, { marginTop: 16, textAlign: "center" }]}>
                LINE / Google でサインアップ
              </Text>
            </Pressable>
          }
        />
      )}
    </View>
  );
}
