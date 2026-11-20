import React, { useEffect, useState } from "react";
import { ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { LoggerSetup, LoggerSetupResponse } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import {
  applyResume,
  errorMap,
  initialSetup,
  levelLabels,
  setName,
  setStack,
  toggleSeat,
  toPayload,
} from "../logger/setupForm";
import { BackLink, Button, Chip, colors, ErrorView, Field, Loading, styles } from "./common";

const SEATS = [1, 2, 3, 4, 5, 6, 7, 8, 9];

function Err(props: { text?: string }): React.JSX.Element | null {
  return props.text ? <Text style={[styles.cardMeta, { color: colors.neg }]}>{props.text}</Text> : null;
}

/**
 * 卓を始める（ADR-0073 段階 C）: 卓の設定を入れて、店舗サーバーにハンドロガーを起動させる。
 * PC の設定画面（127.0.0.1:8794）と同じ形・同じ検証（店舗サーバーが build_session_config で確かめ、誤りは項目の横に出す）。
 * ストラクチャーの編集は PC の設定画面（ここは一覧から選ぶだけ）。
 */
export function LoggerStartScreen(props: {
  repository: StaffRepository;
  onBack: () => void;
  onStarted: () => void;
}): React.JSX.Element {
  const { repository, onBack, onStarted } = props;
  const setupState = useAsync<LoggerSetupResponse>(() => repository.getLoggerSetup(), [repository]);
  const playersState = useAsync(() => repository.listPlayers(), [repository]);
  const [form, setForm] = useState<LoggerSetup | null>(null);
  const [seatForName, setSeatForName] = useState<number | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (setupState.data && form === null) setForm(initialSetup(setupState.data));
  }, [setupState.data, form]);

  if (setupState.loading && !setupState.data) return <Loading />;
  if (setupState.errorCode && !setupState.data) {
    return (
      <View style={styles.screen}>
        <BackLink onPress={onBack} label="セッション一覧" />
        <ErrorView code={setupState.errorCode} message={setupState.errorMessage} onRetry={setupState.reload} />
      </View>
    );
  }
  if (!form || !setupState.data) return <Loading />;
  const setup = setupState.data;
  const players = (playersState.data ?? []).filter((p) => !p.merged_into);
  const structure = setup.structures.find((s) => s.name === form.structure);
  const update = (next: LoggerSetup): void => setForm(next);

  const onSubmit = (): void => {
    setBusy(true);
    setErrors({});
    setMsg(null);
    repository
      .startLogger(toPayload(form))
      .then(() => onStarted())
      .catch((e) => {
        if (e instanceof StaffApiError) {
          setErrors(errorMap(e.errors));
          setMsg(e.message);
        } else {
          setMsg(String(e));
        }
      })
      .finally(() => setBusy(false));
  };

  return (
    <ScrollView style={{ flex: 1, backgroundColor: colors.bg }} contentContainerStyle={[styles.screen, { paddingBottom: 48 }]}>
      <BackLink onPress={onBack} label="セッション一覧" />
      <Text style={styles.title}>卓を始める</Text>
      <Text style={styles.subtitle}>
        店舗 PC のハンドロガーを起動します（前回の設定で開いています）。音声認識の準備に数十秒かかります。
      </Text>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>モード</Text>
        <View style={[styles.row, { flexWrap: "wrap", marginTop: 8 }]}>
          <Chip label="リングゲーム" selected={form.mode === "ring"} onPress={() => update({ ...form, mode: "ring" })} />
          <Chip label="トーナメント" selected={form.mode === "tournament"}
                onPress={() => update({ ...form, mode: "tournament" })} />
          <Chip label="持ち点を数えない" selected={form.stackless}
                onPress={() => update({ ...form, stackless: !form.stackless })} />
        </View>
        <Err text={errors.mode} />
        {form.mode === "ring" ? (
          <View style={[styles.row, { marginTop: 8 }]}>
            <Field label="SB" value={String(form.sb)} keyboardType="number-pad" style={{ marginRight: 8 }}
                   onChangeText={(t) => update({ ...form, sb: Number(t) || 0 })} />
            <Field label="BB" value={String(form.bb)} keyboardType="number-pad"
                   onChangeText={(t) => update({ ...form, bb: Number(t) || 0 })} />
          </View>
        ) : (
          <View style={{ marginTop: 8 }}>
            <Text style={styles.label}>ストラクチャー（編集は PC の設定画面）</Text>
            <View style={[styles.row, { flexWrap: "wrap" }]}>
              {setup.structures.map((s) => (
                <Chip key={s.name} label={s.name} selected={form.structure === s.name}
                      onPress={() => update({ ...form, structure: s.name })} />
              ))}
              {setup.structures.length === 0 ? (
                <Text style={styles.cardMeta}>ストラクチャーがありません（PC の設定画面で作ります）</Text>
              ) : null}
            </View>
            <Err text={errors.structure} />
            <View style={[styles.row, { marginTop: 4 }]}>
              <Field label="始めるレベル" value={String(form.level)} keyboardType="number-pad" style={{ marginRight: 8 }}
                     onChangeText={(t) => update({ ...form, level: Number(t) || 1, start_time: "" })} />
              <Field label="経過分" value={String(form.elapsed_min)} keyboardType="number-pad"
                     onChangeText={(t) => update({ ...form, elapsed_min: Number(t) || 0, start_time: "" })} />
            </View>
            <Err text={errors.level ?? errors.elapsed_min ?? errors.start_time} />
            {levelLabels(structure).slice(Math.max(0, form.level - 1), form.level + 1).map((l) => (
              <Text key={l} style={styles.cardMeta}>{l}</Text>
            ))}
            {setup.resume ? (
              <Button label={`前回の時計から続ける（${setup.resume.text ?? `レベル ${setup.resume.level}`}）`} kind="neutral"
                      style={{ marginTop: 8, alignSelf: "flex-start" }}
                      onPress={() => update(applyResume(form, setup.resume))} />
            ) : null}
          </View>
        )}
        <Err text={errors.sb ?? errors.bb} />
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>使う席</Text>
        <View style={[styles.row, { flexWrap: "wrap", marginTop: 8 }]}>
          {SEATS.map((seat) => (
            <Chip key={seat} label={`席${seat}`} selected={form.seats.includes(seat)}
                  onPress={() => update(toggleSeat(form, seat))} />
          ))}
        </View>
        <Err text={errors.seats} />
        {!form.stackless ? (
          <Field label="持ち点（席ごとに入れなければこの値）" value={String(form.default_stack)} keyboardType="number-pad"
                 style={{ marginTop: 4 }} onChangeText={(t) => update({ ...form, default_stack: Number(t) || 0 })} />
        ) : null}
        {form.seats.map((seat) => (
          <View key={seat} style={{ marginTop: 10 }}>
            <View style={styles.row}>
              <Field label={`席${seat} の名前`} value={form.names[String(seat)] ?? ""} placeholder={`Player${seat}`}
                     style={{ marginRight: 8 }} onChangeText={(t) => update(setName(form, seat, t))} />
              {!form.stackless ? (
                <Field label="持ち点" value={form.stacks[String(seat)] != null ? String(form.stacks[String(seat)]) : ""}
                       placeholder={String(form.default_stack)} keyboardType="number-pad" style={{ maxWidth: 120 }}
                       onChangeText={(t) => update(setStack(form, seat, t))} />
              ) : null}
            </View>
            <Button label={seatForName === seat ? "閉じる" : "登録済みのお客さんから"} kind="ghost"
                    style={{ marginTop: 6, alignSelf: "flex-start" }}
                    onPress={() => setSeatForName(seatForName === seat ? null : seat)} />
            {seatForName === seat ? (
              <View style={[styles.row, { flexWrap: "wrap", marginTop: 6 }]}>
                {players.map((p) => (
                  <Chip key={p.player_id} label={p.display_name}
                        selected={form.names[String(seat)] === p.display_name}
                        onPress={() => { update(setName(form, seat, p.display_name)); setSeatForName(null); }} />
                ))}
              </View>
            ) : null}
            <Err text={errors[`name_${seat}`] ?? errors[`stack_${seat}`]} />
          </View>
        ))}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>ディーラー（任意）</Text>
        <Field label="配っている人の名前" value={form.dealer ?? ""} placeholder="名前" style={{ marginTop: 4 }}
               onChangeText={(t) => update({ ...form, dealer: t })} />
        <Text style={styles.cardMeta}>ディーラーごとの聞き取りの振り返りに使います。交代はハンドのタブから（次のハンドから）。</Text>
        <Err text={errors.dealer} />
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>1 ハンド目のボタン</Text>
        <View style={[styles.row, { flexWrap: "wrap", marginTop: 8 }]}>
          {form.seats.map((seat) => (
            <Chip key={seat} label={`席${seat}`} selected={form.button === seat}
                  onPress={() => update({ ...form, button: seat })} />
          ))}
        </View>
        <Text style={styles.cardMeta}>選ばなければ、いちばん大きい番号の席です。</Text>
        <Err text={errors.button} />
      </View>

      {msg ? <Text style={[styles.status, { color: colors.neg }]}>{msg}</Text> : null}
      <Button label={busy ? "起動しています…" : "始める"} onPress={onSubmit} disabled={busy} style={{ marginTop: 8 }} />
    </ScrollView>
  );
}
