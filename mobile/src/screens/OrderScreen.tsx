import React, { useCallback, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { MenuItem, OrderRequest, Player, PlayerSessionSummary } from "../api/types";
import { ViewerApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, ErrorView, Loading, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  player: Player;
  session: PlayerSessionSummary;
  onBack: () => void;
}

const STATUS_LABELS: Record<OrderRequest["status"], string> = {
  pending: "受付中…",
  confirmed: "確定（会計に反映済み）",
  rejected: "却下",
};

/** ドリンク注文 (M5, ADR-0018)。リクエストは pending で送られ、スタッフ確定で会計に載る。 */
export function OrderScreen({ repository, player, session, onBack }: Props): React.JSX.Element {
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [requestsVersion, setRequestsVersion] = useState(0);

  const menuState = useAsync(() => repository.getMenu(), [repository]);
  const requestsState = useAsync(
    () => repository.listOrderRequests(player.player_id, session.session_id),
    [repository, player.player_id, session.session_id, requestsVersion],
  );

  const qtyOf = (item: MenuItem) => quantities[item.item_name] ?? 1;
  const bumpQty = (item: MenuItem, delta: number) =>
    setQuantities((q) => ({
      ...q,
      [item.item_name]: Math.min(99, Math.max(1, qtyOf(item) + delta)),
    }));

  const submit = useCallback(
    async (item: MenuItem) => {
      setSending(true);
      setMessage(null);
      try {
        const req = await repository.createOrderRequest(player.player_id, session.session_id, {
          item_name: item.item_name,
          quantity: qtyOf(item),
        });
        setMessage(`注文を送信しました: ${req.item_name} ×${req.quantity}（スタッフ確定待ち）`);
        setRequestsVersion((v) => v + 1);
      } catch (err) {
        if (err instanceof ViewerApiError && err.code === "orders_unavailable") {
          setMessage("現在は注文を受け付けていません（スタッフ画面が起動していません）。");
        } else if (err instanceof ViewerApiError && err.code === "session_closed") {
          setMessage("このセッションは終了しています。");
        } else {
          setMessage(err instanceof Error ? err.message : String(err));
        }
      } finally {
        setSending(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [repository, player.player_id, session.session_id, quantities],
  );

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="会計" />
      <Text style={styles.title}>ドリンク注文</Text>
      <Text style={styles.subtitle}>
        {player.display_name} ・ {session.label ?? session.started_at} ・ スタッフ確定後に会計へ反映
      </Text>
      {menuState.loading ? (
        <Loading />
      ) : menuState.errorCode ? (
        <ErrorView code={menuState.errorCode} message={menuState.errorMessage} />
      ) : (
        <ScrollView>
          {message ? (
            <View style={styles.card}>
              <Text style={styles.cardMeta}>{message}</Text>
            </View>
          ) : null}

          {(menuState.data ?? []).length === 0 ? (
            <Text style={styles.empty}>メニューが登録されていません。</Text>
          ) : (
            (menuState.data ?? []).map((item) => (
              <View key={item.item_name} style={styles.card}>
                <Text style={styles.cardTitle}>
                  {item.item_name}
                  <Text style={styles.cardMeta}>  {item.unit_amount.toLocaleString()} / 個</Text>
                </Text>
                <View style={{ flexDirection: "row", alignItems: "center", marginTop: 8 }}>
                  <Pressable onPress={() => bumpQty(item, -1)}>
                    <Text style={styles.back}>−</Text>
                  </Pressable>
                  <Text style={[styles.cardTitle, { marginHorizontal: 12 }]}>{qtyOf(item)}</Text>
                  <Pressable onPress={() => bumpQty(item, +1)}>
                    <Text style={styles.back}>＋</Text>
                  </Pressable>
                  <Pressable
                    disabled={sending}
                    onPress={() => submit(item)}
                    style={{ marginLeft: "auto" }}
                  >
                    <Text style={styles.back}>{sending ? "送信中…" : "注文する →"}</Text>
                  </Pressable>
                </View>
              </View>
            ))
          )}

          <View style={styles.card}>
            <Text style={styles.cardTitle}>注文状況</Text>
            {requestsState.loading ? (
              <Text style={styles.cardMeta}>読み込み中…</Text>
            ) : (requestsState.data ?? []).length === 0 ? (
              <Text style={styles.cardMeta}>まだ注文はありません。</Text>
            ) : (
              [...(requestsState.data ?? [])].reverse().map((r) => (
                <Text key={r.request_id} style={styles.cardMeta}>
                  {r.requested_at} ・ {r.item_name} ×{r.quantity} ・{" "}
                  <Text style={r.status === "rejected" ? styles.neg : styles.pos}>
                    {STATUS_LABELS[r.status]}
                  </Text>
                </Text>
              ))
            )}
            <Pressable onPress={() => setRequestsVersion((v) => v + 1)}>
              <Text style={[styles.back, { marginTop: 8 }]}>状況を更新 ↺</Text>
            </Pressable>
          </View>
        </ScrollView>
      )}
    </View>
  );
}
