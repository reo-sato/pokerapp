import React, { useState } from "react";
import { ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { HandControlInput, StaffSession } from "../api/types";
import { StaffApiError } from "../api/types";
import { Button, colors, Field, styles } from "./common";

/**
 * ハンドタブ（ADR-0039 §C）: hand logger に「新ハンド / ウィナー / リバイ」を遠隔送信する。
 * 録音（音声/RFID）は録音 PC で継続。iPad はコマンドを control queue に積むだけで、適用は
 * hand logger プロセス（`hand_control.enabled`）が行う。反映には数百ms の遅延がある。
 */
export function HandTab(props: {
  repository: StaffRepository;
  session: StaffSession;
}): React.JSX.Element {
  const { repository, session } = props;
  const [seat, setSeat] = useState("");
  const [amount, setAmount] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [sent, setSent] = useState<string[]>([]);

  const send = async (input: HandControlInput, label: string): Promise<void> => {
    try {
      await repository.sendControl(session.session_id, input);
      setMsg({ text: `送信しました: ${label}`, ok: true });
      setSent((prev) => [`${new Date().toLocaleTimeString("ja-JP")}  ${label}`, ...prev].slice(0, 10));
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const seatNum = (): number | null => {
    const n = Number(seat.trim());
    return Number.isInteger(n) && n >= 1 && n <= 9 ? n : null;
  };

  const onWinner = (): void => {
    const s = seatNum();
    if (s === null) {
      setMsg({ text: "席番号は 1〜9 で入力してください。", ok: false });
      return;
    }
    void send({ type: "winner", seat: s }, `ウィナー 席${s}`);
  };

  const onRebuy = (): void => {
    const s = seatNum();
    const a = Number(amount.trim());
    if (s === null) {
      setMsg({ text: "席番号は 1〜9 で入力してください。", ok: false });
      return;
    }
    if (!Number.isInteger(a) || a <= 0) {
      setMsg({ text: "リバイ金額は正の整数で入力してください。", ok: false });
      return;
    }
    void send({ type: "rebuy", seat: s, amount: a }, `リバイ 席${s} +${a}`);
  };

  return (
    <ScrollView keyboardShouldPersistTaps="handled">
      <View style={styles.card}>
        <Text style={styles.cardMeta}>
          操作は録音 PC の hand logger に送られます（反映に数百ms）。録音は PC で継続します。
        </Text>
        {session.status === "closed" ? (
          <Text style={[styles.cardMeta, { color: colors.warn, marginTop: 6 }]}>
            closed session です（hand logger は通常 open 卓を録音中）。
          </Text>
        ) : null}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>新ハンド</Text>
        <Button label="新ハンド開始" onPress={() => void send({ type: "new_hand" }, "新ハンド")} style={{ marginTop: 8 }} />
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>ウィナー / リバイ</Text>
        <View style={[styles.row, { marginTop: 4 }]}>
          <Field label="席番号 (1-9)" value={seat} onChangeText={setSeat} keyboardType="number-pad" placeholder="席1-9" />
          <View style={{ width: 8 }} />
          <Field label="リバイ金額" value={amount} onChangeText={setAmount} keyboardType="number-pad" placeholder="金額" />
        </View>
        <View style={[styles.row, { marginTop: 10 }]}>
          <Button label="ウィナー確定" onPress={onWinner} kind="neutral" />
          <View style={{ width: 8 }} />
          <Button label="リバイ適用" onPress={onRebuy} kind="neutral" />
        </View>
      </View>

      {msg ? (
        <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg }]}>{msg.text}</Text>
      ) : null}

      {sent.length > 0 ? (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>送信履歴（この画面）</Text>
          {sent.map((line, i) => (
            <Text key={i} style={styles.cardMeta}>{line}</Text>
          ))}
        </View>
      ) : null}
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}
