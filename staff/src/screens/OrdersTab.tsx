import React, { useState } from "react";
import { ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { MenuItem, OrderRequest, StaffSession } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import type { NameResolver } from "./TableViewScreen";
import { Button, colors, ErrorView, Field, Loading, styles, yen } from "./common";

/** 注文タブ: pending を確定（単価を確定し order entry を起こす）/ 却下する（M5 / ADR-0018）。 */
export function OrdersTab(props: {
  repository: StaffRepository;
  session: StaffSession;
  resolveName: NameResolver;
  onChanged: () => void;
}): React.JSX.Element {
  const { repository, session, resolveName, onChanged } = props;

  const pending = useAsync(
    () => repository.listOrderRequests(session.session_id, "pending"),
    [repository, session.session_id],
  );
  const menu = useAsync(() => repository.getMenu(), [repository]);
  const [units, setUnits] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const prefill = (itemName: string): number | undefined =>
    menu.data?.find((m: MenuItem) => m.item_name === itemName)?.unit_amount;

  const unitFor = (req: OrderRequest): string => {
    const current = units[req.request_id];
    if (current !== undefined) return current;
    const p = prefill(req.item_name);
    return p !== undefined ? String(p) : "";
  };

  const refresh = (): void => {
    pending.reload();
    onChanged();
  };

  const onConfirm = async (req: OrderRequest): Promise<void> => {
    const s = unitFor(req).trim();
    if (!/^\d+$/.test(s)) {
      setMsg({ text: "単価は 0 以上の整数で入力してください。", ok: false });
      return;
    }
    try {
      await repository.confirmOrder(req.request_id, parseInt(s, 10));
      setMsg({ text: `確定しました: ${req.item_name} x${req.quantity}`, ok: true });
      refresh();
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onReject = async (req: OrderRequest): Promise<void> => {
    try {
      await repository.rejectOrder(req.request_id);
      setMsg({ text: `却下しました: ${req.item_name}`, ok: true });
      refresh();
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  return (
    <ScrollView keyboardShouldPersistTaps="handled">
      <Text style={styles.cardMeta}>
        確定すると単価 × 数量で order の会計エントリを作成します（staff-in-the-loop, ADR-0018）。
      </Text>
      {msg ? (
        <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg }]}>{msg.text}</Text>
      ) : null}

      {pending.loading ? (
        <Loading />
      ) : pending.errorCode ? (
        <ErrorView code={pending.errorCode} message={pending.errorMessage} onRetry={pending.reload} />
      ) : !pending.data || pending.data.length === 0 ? (
        <Text style={styles.empty}>保留中の注文はありません。</Text>
      ) : (
        pending.data.map((req) => (
          <View key={req.request_id} style={[styles.card, { marginTop: 10 }]}>
            <Text style={styles.cardTitle}>
              {req.item_name} x{req.quantity}
            </Text>
            <Text style={styles.cardMeta}>
              {resolveName(req.player_id)}
              {req.note ? `　「${req.note}」` : ""}
              {prefill(req.item_name) !== undefined ? `　menu ${yen(prefill(req.item_name) as number)}` : ""}
            </Text>
            <View style={[styles.row, { marginTop: 8 }]}>
              <Field
                label="単価 (円)"
                value={unitFor(req)}
                onChangeText={(t) => setUnits((prev) => ({ ...prev, [req.request_id]: t }))}
                keyboardType="number-pad"
              />
              <View style={{ width: 8 }} />
              <Button label="確定" onPress={() => onConfirm(req)} style={{ alignSelf: "flex-end" }} />
              <View style={{ width: 8 }} />
              <Button
                label="却下"
                onPress={() => onReject(req)}
                kind="danger"
                style={{ alignSelf: "flex-end" }}
              />
            </View>
          </View>
        ))
      )}
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}
