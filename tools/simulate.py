#!/usr/bin/env python3
"""tools/simulate.py — 聞き取りのシミュレーション（推定器の開発と、原因ごとの効き目, テスト方針 週 1, 2026-09-30）

台本のハンド（`tools/test_script.py` の声だけの台本 = 正解つき）を、ディーラーが読んだとして、Whisper の書き起こしと
第 2 の耳の候補を**決まった割合の誤り**で作る。推定器（`tools/estimate.py`）と、いまの読み直し（既定）を同じ入力で
比べる。**精度の主張には使わない**（誤りの割合と形は手置き。店舗の実測に合わせて直していく）。

誤りの種類（`Noise`）:

- `misread`: 額を聞き違える（600 → 200 のように先頭の数字が似た音の数字に）
- `hallucinate`: 定型の幻聴（「ご視聴ありがとうございました。」）に置き換わる（第 2 の耳は聞いている）
- `garble`: 崩れた書き起こし（「のっぴょく」）になる（第 2 の耳は聞いている）
- `missing`: 発話が書き起こしに残らない（小さすぎる声）
- `chatter`: 行のあいだに雑談が入る（`chatter_action` の割合でアクションの言葉を含む = 読み取りが拾う）
- `ear_agree`: 第 2 の耳が自由に聞いた文が正しい候補と同じアクションに読める割合（ライブの規則で使える）

使い方:

    python tools/simulate.py                          # 既定の誤りで 4 セッション × 30 ハンド
    python tools/simulate.py --by-cause               # 誤りを 1 種類ずつ入れて、読み直しと推定器の差を見る
    python tools/simulate.py --noise misread=0.15 --sessions 2 --hands 20
"""
from __future__ import annotations

import argparse
import logging
import random
import re
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio.second_ear import kanji_number  # noqa: E402
from audio.recognizer import parse_amount_ex  # noqa: E402
from core.control_queue import ControlCommand, command_to_audio_event, script_hand_text  # noqa: E402
from core.events import AudioEvent  # noqa: E402
from tools.estimate import PARAMS, SessionInput, compare_with_truth, estimate_session, exact  # noqa: E402
from tools.test_script import DEFAULT_SEATS, generate_voice_script, winner_control  # noqa: E402

T0 = 1_800_000_000.0

_STOCK = ("ご視聴ありがとうございました。", "ご覧いただきありがとうございます。")
_CHATTER = ("まあでもそういうのもあるよね。", "これちゃんと読み込ませた方がいい", "何分経ったの?", "さっき言ったこと全部忘れてました。",
            "温まってないんだね。", "では、始めましょう。", "ちょっと待ってください", "これで大丈夫かな")
_CHATTER_ACTION = ("4番レイズとかね。最悪ね。", "さっきコールしたっけ", "フォールドでいいよ", "オールインはやめとこう")
_KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわん"
# 先頭の数字の聞き違い（音の近い数字: ニ↔ロク・イチ↔ナナ・サン↔ニ・ヨン↔ナナ・ゴ↔キュウ・ハチ↔イチ）
_CONFUSE = {"1": "7", "2": "6", "3": "2", "4": "7", "5": "9", "6": "2", "7": "1", "8": "1", "9": "5"}
_PREFIX = re.compile(r"^((?:シート\d+\s*)+|(?:BTN|SB|BB|UTG\+?\d?|HJ|CO|MP)\s+)")


@dataclass(frozen=True)
class Noise:
    misread: float = 0.06
    hallucinate: float = 0.05
    garble: float = 0.04
    missing: float = 0.02
    chatter: float = 0.15
    chatter_action: float = 0.15
    ear_agree: float = 0.8

    @classmethod
    def only(cls, name: str, value: float) -> "Noise":
        zero = {f.name: 0.0 for f in fields(cls) if f.name != "ear_agree"}
        return cls(**{**zero, name: value})


def ear_form(say: str) -> Optional[str]:
    """第 2 の耳の候補の形（閉じた語彙: アクションの語・額・レイズ / ベット + 額・2 つのアクション）。席・ポジション
    は語彙に無いので落とす。語彙で言えない行（チョップ・ウィナー・役の名前・残りの人数・札の言葉・3 つ以上続けて言う）
    は None。"""
    body = _PREFIX.sub("", say).strip()
    if "チョップ" in body or "ウィナー" in body:
        return None
    words = [w.strip() for w in body.split("、") if w.strip()]
    if not words or len(words) > 2:
        return None
    out = []
    for w in words:
        m = re.fullmatch(r"(レイズ|ベット)?\s*([\d万千]+)点?", w)       # 額だけ（「600」「2千点」「1万2千」）も
        if m:
            value = parse_amount_ex(m.group(2)).value
            out.append((f"{m.group(1)} " if m.group(1) else "") + kanji_number(value))
        elif w in ("フォールド", "コール", "チェック", "オールイン", "チェックアラウンド", "ヘッズアップ", "ショーダウン"):
            out.append(w)
        else:
            return None
    return "、".join(out)


def _misread(say: str, rng: random.Random) -> Optional[str]:
    m = re.search(r"\d+", say)
    if not m or m.group()[0] not in _CONFUSE:
        return None
    digits = m.group()
    return say[:m.start()] + _CONFUSE[digits[0]] + digits[1:] + say[m.end():]


def _garble(say: str, rng: random.Random) -> str:
    return "".join(rng.choice(_KANA) for _ in range(max(3, min(8, len(say)))))


def _ear(truth: Optional[str], rng: random.Random, noise: Noise, free: Optional[str] = None) -> dict:
    """第 2 の耳の結果（`EarResult.to_dict()` の形）。truth = 正しい候補の文（無ければ語彙で言えない行・雑談）。"""
    if truth is None:
        text = free or _garble("xxxx", rng)
        cands = [{"text": t, "logp": round(-12.0 - rng.random() * 6, 3)} for t in ("百", "千", "二千")]
        return {"text": text, "logp": -1.0, "candidates": cands, "sec": 0.2}
    agree = rng.random() < noise.ear_agree
    free_logp = round(-0.3 - rng.random() * 0.7, 3)
    if agree:
        text, true_logp = truth, free_logp - abs(rng.gauss(0, 0.3))
    else:
        text, true_logp = _garble(truth, rng), free_logp - rng.uniform(1.0, 6.0)
    others = []
    wrong = _misread(truth.replace(" ", ""), rng) if re.search(r"\d", truth) else None
    for alt in (wrong, "コール" if "コール" not in truth else "チェック"):
        if alt:
            others.append({"text": alt, "logp": round(free_logp - rng.uniform(6.0, 12.0), 3)})
    cands = sorted([{"text": truth, "logp": round(true_logp, 3)}, *others], key=lambda c: -c["logp"])
    return {"text": text, "logp": free_logp, "candidates": cands, "sec": 0.2}


def _row(start: float, text: str, ear: Optional[dict], rng: random.Random) -> dict:
    return {"utterance_start_ts": round(start, 3), "heard_at": round(start + 1.5 + rng.random(), 3), "text": text,
            "audio_sec": round(0.6 + rng.random(), 2), "confidence": round(0.35 + rng.random() * 0.3, 3),
            "no_speech": False, "ear": ear}


def observe(say: str, start: float, rng: random.Random, noise: Noise) -> Optional[dict]:
    """ディーラーが `say` を読んだときの書き起こしの行（無ければ None = 残らなかった）。"""
    truth = ear_form(say)
    r = rng.random()
    if r < noise.missing:
        return None
    r -= noise.missing
    if r < noise.hallucinate:
        return _row(start, rng.choice(_STOCK), _ear(truth, rng, noise), rng)
    r -= noise.hallucinate
    if r < noise.garble:
        return _row(start, _garble(say, rng), _ear(truth, rng, noise), rng)
    r -= noise.garble
    if r < noise.misread:
        wrong = _misread(say, rng)
        if wrong is not None:
            return _row(start, wrong, _ear(truth, rng, noise), rng)
    return _row(start, say, _ear(truth, rng, noise), rng)


def chatter(start: float, rng: random.Random, noise: Noise) -> dict:
    if rng.random() < noise.chatter_action:
        text = rng.choice(_CHATTER_ACTION)
        return _row(start, text, _ear(None, rng, noise, free=text), rng)
    text = rng.choice(_CHATTER)
    return _row(start, text, _ear(None, rng, noise, free=text), rng)


def simulate_session(seed: int, hands: int = 30, seats: int = DEFAULT_SEATS, noise: Noise = Noise()) -> SessionInput:
    """台本（seed）を読んだセッション。正解は台本のアクション。"""
    script = generate_voice_script(seed, hands=hands, seats=seats)
    rng = random.Random(seed * 7919 + 17)
    table = script["table"]
    t = T0 + seed * 100_000
    events: list = []
    transcripts: list[dict] = []
    for spec in script["hands"]:
        stacks = {int(s): int(v) for s, v in spec["stacks"].items()}
        events.append(AudioEvent(action="script_hand", amount=0, timestamp=t,
                                 raw_text=script_hand_text(int(spec["button"]), stacks)))
        t += 3.0
        for line in spec["lines"]:
            if rng.random() < noise.chatter:
                transcripts.append(chatter(t, rng, noise))
                t += 2.5
            row = observe(line["say"], t, rng, noise)
            if row is not None:
                transcripts.append(row)
            t += 2.5 + rng.random()
        args = winner_control(spec)            # 声では決まらない勝者は台本の画面が送る（札を置かない）
        if args is not None:
            events.append(command_to_audio_event(ControlCommand("w", "winner", args, ""), lambda: t + 4.0))
        t += 8.0
    seats_list = [int(s) for s in table["seats"]]
    first = int(table["first_button"])
    prior = seats_list[(seats_list.index(first) - 1) % len(seats_list)]
    setup = {
        "players": [{"seat": s, "name": table["names"][str(s)], "stack": int(table["stacks"][str(s)])} for s in seats_list],
        "sb": int(table["sb"]), "bb": int(table["bb"]), "button_prior": prior, "first_hand_id": 1,
        "hand_stacks": {str(h["n"]): {str(s): int(v) for s, v in h["stacks"].items()} for h in script["hands"]},
    }
    truth = {"hands": [{"hand_id": h["n"], "actions": h["actions"], "winner_seat": h["winner_seat"], "board": [],
                        "players": []} for h in script["hands"]]}
    return SessionInput(f"sim{seed}", events, transcripts, setup,
                        {"auto_new_hand": False, "auto_winner": True, "rfid_folds": False}, truth)


@dataclass
class SimResult:
    hands: int
    default_rows: tuple[int, int]
    estimate_rows: tuple[int, int]
    default_exact: int
    estimate_exact: int
    in_nbest: int
    better: int
    worse: int
    replays: int


def run(noise: Noise, sessions: int, hands: int, seats: int, params: dict = PARAMS, seed0: int = 1) -> SimResult:
    dr = dt = er = et = dx = ex = nb = better = worse = replays = n = 0
    for seed in range(seed0, seed0 + sessions):
        inp = simulate_session(seed, hands, seats, noise)
        estimates, k = estimate_session(inp, params)
        replays += k
        by_id = {e.hand_id: e for e in estimates}
        for r in compare_with_truth(estimates, inp.truth):
            n += 1
            dr, dt = dr + r.default[0], dt + r.default[1]
            er, et = er + r.estimate[0], et + r.estimate[1]
            truth = next(h for h in inp.truth["hands"] if h["hand_id"] == r.hand_id)
            dx += exact(truth, by_id[r.hand_id].default.hand)
            ex += exact(truth, by_id[r.hand_id].best.hand)
            nb += r.in_nbest
            better += r.estimate[0] > r.default[0]
            worse += r.estimate[0] < r.default[0]
    return SimResult(n, (dr, dt), (er, et), dx, ex, nb, better, worse, replays)


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "—"


def format_result(label: str, r: SimResult) -> str:
    return (f"{label}: 行 読み直し {_pct(*r.default_rows)} → 推定 {_pct(*r.estimate_rows)}"
            f" ／ 全部正しいハンド {r.default_exact}/{r.hands} → {r.estimate_exact}/{r.hands}"
            f" ／ 正解が候補に {r.in_nbest}/{r.hands} ／ 良くなった {r.better}・悪くなった {r.worse}（再生 {r.replays} 回）")


def _parse_noise(specs: list[str]) -> Noise:
    values = {}
    for spec in specs:
        name, _, value = spec.partition("=")
        values[name.strip()] = float(value)
    return replace(Noise(), **values)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="聞き取りのシミュレーション（推定器の開発用。精度の主張には使わない）")
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--hands", type=int, default=30)
    ap.add_argument("--seats", type=int, default=DEFAULT_SEATS)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--noise", action="append", default=[], help="誤りの割合（例: misread=0.1）。何度でも")
    ap.add_argument("--by-cause", action="store_true", help="誤りを 1 種類ずつ入れて比べる")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)      # 候補の再生で出るエンジンのログは要らない
    if args.by_cause:
        base = Noise()
        for f in fields(Noise):
            if f.name in ("ear_agree", "chatter_action"):
                continue
            value = max(getattr(base, f.name) * 2, 0.1)
            noise = replace(Noise.only(f.name, value), chatter_action=base.chatter_action)
            print(format_result(f"{f.name}={value:.2f}", run(noise, args.sessions, args.hands, args.seats,
                                                                   seed0=args.seed)), flush=True)
        return 0
    noise = _parse_noise(args.noise)
    print(noise)
    print(format_result("全部", run(noise, args.sessions, args.hands, args.seats, seed0=args.seed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
