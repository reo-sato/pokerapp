import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Player } from '../models/player';
import { theme } from '../theme/theme';

interface PlayerListItemProps {
  player: Player;
  /** Tapping a player navigates to rename (UI goal #1). */
  onPress: (player: Player) => void;
}

export function PlayerListItem({ player, onPress }: PlayerListItemProps) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Rename ${player.display_name}`}
      onPress={() => onPress(player)}
      style={({ pressed }) => [styles.row, pressed && styles.pressed]}
    >
      <View style={styles.textBlock}>
        <Text style={styles.name}>{player.display_name}</Text>
        <Text style={styles.id} numberOfLines={1}>
          {player.player_id}
        </Text>
      </View>
      <Text style={styles.chevron}>›</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: theme.colors.surface,
    borderRadius: theme.radius,
    padding: theme.spacing(1.5),
    marginBottom: theme.spacing(1),
  },
  pressed: { opacity: 0.7 },
  textBlock: { flex: 1 },
  name: { color: theme.colors.text, fontSize: theme.fontSize.md, fontWeight: '600' },
  id: { color: theme.colors.textMuted, fontSize: theme.fontSize.sm, marginTop: 2 },
  chevron: { color: theme.colors.textMuted, fontSize: theme.fontSize.lg },
});
