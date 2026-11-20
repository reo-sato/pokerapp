import React, { useEffect, useRef, useState } from "react";
import { Pressable, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { LoggerNotice, LoggerStatusResponse } from "../api/types";
import { StaffApiError } from "../api/types";
import {
  backlogLine,
  mergeNotices,
  asrLine,
  micLine,
  noticeLabel,
  rfidLine,
  sessionWarning,
  sessionLine,
  statusLine,
  type Tone,
} from "../logger/loggerView";
import { canStart, canStop } from "../logger/setupForm";
import { Button, colors, styles } from "./common";

const POLL_MS = 3000;
const SHOWN = 8;

function toneColor(t: Tone): string {
  return t === "ok" ? colors.ok : t === "warn" ? colors.warn : t === "bad" ? colors.neg : colors.muted;
}

/** ロガーの様子（ADR-0073 D3）: 動いているか・マイク・RFID・お知らせ。3 秒ごとに読み直す。
 * 止まっていれば「卓を始める」（`onStart` = 卓の設定の画面へ）、動いていれば「終了」（画面の中で確かめる, 段階 C）。 */
export function LoggerPanel(props: { repository: StaffRepository; onStart?: () => void }): React.JSX.Element {
  const { repository, onStart } = props;
  const [status, setStatus] = useState<LoggerStatusResponse | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopMsg, setStopMsg] = useState<string | null>(null);
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

  const onStop = (): void => {
    const sid = status?.state?.session_id;
    if (!sid) return;
    setConfirmStop(false);
    repository
      .stopLogger(sid)
      .then(() => setStopMsg("終了を送りました（確定していないハンドを閉じて保存します）"))
      .catch((e) => setStopMsg(e instanceof StaffApiError ? e.message : String(e)));
  };

  const head = statusLine(status);
  const lines = [micLine(status), asrLine(status), rfidLine(status)].filter((x): x is { text: string; tone: Tone } => x !== null);
  const sub = sessionLine(status);
  const backlog = backlogLine(status);
  const rdp = sessionWarning(status);
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
      {rdp ? <Text style={[styles.cardMeta, { color: colors.warn }]}>{rdp}</Text> : null}
      {backlog ? <Text style={[styles.cardMeta, { color: colors.warn }]}>{backlog}</Text> : null}
      {error ? <Text style={[styles.cardMeta, { color: colors.neg }]}>読めません: {error}</Text> : null}
      {onStart && canStart(status) ? (
        <Button label="卓を始める" onPress={onStart} style={{ marginTop: 10, alignSelf: "flex-start" }} />
      ) : null}
      {canStop(status) && status?.state?.mode === "headless" ? (
        confirmStop ? (
          <View style={[styles.row, { marginTop: 10, flexWrap: "wrap" }]}>
            <Text style={[styles.cardMeta, { marginRight: 10, marginTop: 0, color: colors.text }]}>本当に終了しますか？</Text>
            <Button label="終了する" kind="danger" onPress={onStop} style={{ marginRight: 8 }} />
            <Button label="やめる" kind="ghost" onPress={() => setConfirmStop(false)} />
          </View>
        ) : (
          <Button label="終了" kind="neutral" onPress={() => { setStopMsg(null); setConfirmStop(true); }}
                  style={{ marginTop: 10, alignSelf: "flex-start" }} />
        )
      ) : null}
      {canStop(status) && status?.state?.mode !== "headless" ? (
        <Text style={styles.cardMeta}>このロガーは店舗 PC（RDP）で起動しています。終了は PC の端末で q。</Text>
      ) : null}
      {stopMsg ? <Text style={[styles.cardMeta, { color: colors.warn }]}>{stopMsg}</Text> : null}
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
