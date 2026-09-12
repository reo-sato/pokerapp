import React, { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { ActionRecord, PlayerSessionSummary } from "../api/types";
import { ViewerApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, ErrorView, Loading, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  session: PlayerSessionSummary;
  handId: number;
  onBack: () => void;
}

// 訂正で選べるアクション種別（B4 / ADR-0036。core の射影は pokerkit が権威）。
const ACTION_TYPES = ["check", "call", "bet", "raise", "fold", "all_in"] as const;

function messageFor(err: unknown): string {
  if (err instanceof ViewerApiError) {
    const map: Record<string, string> = {
      unauthorized: "スタッフ権限が必要です（staff token 未設定/無効）。",
      invalid_correction: "訂正内容が不正です。",
      not_found: "ハンドが見つかりません。",
      orders_unavailable: "会計画面（--ledger）の起動中のみ訂正できます。",
    };
    return map[err.code] ?? err.message;
  }
  return "サーバーに接続できません。";
}

/**
 * ハンド訂正画面（B4 / ADR-0036。iPad などスタッフ端末で誤認識を訂正）。
 * getHand は訂正済みビュー（オーバーレイ）を返すので、訂正→再読込で結果が即反映される。
 * 元 hand log は不変（append-only）。
 */
export function CorrectionScreen({ repository, session, handId, onBack }: Props): React.JSX.Element {
  const [reloadKey, setReloadKey] = useState(0);
  const { data: hand, loading, errorCode, errorMessage } = useAsync(
    () => repository.getHand(session.session_id, handId),
    [repository, session.session_id, handId, reloadKey],
  );

  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [draftAction, setDraftAction] = useState<string>("");
  const [draftAmount, setDraftAmount] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [statusError, setStatusError] = useState(false);

  const beginEdit = (i: number, a: ActionRecord) => {
    setEditingIndex(i);
    setDraftAction(a.action);
    setDraftAmount(String(a.amount ?? 0));
    setStatus(null);
  };

  const submit = async (i: number, current: ActionRecord) => {
    setBusy(true);
    setStatus(null);
    try {
      let applied = 0;
      if (draftAction && draftAction !== current.action) {
        await repository.addHandCorrection(session.session_id, handId, {
          field: "action", new_value: draftAction, action_index: i,
        });
        applied += 1;
      }
      const amt = parseInt(draftAmount, 10);
      if (Number.isFinite(amt) && amt >= 0 && amt !== (current.amount ?? 0)) {
        await repository.addHandCorrection(session.session_id, handId, {
          field: "amount", new_value: amt, action_index: i,
        });
        applied += 1;
      }
      if (applied === 0) {
        setStatusError(true);
        setStatus("変更がありません。");
        return;
      }
      setEditingIndex(null);
      setStatusError(false);
      setStatus(`訂正しました（${applied} 件）。`);
      setReloadKey((k) => k + 1); // 訂正済みビューを再読込
    } catch (err) {
      setStatusError(true);
      setStatus(messageFor(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="ハンド詳細" />
      <Text style={styles.title}>Hand #{handId} 訂正</Text>
      <Text style={styles.subtitle}>誤認識を訂正します（元の記録は保持されます）</Text>
      {loading ? (
        <Loading />
      ) : errorCode || !hand ? (
        <ErrorView code={errorCode} message={errorMessage} />
      ) : (
        <ScrollView>
          {hand.actions.length === 0 ? (
            <Text style={styles.empty}>アクションの記録がありません。</Text>
          ) : (
            hand.actions.map((a, i) => {
              const corrected = (a as unknown as Record<string, unknown>).corrected === true;
              const editing = editingIndex === i;
              return (
                <View key={i} style={styles.card}>
                  <Text style={styles.cardTitle}>
                    [{a.street}] 席{a.seat} {a.player_name}
                  </Text>
                  <Text style={styles.cardMeta}>
                    {a.action}{a.amount ? ` ${a.amount}` : ""}
                    {a.needs_review ? "  ・ ⚠ 要確認" : ""}
                    {corrected ? "  ・ ✎ 訂正済" : ""}
                  </Text>
                  {!editing ? (
                    <Pressable onPress={() => beginEdit(i, a)}>
                      <Text style={[styles.cardMeta, { color: "#5ab0f0" }]}>訂正する</Text>
                    </Pressable>
                  ) : (
                    <View style={local.editor}>
                      <View style={local.row}>
                        {ACTION_TYPES.map((t) => (
                          <Pressable
                            key={t}
                            style={[local.chip, draftAction === t && local.chipOn]}
                            onPress={() => setDraftAction(t)}
                          >
                            <Text style={local.chipText}>{t}</Text>
                          </Pressable>
                        ))}
                      </View>
                      <TextInput
                        style={local.input}
                        value={draftAmount}
                        onChangeText={setDraftAmount}
                        placeholder="金額"
                        placeholderTextColor="#5a646e"
                        keyboardType="number-pad"
                        editable={!busy}
                      />
                      <View style={local.row}>
                        <Pressable
                          style={[local.btn, busy && local.btnOff]}
                          disabled={busy}
                          onPress={() => submit(i, a)}
                        >
                          <Text style={local.btnText}>{busy ? "保存中…" : "訂正を保存"}</Text>
                        </Pressable>
                        <Pressable
                          style={[local.btn, local.btnGhost]}
                          disabled={busy}
                          onPress={() => setEditingIndex(null)}
                        >
                          <Text style={local.btnText}>キャンセル</Text>
                        </Pressable>
                      </View>
                    </View>
                  )}
                </View>
              );
            })
          )}
          {status ? (
            <Text style={[styles.error, statusError ? undefined : styles.pos, { marginTop: 12 }]}>
              {status}
            </Text>
          ) : null}
        </ScrollView>
      )}
    </View>
  );
}

const local = StyleSheet.create({
  editor: { marginTop: 8, gap: 8 },
  row: { flexDirection: "row", flexWrap: "wrap", gap: 6, alignItems: "center" },
  chip: { backgroundColor: "#2a313a", borderRadius: 8, paddingVertical: 6, paddingHorizontal: 10 },
  chipOn: { backgroundColor: "#2563a8" },
  chipText: { color: "#f2f5f7", fontSize: 13 },
  input: {
    backgroundColor: "#11161b", borderRadius: 8, padding: 10, color: "#f2f5f7", fontSize: 15,
  },
  btn: {
    backgroundColor: "#2563a8", borderRadius: 8, paddingVertical: 10, paddingHorizontal: 16,
    alignItems: "center",
  },
  btnGhost: { backgroundColor: "#2a313a" },
  btnOff: { opacity: 0.5 },
  btnText: { color: "#f2f5f7", fontSize: 14, fontWeight: "600" },
});
