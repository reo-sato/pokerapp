import React, { useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { Player } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, Button, colors, ErrorView, Field, Loading, styles } from "./common";

function errText(err: unknown): string {
  if (err instanceof StaffApiError) {
    const map: Record<string, string> = {
      empty_display_name: "表示名を入力してください。",
      duplicate_display_name: "その表示名は既に存在します。",
      invalid_merge: "その組み合わせでは統合できません（自己統合など）。",
      not_found: "player が見つかりません。",
    };
    return map[err.code] ?? err.message;
  }
  return String(err);
}

/**
 * プレイヤー管理（registry, iPad 導線）: 一覧 / 新規作成 / リネーム / 統合（merge, ADR-0030）。
 * merge は「重複登録された同一人物」を alias/tombstone で片寄せする操作 — 会計・座席・ハンドの
 * 履歴はすべて survivor に集約して読める（core が canonicalize。ここに業務ルールは複製しない）。
 */
export function PlayersScreen(props: {
  repository: StaffRepository;
  onBack: () => void;
}): React.JSX.Element {
  const { repository, onBack } = props;
  const state = useAsync(() => repository.listPlayers(), [repository]);
  const players = state.data ?? [];

  const [newName, setNewName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  // merge は「統合先（survivor）を選ぶ → 吸収される側（absorbed）を選ぶ」の 2 段。
  const [mergeSurvivor, setMergeSurvivor] = useState<Player | null>(null);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const run = async (fn: () => Promise<string>): Promise<void> => {
    try {
      const text = await fn();
      setMsg({ text, ok: true });
      state.reload();
    } catch (err) {
      setMsg({ text: errText(err), ok: false });
    }
  };

  const onCreate = (): void => {
    void run(async () => {
      const p = await repository.createPlayer(newName.trim());
      setNewName("");
      return `作成しました: ${p.display_name}`;
    });
  };

  const onRename = (playerId: string): void => {
    void run(async () => {
      const p = await repository.renamePlayer(playerId, renameDraft.trim());
      setRenamingId(null);
      return `リネームしました: ${p.display_name}`;
    });
  };

  const onMerge = (absorbed: Player): void => {
    const survivor = mergeSurvivor;
    if (!survivor) return;
    void run(async () => {
      await repository.mergePlayers(survivor.player_id, absorbed.player_id);
      setMergeSurvivor(null);
      return `統合しました: ${absorbed.display_name} → ${survivor.display_name}`;
    });
  };

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="セッション一覧" />
      <Text style={styles.title}>プレイヤー管理</Text>
      <Text style={styles.subtitle}>
        新規作成・リネーム・重複登録の統合（履歴は統合先に集約されます）。
      </Text>

      <View style={styles.card}>
        <View style={styles.row}>
          <Field label="新規プレイヤー名" value={newName} onChangeText={setNewName} placeholder="表示名" />
          <View style={{ width: 8 }} />
          <Button label="作成" onPress={onCreate} style={{ alignSelf: "flex-end" }} />
        </View>
      </View>

      {mergeSurvivor ? (
        <View style={[styles.card, { borderWidth: 1, borderColor: colors.accent }]}>
          <Text style={styles.cardTitle}>
            統合先: {mergeSurvivor.display_name}
          </Text>
          <Text style={styles.cardMeta}>
            重複している（消える側の）プレイヤーの「→ {mergeSurvivor.display_name} に統合」を
            押してください。履歴はすべて統合先に集約されます。
          </Text>
          <Button
            label="統合をやめる"
            kind="ghost"
            onPress={() => setMergeSurvivor(null)}
            style={{ marginTop: 8, alignSelf: "flex-start" }}
          />
        </View>
      ) : null}

      {msg ? (
        <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg, marginBottom: 8 }]}>
          {msg.text}
        </Text>
      ) : null}

      {state.loading && players.length === 0 ? (
        <Loading />
      ) : state.errorCode ? (
        <ErrorView code={state.errorCode} message={state.errorMessage} onRetry={state.reload} />
      ) : players.length === 0 ? (
        <Text style={styles.empty}>player が未登録です。</Text>
      ) : (
        <ScrollView>
          {players.map((p) => (
            <View key={p.player_id} style={styles.card}>
              <View style={[styles.row, { justifyContent: "space-between" }]}>
                <Text style={styles.cardTitle}>{p.display_name}</Text>
                <Text style={styles.cardMeta}>{p.player_id.slice(0, 8)}</Text>
              </View>
              {renamingId === p.player_id ? (
                <View style={[styles.row, { marginTop: 8 }]}>
                  <Field label="新しい表示名" value={renameDraft} onChangeText={setRenameDraft} />
                  <View style={{ width: 8 }} />
                  <Button label="保存" onPress={() => onRename(p.player_id)} style={{ alignSelf: "flex-end" }} />
                  <View style={{ width: 8 }} />
                  <Button label="やめる" kind="ghost" onPress={() => setRenamingId(null)} style={{ alignSelf: "flex-end" }} />
                </View>
              ) : (
                <View style={[styles.row, { marginTop: 8 }]}>
                  <Button
                    label="リネーム"
                    kind="neutral"
                    onPress={() => {
                      setRenamingId(p.player_id);
                      setRenameDraft(p.display_name);
                      setMsg(null);
                    }}
                  />
                  <View style={{ width: 8 }} />
                  {mergeSurvivor === null ? (
                    <Button
                      label="統合先にする"
                      kind="ghost"
                      onPress={() => {
                        setMergeSurvivor(p);
                        setMsg(null);
                      }}
                    />
                  ) : mergeSurvivor.player_id !== p.player_id ? (
                    <Button
                      label={`→ ${mergeSurvivor.display_name} に統合`}
                      kind="danger"
                      onPress={() => onMerge(p)}
                    />
                  ) : null}
                </View>
              )}
            </View>
          ))}
          <View style={{ height: 24 }} />
        </ScrollView>
      )}
      <Pressable onPress={state.reload}>
        <Text style={styles.back}>↻ 再読込</Text>
      </Pressable>
    </View>
  );
}
