import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { theme } from '../theme/theme';

export type FeedbackKind = 'success' | 'error';

export interface Feedback {
  kind: FeedbackKind;
  message: string;
}

export function FeedbackBanner({ feedback }: { feedback: Feedback | null }) {
  if (!feedback) {
    return null;
  }
  const isError = feedback.kind === 'error';
  return (
    <View
      style={[styles.banner, isError ? styles.error : styles.success]}
      accessibilityLiveRegion="polite"
    >
      <Text style={styles.text}>{feedback.message}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    paddingVertical: theme.spacing(1),
    paddingHorizontal: theme.spacing(1.5),
    borderRadius: theme.radius,
    marginBottom: theme.spacing(1.5),
    borderWidth: 1,
  },
  success: {
    backgroundColor: 'rgba(76, 208, 125, 0.12)',
    borderColor: theme.colors.success,
  },
  error: {
    backgroundColor: 'rgba(255, 107, 107, 0.12)',
    borderColor: theme.colors.danger,
  },
  text: { color: theme.colors.text, fontSize: theme.fontSize.sm },
});
