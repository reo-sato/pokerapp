import React, { useState } from "react";
import { ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { LedgerKind, Player, SessionSettlement, StaffSession } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import type { NameResolver } from "./TableViewScreen";
import {
  Button,
  Chip,
  colors,
  ErrorView,
  Field,
  Loading,
  paymentColor,
  paymentLabel,
  styles,
  yen,
} from "./common";

const KINDS: LedgerKind[] = ["buy_in", "rebuy", "add_on", "order", "entry_fee", "adjustment"];

/** 会計タブ: ledger entry 追加 / buy-in プリセット / 中間集計 / 精算確定・支払（ADR-0016/0023/0026）。 */
export function LedgerTab(props: {
  repository: StaffRepository;
  session: StaffSession;
  players: Player[];
  resolveName: NameResolver;
}): React.JSX.Element {
  const { repository, session, players, resolveName } = props;

  const settlement = useAsync(
    () => repository.getSettlement(session.session_id),
    [repository, session.session_id],
  );
  const entries = useAsync(
    () => repository.listLedgerEntries(session.session_id),
    [repository, session.session_id],
  );
  const presets = useAsync(() => repository.getBuyinPresets(), [repository]);

  const [playerId, setPlayerId] = useState<string | null>(null);
  const [kind, setKind] = useState<LedgerKind>("buy_in");
  const [cash, setCash] = useState("");
  const [point, setPoint] = useState("");
  const [note, setNote] = useState("");
  const [grant, setGrant] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  // 確定済 settlement（commit の戻り値を保持。GET committed API は ADR-0038 で追加予定）。
  const [committed, setCommitted] = useState<SessionSettlement[] | null>(null);
  const [paidInputs, setPaidInputs] = useState<Record<string, string>>({});

  const parseAmount = (t: string): number | null => {
    const s = t.trim();
    if (s === "") return 0;
    return /^-?\d+$/.test(s) ? parseInt(s, 10) : null;
  };

  const onAddEntry = async (): Promise<void> => {
    if (!playerId) {
      setMsg({ text: "プレイヤーを選択してください。", ok: false });
      return;
    }
    const cashN = parseAmount(cash);
    const pointN = parseAmount(point);
    if (cashN === null || pointN === null) {
      setMsg({ text: "cash / point は整数で入力してください。", ok: false });
      return;
    }
    try {
      const entry = await repository.addLedgerEntry(session.session_id, {
        player_id: playerId,
        kind,
        cash_amount: cashN,
        point_amount: pointN,
        note: note.trim() || null,
      });
      setCash("");
      setPoint("");
      setNote("");
      settlement.reload();
      entries.reload();
      setMsg({ text: `記録しました: ${entry.kind} ${yen(entry.cash_amount)}`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onGrantPoints = async (): Promise<void> => {
    if (!playerId) {
      setMsg({ text: "プレイヤーを選択してください。", ok: false });
      return;
    }
    const n = parseAmount(grant);
    if (n === null || n <= 0) {
      setMsg({ text: "付与ポイントは正の整数で入力してください。", ok: false });
      return;
    }
    try {
      await repository.grantPoints(playerId, n);
      setGrant("");
      setMsg({ text: `ポイントを付与しました: +${n} pt`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onReverse = async (entryId: string): Promise<void> => {
    try {
      await repository.reverseEntry(entryId);
      settlement.reload();
      entries.reload();
      setMsg({ text: "取り消しました（reversal を追加）。", ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onCommit = async (): Promise<void> => {
    try {
      const rows = await repository.commitSettlement(session.session_id);
      setCommitted(rows);
      setMsg({ text: `精算を確定しました（${rows.length} 名）。`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const replaceCommitted = (row: SessionSettlement): void => {
    setCommitted((prev) =>
      (prev ?? []).map((r) => (r.player_id === row.player_id ? row : r)),
    );
  };

  const onTogglePaid = async (row: SessionSettlement): Promise<void> => {
    const next = row.payment_status === "paid" ? "unpaid" : "paid";
    try {
      replaceCommitted(await repository.setPaymentStatus(session.session_id, row.player_id, next));
      setMsg({ text: `支払状態を ${next} に更新しました。`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onRecordPayment = async (row: SessionSettlement): Promise<void> => {
    const amount = parseAmount(paidInputs[row.player_id] ?? "");
    if (amount === null || amount < 0) {
      setMsg({ text: "受領額は 0 以上の整数で入力してください。", ok: false });
      return;
    }
    try {
      replaceCommitted(await repository.recordPayment(session.session_id, row.player_id, amount));
      setMsg({ text: `受領額を記録しました: ${yen(amount)}`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  return (
    <ScrollView keyboardShouldPersistTaps="handled">
      {/* ――― エントリ追加 ――― */}
      <View style={styles.card}>
        <Text style={styles.cardTitle}>エントリ追加</Text>
        <Text style={styles.label}>プレイヤー</Text>
        <View style={[styles.row, { flexWrap: "wrap", marginBottom: 4 }]}>
          {players.length === 0 ? (
            <Text style={styles.cardMeta}>player を取得できませんでした。</Text>
          ) : (
            players.map((p) => (
              <Chip
                key={p.player_id}
                label={p.display_name}
                selected={playerId === p.player_id}
                onPress={() => setPlayerId(p.player_id)}
              />
            ))
          )}
        </View>
        <Text style={styles.label}>種別</Text>
        <View style={[styles.row, { flexWrap: "wrap", marginBottom: 4 }]}>
          {KINDS.map((k) => (
            <Chip key={k} label={k} selected={kind === k} onPress={() => setKind(k)} />
          ))}
        </View>
        {presets.data && presets.data.length > 0 ? (
          <>
            <Text style={styles.label}>buy-in プリセット</Text>
            <View style={[styles.row, { flexWrap: "wrap", marginBottom: 4 }]}>
              {presets.data.map((amount) => (
                <Chip
                  key={amount}
                  label={yen(amount)}
                  onPress={() => {
                    setKind("buy_in");
                    setCash(String(amount));
                  }}
                />
              ))}
            </View>
          </>
        ) : null}
        <View style={[styles.row, { marginTop: 4 }]}>
          <Field label="cash (円)" value={cash} onChangeText={setCash} keyboardType="number-pad" placeholder="cash(円)" />
          <View style={{ width: 8 }} />
          <Field label="point" value={point} onChangeText={setPoint} keyboardType="number-pad" placeholder="point(任意)" />
        </View>
        <View style={{ marginTop: 8 }}>
          <Field label="メモ" value={note} onChangeText={setNote} placeholder="任意" />
        </View>
        <Button label="エントリ追加" onPress={onAddEntry} style={{ marginTop: 12 }} />
        <View style={[styles.row, { marginTop: 10 }]}>
          <Field label="ポイント付与" value={grant} onChangeText={setGrant} keyboardType="number-pad" placeholder="付与pt" />
          <View style={{ width: 8 }} />
          <Button label="付与" onPress={onGrantPoints} kind="neutral" style={{ alignSelf: "flex-end" }} />
        </View>
        {msg ? (
          <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg }]}>{msg.text}</Text>
        ) : null}
      </View>

      {/* ――― エントリ一覧（取消 = reversal）――― */}
      <View style={styles.card}>
        <Text style={styles.cardTitle}>エントリ一覧</Text>
        <Text style={styles.cardMeta}>取消は reversal（append-only）で記録します。</Text>
        {entries.loading ? (
          <Loading />
        ) : entries.errorCode ? (
          <ErrorView code={entries.errorCode} message={entries.errorMessage} onRetry={entries.reload} />
        ) : !entries.data || entries.data.length === 0 ? (
          <Text style={styles.empty}>エントリはありません。</Text>
        ) : (
          entries.data.map((e) => {
            const isReversal = e.reverses_entry_id != null;
            return (
              <View
                key={e.entry_id}
                style={[styles.row, { justifyContent: "space-between", marginTop: 8 }]}
              >
                <View style={{ flex: 1 }}>
                  <Text style={{ color: isReversal ? colors.muted : colors.text }}>
                    {isReversal ? "↩ " : ""}
                    {e.occurred_at.slice(11, 19)}　{resolveName(e.player_id)}　{e.kind}
                  </Text>
                  <Text style={styles.cardMeta}>
                    {yen(e.cash_amount)}
                    {e.point_amount ? ` / ${e.point_amount}pt` : ""}
                    {e.note ? `　「${e.note}」` : ""}
                  </Text>
                </View>
                {isReversal ? null : (
                  <Button label="取消" kind="ghost" onPress={() => onReverse(e.entry_id)} />
                )}
              </View>
            );
          })
        )}
      </View>

      {/* ――― 中間集計（暫定）――― */}
      <View style={styles.card}>
        <Text style={styles.cardTitle}>中間集計（暫定 / speculative）</Text>
        <Text style={styles.cardMeta}>確定は session 締めで行います。</Text>
        {settlement.loading ? (
          <Loading />
        ) : settlement.errorCode ? (
          <ErrorView code={settlement.errorCode} message={settlement.errorMessage} onRetry={settlement.reload} />
        ) : !settlement.data || settlement.data.length === 0 ? (
          <Text style={styles.empty}>まだ会計エントリがありません。</Text>
        ) : (
          settlement.data.map((s) => (
            <View key={s.player_id} style={{ marginTop: 8 }}>
              <Text style={{ color: colors.text, fontWeight: "600" }}>
                {resolveName(s.player_id)}
                <Text style={{ color: colors.warn }}>net {yen(s.net_due_to_store)}</Text>
              </Text>
              <Text style={styles.cardMeta}>
                buy-in {yen(s.cash_in_total)} / order {yen(s.order_total)} / fee {yen(s.entry_fee)} /
                spent {s.point_spent_total}pt
              </Text>
            </View>
          ))
        )}
      </View>

      {/* ――― 精算確定 / 支払（closed session のみ）――― */}
      <View style={styles.card}>
        <Text style={styles.cardTitle}>精算（確定 / 支払状態）</Text>
        {session.status !== "closed" ? (
          <Text style={styles.cardMeta}>open session です。確定は session を close 後に行います。</Text>
        ) : (
          <>
            <Button
              label="このセッションを精算確定（commit）"
              onPress={onCommit}
              kind="neutral"
              style={{ marginTop: 8 }}
            />
            {committed?.map((row) => (
              <View key={row.player_id} style={{ marginTop: 10 }}>
                <Text style={{ color: colors.text, fontWeight: "600" }}>
                  {resolveName(row.player_id)}　net {yen(row.net_due_to_store)}
                  <Text style={{ color: paymentColor(row.payment_status) }}>
                    [{paymentLabel(row.payment_status)}]
                  </Text>
                </Text>
                <Text style={styles.cardMeta}>受領 {yen(row.paid_amount)}</Text>
                <View style={[styles.row, { marginTop: 6 }]}>
                  <Button
                    label={row.payment_status === "paid" ? "未払いにする" : "支払済みにする"}
                    onPress={() => onTogglePaid(row)}
                    kind="ghost"
                  />
                  <View style={{ width: 8 }} />
                  <Field
                    label=""
                    value={paidInputs[row.player_id] ?? ""}
                    onChangeText={(t) =>
                      setPaidInputs((prev) => ({ ...prev, [row.player_id]: t }))
                    }
                    placeholder="受領額"
                    keyboardType="number-pad"
                  />
                  <View style={{ width: 8 }} />
                  <Button label="記録" onPress={() => onRecordPayment(row)} />
                </View>
              </View>
            ))}
          </>
        )}
      </View>
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}
