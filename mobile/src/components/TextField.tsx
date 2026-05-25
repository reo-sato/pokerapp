import React from 'react';
import { StyleSheet, Text, TextInput, View } from 'react-native';
import { theme } from '../theme/theme';

interface TextFieldProps {
  label: string;
  value: string;
  onChangeText: (text: string) => void;
  placeholder?: string;
  /** Set when the field has a validation error (drives border + message). */
  errorText?: string;
  autoFocus?: boolean;
  onSubmitEditing?: () => void;
}

export function TextField({
  label,
  value,
  onChangeText,
  placeholder,
  errorText,
  autoFocus,
  onSubmitEditing,
}: TextFieldProps) {
  return (
    <View style={styles.container}>
      <Text style={styles.label}>{label}</Text>
      <TextInput
        style={[styles.input, !!errorText && styles.inputError]}
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={theme.colors.textMuted}
        autoFocus={autoFocus}
        autoCorrect={false}
        returnKeyType="done"
        onSubmitEditing={onSubmitEditing}
      />
      {!!errorText && (
        <Text style={styles.errorText} accessibilityLiveRegion="polite">
          {errorText}
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { marginBottom: theme.spacing(2) },
  label: {
    color: theme.colors.textMuted,
    fontSize: theme.fontSize.sm,
    marginBottom: theme.spacing(0.5),
  },
  input: {
    backgroundColor: theme.colors.inputBackground,
    borderWidth: 1,
    borderColor: theme.colors.border,
    borderRadius: theme.radius,
    paddingVertical: theme.spacing(1.5),
    paddingHorizontal: theme.spacing(1.5),
    color: theme.colors.text,
    fontSize: theme.fontSize.md,
  },
  inputError: { borderColor: theme.colors.danger },
  errorText: {
    color: theme.colors.danger,
    fontSize: theme.fontSize.sm,
    marginTop: theme.spacing(0.5),
  },
});
