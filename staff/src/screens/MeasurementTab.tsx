import React, { useCallback, useMemo, useState } from "react";
import { Modal, Pressable, ScrollView, Text, TextInput, View } from "react-native";

import type { StaffRepository } from "../api/repository";
import type {
  GroundTruthEditPayload,
  MeasurementRow,
  StaffSession,
} from "../api/types";
import { StaffApiError } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { Button, Chip, colors, ErrorView, Field, Loading, styles, yen } from "./common";

/**
 * 計測タブ（ADR-0043）: Phase A 捕捉精度の ground truth を triage で記録する。
 *
 * - 行: hand_id + winner_seat + winner chip won + needs_review バッジ + 「✓ 流す」「✏ 修正」
 * - 一括: 「表示中の全件を流す」（needs_review 入りは skip）
 * - C-2 ガード（ADR-0043 §3）: has_needs_review=true の行は 「✓ 流す」を無効化、強制 drill-in
 * - polling: 5 秒間隔で listMeasurementRows を refetch（タブ表示中のみ）
 * - 「✏ 修正」モーダル: winner_seat / board / notes の override（action 訂正は ADR-0036 経由）
 */
export function MeasurementTab(props: {
  repository: StaffRepository;
  session: StaffSession;
  annotator?: string;
}): React.JSX.Element {
  const { repository, session, annotator = "staff" } = props;
  const [needsReviewOnly, setNeedsReviewOnly] = useState(false);
  const [editTarget, setEditTarget] = useState<MeasurementRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<{ text: string; ok: boolean } | null>(null);

  const rowsState = useAsync(
    () => repository.listMeasurementRows(session.session_id),
    [repository, session.session_id],
  );

  // 5 秒 polling（タブ表示中のみ）。useAsync の reload を叩く。
  React.useEffect(() => {
    const id = setInterval(() => {
      rowsState.reload();
    }, 5000);
    return () => clearInterval(id);
  }, [rowsState]);

  const rows = rowsState.data ?? [];
  const visible = useMemo(
    () => (needsReviewOnly ? rows.filter((r) => r.has_needs_review) : rows),
    [rows, needsReviewOnly],
  );
  const pending = rows.filter((r) => r.ground_truth === null).length;
  const passableVisible = visible.filter(
    (r) => r.ground_truth === null && !r.has_needs_review,
  );

  const showFlash = useCallback(
    (text: string, ok: boolean): void => {
      setFlash({ text, ok });
      setTimeout(() => setFlash(null), 2000);
    },
    [],
  );

  const passThrough = useCallback(
    async (hand_id: number): Promise<void> => {
      setBusy(true);
      try {
        await repository.passThroughGroundTruth(session.session_id, hand_id, annotator);
        showFlash(`Hand #${hand_id}: ✓ 流しました`, true);
        await rowsState.reload();
      } catch (err: unknown) {
        showFlash(err instanceof StaffApiError ? err.message : String(err), false);
      } finally {
        setBusy(false);
      }
    },
    [repository, session.session_id, annotator, rowsState, showFlash],
  );

  const passAllVisible = useCallback(async (): Promise<void> => {
    if (passableVisible.length === 0) return;
    setBusy(true);
    try {
      for (const r of passableVisible) {
        await repository.passThroughGroundTruth(session.session_id, r.hand_id, annotator);
      }
      showFlash(`${passableVisible.length} 件を流しました`, true);
      await rowsState.reload();
    } catch (err: unknown) {
      showFlash(err instanceof StaffApiError ? err.message : String(err), false);
    } finally {
      setBusy(false);
    }
  }, [repository, session.session_id, annotator, passableVisible, rowsState, showFlash]);

  const submitEdit = useCallback(
    async (handId: number, payload: GroundTruthEditPayload): Promise<void> => {
      setBusy(true);
      try {
        await repository.submitGroundTruthEdit(
          session.session_id, handId, payload, annotator,
        );
        showFlash(`Hand #${handId}: ✏ 修正を記録しました`, true);
        setEditTarget(null);
        await rowsState.reload();
      } catch (err: unknown) {
        showFlash(err instanceof StaffApiError ? err.message : String(err), false);
      } finally {
        setBusy(false);
      }
    },
    [repository, session.session_id, annotator, rowsState, showFlash],
  );

  if (rowsState.loading && rows.length === 0) return <Loading />;
  if (rowsState.errorCode && rows.length === 0) {
    return (
      <ErrorView
        code={rowsState.errorCode}
        message={rowsState.errorMessage}
        onRetry={() => rowsState.reload()}
      />
    );
  }

  return (
    <View style={{ flex: 1 }}>
      <View style={[styles.row, { marginBottom: 8 }]}>
        <Text style={[styles.sectionTitle, { flex: 1 }]}>
          未処理 {pending} 件 / 全 {rows.length} 件
        </Text>
        <Chip
          label="⚠ needs_review のみ"
          selected={needsReviewOnly}
          onPress={() => setNeedsReviewOnly((v) => !v)}
        />
      </View>
      {flash ? (
        <Text
          style={{
            color: flash.ok ? colors.ok : colors.neg,
            fontSize: 13, marginBottom: 8, textAlign: "center",
          }}
        >
          {flash.text}
        </Text>
      ) : null}
      <ScrollView style={{ flex: 1 }}>
        {visible.length === 0 ? (
          <Text style={styles.empty}>
            {rows.length === 0 ? "captured hand がありません。" : "条件に合うハンドがありません。"}
          </Text>
        ) : (
          visible.map((r) => (
            <Row
              key={r.hand_id}
              row={r}
              busy={busy}
              onPassThrough={() => passThrough(r.hand_id)}
              onEdit={() => setEditTarget(r)}
            />
          ))
        )}
      </ScrollView>
      <View style={{ paddingVertical: 12 }}>
        <Button
          label={
            passableVisible.length > 0
              ? `✓ 表示中の ${passableVisible.length} 件を流す`
              : "（流せる行がありません）"
          }
          onPress={passAllVisible}
          disabled={busy || passableVisible.length === 0}
        />
      </View>
      <EditModal
        target={editTarget}
        onClose={() => setEditTarget(null)}
        onSubmit={submitEdit}
        busy={busy}
      />
    </View>
  );
}

function Row(props: {
  row: MeasurementRow;
  busy: boolean;
  onPassThrough: () => void;
  onEdit: () => void;
}): React.JSX.Element {
  const { row, busy, onPassThrough, onEdit } = props;
  const passDisabled = busy || row.has_needs_review || row.ground_truth !== null;
  const passLabel = row.ground_truth
    ? row.ground_truth.source === "captured-passthrough"
      ? "✓ 流し済"
      : "✏ 修正済"
    : "✓ 流す";

  return (
    <View style={styles.card}>
      <View style={[styles.row, { marginBottom: 6 }]}>
        <Text style={[styles.cardTitle, { flex: 1 }]}>Hand #{row.hand_id}</Text>
        {row.has_needs_review ? (
          <Text style={{ color: colors.warn, fontSize: 12, fontWeight: "600" }}>
            ⚠ needs_review
          </Text>
        ) : null}
      </View>
      <Text style={styles.cardMeta}>
        Winner: 席 {row.winner_seat ?? "-"}
        {row.winner_result !== null ? `（${yen(row.winner_result)}）` : ""}
      </Text>
      {row.ground_truth ? (
        <Text style={[styles.cardMeta, { color: colors.ok }]}>
          GT 記録済: {row.ground_truth.source === "captured-passthrough" ? "流す" : "修正"}{" "}
          ({row.ground_truth.annotator})
        </Text>
      ) : null}
      <View style={[styles.row, { marginTop: 10 }]}>
        <Button
          label={passLabel}
          onPress={onPassThrough}
          disabled={passDisabled}
          style={{ flex: 1, marginRight: 6 }}
        />
        <Button
          label="✏ 修正"
          onPress={onEdit}
          kind="ghost"
          disabled={busy}
          style={{ flex: 1, marginLeft: 6 }}
        />
      </View>
    </View>
  );
}

function EditModal(props: {
  target: MeasurementRow | null;
  onClose: () => void;
  onSubmit: (handId: number, payload: GroundTruthEditPayload) => void;
  busy: boolean;
}): React.JSX.Element {
  const { target, onClose, onSubmit, busy } = props;
  const [winnerSeatText, setWinnerSeatText] = useState("");
  const [boardText, setBoardText] = useState("");
  const [notesText, setNotesText] = useState("");

  React.useEffect(() => {
    setWinnerSeatText(target?.winner_seat?.toString() ?? "");
    setBoardText("");
    setNotesText("");
  }, [target]);

  const handleSubmit = (): void => {
    if (target === null) return;
    const payload: GroundTruthEditPayload = { hand_id: target.hand_id };
    const winner = Number.parseInt(winnerSeatText.trim(), 10);
    if (Number.isInteger(winner)) payload.winner_seat = winner;
    const board = boardText.trim().split(/\s+/).filter((s) => s.length > 0);
    if (board.length > 0) payload.board = board;
    if (notesText.trim()) payload.notes = notesText.trim();
    onSubmit(target.hand_id, payload);
  };

  return (
    <Modal
      visible={target !== null}
      transparent
      animationType="fade"
      onRequestClose={onClose}
    >
      <View
        style={{
          flex: 1, backgroundColor: "rgba(0,0,0,0.6)",
          justifyContent: "center", padding: 24,
        }}
      >
        <View style={{ backgroundColor: colors.card, borderRadius: 12, padding: 18 }}>
          <Text style={styles.cardTitle}>✏ Hand #{target?.hand_id} を修正</Text>
          <Text style={[styles.cardMeta, { marginBottom: 12 }]}>
            空欄は GT に書きません（action 訂正は別画面 / ADR-0036）。
          </Text>
          <Field
            label="正しい winner_seat（1〜9, 空欄なら不変）"
            value={winnerSeatText}
            onChangeText={setWinnerSeatText}
            keyboardType="number-pad"
            placeholder={target?.winner_seat?.toString() ?? ""}
            style={{ marginBottom: 12 }}
          />
          <Field
            label="正しい board（空白区切り、例: As Kc Qd 5h 2s）"
            value={boardText}
            onChangeText={setBoardText}
            placeholder="（空欄なら不変）"
            style={{ marginBottom: 12 }}
          />
          <Text style={styles.label}>メモ</Text>
          <TextInput
            style={[styles.input, { minHeight: 64, marginBottom: 16 }]}
            value={notesText}
            onChangeText={setNotesText}
            placeholder="（任意）"
            placeholderTextColor={colors.muted}
            multiline
          />
          <View style={styles.row}>
            <Pressable
              onPress={onClose}
              style={{ flex: 1, paddingVertical: 10, alignItems: "center" }}
            >
              <Text style={{ color: colors.muted, fontSize: 14 }}>キャンセル</Text>
            </Pressable>
            <Button
              label="送信"
              onPress={handleSubmit}
              disabled={busy}
              style={{ flex: 2 }}
            />
          </View>
        </View>
      </View>
    </Modal>
  );
}
