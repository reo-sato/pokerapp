import React, { useState } from "react";
import { Pressable, Share, Text, View } from "react-native";

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

  const own = hand ? findOwnRow(hand, player) : null;

  return (
    <View style={styles.screen}>
      <View style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center" }}>
        <BackLink onPress={onBack} label="ハンド一覧" />
        <ReloadLink onPress={reload} />
      </View>
      {/* 1 画面に収めるため、ハンド番号と自分の収支は 1 行に畳む（ADR-0051） */}
      <View style={{ flexDirection: "row", alignItems: "baseline", marginBottom: 6 }}>
        <Text style={[styles.title, { marginBottom: 0 }]}>Hand #{handId}</Text>
        {own ? (
          <Text style={[styles.subtitle, { marginLeft: 10, marginBottom: 0 }]}>
            自分: 席{own.seat} 収支 {formatResult(own.result)}
          </Text>
        ) : null}
      </View>
      {loading ? (
        <Loading />
      ) : errorCode || !hand ? (
        <ErrorView code={errorCode} message={errorMessage} onRetry={reload} />
      ) : (
        <>
          {/* テーブル + 4 ストリート列（共有コンポーネント, ADR-0051。訂正適用済みビュー） */}
          <HandReplay hand={hand} selfSeat={own?.seat} />

          {/* フッタ: 画面を食わないよう横並びの小ボタンにする */}
          <View style={{ flexDirection: "row", alignItems: "center", paddingVertical: 10 }}>
            <Pressable onPress={() => void onShare()}>
              <Text style={[styles.back, { marginRight: 18, marginBottom: 0 }]}>📤 共有 / コピー</Text>
            </Pressable>
            {onCorrect ? (
              <Pressable onPress={onCorrect}>
                <Text style={[styles.back, { marginBottom: 0 }]}>✎ 訂正（スタッフ）</Text>
              </Pressable>
            ) : null}
            {shareMsg ? (
              <Text style={[styles.cardMeta, { marginLeft: "auto" }]} numberOfLines={1}>
                {shareMsg}
              </Text>
            ) : null}
          </View>
        </>
      )}
    </View>
  );
}
