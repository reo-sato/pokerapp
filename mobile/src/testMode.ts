/**
 * 音声テスト用の表示 (ADR-0060)。画面の URL に `?test` を付けた端末だけで、ハンドの各アクションの
 * 下に「何と聞こえたか」と補正の理由を出す (お客さんの通常の表示は変えない)。native では常に false。
 */
export function isTestMode(search: string | undefined = currentSearch()): boolean {
  return /[?&]test(=|&|$)/.test(search ?? "");
}

function currentSearch(): string | undefined {
  const loc = (globalThis as { location?: { search?: string } }).location;
  return loc?.search;
}
