import React, { useState } from "react";
import { Pressable, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { Player, StaffSession } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, colors, styles } from "./common";
import { HandTab } from "./HandTab";
import { LedgerTab } from "./LedgerTab";
import { OrdersTab } from "./OrdersTab";
import { SeatingTab } from "./SeatingTab";

export type NameResolver = (playerId: string) => string;

type Tab = "ledger" | "orders" | "seating" | "hand";

/**
 * 卓ビュー（ADR-0037 §5）: 1 つの session について会計 / 注文をタブで統合する。
 * 座席 / ハンドタブは ADR-0038 の staff API 追加後に足す。
 */
export function TableViewScreen(props: {
  repository: StaffRepository;
  session: StaffSession;
  onBack: () => void;
}): React.JSX.Element {
  const { repository, session, onBack } = props;
  const [tab, setTab] = useState<Tab>("ledger");

  // player 名解決（失敗時は player_id 先頭で代替）。会計/注文の両タブで共有。
  const playersState = useAsync(() => repository.listPlayers(), [repository]);
  const players: Player[] = playersState.data ?? [];
  const resolveName: NameResolver = (pid) =>
    players.find((p) => p.player_id === pid)?.display_name ?? pid.slice(0, 8);

  // 注文 pending 件数（タブのバッジ用。確定/却下後に reload）。
  const pendingState = useAsync(
    () => repository.listOrderRequests(session.session_id, "pending"),
    [repository, session.session_id],
  );
  const pendingCount = pendingState.data?.length ?? 0;

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="セッション一覧" />
      <View style={[styles.row, { justifyContent: "space-between" }]}>
        <Text style={styles.title}>{session.label ?? session.session_id.slice(0, 8)}</Text>
        <Text
          style={{
            color: session.status === "open" ? colors.ok : colors.muted,
            fontWeight: "600",
            fontSize: 13,
          }}
        >
          {session.status === "open" ? "● open" : "closed"}
        </Text>
      </View>

      <View style={[styles.row, { marginTop: 8, marginBottom: 12, flexWrap: "wrap" }]}>
        <TabButton label="会計" active={tab === "ledger"} onPress={() => setTab("ledger")} />
        <TabButton
          label={pendingCount > 0 ? `注文 (${pendingCount})` : "注文"}
          active={tab === "orders"}
          onPress={() => setTab("orders")}
        />
        <TabButton label="座席" active={tab === "seating"} onPress={() => setTab("seating")} />
        <TabButton label="ハンド" active={tab === "hand"} onPress={() => setTab("hand")} />
      </View>

      {tab === "ledger" ? (
        <LedgerTab repository={repository} session={session} players={players} resolveName={resolveName} />
      ) : tab === "orders" ? (
        <OrdersTab
          repository={repository}
          session={session}
          resolveName={resolveName}
          onChanged={pendingState.reload}
        />
      ) : tab === "seating" ? (
        <SeatingTab
          repository={repository}
          session={session}
          players={players}
          resolveName={resolveName}
          reloadPlayers={playersState.reload}
        />
      ) : (
        <HandTab repository={repository} session={session} />
      )}
    </View>
  );
}

function TabButton(props: {
  label: string;
  active: boolean;
  onPress: () => void;
}): React.JSX.Element {
  return (
    <Pressable
      onPress={props.onPress}
      style={{
        paddingVertical: 8,
        paddingHorizontal: 18,
        marginRight: 8,
        borderRadius: 8,
        backgroundColor: props.active ? colors.accent : colors.cardAlt,
      }}
    >
      <Text style={{ color: props.active ? "#0c0f12" : colors.text, fontWeight: "700", fontSize: 14 }}>
        {props.label}
      </Text>
    </Pressable>
  );
}
