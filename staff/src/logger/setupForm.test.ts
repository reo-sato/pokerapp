/** iPad の「卓を始める」の形（ADR-0073 段階 C, setupForm.ts）。 */
import assert from "node:assert/strict";
import test from "node:test";

import type { LoggerStatusResponse } from "../api/types";
import { MockStaffRepository } from "../api/mockRepository";
import { StaffApiError } from "../api/types";
import { VALID_STAFF_TOKEN } from "../mocks/fixtures";
import { statusLine } from "./loggerView";
import {
  applyResume, canStart, canStop, errorMap, initialSetup, levelLabels, setName, setStack, toggleSeat, toPayload,
} from "./setupForm";

const STRUCTURES = [{ name: "デイリー", levels: [{ sb: 100, bb: 200, ante: 200, minutes: 20 }, { break: true, minutes: 5 },
                                               { sb: 200, bb: 400, ante: 400, minutes: 20 }] }];

test("前回の設定で開く・使わない席のボタンと名前は落とす", () => {
  const s = initialSetup({ last: { seats: [5, 2, 3], button: 9, names: { "2": "太郎" }, structure: "無い" },
                           structures: STRUCTURES, resume: null, can_start: true });
  assert.deepEqual(s.seats, [2, 3, 5]);
  assert.equal(s.button, null);
  assert.equal(s.structure, "デイリー");
  const t = toggleSeat({ ...s, button: 5 }, 5);
  assert.deepEqual(t.seats, [2, 3]);
  assert.equal(t.button, null);
  const named = setStack(setName(setName(t, 2, "花子"), 9, "居ない"), 3, "1500");
  assert.deepEqual(toPayload(named).names, { "2": "花子" });
  assert.deepEqual(toPayload(named).stacks, { "3": 1500 });
  assert.deepEqual(setName(named, 2, "  ").names, { "9": "居ない" });
});

test("前回の時計・レベルの一覧・項目ごとの誤り", () => {
  const s = applyResume(initialSetup(null), { structure: "デイリー", level: 2, elapsed_min: 7 });
  assert.equal(s.mode, "tournament");
  assert.equal(s.start_time, "");
  assert.deepEqual(levelLabels(STRUCTURES[0]), ["レベル 1: 100/200（アンテ 200）", "レベル 2: 200/400（アンテ 400）"]);
  assert.deepEqual(errorMap([{ field: "seats", message: "a" }, { field: "seats", message: "b" }]), { seats: "a" });
});

const status = (over: Partial<LoggerStatusResponse>): LoggerStatusResponse => ({
  status: "running", heartbeat_age_sec: 1, holder_text: null, can_start: true,
  state: { phase: "running", mode: "headless", session_id: "sid" }, ...over,
});

test("始める・終了のボタンを出すか", () => {
  assert.equal(canStart(status({ status: "stopped" })), true);
  assert.equal(canStart(status({ status: "crashed" })), true);
  assert.equal(canStart(status({})), false);
  assert.equal(canStart(status({ status: "stopped", can_start: false })), false);    // 見張り役の無いプロセス
  assert.equal(canStop(status({})), true);
  assert.equal(canStop(status({ state: { phase: "transcribing", mode: "headless", session_id: "sid" } })), false);
  assert.match(statusLine(status({ state: { phase: "setup", mode: "headless", session_id: null } })).text, /卓の設定を確かめています/);
  assert.match(statusLine(status({ status: "crashed", crash: { hand_id: 7 } })).text, /ハンド 7 は記録の途中/);
});

test("偽物: 卓を始める → 動作中 → 終了", async () => {
  const repo = new MockStaffRepository();
  repo.setToken(VALID_STAFF_TOKEN);
  const setup = initialSetup(await repo.getLoggerSetup());
  await assert.rejects(repo.startLogger({ ...setup, seats: [1] }),
                       (e) => e instanceof StaffApiError && e.code === "invalid_setup" && e.errors?.[0].field === "seats");
  await repo.startLogger(setup);
  const running = await repo.getLoggerStatus();
  assert.equal(running.status, "running");
  assert.equal(running.state?.mode, "headless");
  await assert.rejects(repo.startLogger(setup), (e) => e instanceof StaffApiError && e.code === "logger_running");
  await assert.rejects(repo.stopLogger("other"), (e) => e instanceof StaffApiError && e.code === "session_mismatch");
  await repo.stopLogger(running.state!.session_id!);
  assert.equal((await repo.getLoggerStatus()).status, "stopped");
});
