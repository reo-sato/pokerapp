import React from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

export const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#101418", paddingTop: 56, paddingHorizontal: 16 },
  title: { color: "#f2f5f7", fontSize: 22, fontWeight: "700", marginBottom: 4 },
  subtitle: { color: "#8a949e", fontSize: 13, marginBottom: 16 },
  card: {
    backgroundColor: "#1b2026", borderRadius: 10, padding: 14, marginBottom: 10,
  },
  cardTitle: { color: "#f2f5f7", fontSize: 16, fontWeight: "600" },
  cardMeta: { color: "#8a949e", fontSize: 12, marginTop: 4 },
  back: { color: "#5ab0f0", fontSize: 14, marginBottom: 12 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  error: { color: "#f08080", fontSize: 14, textAlign: "center" },
  empty: { color: "#8a949e", fontSize: 14, textAlign: "center", marginTop: 32 },
  pos: { color: "#7fd48a" },
  neg: { color: "#f08080" },
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
      <ActivityIndicator size="large" color="#5ab0f0" />
    </View>
  );
}

export function ErrorView(props: {
  code: string | null;
  message: string | null;
  onRetry?: () => void;
}): React.JSX.Element {
  // 分岐は code（error-shapes.md）。message は表示用。
  const text =
    props.code === "not_found"
      ? "データが見つかりませんでした。"
      : props.code === "network_error"
        ? "サーバーに接続できません。運営 PC の viewer API が起動しているか確認してください。"
        : (props.message ?? "エラーが発生しました。");
  return (
    <View style={styles.center}>
      <Text style={styles.error}>{text}</Text>
      {props.onRetry ? (
        <Pressable onPress={props.onRetry} style={{ marginTop: 16 }}>
          <Text style={styles.back}>↻ 再試行</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

/** 一覧/詳細の手動更新リンク（web export では RefreshControl が効かないため常設する）。 */
export function ReloadLink(props: { onPress: () => void }): React.JSX.Element {
  return (
    <Pressable onPress={props.onPress} accessibilityRole="button">
      <Text style={[styles.back, { marginBottom: 0 }]}>↻ 再読込</Text>
    </Pressable>
  );
}

export function formatResult(result: number): string {
  return result > 0 ? `+${result}` : `${result}`;
}
