import React, { useEffect, useState } from "react";
import { Text, View } from "react-native";

import type { ViewerRepository } from "../api/repository";
import type { Player } from "../api/types";
import { ViewerApiError } from "../api/types";
import { Loading, styles } from "./common";

interface Props {
  repository: ViewerRepository;
  link: string | null; // URL の #link= の後ろ。無ければ「リンクから開いて」の案内だけ
  onAuthed: (player: Player) => void;
}

/**
 * 店外での閲覧の入口（クラウド, ADR-0067 D5）。店でスタッフが出した QR（閲覧リンク）で開くと、
 * その人として入り、自分の記録だけが見られる。名前を選ぶ画面は出さない（他人の記録が見えてしまうため）。
 */
export function LinkScreen({ repository, link, onAuthed }: Props): React.JSX.Element {
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!link) return;
    let alive = true;
    repository
      .linkLogin(link)
      .then((session) => repository.getPlayer(session.player_id))
      .then((player) => {
        if (alive) onAuthed(player);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(
          err instanceof ViewerApiError
            ? err.message
            : "つながりませんでした。電波の良いところで、もう一度リンクを開いてください。",
        );
      });
    return () => {
      alive = false;
    };
  }, [repository, link, onAuthed]);

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>ハンド履歴</Text>
      {link && !error ? (
        <>
          <Text style={styles.subtitle}>読み込んでいます…</Text>
          <Loading />
        </>
      ) : (
        <View style={styles.card}>
          {error ? <Text style={[styles.cardTitle, styles.neg]}>{error}</Text> : null}
          <Text style={styles.cardMeta}>
            お店でスタッフが出す QR コード（あなた専用のリンク）から開いてください。{"\n"}
            一度開けば、このスマホでは 30 日のあいだ、そのまま見られます。
          </Text>
        </View>
      )}
    </View>
  );
}
