// 一覧の並び: 新しいセッション・新しいハンドを上に（オーナー 2026-10-26）。API は作成順・ハンド番号順で返すので、画面で並べ替える。

export function newestSessionsFirst<T extends { started_at?: string | null }>(sessions: readonly T[]): T[] {
  // started_at は ISO 8601（同じ形）なので文字列の比較で時刻の順になる。無いものは下へ
  return sessions
    .map((s, i) => ({ s, i }))
    .sort((a, b) => {
      const x = a.s.started_at ?? "", y = b.s.started_at ?? "";
      return x === y ? b.i - a.i : x < y ? 1 : -1;
    })
    .map(({ s }) => s);
}

export function newestHandsFirst<T extends { hand_id: number }>(hands: readonly T[]): T[] {
  return [...hands].sort((a, b) => b.hand_id - a.hand_id);
}
