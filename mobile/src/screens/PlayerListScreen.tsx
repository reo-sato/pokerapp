import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { NativeStackScreenProps } from '@react-navigation/native-stack';
import { useFocusEffect } from '@react-navigation/native';
import { RootStackParamList } from '../navigation/types';
import { Player } from '../models/player';
import { usePlayerRepository } from '../context/RepositoryContext';
import { PlayerListItem } from '../components/PlayerListItem';
import { PrimaryButton } from '../components/PrimaryButton';
import { theme } from '../theme/theme';

type Props = NativeStackScreenProps<RootStackParamList, 'PlayerList'>;

export function PlayerListScreen({ navigation }: Props) {
  const repo = usePlayerRepository();
  const [players, setPlayers] = useState<Player[] | null>(null);

  const load = useCallback(() => {
    let active = true;
    repo.listPlayers().then((list) => {
      if (active) {
        setPlayers(list);
      }
    });
    return () => {
      active = false;
    };
  }, [repo]);

  // Reload whenever the screen regains focus (e.g. returning from add/rename).
  useFocusEffect(load);

  return (
    <View style={styles.container}>
      {players === null ? (
        <ActivityIndicator color={theme.colors.primary} style={styles.loader} />
      ) : (
        <FlatList
          data={players}
          keyExtractor={(item) => item.player_id}
          contentContainerStyle={styles.listContent}
          renderItem={({ item }) => (
            <PlayerListItem
              player={item}
              onPress={(p) =>
                navigation.navigate('RenamePlayer', {
                  playerId: p.player_id,
                  currentDisplayName: p.display_name,
                })
              }
            />
          )}
          ListEmptyComponent={
            <View style={styles.empty}>
              <Text style={styles.emptyTitle}>まだ player がいません</Text>
              <Text style={styles.emptyBody}>
                右下の「+ Player を追加」から最初の player を作成してください。
              </Text>
            </View>
          }
        />
      )}
      <View style={styles.footer}>
        <PrimaryButton
          label="+ Player を追加"
          onPress={() => navigation.navigate('AddPlayer')}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: theme.colors.background },
  loader: { marginTop: theme.spacing(4) },
  listContent: { padding: theme.spacing(2), flexGrow: 1 },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: theme.spacing(3) },
  emptyTitle: {
    color: theme.colors.text,
    fontSize: theme.fontSize.lg,
    fontWeight: '600',
    marginBottom: theme.spacing(1),
  },
  emptyBody: { color: theme.colors.textMuted, fontSize: theme.fontSize.md, textAlign: 'center' },
  footer: {
    padding: theme.spacing(2),
    borderTopWidth: 1,
    borderTopColor: theme.colors.border,
    backgroundColor: theme.colors.background,
  },
});
