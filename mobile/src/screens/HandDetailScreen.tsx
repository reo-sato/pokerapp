import React, { useState } from "react";
import { Pressable, ScrollView, Share, Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player, PlayerSessionSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { HandReplay } from "../shared/hand_replay/HandReplay";
import { buildHandText } from "../shared/hand_replay/handReplayText";
import { BackLink, ErrorView, Loading, ReloadLink, formatResult, styles } from "./common";
import { findOwnRow } from "./MyHandsScreen";

/** navigator.clipboard（web）への安全な参照。native / 非対応環境では null。 */
function clipboardOrNull(): { writeText(text: string): Promise<void> } | null {
  const nav = (globalThis as {
    navigator?: { clipboard?: { writeText(text: string): Promise<void> } };
  }).navigator;
  return nav?.clipboard ?? null;
}

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
  const { data: hand, loading, errorCode, errorMessage, reload } = useAsync(
    () => repository.getHand(session.session_id, handId),
    [repository, session.session_id, handId],
  );
  const [shareMsg, setShareMsg] = useState<string | null>(null);

  // Phase B「書き出し」導線: OS 共有シート → 使えない環境（web の非対応ブラウザ等）は
  // クリップボードに fallback する。
  const onShare = async (): Promise<void> => {
    if (!hand) return;
    const text = buildHandText(hand);
    setShareMsg(null);
    try {
      await Share.share({ message: text });
      return;
    } catch {
      // 続けて clipboard を試す。
    }
    const clipboard = clipboardOrNull();
    if (clipboard) {
      try {
        await clipboard.writeText(text);
        setShareMsg("クリップボードにコピーしました。");
        return;
      } catch {
        // fallthrough
      }
    }
    setShareMsg("この環境では共有できませんでした。");
  };

  return (
    <View style={styles.screen}>
      <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
        <BackLink onPress={onBack} label="ハンド一覧" />
        <ReloadLink onPress={reload} />
      </View>
      <Text style={styles.title}>Hand #{handId}</Text>
      {loading ? (
        <Loading />
      ) : errorCode || !hand ? (
        <ErrorView code={errorCode} message={errorMessage} onRetry={reload} />
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

          <Pressable style={styles.card} onPress={() => void onShare()}>
            <Text style={[styles.cardTitle, { color: "#5ab0f0" }]}>📤 このハンドを共有 / コピー</Text>
            <Text style={styles.cardMeta}>
              テキストで書き出します（SNS・メモへの貼り付け用）
              {shareMsg ? ` ・ ${shareMsg}` : ""}
            </Text>
          </Pressable>

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
