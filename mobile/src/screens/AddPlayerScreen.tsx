import React, { useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { NativeStackScreenProps } from '@react-navigation/native-stack';
import { RootStackParamList } from '../navigation/types';
import { usePlayerRepository } from '../context/RepositoryContext';
import { isRepositoryError } from '../repositories/errors';
import { TextField } from '../components/TextField';
import { PrimaryButton } from '../components/PrimaryButton';
import { FeedbackBanner, Feedback } from '../components/FeedbackBanner';
import { theme } from '../theme/theme';

type Props = NativeStackScreenProps<RootStackParamList, 'AddPlayer'>;

// display_name errors render under the field; anything else is a banner.
const FIELD_ERROR_CODES = ['empty_display_name', 'duplicate_display_name'];

export function AddPlayerScreen({ navigation }: Props) {
  const repo = usePlayerRepository();
  const [displayName, setDisplayName] = useState('');
  const [fieldError, setFieldError] = useState<string | undefined>();
  const [banner, setBanner] = useState<Feedback | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleAdd() {
    setFieldError(undefined);
    setBanner(null);
    setSubmitting(true);
    try {
      // No client-side validation: the repository (core contract) decides.
      await repo.createPlayer(displayName);
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
      <TextField
        label="display_name"
        value={displayName}
        onChangeText={setDisplayName}
        placeholder="例: Alice"
        errorText={fieldError}
        autoFocus
        onSubmitEditing={handleAdd}
      />
      <PrimaryButton label="Player を作成" onPress={handleAdd} loading={submitting} />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: theme.colors.background, padding: theme.spacing(2) },
});
