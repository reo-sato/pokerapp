/**
 * 依存なしの静的サーバー（Playwright E2E 用）。`dist/`（expo export --platform web の出力）を
 * 配信する。staff アプリは web export を LAN 配信して iPad Safari で開く運用なので、E2E も
 * 同じ「ブラウザで静的バンドルを開く」経路を再現する。
 *
 * 使い方: node e2e/serve.mjs [port]（既定 4173）。EXPO_PUBLIC_API_URL を build 時に渡していない
 * 限り、アプリは MockRepository で動く（API 不要）。
 */
import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";

const port = Number(process.argv[2] ?? process.env.PORT ?? 4173);
const root = join(process.cwd(), "dist");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".ttf": "font/ttf",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function resolve(urlPath) {
  // path traversal 防止 + クエリ除去。
  const clean = normalize(decodeURIComponent(urlPath.split("?")[0])).replace(/^(\.\.[/\\])+/, "");
  let p = join(root, clean);
  if (existsSync(p) && statSync(p).isDirectory()) p = join(p, "index.html");
  if (!existsSync(p)) p = join(root, "index.html"); // SPA fallback
  return p;
}

const server = createServer((req, res) => {
  const file = resolve(req.url ?? "/");
  res.writeHead(200, { "Content-Type": MIME[extname(file)] ?? "application/octet-stream" });
  createReadStream(file).pipe(res);
});

server.listen(port, "127.0.0.1", () => {
  // eslint-disable-next-line no-console
  console.log(`staff dist served at http://127.0.0.1:${port}`);
});
