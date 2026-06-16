import React from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { StaffSession } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, colors, ErrorView, Loading, styles } from "./common";

/** session（卓）一覧。選ぶと会計/注文を扱う TableView に遷移する。 */
export function SessionListScreen(props: {
  repository: StaffRepository;
  onSelect: (session: StaffSession) => void;
  onLogout: () => void;
}): React.JSX.Element {
  const { repository, onSelect, onLogout } = props;
  const state = useAsync(() => repository.listSessions(), [repository]);

  return (
    <View style={styles.screen}>
      <View style={[styles.row, { justifyContent: "space-between" }]}>
        <Text style={styles.title}>セッション一覧</Text>
        <Pressable onPress={onLogout}>
          <Text style={styles.back}>ログアウト</Text>
        </Pressable>
      </View>
      <Text style={styles.subtitle}>卓を選んで会計・注文を操作します。</Text>

      {state.loading ? (
        <Loading />
      ) : state.errorCode ? (
        <ErrorView code={state.errorCode} message={state.errorMessage} onRetry={state.reload} />
      ) : !state.data || state.data.length === 0 ? (
        <Text style={styles.empty}>セッションがありません。</Text>
      ) : (
        <ScrollView>
          {state.data.map((s) => (
            <Pressable key={s.session_id} style={styles.card} onPress={() => onSelect(s)}>
              <View style={[styles.row, { justifyContent: "space-between" }]}>
                <Text style={styles.cardTitle}>{s.label ?? s.session_id.slice(0, 8)}</Text>
                <Text
                  style={{
                    color: s.status === "open" ? colors.ok : colors.muted,
                    fontWeight: "600",
                    fontSize: 13,
                  }}
                >
                  {s.status === "open" ? "● open" : "closed"}
                </Text>
              </View>
              <Text style={styles.cardMeta}>
                {s.started_at.replace("T", " ")}
                {s.blinds ? `　SB ${s.blinds.sb ?? "?"} / BB ${s.blinds.bb ?? "?"}` : ""}
              </Text>
            </Pressable>
          ))}
        </ScrollView>
      )}
      <View style={{ height: 8 }} />
      <BackLink onPress={state.reload} label="再読込" />
    </View>
  );
}
