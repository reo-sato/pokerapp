import React, { useEffect, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type { MenuItem } from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { BackLink, Button, colors, ErrorView, Field, Loading, styles, yen } from "./common";

/** 編集中の 1 行（unit_amount は入力途中の文字列で保持する）。 */
interface DraftItem {
  item_name: string;
  unit_amount: string;
  sold_out: boolean;
}

function toDraft(items: MenuItem[]): DraftItem[] {
  return items.map((i) => ({
    item_name: i.item_name,
    unit_amount: String(i.unit_amount),
    sold_out: Boolean(i.sold_out),
  }));
}

/**
 * メニュー管理（ADR-0046）: 価格改定・品切れトグル・追加/削除。
 * 全量編集 → 「保存」で PUT /api/staff/menu（last-write-wins）。validation は core が
 * source of truth（ここでは数値化だけ行い、詳細エラーは invalid_menu の message を表示）。
 */
export function MenuScreen(props: {
  repository: StaffRepository;
  onBack: () => void;
}): React.JSX.Element {
  const { repository, onBack } = props;
  const state = useAsync(() => repository.getMenu(), [repository]);

  const [draft, setDraft] = useState<DraftItem[] | null>(null);
  const [newName, setNewName] = useState("");
  const [newPrice, setNewPrice] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  // 初回ロード（および保存後の再読込）でサーバ状態を編集バッファへ展開する。
  useEffect(() => {
    if (state.data && draft === null) setDraft(toDraft(state.data));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.data]);

  const patch = (index: number, change: Partial<DraftItem>): void => {
    setDraft((prev) =>
      prev ? prev.map((d, i) => (i === index ? { ...d, ...change } : d)) : prev,
    );
  };

  const onAdd = (): void => {
    const name = newName.trim();
    if (!name) {
      setMsg({ text: "品名を入力してください。", ok: false });
      return;
    }
    setDraft((prev) => [
      ...(prev ?? []),
      { item_name: name, unit_amount: newPrice.trim() || "0", sold_out: false },
    ]);
    setNewName("");
    setNewPrice("");
    setMsg(null);
  };

  const onSave = async (): Promise<void> => {
    if (!draft) return;
    setBusy(true);
    setMsg(null);
    try {
      const items: MenuItem[] = draft.map((d) => ({
        item_name: d.item_name.trim(),
        unit_amount: Number(d.unit_amount),
        ...(d.sold_out ? { sold_out: true } : {}),
      }));
      const saved = await repository.updateMenu(items);
      setDraft(toDraft(saved));
      setMsg({ text: `保存しました（${saved.length} 品）。`, ok: true });
    } catch (err) {
      setMsg({
        text: err instanceof StaffApiError ? err.message : String(err),
        ok: false,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <BackLink onPress={onBack} label="セッション一覧" />
      <View style={[styles.row, { justifyContent: "space-between" }]}>
        <Text style={styles.title}>メニュー管理</Text>
        <Button label={busy ? "保存中…" : "保存"} onPress={() => void onSave()} disabled={busy || !draft} />
      </View>
      <Text style={styles.subtitle}>
        価格・品切れを編集して「保存」。品切れは注文アプリで注文不可になります（確定時単価の
        prefill もここの値）。
      </Text>

      {msg ? (
        <Text style={[styles.status, { color: msg.ok ? colors.ok : colors.neg, marginBottom: 8 }]}>
          {msg.text}
        </Text>
      ) : null}

      {state.loading && draft === null ? (
        <Loading />
      ) : state.errorCode && draft === null ? (
        <ErrorView code={state.errorCode} message={state.errorMessage} onRetry={state.reload} />
      ) : (
        <ScrollView keyboardShouldPersistTaps="handled">
          {(draft ?? []).length === 0 ? (
            <Text style={styles.empty}>メニューが空です。下で追加してください。</Text>
          ) : (
            (draft ?? []).map((d, i) => (
              <View key={i} style={styles.card}>
                <View style={[styles.row, { justifyContent: "space-between" }]}>
                  <Text style={[styles.cardTitle, d.sold_out && { color: colors.muted }]}>
                    {d.item_name}
                    {d.sold_out ? "（品切れ）" : ""}
                  </Text>
                  <Text style={styles.cardMeta}>
                    {Number.isInteger(Number(d.unit_amount)) ? yen(Number(d.unit_amount)) : "—"}
                  </Text>
                </View>
                <View style={[styles.row, { marginTop: 8 }]}>
                  <Field
                    label="単価 (円)"
                    value={d.unit_amount}
                    onChangeText={(v) => patch(i, { unit_amount: v })}
                    keyboardType="number-pad"
                  />
                  <View style={{ width: 8 }} />
                  <Button
                    label={d.sold_out ? "品切れ解除" : "品切れにする"}
                    kind={d.sold_out ? "neutral" : "ghost"}
                    onPress={() => patch(i, { sold_out: !d.sold_out })}
                    style={{ alignSelf: "flex-end" }}
                  />
                  <View style={{ width: 8 }} />
                  <Button
                    label="削除"
                    kind="danger"
                    onPress={() =>
                      setDraft((prev) => (prev ? prev.filter((_, j) => j !== i) : prev))
                    }
                    style={{ alignSelf: "flex-end" }}
                  />
                </View>
              </View>
            ))
          )}

          <View style={styles.card}>
            <Text style={styles.cardTitle}>品を追加</Text>
            <View style={[styles.row, { marginTop: 4 }]}>
              <Field label="品名" value={newName} onChangeText={setNewName} placeholder="例: ハイボール" />
              <View style={{ width: 8 }} />
              <Field
                label="単価 (円)"
                value={newPrice}
                onChangeText={setNewPrice}
                keyboardType="number-pad"
                placeholder="600"
              />
              <View style={{ width: 8 }} />
              <Button label="追加" kind="neutral" onPress={onAdd} style={{ alignSelf: "flex-end" }} />
            </View>
            <Text style={[styles.cardMeta, { marginTop: 6 }]}>
              追加・削除・価格変更は「保存」を押すまで反映されません。
            </Text>
          </View>
          <View style={{ height: 40 }} />
        </ScrollView>
      )}
      <Pressable
        onPress={() => {
          setDraft(null);
          state.reload();
        }}
      >
        <Text style={styles.back}>↻ サーバの内容を再読込（編集を破棄）</Text>
      </Pressable>
    </View>
  );
}
