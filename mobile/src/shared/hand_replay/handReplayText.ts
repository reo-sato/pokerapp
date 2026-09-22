/**
 * ハンドのテキスト書き出し (ADR-0044 / Phase B「書き出し」導線)。
 *
 * **正本は `shared/hand_replay/`** — 編集後は `python scripts/sync_shared_ui.py` で再配布。
 *
 * リプレイ表示モデル (handReplayModel) と同じストリート分割・ボードスライス・ポット境界で
 * 共有向けプレーンテキストを組み立てる純関数。SNS / メモアプリへの貼り付けを想定し、
 * 等幅整形はしない（行頭ラベルのみで揃える）。
 */
import {
  actionLabel,
  buildReplayModel,
  formatChips,
  formatSigned,
  type ReplayHand,
} from "./handReplayModel";

/** 1 ハンドを共有用テキストにする（訂正適用済みビューを渡すこと — ADR-0036）。 */
export function buildHandText(hand: ReplayHand): string {
  const m = buildReplayModel(hand);
  // view (HandReplay) と同じ名前解決: action に player_name が無ければ席から引く。
  const nameBySeat = new Map(m.seats.map((p) => [p.seat, p.name]));
  const lines: string[] = [];

  let header = `Hand #${m.handId}`;
  if (hand.started_at) header += ` ・ ${hand.started_at}`;
  if (m.blinds?.sb != null) {
    header += ` ・ ブラインド ${formatChips(m.blinds.sb)}/${formatChips(m.blinds.bb ?? 0)}`;
  }
  lines.push(header);

  for (const p of m.seats) {
    let line = `席${p.seat} ${p.name}`;
    if (p.seat === m.winnerSeat) line += " 🏆";
    line += ` [${p.hole_cards?.length ? p.hole_cards.join(" ") : "?? ??"}]`;
    if (p.stack_start != null && p.stack_end != null) {
      line += ` ${formatChips(p.stack_start)}→${formatChips(p.stack_end)}`;
    }
    if (p.result != null) line += ` (${formatSigned(p.result)})`;
    lines.push(line);
  }

  for (const st of m.streets) {
    let head = `--- ${st.label}`;
    if (st.board.length) head += ` [${st.board.join(" ")}]`;
    head += ` (ポット ${formatChips(st.potStart)})`;
    lines.push(head);
    if (st.actions.length === 0) {
      lines.push("（アクションなし）");
      continue;
    }
    for (const a of st.actions) {
      let line = `席${a.seat}`;
      const name = a.player_name || nameBySeat.get(a.seat);
      if (name) line += ` ${name}`;
      line += ` ${actionLabel(a.action)}`;
      if (a.amount) line += ` ${formatChips(a.amount)}`;
      if (a.needs_review) line += "（要確認）";
      lines.push(line);
    }
  }

  if (m.winnerSeat != null) {
    const winner = m.seats.find((p) => p.seat === m.winnerSeat);
    let result = `結果: 🏆 席${m.winnerSeat}`;
    if (winner) result += ` ${winner.name}`;
    if (m.potTotal != null) result += ` ・ ポット合計 ${formatChips(m.potTotal)}`;
    lines.push(result);
  } else if (m.potTotal != null) {
    lines.push(`結果: ポット合計 ${formatChips(m.potTotal)}`);
  }

  return lines.join("\n");
}
