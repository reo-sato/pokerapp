import React, { useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { StaffSession } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, Button, colors, ErrorView, Field, Loading, styles } from "./common";

/** session（卓）一覧 + 作成 / close。選ぶと会計・注文・座席を扱う TableView に遷移する。 */
export function SessionListScreen(props: {
  repository: StaffRepository;
  onSelect: (session: StaffSession) => void;
  onLogout: () => void;
}): React.JSX.Element {
  const { repository, onSelect, onLogout } = props;
  const state = useAsync(() => repository.listSessions(), [repository]);
  const [label, setLabel] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const onCreate = async (): Promise<void> => {
    try {
      const s = await repository.createSession(label.trim() || undefined);
      setLabel("");
      state.reload();
      setMsg({ text: `セッションを作成しました: ${s.label ?? s.session_id.slice(0, 8)}`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onClose = async (sessionId: string): Promise<void> => {
    try {
      await repository.closeSession(sessionId);
      state.reload();
      setMsg({ text: "セッションを close しました。", ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  return (
    <View style={styles.screen}>
      <View style={[styles.row, { justifyContent: "space-between" }]}>
        <Text style={styles.title}>セッション一覧</Text>
        <Pressable onPress={onLogout}>
          <Text style={styles.back}>ログアウト</Text>
        </Pressable>
      </View>
      <Text style={styles.subtitle}>卓を選んで会計・注文・座席を操作します。</Text>

      <View style={[styles.card]}>
        <View style={styles.row}>
          <Field label="新規セッション名" value={label} onChangeText={setLabel} placeholder="例: 土曜ナイト #5" />
          <View style={{ width: 8 }} />
          <Button label="作成" onPress={onCreate} style={{ alignSelf: "flex-end" }} />
        </View>
        {msg ? (
          <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg }]}>{msg.text}</Text>
        ) : null}
      </View>

      {state.loading ? (
        <Loading />
      ) : state.errorCode ? (
        <ErrorView code={state.errorCode} message={state.errorMessage} onRetry={state.reload} />
      ) : !state.data || state.data.length === 0 ? (
        <Text style={styles.empty}>セッションがありません。上で作成してください。</Text>
      ) : (
        <ScrollView>
          {state.data.map((s) => (
            <View key={s.session_id} style={styles.card}>
              <Pressable onPress={() => onSelect(s)}>
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
              <View style={[styles.row, { marginTop: 8 }]}>
                <Button label="開く" onPress={() => onSelect(s)} />
                {s.status === "open" ? (
                  <>
                    <View style={{ width: 8 }} />
                    <Button label="close" kind="ghost" onPress={() => onClose(s.session_id)} />
                  </>
                ) : null}
              </View>
            </View>
          ))}
        </ScrollView>
      )}
      <View style={{ height: 8 }} />
      <BackLink onPress={state.reload} label="再読込" />
    </View>
  );
}
