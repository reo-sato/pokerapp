import React, { useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { HandControlInput, StaffSession } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { HandReplay } from "../shared/hand_replay/HandReplay";
import { HandCorrectionPanel } from "./HandCorrectionPanel";
import { Button, colors, ErrorView, Field, Loading, styles } from "./common";

/**
 * ハンドタブ:
 * - 遠隔制御（ADR-0039 §C）: hand logger に「新ハンド / ウィナー / リバイ」を送信する。
 *   録音（音声/RFID）は録音 PC で継続。iPad はコマンドを control queue に積むだけ。
 * - ハンド履歴（ADR-0044）: 卓の全ハンド（訂正適用済）を一覧し、タップで
 *   ストリート単位リプレイ（共有コンポーネント）+ 訂正パネル（B4/ADR-0036）を開く。
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
  // 選択は hand_id で保持し、本体は常に handsState（訂正適用済ビュー）から引く。
  // 訂正 → reload で detail が最新の訂正済みビューに更新される。
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const handsState = useAsync(
    () => repository.listSessionHands(session.session_id),
    [repository, session.session_id],
  );
  const selected =
    selectedId === null
      ? null
      : (handsState.data ?? []).find((h) => h.hand_id === selectedId) ?? null;

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

  // ハンド選択中はリプレイ detail + 訂正パネルを表示（一覧へは戻るリンク）。
  if (selected) {
    return (
      <ScrollView keyboardShouldPersistTaps="handled">
        <Pressable onPress={() => setSelectedId(null)}>
          <Text style={styles.back}>← ハンド履歴</Text>
        </Pressable>
        <Text style={styles.cardTitle}>Hand #{selected.hand_id}</Text>
        <Text style={[styles.cardMeta, { marginBottom: 10 }]}>
          {selected.started_at}
          {selected.review_required ? " ・ 要確認あり" : ""}
        </Text>
        <HandReplay hand={selected} />
        <HandCorrectionPanel
          repository={repository}
          sessionId={session.session_id}
          hand={selected}
          onCorrected={handsState.reload}
        />
        <View style={{ height: 40 }} />
      </ScrollView>
    );
  }

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

      {/* ハンド履歴（ADR-0044）: タップでストリート単位リプレイへ */}
      <View style={styles.card}>
        <View style={[styles.row, { justifyContent: "space-between" }]}>
          <Text style={styles.cardTitle}>ハンド履歴</Text>
          <Button label="再読込" onPress={handsState.reload} kind="ghost" />
        </View>
        {handsState.loading ? (
          <Loading />
        ) : handsState.errorCode ? (
          <ErrorView
            code={handsState.errorCode}
            message={handsState.errorMessage}
            onRetry={handsState.reload}
          />
        ) : (handsState.data ?? []).length === 0 ? (
          <Text style={styles.cardMeta}>
            ハンドログがまだありません（記録が始まると表示されます）。
          </Text>
        ) : (
          (handsState.data ?? []).map((h) => (
            <Pressable
              key={h.hand_id}
              style={{
                paddingVertical: 10,
                borderBottomWidth: 1,
                borderBottomColor: colors.border,
              }}
              onPress={() => setSelectedId(h.hand_id)}
            >
              <Text style={{ color: colors.text, fontSize: 14, fontWeight: "600" }}>
                Hand #{h.hand_id}
                {h.winner_seat != null ? `  🏆 席${h.winner_seat}` : ""}
                {h.review_required ? (
                  <Text style={{ color: colors.warn }}>  要確認</Text>
                ) : null}
              </Text>
              <Text style={styles.cardMeta}>
                {h.started_at}
                {h.board?.length ? ` ・ board ${h.board.join(" ")}` : ""}
                {h.pot_total != null ? ` ・ pot ${h.pot_total}` : ""}
              </Text>
            </Pressable>
          ))
        )}
      </View>
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}
