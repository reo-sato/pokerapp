import React from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { LedgerEntry, Player, PlayerSessionSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, ErrorView, Loading, ReloadLink, formatResult, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  player: Player;
  session: PlayerSessionSummary;
  onBack: () => void;
  onOpenOrder: () => void;
}

const KIND_LABELS: Record<LedgerEntry["kind"], string> = {
  buy_in: "バイイン",
  rebuy: "リバイ",
  add_on: "アドオン",
  order: "注文",
  entry_fee: "参加費",
  adjustment: "調整",
};

/** 自分の会計参照 (M4/S3a, read-only)。summary は中間集計で確定値ではない。 */
export function MyLedgerScreen({
  repository, player, session, onBack, onOpenOrder,
}: Props): React.JSX.Element {
  const { data, loading, errorCode, errorMessage, reload } = useAsync(
    () => repository.getPlayerLedger(player.player_id, session.session_id),
    [repository, player.player_id, session.session_id],
  );

  return (
    <View style={styles.screen}>
      <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
        <BackLink onPress={onBack} label="ハンド一覧" />
        <ReloadLink onPress={reload} />
      </View>
      <Text style={styles.title}>{player.display_name} の会計</Text>
      <Text style={styles.subtitle}>
        {session.label ?? session.started_at} ・ 中間集計（確定値ではありません）
      </Text>
      <Pressable onPress={onOpenOrder}>
        <Text style={styles.back}>ドリンクを注文する →</Text>
      </Pressable>
      {loading ? (
        <Loading />
      ) : errorCode || !data ? (
        <ErrorView code={errorCode} message={errorMessage} onRetry={reload} />
      ) : (
        <ScrollView>
          <View style={styles.card}>
            <Text style={styles.cardTitle}>
              {data.summary.settled ? "精算額（確定）" : "合計（店への支払い見込み）"}:{" "}
              {data.summary.net_due_to_store.toLocaleString()}
            </Text>
            <Text style={styles.cardMeta}>
              バイイン {data.summary.cash_in_total.toLocaleString()} ・ 注文{" "}
              {data.summary.order_total.toLocaleString()} ・ 参加費{" "}
              {data.summary.entry_fee.toLocaleString()}
            </Text>
            {data.summary.point_spent_total > 0 ? (
              <Text style={styles.cardMeta}>
                使用ポイント {data.summary.point_spent_total.toLocaleString()}
              </Text>
            ) : null}
            <Text style={styles.cardMeta}>
              精算状況:{" "}
              {data.summary.settled ? (
                <Text
                  style={data.summary.payment_status === "paid" ? styles.pos : styles.neg}
                >
                  {data.summary.payment_status === "paid"
                    ? "確定済（支払済み）"
                    : data.summary.payment_status === "partial"
                      ? `確定済（一部支払い ${(
                          data.summary.paid_amount ?? 0
                        ).toLocaleString()}/${data.summary.net_due_to_store.toLocaleString()} 円）`
                      : "確定済（未払い）"}
                </Text>
              ) : (
                "未確定（暫定）"
              )}
            </Text>
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>明細（{data.entries.length} 件）</Text>
            {data.entries.length === 0 ? (
              <Text style={styles.cardMeta}>まだ記録がありません。</Text>
            ) : (
              data.entries.map((e) => (
                <Text key={e.entry_id} style={styles.cardMeta}>
                  {e.occurred_at} ・ {KIND_LABELS[e.kind]}{" "}
                  <Text style={e.cash_amount >= 0 ? styles.pos : styles.neg}>
                    {formatResult(e.cash_amount)}
                  </Text>
                  {e.order ? ` ・ ${e.order.item_name}×${e.order.quantity}` : ""}
                  {e.note ? ` ・ ${e.note}` : ""}
                </Text>
              ))
            )}
          </View>
        </ScrollView>
      )}
    </View>
  );
}
