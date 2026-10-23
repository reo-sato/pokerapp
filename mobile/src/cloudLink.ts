/**
 * 閲覧リンク（ADR-0067 D5）: URL の `#link=<乱数>` を取り出す。
 *
 * 乱数は `#` の後ろにある（サーバーへは送られない = アクセスログに残らない）。取り出したら
 * アドレスバーからも消す（画面を人に見せたり、URL をコピーして送ったりしても漏れないように）。
 */
export function parseLinkFragment(hash: string): string | null {
  const body = hash.startsWith("#") ? hash.slice(1) : hash;
  for (const part of body.split("&")) {
    const [key, value] = part.split("=", 2);
    if (key === "link" && value) {
      const link = decodeURIComponent(value);
      return /^pkl_[A-Za-z0-9_-]{10,200}$/.test(link) ? link : null;
    }
  }
  return null;
}

interface LocationLike {
  hash: string;
  pathname: string;
  search: string;
}

interface HistoryLike {
  replaceState(data: unknown, unused: string, url?: string): void;
}

export function takeLinkFromLocation(
  loc: LocationLike | undefined = (globalThis as { location?: LocationLike }).location,
  history: HistoryLike | undefined = (globalThis as { history?: HistoryLike }).history,
): string | null {
  if (!loc || !loc.hash) return null;
  const link = parseLinkFragment(loc.hash);
  if (link && history) {
    history.replaceState(null, "", `${loc.pathname}${loc.search}`);
  }
  return link;
}
