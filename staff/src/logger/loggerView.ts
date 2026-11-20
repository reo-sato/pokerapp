/**
 * ロガーの様子の表示（ADR-0073 D3）。画面（LoggerPanel）から使う純粋な関数（試験しやすいように分ける）。
 */
import type { LoggerNotice, LoggerNoticesResponse, LoggerStatusResponse } from "../api/types";

export type Tone = "ok" | "warn" | "bad" | "muted";

/** 一番上の 1 行: 動いているか（色つき）。 */
export function statusLine(r: LoggerStatusResponse | null): { text: string; tone: Tone } {
  if (!r) return { text: "読み込み中…", tone: "muted" };
  const phase = r.state?.phase;
  switch (r.status) {
    case "running":
      if (phase === "setup") {
        return r.state?.mode === "headless"
          ? { text: "起動中（卓の設定を確かめています）", tone: "warn" }
          : { text: "卓の設定待ち（PC の設定画面で「始める」を押してください）", tone: "warn" };
      }
      if (phase === "starting") return { text: "起動中", tone: "warn" };
      if (phase === "stopping") return { text: "終了中（確定していないハンドを閉じて保存しています）", tone: "warn" };
      if (phase === "transcribing") return { text: "終了中（記録は保存済み。聞き流した発話を書き起こしています）", tone: "warn" };
      return { text: "動作中", tone: "ok" };
    case "unresponsive":
      return { text: `応答なし（心拍が ${Math.round(r.heartbeat_age_sec ?? 0)} 秒来ていません）`, tone: "bad" };
    case "crashed": {
      const hand = r.crash?.hand_id != null ? `・ハンド ${r.crash.hand_id} は記録の途中` : "";
      return { text: `止まりました（終了の手順を通らずに終わった${hand}）`, tone: "bad" };
    }
    default:
      return { text: "止まっています（ロガーは動いていません）", tone: "muted" };
  }
}

const MODE: Record<string, string> = { cli: "端末（RDP）", gui: "画面（RDP）", script: "台本", headless: "画面なし（iPad）" };

/** 2 行目: 起動の入口・セッション・卓。 */
export function sessionLine(r: LoggerStatusResponse | null): string | null {
  const s = r?.state;
  if (!s || r?.status === "stopped") return null;
  const parts = [`起動: ${MODE[s.mode] ?? s.mode}`];
  if (s.session_id) parts.push(`セッション ${s.session_id.slice(0, 8)}`);
  const t = s.table;
  if (t?.seats?.length) parts.push(`${t.seats.length} 席`);
  if (t?.mode === "tournament") parts.push("トーナメント");
  else if (t?.sb != null && t?.bb != null) parts.push(`SB ${t.sb} / BB ${t.bb}`);
  return parts.join("・");
}

/** マイク（開けているか・名前・入力の大きさ）。 */
export function micLine(r: LoggerStatusResponse | null): { text: string; tone: Tone } | null {
  const m = r?.state?.mic;
  if (!m || r?.status !== "running") return null;
  if (m.state === "disabled") return { text: "マイク: 使わない設定", tone: "muted" };
  if (m.state === "error") return { text: `マイク: 開けていません（${m.error ?? "理由不明"}）`, tone: "bad" };
  if (m.state === "muted") return { text: `マイク: 声が届いていません（${m.device_name ?? ""}）`, tone: "warn" };
  if (m.state === "running") {
    const level = Math.round(Math.max(0, Math.min(1, m.level ?? 0)) * 10);
    return { text: `マイク: ${m.device_name ?? "開けています"}  ${"■".repeat(level)}${"□".repeat(10 - level)}`, tone: "ok" };
  }
  return { text: `マイク: ${m.state ?? "不明"}`, tone: "muted" };
}

/** 書き起こし（OpenAI の API か店舗 PC の Whisper か・API につながらない間, ADR-0072）。 */
export function asrLine(r: LoggerStatusResponse | null): { text: string; tone: Tone } | null {
  const st = r?.state;
  if (!st || r?.status !== "running" || st.transcriber === "off" || st.transcriber === undefined) return null;
  const a = st.asr ?? {};
  if (a.remote) {
    const where = st.transcriber === "cloud" ? "クラウドの中継" : "OpenAI の API";
    const model = (a.name ?? "").replace(/^openai-/, "");
    if (a.down) {
      const waiting = a.waiting ? `・送り直し待ち ${a.waiting} 件` : "";
      return { text: `書き起こし: ${where}につながりません（音声は保存して送り直します${waiting}）`, tone: "bad" };
    }
    const waiting = a.waiting ? `（送り直し ${a.waiting} 件）` : "";
    return { text: `書き起こし: ${where}${model ? `・${model}` : ""}${waiting}`, tone: a.waiting ? "warn" : "ok" };
  }
  if (st.transcriber === "openai") return { text: "書き起こし: 店舗 PC の Whisper（API の鍵がありません）", tone: "warn" };
  if (st.transcriber === "cloud") return { text: "書き起こし: 店舗 PC の Whisper（クラウドにつないでいません）", tone: "warn" };
  return { text: "書き起こし: 店舗 PC の Whisper", tone: "muted" };
}

/** RFID（つながったリーダーの数）。 */
export function rfidLine(r: LoggerStatusResponse | null): { text: string; tone: Tone } | null {
  const f = r?.state?.rfid;
  if (!f || r?.status !== "running") return null;
  if (f.state === "disabled") return { text: "RFID: 使わない設定", tone: "muted" };
  if (f.state === "no_readers") return { text: `RFID: リーダーにつながっていません（0 / ${f.configured ?? "?"} 台）`, tone: "bad" };
  if (f.connected != null && f.configured != null) {
    return { text: `RFID: ${f.connected} / ${f.configured} 台`, tone: f.connected < f.configured ? "warn" : "ok" };
  }
  return { text: `RFID: ${f.state ?? "不明"}`, tone: "muted" };
}

/** RDP でつないでいる・RDP を閉じたまま（切断）のときの注意（マイクの行き先が変わることがある, ADR-0073 段階 0）。 */
export function sessionWarning(r: LoggerStatusResponse | null): string | null {
  const w = r?.state?.win_session;
  if (!w || r?.status !== "running" || w.platform) return null;
  if (w.state === "disconnected") return "RDP を閉じた状態です（声が取れないときは PC で「コンソールに戻す」）";
  if (w.remote) return "RDP でつながっています（マイクの行き先が変わることがあります）";
  return null;
}

/** 聞き取り待ちの件数（多いと iPad の操作が遅れて効く）。 */
export function backlogLine(r: LoggerStatusResponse | null): string | null {
  const n = r?.state?.speech_backlog;
  return r?.status === "running" && n != null && n > 0 ? `聞き取り待ち ${n} 件（操作はこのあとに効きます）` : null;
}

export const NOTICES_KEPT = 40;

/** お知らせを足す（起動し直したら捨てて入れ直す）。新しい順に `NOTICES_KEPT` 件。 */
export function mergeNotices(current: LoggerNotice[], got: LoggerNoticesResponse): LoggerNotice[] {
  const base = got.reset ? [] : current;
  const seen = new Set(base.map((n) => n.seq));
  const merged = [...base, ...got.notices.filter((n) => !seen.has(n.seq))];
  merged.sort((a, b) => b.seq - a.seq);
  return merged.slice(0, NOTICES_KEPT);
}

const KIND: Record<string, string> = { notice: "●", unapplied: "未適用", action: "", heard: "聞き取り", cards: "カード" };

export function noticeLabel(n: LoggerNotice): string {
  const d = new Date(n.t * 1000);
  const p = (x: number): string => String(x).padStart(2, "0");
  const tag = KIND[n.kind] ?? n.kind;
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} ${tag ? tag + " " : ""}${n.text}`;
}
