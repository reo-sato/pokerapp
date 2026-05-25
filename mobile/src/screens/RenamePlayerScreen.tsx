import React, { useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { NativeStackScreenProps } from '@react-navigation/native-stack';
import { RootStackParamList } from '../navigation/types';
import { usePlayerRepository } from '../context/RepositoryContext';
import { isRepositoryError } from '../repositories/errors';
import { TextField } from '../components/TextField';
import { PrimaryButton } from '../components/PrimaryButton';
import { FeedbackBanner, Feedback } from '../components/FeedbackBanner';
import { theme } from '../theme/theme';

type Props = NativeStackScreenProps<RootStackParamList, 'RenamePlayer'>;

const FIELD_ERROR_CODES = ['empty_display_name', 'duplicate_display_name'];

export function RenamePlayerScreen({ navigation, route }: Props) {
  const repo = usePlayerRepository();
  const { playerId, currentDisplayName } = route.params;
  const [displayName, setDisplayName] = useState(currentDisplayName);
  const [fieldError, setFieldError] = useState<string | undefined>();
  const [banner, setBanner] = useState<Feedback | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleRename() {
    setFieldError(undefined);
    setBanner(null);
    setSubmitting(true);
    try {
      await repo.renamePlayer(playerId, displayName);
      navigation.navigate('PlayerList');
    } catch (err) {
      if (isRepositoryError(err) && FIELD_ERROR_CODES.includes(err.code)) {
        setFieldError(err.message);
      } else {
        const message = isRepositoryError(err) ? err.message : '予期しないエラーが発生しました。';
        setBanner({ kind: 'error', message });
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <View style={styles.container}>
      <FeedbackBanner feedback={banner} />
      <Text style={styles.current}>現在の display_name: {currentDisplayName}</Text>
      <TextField
        label="新しい display_name"
        value={displayName}
        onChangeText={setDisplayName}
        errorText={fieldError}
        autoFocus
        onSubmitEditing={handleRename}
      />
      <PrimaryButton label="リネームを保存" onPress={handleRename} loading={submitting} />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: theme.colors.background, padding: theme.spacing(2) },
  current: {
    color: theme.colors.textMuted,
    fontSize: theme.fontSize.sm,
    marginBottom: theme.spacing(2),
  },
});
