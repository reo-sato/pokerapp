/**
 * iPad の「卓を始める」の形（ADR-0073 段階 C）。画面（LoggerStartScreen）から使う純粋な関数（試験しやすいように分ける）。
 * 形は PC の設定画面と同じ（tools/session_setup_ui.DEFAULT_SETUP）。検証は店舗サーバー（build_session_config）がする。
 */
import type { BlindStructureInfo, FieldError, LoggerSetup, LoggerSetupResponse, LoggerStatusResponse } from "../api/types";

export const DEFAULT_SETUP: LoggerSetup = {
  mode: "ring",
  seats: [1, 2, 3, 4, 5, 6],
  names: {},
  stacks: {},
  default_stack: 20000,
  sb: 100,
  bb: 200,
  structure: "",
  level: 1,
  elapsed_min: 0,
  start_time: null,
  button: null,
  stackless: false,
};

/** 前回の設定で開く（無い項目は既定。ストラクチャーが一覧に無ければ一覧の先頭）。 */
export function initialSetup(r: LoggerSetupResponse | null): LoggerSetup {
  const last = r?.last ?? {};
  const setup: LoggerSetup = {
    ...DEFAULT_SETUP,
    ...last,
    names: { ...(last.names ?? {}) },
    stacks: { ...(last.stacks ?? {}) },
    seats: [...(last.seats ?? DEFAULT_SETUP.seats)].sort((a, b) => a - b),
  } as LoggerSetup;
  const names = (r?.structures ?? []).map((s) => s.name);
  if (names.length && !names.includes(setup.structure)) setup.structure = names[0];
  if (setup.button != null && !setup.seats.includes(setup.button)) setup.button = null;
  return setup;
}

/** 席を使う / 使わない（ボタンが使わない席なら外す）。 */
export function toggleSeat(s: LoggerSetup, seat: number): LoggerSetup {
  const seats = s.seats.includes(seat) ? s.seats.filter((x) => x !== seat) : [...s.seats, seat].sort((a, b) => a - b);
  return { ...s, seats, button: s.button != null && !seats.includes(s.button) ? null : s.button };
}

export function setName(s: LoggerSetup, seat: number, name: string): LoggerSetup {
  const names = { ...s.names };
  if (name.trim()) names[String(seat)] = name;
  else delete names[String(seat)];
  return { ...s, names };
}

export function setStack(s: LoggerSetup, seat: number, text: string): LoggerSetup {
  const stacks = { ...s.stacks };
  const n = Number(text);
  if (text.trim() && Number.isFinite(n)) stacks[String(seat)] = Math.floor(n);
  else delete stacks[String(seat)];
  return { ...s, stacks };
}

/** 前回の時計の続きから（ストラクチャー・レベル・経過分。開始時刻は使わない）。 */
export function applyResume(s: LoggerSetup, resume: LoggerSetupResponse["resume"]): LoggerSetup {
  if (!resume) return s;
  return { ...s, mode: "tournament", structure: resume.structure, level: resume.level,
           elapsed_min: resume.elapsed_min, start_time: "" };
}

/** 送る形: 使わない席の名前・持ち点は落とす。 */
export function toPayload(s: LoggerSetup): LoggerSetup {
  const keep = (m: Record<string, unknown>) =>
    Object.fromEntries(Object.entries(m).filter(([k]) => s.seats.includes(Number(k))));
  return { ...s, names: keep(s.names) as Record<string, string>, stacks: keep(s.stacks) as Record<string, number> };
}

/** 項目ごとの誤り（店舗サーバーの invalid_setup）: 項目名 → 文。 */
export function errorMap(errors: FieldError[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const e of errors ?? []) if (!out[e.field]) out[e.field] = e.message;
  return out;
}

/** ストラクチャーのレベルの一覧（ブレイクを除く 1 から）: 「レベル 3: 200/400 (アンテ 400)」。 */
export function levelLabels(st: BlindStructureInfo | undefined): string[] {
  const out: string[] = [];
  for (const lv of st?.levels ?? []) {
    if (lv.break) continue;
    out.push(`レベル ${out.length + 1}: ${lv.sb}/${lv.bb}${lv.ante ? `（アンテ ${lv.ante}）` : ""}`);
  }
  return out;
}

/** iPad から起動できるか（見張り役があり、ロガーが動いていない）。 */
export function canStart(r: LoggerStatusResponse | null): boolean {
  return !!r && r.can_start !== false && (r.status === "stopped" || r.status === "crashed");
}

/** 「終了」を出すか（動作中でセッションがあり、まだ終了の段階に入っていない）。 */
export function canStop(r: LoggerStatusResponse | null): boolean {
  const phase = r?.state?.phase;
  return r?.status === "running" && !!r.state?.session_id && phase !== "stopping" && phase !== "transcribing";
}
