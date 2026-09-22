import React, { useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { StaffSession } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, Button, colors, ErrorView, Field, Loading, styles, yen } from "./common";

/** 営業日サマリ 1 卓分（compute_settlement のクライアント側集計, 中間値）。 */
interface DaySessionRow {
  session: StaffSession;
  players: number;
  cashIn: number;
  orders: number;
  entryFees: number;
  net: number;
}

/** 端末ローカル日付の YYYY-MM-DD（started_at は naive local ISO なので prefix 比較でよい）。 */
function localDateKey(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** session（卓）一覧 + 作成 / close + 本日の集計。選ぶと会計・注文・座席を扱う TableView に遷移する。 */
export function SessionListScreen(props: {
  repository: StaffRepository;
  onSelect: (session: StaffSession) => void;
  onOpenPlayers: () => void;
  onOpenMenu: () => void;
  onLogout: () => void;
}): React.JSX.Element {
  const { repository, onSelect, onOpenPlayers, onOpenMenu, onLogout } = props;
  const state = useAsync(() => repository.listSessions(), [repository]);
  const [label, setLabel] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [dayRows, setDayRows] = useState<DaySessionRow[] | null>(null);
  const [dayBusy, setDayBusy] = useState(false);
  const [dayError, setDayError] = useState<string | null>(null);

  // 本日の集計: 当日開始の全 session の中間集計（compute_settlement）をクライアント側で合算する。
  // 締め作業の目安であり確定値ではない（確定は各卓の精算 commit）。
  const computeDaySummary = async (): Promise<void> => {
    setDayBusy(true);
    setDayError(null);
    try {
      const sessions = state.data ?? (await repository.listSessions());
      const today = localDateKey(new Date());
      const todays = sessions.filter((s) => s.started_at.slice(0, 10) === today);
      const rows: DaySessionRow[] = [];
      for (const s of todays) {
        const settlements = await repository.getSettlement(s.session_id);
        rows.push({
          session: s,
          players: settlements.length,
          cashIn: settlements.reduce((a, r) => a + r.cash_in_total, 0),
          orders: settlements.reduce((a, r) => a + r.order_total, 0),
          entryFees: settlements.reduce((a, r) => a + r.entry_fee, 0),
          net: settlements.reduce((a, r) => a + r.net_due_to_store, 0),
        });
      }
      setDayRows(rows);
    } catch (err) {
      setDayError(err instanceof StaffApiError ? err.message : String(err));
    } finally {
      setDayBusy(false);
    }
  };

  const onCreate = async (): Promise<void> => {
    try {
      const s = await repository.createSession(label.trim() || undefined);
      setLabel("");
      state.reload();
      setMsg({ text: `セッションを作成しました: ${s.label ?? s.session_id.slice(0, 8)}`, ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  const onClose = async (sessionId: string): Promise<void> => {
    try {
      await repository.closeSession(sessionId);
      state.reload();
      setMsg({ text: "セッションを close しました。", ok: true });
    } catch (err: unknown) {
      setMsg({ text: err instanceof StaffApiError ? err.message : String(err), ok: false });
    }
  };

  return (
    <View style={styles.screen}>
      <View style={[styles.row, { justifyContent: "space-between" }]}>
        <Text style={styles.title}>セッション一覧</Text>
        <View style={styles.row}>
          <Pressable onPress={onOpenPlayers} style={{ marginRight: 16 }}>
            <Text style={styles.back}>プレイヤー管理</Text>
          </Pressable>
          <Pressable onPress={onOpenMenu} style={{ marginRight: 16 }}>
            <Text style={styles.back}>メニュー管理</Text>
          </Pressable>
          <Pressable onPress={onLogout}>
            <Text style={styles.back}>ログアウト</Text>
          </Pressable>
        </View>
      </View>
      <Text style={styles.subtitle}>卓を選んで会計・注文・座席を操作します。</Text>

      {/* 本日の集計（締め作業の目安。確定は各卓の精算 commit） */}
      <View style={styles.card}>
        <View style={[styles.row, { justifyContent: "space-between" }]}>
          <Text style={styles.cardTitle}>本日の集計</Text>
          <Button
            label={dayBusy ? "計算中…" : dayRows === null ? "計算する" : "再計算"}
            kind="neutral"
            onPress={() => void computeDaySummary()}
            disabled={dayBusy}
          />
        </View>
        {dayError ? (
          <Text style={[styles.status, { color: colors.neg }]}>{dayError}</Text>
        ) : dayRows === null ? (
          <Text style={styles.cardMeta}>
            当日開始の全卓の中間集計を合算します（確定値ではありません）。
          </Text>
        ) : dayRows.length === 0 ? (
          <Text style={styles.cardMeta}>本日開始のセッションはありません。</Text>
        ) : (
          <View>
            {dayRows.map((r) => (
              <Text key={r.session.session_id} style={styles.cardMeta}>
                {r.session.label ?? r.session.session_id.slice(0, 8)}
                {r.session.status === "open" ? "（進行中）" : "（closed）"} ・ {r.players} 名 ・
                net {yen(r.net)}
              </Text>
            ))}
            <Text style={[styles.cardTitle, { marginTop: 8 }]}>
              合計 {yen(dayRows.reduce((a, r) => a + r.net, 0))}
            </Text>
            <Text style={styles.cardMeta}>
              バイイン {yen(dayRows.reduce((a, r) => a + r.cashIn, 0))} ・ 注文{" "}
              {yen(dayRows.reduce((a, r) => a + r.orders, 0))} ・ 参加費{" "}
              {yen(dayRows.reduce((a, r) => a + r.entryFees, 0))}
            </Text>
          </View>
        )}
      </View>

      <View style={[styles.card]}>
        <View style={styles.row}>
          <Field label="新規セッション名" value={label} onChangeText={setLabel} placeholder="例: 土曜ナイト #5" />
          <View style={{ width: 8 }} />
          <Button label="作成" onPress={onCreate} style={{ alignSelf: "flex-end" }} />
        </View>
        {msg ? (
          <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg }]}>{msg.text}</Text>
        ) : null}
      </View>

      {state.loading ? (
        <Loading />
      ) : state.errorCode ? (
        <ErrorView code={state.errorCode} message={state.errorMessage} onRetry={state.reload} />
      ) : !state.data || state.data.length === 0 ? (
        <Text style={styles.empty}>セッションがありません。上で作成してください。</Text>
      ) : (
        <ScrollView>
          {state.data.map((s) => (
            <View key={s.session_id} style={styles.card}>
              <Pressable onPress={() => onSelect(s)}>
                <View style={[styles.row, { justifyContent: "space-between" }]}>
                  <Text style={styles.cardTitle}>{s.label ?? s.session_id.slice(0, 8)}</Text>
                  <Text
                    style={{
                      color: s.status === "open" ? colors.ok : colors.muted,
                      fontWeight: "600",
                      fontSize: 13,
                    }}
                  >
                    {s.status === "open" ? "● open" : "closed"}
                  </Text>
                </View>
                <Text style={styles.cardMeta}>
                  {s.started_at.replace("T", " ")}
                  {s.blinds ? `　SB ${s.blinds.sb ?? "?"} / BB ${s.blinds.bb ?? "?"}` : ""}
                </Text>
              </Pressable>
              <View style={[styles.row, { marginTop: 8 }]}>
                <Button label="開く" onPress={() => onSelect(s)} />
                {s.status === "open" ? (
                  <>
                    <View style={{ width: 8 }} />
                    <Button label="close" kind="ghost" onPress={() => onClose(s.session_id)} />
                  </>
                ) : null}
              </View>
            </View>
          ))}
        </ScrollView>
      )}
      <View style={{ height: 8 }} />
      <BackLink onPress={state.reload} label="再読込" />
    </View>
  );
}
