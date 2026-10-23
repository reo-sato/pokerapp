#!/usr/bin/env python3
"""tools/llm_candidates.py — LLM にハンドの解釈を出させ、推定器の候補（直しの組）にする（オフラインの実験）

推定器（ADR-0056）は 10/19 の 8 人卓で、誤ったハンドの多くで**正解が候補にそもそも無い**（直しの語彙で表せない,
`docs/worklog/2026-10-23-estimator-improvement-plan.md`）。そこで LLM にハンド全体（発話 + RFID の時刻表）を読ませ、
その解釈を**今の直しの語彙**（read / insert / button）に直して推定器の候補に足す。選ぶのは今の推定器（pokerkit の
合法性と今の採点）で、LLM は正本にしない。本番の経路・店舗 PC では使わない。

- 入力の形と説明文: `tools/llm_hand_prompt.py`（名前・真のアクション・メモ・音声は送らない）
- 答えはキャッシュ（`logs/llm_cache/<モデル>/<セッション>/<ハンド>.json`）。同じ入力・同じ説明文なら API を呼ばない
  = 物差し（`tools/bench_llm.py`）は何度回しても同じ
- 送るには `ANTHROPIC_API_KEY`（と `pip install ".[llm]"`）が要る。`--dry-run` は API を呼ばずに件数と量の目安を出す

使い方:

    python tools/llm_candidates.py --split A --dry-run              # 開発 A の件数・量・費用の目安
    python tools/llm_candidates.py --split A --limit 10              # パイロット（Opus 5.5）
    python tools/llm_candidates.py --split A --model claude-haiku-5-5
    python tools/llm_candidates.py <店舗の zip ...> --model ...      # 店舗のログ（真のアクション付き）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm_hand_prompt import (  # noqa: E402
    SYSTEM_PROMPT,
    canonical_json,
    hand_input,
    input_hash,
    prompt_hash,
    spoken_text,
    window_utterance_starts,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_CACHE = ROOT / "logs" / "llm_cache"
MAX_TOKENS = 16000
# LLM の読み（read の直し）の確からしさ。今の読みの選択肢（第 2 の耳など）と同じ物差しの log 確率で、実験の値
# （推定器の PARAMS は触らない）。`tools/bench_llm.py --read-logp` で変えて比べる
DEFAULT_READ_LOGP = -1.0

# 開発データ（`tests/fixtures/store`）の分け方（事前登録 `docs/dogfood/llm-proposer-preregistration.md`）:
# A = 説明文を作る / B = 説明文を固定してから 1 回だけ。セッション ID の先頭 8 文字
SPLIT_A = ("027e4b15", "2e658148", "7b897671", "9d1d8536", "a6ee12e4", "d0f055fb", "fded6f75",
           "1f838667", "5b1ca234", "7bd25188", "80078377", "9db1e032", "b7f32031")
SPLIT_B = ("e82f5005", "05cccd6c", "61eda7bd", "782c457d", "15cad555", "62cad44b", "65c412b2", "845da8e6")

# サーバー側のフォールバック（拒否されたら別のモデルで答える）が使えるモデル。Haiku 5.5 には無い
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
# 1M トークンあたりの USD（入力・出力・キャッシュの読み・キャッシュの書き 5 分）。platform.claude.com の料金表 2026-10
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
}


# ───────────────────────── 答えの形 ─────────────────────────


def answer_model():
    """構造化出力の型（pydantic は API を呼ぶときだけ要る）。"""
    from typing import Literal

    from pydantic import BaseModel

    Act = Literal["fold", "check", "call", "bet", "raise", "allin"]

    class SpokenAction(BaseModel):
        action: Act
        amount: Optional[int]
        seat: Optional[int]

    class UtteranceLabel(BaseModel):
        index: int
        kind: Literal["action", "muck", "noise", "restated", "correction"]
        actions: list[SpokenAction]

    class SilentAction(BaseModel):
        after_index: int
        action: Literal["fold", "check", "call"]

    class Row(BaseModel):
        street: Literal["preflop", "flop", "turn", "river"]
        seat: int
        action: Act
        amount: int

    class Interpretation(BaseModel):
        utterances: list[UtteranceLabel]
        silent_actions: list[SilentAction]
        actions: list[Row]
        button_seat: Optional[int]
        winner_seat: Optional[int]
        confidence: float
        note: str

    class HandAnswer(BaseModel):
        interpretations: list[Interpretation]

    return HandAnswer


# ───────────────────────── 呼び出しとキャッシュ ─────────────────────────


def cache_path(cache_dir: Path, model: str, session_id: str, hand_id: int) -> Path:
    return Path(cache_dir) / model / session_id / f"{int(hand_id)}.json"


def load_cached(path: Path, payload: dict) -> Optional[dict]:
    """同じ説明文・同じ入力の答えがあれば返す（違えば None = 古い答えは使わない）。"""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if record.get("prompt_hash") != prompt_hash() or record.get("input_hash") != input_hash(payload):
        return None
    return record


def ask(client, model: str, payload: dict, effort: str = "high") -> dict:
    """1 ハンドを LLM に聞く。拒否・出力の上限切れは答え無し（`answer` = None）として記録する。"""
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "次のハンドを復元してください。\n" + canonical_json(payload)}],
        output_format=answer_model(),
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
    )
    if model in _FALLBACK_MODELS:
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    started = time.time()
    response = client.beta.messages.parse(**kwargs)
    usage = getattr(response, "usage", None)
    record: dict[str, Any] = {
        "model": model,
        "served_by": getattr(response, "model", model),
        "prompt_hash": prompt_hash(),
        "input_hash": input_hash(payload),
        "stop_reason": response.stop_reason,
        "seconds": round(time.time() - started, 1),
        "usage": {k: getattr(usage, k, None) for k in
                  ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")},
        "answer": None,
    }
    if response.stop_reason in ("refusal", "max_tokens"):
        return record
    parsed = getattr(response, "parsed_output", None)
    record["answer"] = parsed.model_dump() if parsed is not None else None
    return record


def cost_usd(model: str, usage: dict) -> Optional[float]:
    price = PRICES.get(model)
    if price is None:
        return None
    inp, out, read, write = price
    return ((usage.get("input_tokens") or 0) * inp + (usage.get("output_tokens") or 0) * out
            + (usage.get("cache_read_input_tokens") or 0) * read
            + (usage.get("cache_creation_input_tokens") or 0) * write) / 1e6


# ───────────────────────── 答え → 直しの組 ─────────────────────────


def seeds_for(answer: Optional[dict], est, w, *, with_seat: bool = True,
              read_logp: float = DEFAULT_READ_LOGP) -> tuple[tuple, ...]:
    """LLM の解釈を推定器の直しの組にする（解釈 1 つ = 組 1 つ。直しが無い = 今の読みと同じ解釈は除く）。"""
    out: list[tuple] = []
    for seed in interpretation_edits(answer, est, w, with_seat=with_seat, read_logp=read_logp):
        if seed and seed not in out:
            out.append(seed)
    return tuple(out)


def interpretation_edits(answer: Optional[dict], est, w, *, with_seat: bool = True,
                         read_logp: float = DEFAULT_READ_LOGP) -> list[tuple]:
    """解釈ごとの直しの組（答えの順。今の読みと同じ解釈は空の組）。

    - 発話: 言ったアクションを今の読み取りが読める文にし（`spoken_text`）、今の読みと違えば `read`。アクションで
      ない発話（雑談・言い直し・訂正）は、今の読みがアクションを読んでいれば空の `read`（その発話の語を捨てる）
    - 言われなかったアクション: 次の発話の少し前に `insert`（fold / check / call。誰かは手番の順で engine が決める）
    - ボタン: 入力と違えば `button`
    """
    from integration.estimator import _BETTING, _INSERT_LEAD_SEC, Edit
    from integration.world_replay import delivered_at, read_utterance

    if not answer:
        return []
    starts = window_utterance_starts(est, w)
    _events, rows = est._window_inputs(w)
    by_start = {r["utterance_start_ts"]: r for r in rows}
    seats = {p["seat"] for p in w.base.get("players") or []}
    out: list[tuple] = []
    for interp in answer.get("interpretations") or []:
        edits: list = []
        for lab in interp.get("utterances") or []:
            i = int(lab.get("index", -1))
            if not 0 <= i < len(starts):
                continue
            if lab.get("kind") == "muck":
                continue                  # ショーダウンのマック: 今の読み（「フォールド」）のまま
            row = by_start[starts[i]]
            text = spoken_text(lab.get("actions") or [], with_seat) if lab.get("kind") == "action" else ""
            # 比べるのは賭けの語だけ（「ショーダウン」「ヘッズアップ」などは LLM の答えに無いので、それだけの
            # 発話は直さない。賭けの語を直す発話では、文を置き換えるのでそれらの語は落ちる）
            # 席は比べない（席を足しただけの読みは直しにしない = 誰かは手番の順で engine が決める。直しは 1 つ
            # ごとに読みの確からしさを払うので、要らない直しで正解が点で負ける）
            now = [(e.action, int(e.amount or 0)) for e in read_utterance(row) if e.action in _BETTING]
            new = [(e.action, int(e.amount or 0)) for e in read_utterance(row, text)]
            if now != new:
                edits.append(Edit("read", starts[i], text, logp=read_logp,
                                  label=f"LLM: 「{row.get('text') or ''}」→「{text}」"))
        spots: dict[float, int] = {}
        for s in sorted(interp.get("silent_actions") or [], key=lambda s: int(s.get("after_index", -1))):
            if s.get("action") not in ("fold", "check", "call"):
                continue
            nxt = int(s.get("after_index", -1)) + 1
            if nxt < len(starts):
                at = starts[nxt] - _INSERT_LEAD_SEC
            elif starts:
                at = delivered_at(by_start[starts[-1]]) + 1.0
            else:
                at = w.start + 1.0
            k = spots.get(at, 0)
            spots[at] = k + 1
            # 同じ場所の 2 つ目からは少し前にずらす（同じ時刻の同じ直しは 1 つに数えられてしまう）
            edits.append(Edit("insert", at - 0.01 * k, s["action"],
                              label=f"LLM: 聞こえなかった {s['action']}（発話 {nxt} の前）"))
        button = interp.get("button_seat")
        if button is not None and button != w.button and button in seats and len(seats) > 2:
            edits.append(Edit("button", w.start, int(button), label=f"LLM: ボタン 席{button}"))
        out.append(tuple(edits))
    return out


def direct_hands(answer: Optional[dict], w) -> list[dict]:
    """LLM 単独の答え（解釈ごとのハンド）。ボードは RFID（直しの無い再生）のもの。"""
    if not answer:
        return []
    out = []
    for interp in answer.get("interpretations") or []:
        out.append({"actions": [dict(a) for a in interp.get("actions") or []],
                    "winner_seat": interp.get("winner_seat"),
                    "board": list(w.base.get("board") or []),
                    "button_seat": interp.get("button_seat") or w.button})
    return out


# ───────────────────────── セッションとハンド ─────────────────────────


@dataclass
class ScoredHand:
    session_id: str
    key: str              # 物差しの名前（"15cad555#3"）
    hand_id: int          # 真のアクションのハンド番号
    window: Any
    truth: dict
    live: Any
    has_live: bool = False    # ライブの記録があるセッション（店舗のログ）= ライブとも比べる


def scored_hands(inp, est) -> list[ScoredHand]:
    """真のアクションのあるハンドの窓（`bench_hands.run_bench_v1` と同じ選び方・同じ数えない理由）。"""
    from tools.bench_hands import _live_record, seat_warning, short_id, truth_exclusions
    from tools.estimate_logs import _live_hand

    truth = {int(h["hand_id"]): h for h in inp.truth.get("hands") or []}
    live_hands = _live_record(inp)
    out = []
    for w in est.windows():
        live = _live_hand(live_hands, w.base.get("started_at")) if live_hands else None
        hid = int(live["hand_id"]) if live is not None else w.hand_id
        if hid not in truth:
            continue
        if truth_exclusions(truth[hid], live if live is not None else w.base, seat_warning(inp, w)):
            continue
        out.append(ScoredHand(inp.session_id, f"{short_id(inp.session_id)}#{hid}", hid, w, truth[hid], live,
                              bool(live_hands)))
    return out


def select_sessions(logs: Optional[list[Path]], split: Optional[str], names: Optional[list[str]]):
    """(SessionInput, 在否の履歴) の並び。店舗のログ（zip / フォルダ）か開発データ（分け方 A / B）。"""
    from tools.bench_hands import v1_inputs

    pairs = []
    for _name, sessions in v1_inputs(store=not logs, script=False, logs=logs or None):
        pairs.extend(sessions)
    if split:
        wanted = SPLIT_A if split.upper() == "A" else SPLIT_B
        pairs = [(i, p) for i, p in pairs if i.session_id[:8] in wanted]
    if names:
        pairs = [(i, p) for i, p in pairs if any(n in i.session_id for n in names)]
    return pairs


def make_estimator(inp, presence, params: Optional[dict] = None):
    from integration.estimator import PARAMS, SessionEstimator

    return SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id,
                            params or PARAMS)


# ───────────────────────── main ─────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LLM にハンドの解釈を出させてキャッシュする（実験）")
    ap.add_argument("logs", nargs="*", type=Path, help="店舗のログ（zip / フォルダ）。無ければ開発データ")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--split", choices=["A", "B", "a", "b"], help="開発データの分け方（事前登録）")
    ap.add_argument("--sessions", nargs="*", help="セッション ID の一部で絞る")
    ap.add_argument("--hands", nargs="*", help="ハンドで絞る（例 15cad555#3）")
    ap.add_argument("--limit", type=int, default=0, help="新しく聞くハンドの上限（パイロット）")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--workers", type=int, default=2, help="同時に聞く数")
    ap.add_argument("--dry-run", action="store_true", help="API を呼ばずに件数と量の目安を出す")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("integration").setLevel(logging.CRITICAL)
    logging.getLogger("core").setLevel(logging.CRITICAL)

    jobs: list[tuple[ScoredHand, dict, Path]] = []
    cached = 0
    for inp, presence in select_sessions(args.logs or None, args.split, args.sessions):
        est = make_estimator(inp, presence)
        for h in scored_hands(inp, est):
            if args.hands and h.key not in args.hands:
                continue
            payload = hand_input(est, h.window)
            path = cache_path(args.cache_dir, args.model, h.session_id, h.window.hand_id)
            if load_cached(path, payload) is not None:
                cached += 1
                continue
            jobs.append((h, payload, path))
    if args.limit:
        jobs = jobs[:args.limit]
    chars = sum(len(canonical_json(p)) for _h, p, _path in jobs)
    print(f"説明文の版 {prompt_hash()}・モデル {args.model}・キャッシュ済み {cached}・新しく聞く {len(jobs)} ハンド")
    if args.dry_run or not jobs:
        # 日本語の多い JSON は 1 トークンあたりおよそ 1.5 文字（目安。実測はパイロットの usage で）
        approx_in = chars / 1.5 + len(jobs) * len(SYSTEM_PROMPT) / 1.5
        print(f"入力 {chars} 文字（説明文を除く）・入力トークンの目安 {approx_in:,.0f}")
        price = PRICES.get(args.model)
        if price and jobs:
            est_out = 5000 * len(jobs)          # 思考を含む出力の目安（1 ハンド 5,000）
            usd = (approx_in * price[0] + est_out * price[1]) / 1e6
            print(f"費用の目安 ${usd:.2f}（出力 1 ハンド 5,000 トークンと仮定。キャッシュ・Batch の割引なし）")
        return 0

    import anthropic  # 遅延 import（`pip install ".[llm]"`）

    client = anthropic.Anthropic()
    spent = 0.0
    failed = 0

    def run(job: tuple[ScoredHand, dict, Path]) -> Optional[dict]:
        h, payload, path = job
        try:
            record = ask(client, args.model, payload, effort=args.effort)
        except anthropic.APIStatusError as e:
            log.warning("%s: API エラー %s %s", h.key, e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.warning("%s: 接続エラー %s", h.key, e)
            return None
        record.update(session_id=h.session_id, hand_id=h.window.hand_id, key=h.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return record

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for (h, _p, _path), record in zip(jobs, pool.map(run, jobs)):
            if record is None:
                failed += 1
                print(f"{h.key}: 失敗")
                continue
            c = cost_usd(args.model, record["usage"]) or 0.0
            spent += c
            n = len((record.get("answer") or {}).get("interpretations") or [])
            print(f"{h.key}: {record['stop_reason']}・解釈 {n}・{record['seconds']} 秒・${c:.3f}")
    print(f"費用 ${spent:.2f}（{len(jobs) - failed} ハンド・失敗 {failed}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
