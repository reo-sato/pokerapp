/** ロガーの様子の表示（ADR-0073 D3, loggerView.ts）。 */
import assert from "node:assert/strict";
import test from "node:test";

import type { LoggerStatusResponse } from "../api/types";
import { MockStaffRepository } from "../api/mockRepository";
import { VALID_STAFF_TOKEN } from "../mocks/fixtures";
import {
  asrLine,
  backlogLine,
  mergeNotices,
  micLine,
  NOTICES_KEPT,
  rfidLine,
  sessionLine,
  sessionWarning,
  statusLine,
} from "./loggerView";

const running = (over: Partial<NonNullable<LoggerStatusResponse["state"]>> = {}): LoggerStatusResponse => ({
  status: "running",
  heartbeat_age_sec: 1,
  holder_text: null,
  state: {
    phase: "running", mode: "cli", session_id: "0123456789abcdef", table: { mode: "ring", seats: [1, 2, 3], sb: 100, bb: 200 },
    mic: { state: "running", level: 0.5, device_name: "Wireless Mic Rx" }, rfid: { state: "running", connected: 10, configured: 11 },
    speech_backlog: 0, ...over,
  },
});

test("statusLine: 動作中・設定待ち・応答なし・落ちた・止まっている", () => {
  assert.deepEqual(statusLine(running()), { text: "動作中", tone: "ok" });
  assert.equal(statusLine(running({ phase: "setup" })).tone, "warn");
  assert.match(statusLine(running({ phase: "transcribing" })).text, /記録は保存済み/);
  assert.match(statusLine({ ...running(), status: "unresponsive", heartbeat_age_sec: 21.4 }).text, /21 秒/);
  assert.equal(statusLine({ ...running(), status: "crashed" }).tone, "bad");
  assert.equal(statusLine({ status: "stopped", state: null, heartbeat_age_sec: null, holder_text: null }).tone, "muted");
  assert.equal(statusLine(null).text, "読み込み中…");
});

test("sessionLine / micLine / rfidLine / backlogLine", () => {
  assert.equal(sessionLine(running()), "起動: 端末（RDP）・セッション 01234567・3 席・SB 100 / BB 200");
  assert.match(micLine(running())!.text, /Wireless Mic Rx  ■■■■■□□□□□/);
  assert.equal(micLine(running({ mic: { state: "error", error: "見つかりません" } }))!.tone, "bad");
  assert.deepEqual(rfidLine(running()), { text: "RFID: 10 / 11 台", tone: "warn" });
  assert.equal(rfidLine(running({ rfid: { state: "no_readers", configured: 11 } }))!.tone, "bad");
  assert.equal(backlogLine(running()), null);
  assert.match(backlogLine(running({ speech_backlog: 3 }))!, /聞き取り待ち 3 件/);
  assert.equal(micLine({ ...running(), status: "stopped" }), null);
});

test("asrLine: API・つながらない・鍵が無くて Whisper・Whisper", () => {
  const api = (asr: object, transcriber = "openai") => running({ transcriber, asr });
  assert.deepEqual(asrLine(api({ name: "openai-gpt-transcribe", remote: true, down: false, waiting: 0 })),
    { text: "書き起こし: OpenAI の API・gpt-transcribe", tone: "ok" });
  const down = asrLine(api({ name: "openai-gpt-transcribe", remote: true, down: true, waiting: 4 }))!;
  assert.equal(down.tone, "bad");
  assert.match(down.text, /つながりません.*送り直し待ち 4 件/);
  assert.equal(asrLine(api({ name: "openai-gpt-transcribe", remote: true, down: false, waiting: 2 }))!.tone, "warn");
  assert.match(asrLine(api({ remote: false }))!.text, /鍵がありません/);
  assert.match(asrLine(api({ remote: true, name: "openai-gpt-transcribe" }, "cloud"))!.text, /クラウドの中継/);
  assert.equal(asrLine(api({ remote: false }, "local"))!.tone, "muted");
  assert.equal(asrLine(running({ transcriber: "off" })), null);
  assert.equal(asrLine(running()), null);
});

test("sessionWarning: RDP でつないでいる・切断・コンソール", () => {
  assert.equal(sessionWarning(running({ win_session: { console: true, remote: false, state: "active" } })), null);
  assert.match(sessionWarning(running({ win_session: { console: false, remote: true, state: "active" } }))!, /RDP でつながっています/);
  assert.match(sessionWarning(running({ win_session: { console: false, remote: false, state: "disconnected" } }))!, /コンソールに戻す/);
  assert.equal(sessionWarning(running({ win_session: { platform: "linux" } })), null);
  assert.equal(sessionWarning(running()), null);
});

test("mergeNotices: 新しい順・重複なし・起動し直したら捨てる・件数の上限", () => {
  const n = (seq: number) => ({ seq, t: 0, kind: "notice", text: `n${seq}`, session_id: null });
  let cur = mergeNotices([], { notices: [n(1), n(2)], last_seq: 2, reset: false });
  cur = mergeNotices(cur, { notices: [n(2), n(3)], last_seq: 3, reset: false });
  assert.deepEqual(cur.map((x) => x.seq), [3, 2, 1]);
  assert.deepEqual(mergeNotices(cur, { notices: [n(1)], last_seq: 1, reset: true }).map((x) => x.seq), [1]);
  const many = Array.from({ length: NOTICES_KEPT + 5 }, (_, i) => n(i + 1));
  assert.equal(mergeNotices([], { notices: many, last_seq: many.length, reset: false }).length, NOTICES_KEPT);
});

test("MockStaffRepository: ロガーの様子とお知らせ（認可・since・reset）", async () => {
  const repo = new MockStaffRepository();
  await assert.rejects(repo.getLoggerStatus());
  repo.setToken(VALID_STAFF_TOKEN);
  assert.equal((await repo.getLoggerStatus()).status, "running");
  assert.deepEqual((await repo.listLoggerNotices(0)).notices.map((x) => x.seq), [1, 2, 3]);
  assert.deepEqual((await repo.listLoggerNotices(2)).notices.map((x) => x.seq), [3]);
  const after = await repo.listLoggerNotices(99);
  assert.equal(after.reset, true);
  assert.equal(after.notices.length, 3);
});
