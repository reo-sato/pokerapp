import React from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

export const colors = {
  bg: "#101418",
  card: "#1b2026",
  cardAlt: "#222a31",
  text: "#f2f5f7",
  muted: "#8a949e",
  accent: "#5ab0f0",
  pos: "#7fd48a",
  neg: "#f08080",
  warn: "#ffb74d",
  ok: "#4caf50",
  inputBg: "#0c0f12",
  border: "#2c343c",
};

export const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, paddingTop: 48, paddingHorizontal: 16 },
  title: { color: colors.text, fontSize: 22, fontWeight: "700", marginBottom: 4 },
  subtitle: { color: colors.muted, fontSize: 13, marginBottom: 16 },
  card: { backgroundColor: colors.card, borderRadius: 10, padding: 14, marginBottom: 10 },
  cardTitle: { color: colors.text, fontSize: 16, fontWeight: "600" },
  cardMeta: { color: colors.muted, fontSize: 12, marginTop: 4 },
  row: { flexDirection: "row", alignItems: "center" },
  back: { color: colors.accent, fontSize: 14, marginBottom: 12 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  error: { color: colors.neg, fontSize: 14, textAlign: "center", paddingHorizontal: 24 },
  empty: { color: colors.muted, fontSize: 14, textAlign: "center", marginTop: 32 },
  sectionTitle: { color: colors.text, fontSize: 15, fontWeight: "600", marginTop: 8, marginBottom: 6 },
  label: { color: colors.muted, fontSize: 12, marginBottom: 4 },
  input: {
    backgroundColor: colors.inputBg, color: colors.text, borderRadius: 8,
    borderWidth: 1, borderColor: colors.border, paddingHorizontal: 10, paddingVertical: 8,
    fontSize: 15,
  },
  status: { fontSize: 13, marginTop: 8 },
});

export function BackLink(props: { onPress: () => void; label?: string }): React.JSX.Element {
  return (
    <Pressable onPress={props.onPress}>
      <Text style={styles.back}>{`← ${props.label ?? "戻る"}`}</Text>
    </Pressable>
  );
}

export function Loading(): React.JSX.Element {
  return (
    <View style={styles.center}>
      <ActivityIndicator size="large" color={colors.accent} />
    </View>
  );
}

export function ErrorView(props: {
  code: string | null;
  message: string | null;
  onRetry?: () => void;
}): React.JSX.Element {
  const text =
    props.code === "not_found"
      ? "データが見つかりませんでした。"
      : props.code === "unauthorized" || props.code === "staff_writes_disabled"
        ? "スタッフトークンが無効です。ログインし直してください。"
        : props.code === "orders_unavailable"
          ? "会計 write は --ledger（会計画面）起動中のみ可能です。"
          : props.code === "not_implemented"
            ? (props.message ?? "この機能の API は未実装です。")
            : props.code === "network_error"
              ? "サーバーに接続できません。運営 PC の viewer API 起動を確認してください。"
              : (props.message ?? "エラーが発生しました。");
  return (
    <View style={styles.center}>
      <Text style={styles.error}>{text}</Text>
      {props.onRetry ? (
        <Button label="再試行" onPress={props.onRetry} kind="ghost" style={{ marginTop: 16 }} />
      ) : null}
    </View>
  );
}

type ButtonKind = "primary" | "ghost" | "danger" | "neutral";

export function Button(props: {
  label: string;
  onPress: () => void;
  kind?: ButtonKind;
  disabled?: boolean;
  style?: object;
}): React.JSX.Element {
  const kind = props.kind ?? "primary";
  const bg =
    kind === "primary" ? colors.accent
      : kind === "danger" ? colors.neg
        : kind === "neutral" ? colors.cardAlt
          : "transparent";
  const fg = kind === "primary" || kind === "danger" ? "#0c0f12" : colors.text;
  return (
    <Pressable
      onPress={props.disabled ? undefined : props.onPress}
      style={[
        {
          backgroundColor: bg,
          borderRadius: 8,
          paddingVertical: 9,
          paddingHorizontal: 14,
          opacity: props.disabled ? 0.4 : 1,
          borderWidth: kind === "ghost" ? 1 : 0,
          borderColor: colors.border,
          alignItems: "center",
        },
        props.style,
      ]}
    >
      <Text style={{ color: fg, fontWeight: "600", fontSize: 14 }}>{props.label}</Text>
    </Pressable>
  );
}

export function Field(props: {
  label: string;
  value: string;
  onChangeText: (t: string) => void;
  placeholder?: string;
  keyboardType?: "default" | "number-pad";
  style?: object;
}): React.JSX.Element {
  return (
    <View style={[{ flex: 1 }, props.style]}>
      <Text style={styles.label}>{props.label}</Text>
      <TextInput
        style={styles.input}
        value={props.value}
        onChangeText={props.onChangeText}
        placeholder={props.placeholder}
        placeholderTextColor={colors.muted}
        keyboardType={props.keyboardType ?? "default"}
        autoCapitalize="none"
      />
    </View>
  );
}

export function Chip(props: {
  label: string;
  selected?: boolean;
  onPress: () => void;
}): React.JSX.Element {
  return (
    <Pressable
      onPress={props.onPress}
      style={{
        backgroundColor: props.selected ? colors.accent : colors.cardAlt,
        borderRadius: 16,
        paddingVertical: 6,
        paddingHorizontal: 12,
        marginRight: 8,
        marginBottom: 8,
      }}
    >
      <Text style={{ color: props.selected ? "#0c0f12" : colors.text, fontSize: 13, fontWeight: "600" }}>
        {props.label}
      </Text>
    </Pressable>
  );
}

export function yen(n: number): string {
  return `${n < 0 ? "-" : ""}¥${Math.abs(n).toLocaleString("ja-JP")}`;
}

export function paymentColor(status: string): string {
  return status === "paid" ? colors.ok : status === "partial" ? colors.warn : colors.neg;
}

export function paymentLabel(status: string): string {
  return status === "paid" ? "支払済み" : status === "partial" ? "一部支払い" : "未払い";
}
