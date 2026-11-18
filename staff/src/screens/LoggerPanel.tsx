import React, { useEffect, useRef, useState } from "react";
import { Pressable, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { LoggerNotice, LoggerStatusResponse } from "../api/types";
import {
  backlogLine,
  mergeNotices,
  micLine,
  noticeLabel,
  rfidLine,
  sessionLine,
  statusLine,
  type Tone,
} from "../logger/loggerView";
import { colors, styles } from "./common";

const POLL_MS = 3000;
const SHOWN = 8;

function toneColor(t: Tone): string {
  return t === "ok" ? colors.ok : t === "warn" ? colors.warn : t === "bad" ? colors.neg : colors.muted;
}

/** ロガーの様子（ADR-0073 D3）: 動いているか・マイク・RFID・お知らせ。3 秒ごとに読み直す（読むだけ）。 */
export function LoggerPanel(props: { repository: StaffRepository }): React.JSX.Element {
  const { repository } = props;
  const [status, setStatus] = useState<LoggerStatusResponse | null>(null);
  const [notices, setNotices] = useState<LoggerNotice[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const since = useRef(0);

  useEffect(() => {
    let alive = true;
    const tick = async (): Promise<void> => {
      try {
        const s = await repository.getLoggerStatus();
        const n = await repository.listLoggerNotices(since.current);
        if (!alive) return;
        since.current = n.last_seq;
        setStatus(s);
        setNotices((cur) => mergeNotices(cur, n));
        setError(null);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void tick();
    const id = setInterval(() => void tick(), POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [repository]);

  const head = statusLine(status);
  const lines = [micLine(status), rfidLine(status)].filter((x): x is { text: string; tone: Tone } => x !== null);
  const sub = sessionLine(status);
  const backlog = backlogLine(status);
  const shown = open ? notices : notices.slice(0, SHOWN);
  return (
    <View style={styles.card} testID="logger-panel">
      <View style={[styles.row, { justifyContent: "space-between" }]}>
        <Text style={styles.cardTitle}>ハンドロガー</Text>
        <Text style={{ color: toneColor(head.tone), fontWeight: "600", fontSize: 13, flexShrink: 1, textAlign: "right" }}>
          {head.text}
        </Text>
      </View>
      {sub ? <Text style={styles.cardMeta}>{sub}</Text> : null}
      {lines.map((l) => (
        <Text key={l.text.slice(0, 6)} style={[styles.cardMeta, { color: toneColor(l.tone) }]}>
          {l.text}
        </Text>
      ))}
      {backlog ? <Text style={[styles.cardMeta, { color: colors.warn }]}>{backlog}</Text> : null}
      {error ? <Text style={[styles.cardMeta, { color: colors.neg }]}>読めません: {error}</Text> : null}
      {notices.length ? (
        <View style={{ marginTop: 8 }}>
          {shown.map((n) => (
            <Text
              key={n.seq}
              style={[styles.cardMeta, { marginTop: 2, color: n.kind === "unapplied" ? colors.warn : colors.muted }]}
            >
              {noticeLabel(n)}
            </Text>
          ))}
          {notices.length > SHOWN ? (
            <Pressable onPress={() => setOpen(!open)}>
              <Text style={[styles.back, { marginTop: 6, marginBottom: 0 }]}>
                {open ? "少なく表示" : `お知らせをもっと見る（${notices.length} 件）`}
              </Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}
