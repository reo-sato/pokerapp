import React, { useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player } from "../api/types";
import { ViewerApiError } from "../api/types";
import { BackLink, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  /** "pin" = L1 PIN ログイン (ADR-0027) / "oidc" = L2 外部 IdP サインアップ (ADR-0031)。 */
  mode: "pin" | "oidc";
  player?: Player; // pin モードで必須（誰としてログインするか）
  onAuthed: (player: Player) => void;
  onBack: () => void;
}

// L2: 実機では IdP SDK / web redirect で認可コードを取得する。本 UI はその差し込み点で、
// デモ（mock / 未構成サーバ）では固定コードを送る（実コード取得は実環境タスク, ADR-0031 D5）。
const DEMO_OIDC_CODE = "demo-good";

function messageFor(err: unknown): { code: string; text: string } {
  if (err instanceof ViewerApiError) {
    const map: Record<string, string> = {
      invalid_pin: "PIN が違います。",
      pin_locked: "試行回数が上限です。しばらく待って再試行してください。",
      pin_too_short: "PIN は 4 桁以上で入力してください。",
      player_auth_disabled: "この会場では PIN ログインは無効です（そのまま参照できます）。",
      unknown_provider: "この会場では外部ログインは利用できません。",
      invalid_idp_code: "外部ログインに失敗しました。",
      not_found: "player が見つかりません。",
    };
    return { code: err.code, text: map[err.code] ?? err.message };
  }
  return { code: "network_error", text: "サーバーに接続できません。" };
}

/** L1 PIN ログイン / L2 外部 IdP サインアップ（player_auth=optional/required 時の self-write 用）。 */
export function AuthScreen({ repository, mode, player, onAuthed, onBack }: Props): React.JSX.Element {
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handlePinLogin = async () => {
    if (!player) return;
    setBusy(true);
    setError(null);
    try {
      await repository.login(player.player_id, pin);
      onAuthed(player);
    } catch (err) {
      setError(messageFor(err).text);
    } finally {
      setBusy(false);
    }
  };

  const handleOidc = async (provider: string) => {
    setBusy(true);
    setError(null);
    try {
      const session = await repository.oidcExchange(provider, DEMO_OIDC_CODE);
      const signed = await repository.getPlayer(session.player_id);
      onAuthed(signed);
    } catch (err) {
      setError(messageFor(err).text);
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} />
      {mode === "pin" ? (
        <>
          <Text style={styles.title}>PIN ログイン</Text>
          <Text style={styles.subtitle}>{player?.display_name} として本人確認します</Text>
          <TextInput
            style={local.input}
            value={pin}
            onChangeText={setPin}
            placeholder="PIN"
            placeholderTextColor="#5a646e"
            keyboardType="number-pad"
            secureTextEntry
            editable={!busy}
          />
          <Pressable
            style={[local.button, busy && local.buttonDisabled]}
            disabled={busy}
            onPress={handlePinLogin}
          >
            <Text style={local.buttonText}>{busy ? "確認中…" : "ログイン"}</Text>
          </Pressable>
        </>
      ) : (
        <>
          <Text style={styles.title}>アカウントでサインアップ</Text>
          <Text style={styles.subtitle}>LINE / Google で続けると会員として参照できます</Text>
          <Pressable
            style={[local.button, local.line, busy && local.buttonDisabled]}
            disabled={busy}
            onPress={() => handleOidc("line")}
          >
            <Text style={local.buttonText}>LINE で続ける</Text>
          </Pressable>
          <Pressable
            style={[local.button, busy && local.buttonDisabled]}
            disabled={busy}
            onPress={() => handleOidc("google")}
          >
            <Text style={local.buttonText}>Google で続ける</Text>
          </Pressable>
        </>
      )}
      {error ? <Text style={[styles.error, { marginTop: 16 }]}>{error}</Text> : null}
    </View>
  );
}

const local = StyleSheet.create({
  input: {
    backgroundColor: "#1b2026", borderRadius: 10, padding: 14, marginBottom: 12,
    color: "#f2f5f7", fontSize: 18, letterSpacing: 4,
  },
  button: {
    backgroundColor: "#2563a8", borderRadius: 10, padding: 14, marginBottom: 10,
    alignItems: "center",
  },
  buttonDisabled: { opacity: 0.5 },
  line: { backgroundColor: "#06c755" },
  buttonText: { color: "#f2f5f7", fontSize: 16, fontWeight: "600" },
});
