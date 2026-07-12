import React from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player, PlayerSessionSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { HandReplay } from "../shared/hand_replay/HandReplay";
import { BackLink, ErrorView, Loading, formatResult, styles } from "./common";
import { findOwnRow } from "./MyHandsScreen";

interface Props {
  repository: ViewerRepository;
  player: Player;
  session: PlayerSessionSummary;
  handId: number;
  onBack: () => void;
  /** B4 (ADR-0036): スタッフ訂正画面へ（iPad）。省略時は訂正導線を出さない。 */
  onCorrect?: () => void;
}

export function HandDetailScreen({
  repository, player, session, handId, onBack, onCorrect,
}: Props): React.JSX.Element {
  const { data: hand, loading, errorCode, errorMessage } = useAsync(
    () => repository.getHand(session.session_id, handId),
    [repository, session.session_id, handId],
  );

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="ハンド一覧" />
      <Text style={styles.title}>Hand #{handId}</Text>
      {loading ? (
        <Loading />
      ) : errorCode || !hand ? (
        <ErrorView code={errorCode} message={errorMessage} />
      ) : (
        <ScrollView>
          <Text style={styles.subtitle}>
            {hand.started_at}
            {(() => {
              const own = findOwnRow(hand, player);
              if (!own) return "";
              return ` ・ 自分: 席${own.seat} 収支 ${formatResult(own.result)}`;
            })()}
          </Text>

          {/* ストリート単位リプレイ（共有コンポーネント, ADR-0044。訂正適用済みビュー） */}
          <HandReplay hand={hand} />

          {onCorrect ? (
            <Pressable style={styles.card} onPress={onCorrect}>
              <Text style={[styles.cardTitle, { color: "#5ab0f0" }]}>✎ このハンドを訂正（スタッフ）</Text>
              <Text style={styles.cardMeta}>誤認識のアクション種別・金額を訂正します</Text>
            </Pressable>
          ) : null}
          <View style={{ height: 40 }} />
        </ScrollView>
      )}
    </View>
  );
}
