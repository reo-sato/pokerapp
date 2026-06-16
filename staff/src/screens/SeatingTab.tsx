import React, { useState } from "react";
import { ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { Player, StaffSession } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import type { NameResolver } from "./TableViewScreen";
import { Button, Chip, colors, ErrorView, Field, Loading, styles } from "./common";

/**
 * 座席タブ（ADR-0036 §B）: 現在の seating を表示し、次の hand に seat→player を割り当てる。
 * 未登録 player はその場で作成できる（desktop の SeatSelectionDialog 相当）。
 */
export function SeatingTab(props: {
  repository: StaffRepository;
  session: StaffSession;
  players: Player[];
  resolveName: NameResolver;
  reloadPlayers: () => void;
}): React.JSX.Element {
  const { repository, session, players, resolveName, reloadPlayers } = props;
  const seating = useAsync(() => repository.getSeating(session.session_id), [
    repository,
    session.session_id,
  ]);

  const [staged, setStaged] = useState<Record<number, string>>({});
  const [selectedPlayer, setSelectedPlayer] = useState<string | null>(null);
  const [seatInput, setSeatInput] = useState("");
  const [newName, setNewName] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const nextHand = (seating.data?.hand_ids.length ?? 0) > 0
    ? Math.max(...(seating.data?.hand_ids ?? [0])) + 1
    : 1;

  const stagedEntries = Object.entries(staged).map(([seat, pid]) => ({
    seat_no: Number(seat),
    player_id: pid,
  }));

  const onAddStaged = (): void => {
    const seat = Number(seatInput.trim());
    if (!Number.isInteger(seat) || seat < 1 || seat > 9) {
      setMsg({ text: "席番号は 1〜9 で入力してください。", ok: false });
      return;
    }
    if (!selectedPlayer) {
      setMsg({ text: "プレイヤーを選択してください。", ok: false });
      return;
    }
    if (staged[seat]) {
      setMsg({ text: `席${seat} は既に割り当て予定です。`, ok: false });
      return;
    }
    if (Object.values(staged).includes(selectedPlayer)) {
      setMsg({ text: "同じプレイヤーを複数の席に割り当てられません。", ok: false });
      return;
    }
    setStaged((prev) => ({ ...prev, [seat]: selectedPlayer }));
    setSeatInput("");
    setSelectedPlayer(null);
    setMsg(null);
  };

  const onAssign = async (): Promise<void> => {
    if (stagedEntries.length === 0) {
      setMsg({ text: "割り当てる席がありません。", ok: false });
      return;
    }
    try {
      await repository.assignSeats(session.session_id, nextHand, stagedEntries);
      setStaged({});
      seating.reload();
      setMsg({ text: `hand #${nextHand} に ${stagedEntries.length} 席を割り当てました。`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onCreatePlayer = async (): Promise<void> => {
    try {
      const p = await repository.createPlayer(newName);
      setNewName("");
      reloadPlayers();
      setSelectedPlayer(p.player_id);
      setMsg({ text: `追加しました: ${p.display_name}（席に割り当ててください）`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  return (
    <ScrollView keyboardShouldPersistTaps="handled">
      {/* 現在の seating */}
      <View style={styles.card}>
        <Text style={styles.cardTitle}>現在の座席（最新 hand 由来）</Text>
        {seating.loading ? (
          <Loading />
        ) : seating.errorCode ? (
          <ErrorView code={seating.errorCode} message={seating.errorMessage} onRetry={seating.reload} />
        ) : !seating.data || seating.data.seating.length === 0 ? (
          <Text style={styles.empty}>まだ座席の記録がありません。</Text>
        ) : (
          seating.data.seating
            .slice()
            .sort((a, b) => a.seat_no - b.seat_no)
            .map((sa) => (
              <Text key={sa.seat_no} style={[styles.cardMeta, { fontSize: 14, color: colors.text }]}>
                席{sa.seat_no}　{resolveName(sa.player_id)}
              </Text>
            ))
        )}
      </View>

      {session.status === "closed" ? (
        <View style={styles.card}>
          <Text style={styles.cardMeta}>closed session のため座席は変更できません。</Text>
        </View>
      ) : (
        <>
          {/* 座席割り当て（次ハンド） */}
          <View style={styles.card}>
            <Text style={styles.cardTitle}>座席を設定（次の hand #{nextHand}）</Text>
            <Text style={styles.label}>プレイヤー</Text>
            <View style={[styles.row, { flexWrap: "wrap", marginBottom: 4 }]}>
              {players.length === 0 ? (
                <Text style={styles.cardMeta}>player がいません。下で追加してください。</Text>
              ) : (
                players.map((p) => (
                  <Chip
                    key={p.player_id}
                    label={p.display_name}
                    selected={selectedPlayer === p.player_id}
                    onPress={() => setSelectedPlayer(p.player_id)}
                  />
                ))
              )}
            </View>
            <View style={[styles.row, { marginTop: 4 }]}>
              <Field
                label="席番号 (1-9)"
                value={seatInput}
                onChangeText={setSeatInput}
                keyboardType="number-pad"
                placeholder="席1-9"
              />
              <View style={{ width: 8 }} />
              <Button label="席に追加" onPress={onAddStaged} kind="neutral" style={{ alignSelf: "flex-end" }} />
            </View>

            {stagedEntries.length > 0 ? (
              <View style={{ marginTop: 10 }}>
                <Text style={styles.label}>割り当て予定</Text>
                {stagedEntries
                  .sort((a, b) => a.seat_no - b.seat_no)
                  .map((e) => (
                    <View key={e.seat_no} style={[styles.row, { justifyContent: "space-between", marginBottom: 4 }]}>
                      <Text style={{ color: colors.text }}>
                        席{e.seat_no}　{resolveName(e.player_id)}
                      </Text>
                      <Button
                        label="取消"
                        kind="ghost"
                        onPress={() =>
                          setStaged((prev) => {
                            const next = { ...prev };
                            delete next[e.seat_no];
                            return next;
                          })
                        }
                      />
                    </View>
                  ))}
                <Button label={`hand #${nextHand} に割り当て`} onPress={onAssign} style={{ marginTop: 6 }} />
              </View>
            ) : null}
          </View>

          {/* その場で player 作成 */}
          <View style={styles.card}>
            <Text style={styles.cardTitle}>プレイヤーを追加</Text>
            <View style={[styles.row, { marginTop: 4 }]}>
              <Field label="表示名" value={newName} onChangeText={setNewName} placeholder="新規プレイヤー名" />
              <View style={{ width: 8 }} />
              <Button label="追加" onPress={onCreatePlayer} style={{ alignSelf: "flex-end" }} />
            </View>
          </View>
        </>
      )}

      {msg ? (
        <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg }]}>{msg.text}</Text>
      ) : null}
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}
