import React, { useState } from "react";
import { Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import { StaffApiError } from "../api/types";
import { Button, colors, Field, styles } from "./common";

/**
 * staff token 入力（端末ログイン, ADR-0037 §3）。token をセットして verifyToken で検証する。
 * mock では VALID_STAFF_TOKEN = "demo-staff-token"。
 */
export function LoginScreen(props: {
  repository: StaffRepository;
  onAuthed: () => void;
}): React.JSX.Element {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onLogin = async (): Promise<void> => {
    if (!token.trim()) {
      setError("スタッフトークンを入力してください。");
      return;
    }
    setBusy(true);
    setError(null);
    props.repository.setToken(token.trim());
    try {
      await props.repository.verifyToken();
      props.onAuthed();
    } catch (err: unknown) {
      props.repository.clearToken();
      if (err instanceof StaffApiError) {
        setError(
          err.code === "unauthorized" || err.code === "staff_writes_disabled"
            ? "トークンが無効です。config viewer_api.staff_token を確認してください。"
            : err.message,
        );
      } else {
        setError("サーバーに接続できません。運営 PC の viewer API 起動を確認してください。");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>店舗スタッフ</Text>
      <Text style={styles.subtitle}>
        会計・注文を操作します。スタッフトークンでログインしてください（LAN 限定）。
      </Text>
      <View style={styles.card}>
        <Field
          label="スタッフトークン"
          value={token}
          onChangeText={setToken}
          placeholder="viewer_api.staff_token"
        />
        <Button
          label={busy ? "確認中…" : "ログイン"}
          onPress={onLogin}
          disabled={busy}
          style={{ marginTop: 12 }}
        />
        {error ? <Text style={[styles.status, { color: colors.neg }]}>{error}</Text> : null}
      </View>
      <Text style={[styles.cardMeta, { marginTop: 8 }]}>
        mock モード（EXPO_PUBLIC_API_URL 未設定）では「demo-staff-token」でログインできます。
      </Text>
    </View>
  );
}
