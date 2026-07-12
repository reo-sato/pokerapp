import React, { useState } from "react";
import { Pressable, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { ActionRecord, HandSummary } from "../api/types";
import { StaffApiError } from "../api/types";
import { Button, Chip, colors, Field, styles } from "./common";

// 訂正で選べるアクション種別（B4/ADR-0036。合法手の射影は core/pokerkit が権威）。
const ACTION_TYPES = ["check", "call", "bet", "raise", "fold", "all_in"] as const;

function messageFor(err: unknown): string {
  if (err instanceof StaffApiError) {
    const map: Record<string, string> = {
      invalid_correction: "訂正内容が不正です。",
      not_found: "ハンドが見つかりません。",
      orders_unavailable: "会計画面（--ledger）の起動中のみ訂正できます。",
      unauthorized: "スタッフトークンが無効です。",
    };
    return map[err.code] ?? err.message;
  }
  return "サーバーに接続できません。";
}

/**
 * ハンド訂正パネル（B4/ADR-0036 の staff アプリ内導線）。リプレイ詳細の下に置き、
 * 誤認識アクションの種別/金額と winner_seat を append-only オーバーレイで訂正する。
 * 訂正後は onCorrected() で hands read を再読込し、訂正済みビューが即リプレイに反映される。
 */
export function HandCorrectionPanel(props: {
  repository: StaffRepository;
  sessionId: string;
  hand: HandSummary;
  onCorrected: () => void;
}): React.JSX.Element {
  const { repository, sessionId, hand, onCorrected } = props;
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [draftAction, setDraftAction] = useState("");
  const [draftAmount, setDraftAmount] = useState("");
  const [draftWinner, setDraftWinner] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const beginEdit = (i: number, a: ActionRecord): void => {
    setEditingIndex(i);
    setDraftAction(a.action);
    setDraftAmount(String(a.amount ?? 0));
    setMsg(null);
  };

  const submitAction = async (i: number, current: ActionRecord): Promise<void> => {
    setBusy(true);
    setMsg(null);
    try {
      let applied = 0;
      if (draftAction && draftAction !== current.action) {
        await repository.addHandCorrection(sessionId, hand.hand_id, {
          field: "action", new_value: draftAction, action_index: i,
        });
        applied += 1;
      }
      const amt = parseInt(draftAmount, 10);
      if (Number.isFinite(amt) && amt >= 0 && amt !== (current.amount ?? 0)) {
        await repository.addHandCorrection(sessionId, hand.hand_id, {
          field: "amount", new_value: amt, action_index: i,
        });
        applied += 1;
      }
      if (applied === 0) {
        setMsg({ text: "変更がありません。", ok: false });
        return;
      }
      setEditingIndex(null);
      setMsg({ text: `訂正しました（${applied} 件）。`, ok: true });
      onCorrected();
    } catch (err) {
      setMsg({ text: messageFor(err), ok: false });
    } finally {
      setBusy(false);
    }
  };

  const submitWinner = async (): Promise<void> => {
    const seat = Number(draftWinner.trim());
    if (!Number.isInteger(seat) || seat < 1 || seat > 9) {
      setMsg({ text: "勝者席は 1〜9 で入力してください。", ok: false });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await repository.addHandCorrection(sessionId, hand.hand_id, {
        field: "winner_seat", new_value: seat,
      });
      setDraftWinner("");
      setMsg({ text: `勝者を席${seat} に訂正しました。`, ok: true });
      onCorrected();
    } catch (err) {
      setMsg({ text: messageFor(err), ok: false });
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>✎ このハンドを訂正</Text>
      <Text style={styles.cardMeta}>
        誤認識のアクション種別・金額・勝者席を訂正します（元の記録は保持されます）。
      </Text>

      {(hand.actions ?? []).map((a, i) => {
        const editing = editingIndex === i;
        return (
          <View
            key={i}
            style={{
              paddingVertical: 8,
              borderBottomWidth: 1,
              borderBottomColor: colors.border,
            }}
          >
            <View style={[styles.row, { justifyContent: "space-between" }]}>
              <Text style={styles.cardMeta}>
                [{a.street}] 席{a.seat} {a.player_name} {a.action}
                {a.amount ? ` ${a.amount}` : ""}
                {a.needs_review ? <Text style={{ color: colors.warn }}>  要確認</Text> : null}
                {a.corrected ? <Text style={{ color: colors.accent }}>  訂正済</Text> : null}
              </Text>
              {!editing ? (
                <Pressable onPress={() => beginEdit(i, a)}>
                  <Text style={{ color: colors.accent, fontSize: 13 }}>訂正する</Text>
                </Pressable>
              ) : null}
            </View>
            {editing ? (
              <View style={{ marginTop: 8 }}>
                <View style={[styles.row, { flexWrap: "wrap" }]}>
                  {ACTION_TYPES.map((t) => (
                    <Chip
                      key={t}
                      label={t}
                      selected={draftAction === t}
                      onPress={() => setDraftAction(t)}
                    />
                  ))}
                </View>
                <View style={[styles.row, { marginTop: 8 }]}>
                  <Field
                    label="金額"
                    value={draftAmount}
                    onChangeText={setDraftAmount}
                    keyboardType="number-pad"
                    placeholder="金額"
                  />
                  <View style={{ width: 8 }} />
                  <Button
                    label={busy ? "保存中…" : "訂正を保存"}
                    onPress={() => void submitAction(i, a)}
                    disabled={busy}
                    style={{ alignSelf: "flex-end" }}
                  />
                  <View style={{ width: 8 }} />
                  <Button
                    label="キャンセル"
                    kind="ghost"
                    onPress={() => setEditingIndex(null)}
                    disabled={busy}
                    style={{ alignSelf: "flex-end" }}
                  />
                </View>
              </View>
            ) : null}
          </View>
        );
      })}

      <View style={[styles.row, { marginTop: 10 }]}>
        <Field
          label={`勝者席の訂正（現在: ${hand.winner_seat != null ? `席${hand.winner_seat}` : "記録なし"}）`}
          value={draftWinner}
          onChangeText={setDraftWinner}
          keyboardType="number-pad"
          placeholder="席1-9"
        />
        <View style={{ width: 8 }} />
        <Button
          label="勝者を訂正"
          kind="neutral"
          onPress={() => void submitWinner()}
          disabled={busy}
          style={{ alignSelf: "flex-end" }}
        />
      </View>

      {msg ? (
        <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg }]}>{msg.text}</Text>
      ) : null}
    </View>
  );
}
