"""tools/short_words.py — 短い語の分類器（`audio/short_words.py`）を開発データから作り、確かめる

短い語の聞き間違いの進め方 2（`docs/worklog/2026-10-07-short-word-plan.md`、Fable 5.1 の試作を引き継ぐ）。

1. 開発データ（`tests/fixtures/store`）の数えるハンドごとに、配る人が言った真のアクションの行と、ハンドの時間帯の
   発話（ライブの規則で読んだもの）を動的計画法で突き合わせる。読めなかった発話も、真のアクションの 1 行を
   受け持てる（聞き違い）。どの行も受け持たない発話は「雑談」（none）、1 行ならその種類（コール / チェック /
   フォールド / 額 = ベット・レイズ）。2 行以上（続けて言った）・オールインは学習に使わない。
   ラベルは音（何と言ったか）: 少し前に言った語の言い直しも、その語のラベルにする。オーナーが音声を聞いて付けた
   ラベル（各フォルダの `utterance_labels.json`）があれば、それを突き合わせの答えとして使う。
2. 第 2 の耳の点数（`audio/short_words.features`）から、ラベルを当てる多項ロジスティック回帰を学ぶ。

使い方:

    python tools/short_words.py eval     # セッションを 1 つずつ抜いて学び、抜いたセッションで確かめる
    python tools/short_words.py train    # 全部で学んで audio/short_word_model.json に書く
    python tools/short_words.py labels   # 発話ごとのラベル（突き合わせの確認用, JSON Lines）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio import short_words as sw  # noqa: E402
from audio.recognizer import GARBLED_CALL, is_question, parse_actions  # noqa: E402
from audio.second_ear import SHORT_WORDS, apply_ear, used_ear  # noqa: E402
from tools.estimate import inputs_from_fixtures  # noqa: E402
from tools.eval_store import _is_noise, hand_windows, replay_session, reparse_events  # noqa: E402
from tools.measure_capture_accuracy import is_excluded  # noqa: E402

SPOKEN = frozenset({"fold", "check", "call", "bet", "raise", "allin"})
# 学習の値（L2 の強さ・学習の回数と幅）。開発データ 2,700 発話ほどで、セッションを抜いた確かめが安定する値
L2 = 1.0
EPOCHS = 400
RATE = 0.1
# オーナーが音声を聞いて付けたラベル（開発データの各フォルダ。発話の話し始め `repr(utterance_start_ts)` → ラベル）
LABELS_FILE = "utterance_labels.json"
# ハンドを終わらせるフォールドは、前の行を受け持った発話の次の発話で言う（店舗: はっきり読めた 30 回がすべて次の発話で
# 間は 0.9〜5.2 秒、読めなかった次の発話はオーナーが聞いて 11 回中 10 回がフォールド = 2026-10-07）
END_FOLD_SEC = 6.0
# 言い直し: 少し前（あと）に言った語を繰り返す（推定器の `_restates` と同じ幅）。読めなかった発話は、隣の発話の
# 語と第 2 の耳の候補が同じときだけ（オーナーのラベル: 「コーナー」→「コール7400点」、「これでいいですか?」）
RESTATE_SEC = 15.0
RESTATE_UNREAD_SEC = 5.0


def owner_labels(folder: Optional[Path]) -> dict[float, str]:
    """オーナーが音声を聞いて付けたラベル（発話の話し始め → ラベル）。無ければ空。"""
    path = Path(folder) / LABELS_FILE if folder else None
    if path is None or not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {float(k): v["label"] for k, v in (data.get("labels") or {}).items()
            if isinstance(v, dict) and v.get("label") in sw.LABELS}


@dataclass
class Utterance:
    session: str
    hand_id: int
    start: float
    text: str
    label: str                  # none / call / check / fold / amount / multi / other
    heard: tuple                # ライブの規則で読んだアクション（種類, 額）
    ear: Optional[dict]
    whisper: str = "none"       # Whisper の書き起こしだけで読んだラベル（`whisper_label`）


def _token(action: str, amount) -> Optional[tuple]:
    if action in ("bet", "raise"):
        return ("wager", int(amount or 0))
    if action in SPOKEN or action == "check_around":
        return (action, 0)
    return None


def truth_rows(hand: dict) -> list[dict]:
    """配る人が言う真のアクションの行。ハンドを終わらせる最後のフォールドは言わないことが多い（言われなくてよい）。"""
    actions = [a for a in hand.get("actions") or [] if isinstance(a, dict)]
    showdown = any(a.get("street") == "showdown" for a in actions)
    rows = [dict(a, optional=False) for a in actions if a.get("action") in SPOKEN and a.get("street") != "showdown"]
    if rows and rows[-1].get("action") == "fold" and not showdown:
        rows[-1]["optional"] = True
    return rows


def _ear_token(ear: Optional[dict]) -> Optional[tuple]:
    """第 2 の耳のいちばん確からしい候補が 1 つのアクションに読めれば、その種類（突き合わせの手がかり）。"""
    cands = (ear or {}).get("candidates") or []
    if not cands or not cands[0].get("text"):
        return None
    events = parse_actions(cands[0]["text"])
    return _token(events[0].action, events[0].amount) if len(events) == 1 else None


def heard_items(rows: list[dict], labels: Optional[dict[float, str]] = None) -> list[dict]:
    """発話 → 読んだアクションごとの項目（読めなければ「読めない」項目 1 つ）。`garbled`: ライブの規則が聞き違えた
    コールとして読んだ語（「コールド」「これで終わりです」= コールかフォールド）。オーナーがラベルを付けた発話
    （`labels`）は、そのラベルの項目 1 つ（`owner`。雑談なら行を受け持たない）。"""
    labels = labels or {}
    items = []
    for u, row in enumerate(rows):
        text = (row.get("text") or "").strip()
        at = row["utterance_start_ts"]
        if at in labels:
            label = labels[at]
            tok = None if label == "none" else ("wager", 0) if label == "amount" else (label, 0)
            items.append({"u": u, "kind": "owner" if tok else "other", "tok": tok, "hint": None, "garbled": False})
            continue
        parsed = [] if _is_noise(row, text) else parse_actions(text, confidence=row.get("confidence"),
                                                               utterance_start_ts=at)
        parsed, _ = apply_ear(parsed, text, used_ear(row), question=is_question(text), utterance_start_ts=at,
                              confidence=row.get("confidence"))
        if not parsed:
            items.append({"u": u, "kind": "unreadable", "tok": None, "hint": _ear_token(row.get("ear")),
                          "garbled": False})
        for e in parsed:
            tok = _token(e.action, e.amount)
            items.append({"u": u, "kind": "token" if tok is not None else "other", "tok": tok, "hint": None,
                          "garbled": GARBLED_CALL in e.parse_flags})
    return items


# 突き合わせの重み（Fable 5.1 の試作と同じ）: 言われない行 / 読めない発話が受け持つ / 種類の取り違え / 読めない発話を
# 飛ばす / 読めた語を飛ばす / アクション以外の語を飛ばす
C_MISS, C_GARBLED, C_SUB, C_SKIP_UNREAD, C_SKIP_TOKEN, C_SKIP_OTHER = 1.0, 0.5, 0.9, 0.25, 0.7, 0.0
# 聞き違えたコール（「コールド」「これで終わりです」）がフォールドの行を受け持つ（ライブの規則でもコールか
# フォールド）/ チェックアラウンドの言い直しを飛ばす（ストリートが変わるときに前のストリートのチェックをまとめて
# 言う = オーナー 2026-10-06）
C_GARBLED_FOLD, C_SKIP_AROUND = 0.2, 0.1


def _skip_cost(it: dict) -> float:
    if it["kind"] == "token":
        if it.get("garbled"):
            return C_SKIP_UNREAD
        return C_SKIP_AROUND if it["tok"][0] == "check_around" else C_SKIP_TOKEN
    return {"unreadable": C_SKIP_UNREAD, "other": C_SKIP_OTHER, "owner": C_SKIP_UNREAD}[it["kind"]]


def _match_cost(it: dict, t: tuple) -> Optional[float]:
    """項目が真のアクションの行を受け持つ重み（受け持てなければ None）。"""
    if it["kind"] == "owner":                      # オーナーが聞いた語: 同じ種類の行だけ（額は問わない）
        return 0.0 if it["tok"][0] == t[0] else None
    if it["kind"] == "unreadable":
        return C_GARBLED - (0.2 if it["hint"] is not None and tuple(it["hint"]) == tuple(t) else 0.0)
    if it["kind"] != "token":
        return None
    if it["tok"] == t or (it["tok"][0] == "check_around" and t[0] == "check"):
        return 0.0
    if it.get("garbled") and t[0] == "fold":
        return C_GARBLED_FOLD
    if it["tok"][0] == "wager" and t[0] == "wager":
        return 0.7
    if (it["tok"][0] == "wager") != (t[0] == "wager"):
        return 1.1
    return C_SUB


def align(truth: list[tuple], items: list[dict], optional: list[bool]) -> list[Optional[int]]:
    """真のアクションの行 → 受け持った項目の番号（言われなければ None）。"""
    n, m = len(truth), len(items)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[Optional[tuple]]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best, arg = inf, None
            miss = 0.05 if i > 0 and optional[i - 1] else C_MISS
            if i > 0 and cost[i - 1][j] + miss < best:
                best, arg = cost[i - 1][j] + miss, ("miss", i - 1, j)
            if j > 0 and cost[i][j - 1] + _skip_cost(items[j - 1]) < best:
                best, arg = cost[i][j - 1] + _skip_cost(items[j - 1]), ("skip", i, j - 1)
            if i > 0 and j > 0:
                it, t = items[j - 1], truth[i - 1]
                c = _match_cost(it, t)
                if (it["kind"] == "token" and it["tok"][0] == "check_around" and t[0] == "check"
                        and cost[i - 1][j] < best):
                    best, arg = cost[i - 1][j], ("extend", i - 1, j)    # チェックアラウンドは何人分でも
                if c is not None and cost[i - 1][j - 1] + c < best:
                    best, arg = cost[i - 1][j - 1] + c, ("match", i - 1, j - 1)
            cost[i][j], back[i][j] = best, arg
    out: list[Optional[int]] = [None] * n
    i, j = n, m
    while i > 0 or j > 0:
        kind, pi, pj = back[i][j]
        if kind == "extend":
            out[i - 1] = j - 1
        elif kind == "match":
            out[i - 1] = j - 1
        i, j = pi, pj
    return out


def end_fold(truth: list[dict], items: list[dict], matched: list[Optional[int]],
             rows: list[dict]) -> list[Optional[int]]:
    """ハンドを終わらせるフォールド（言われなくてよい最後の行）は、前の行を受け持った発話の次の発話で言う。次の発話が
    `END_FOLD_SEC` 以内で、読めない・聞き違えたコールの語だけなら、その発話が受け持つ（言われないとした・後ろの別の
    発話に付けた突き合わせを直す。動的計画法は「言われない + 読めない発話を飛ばす」と「読めない発話が受け持つ」の重みが
    近く、言われないほうを選びやすい）。"""
    if len(truth) < 2 or not truth[-1]["optional"] or matched[-2] is None:
        return matched
    u = items[matched[-2]]["u"] + 1
    if matched[-1] is not None and items[matched[-1]]["u"] <= u:     # 前の行と同じ発話か次の発話で受け持った
        return matched
    nxt = [j for j, it in enumerate(items) if it["u"] == u]
    if (not nxt or u >= len(rows) or any(j in matched for j in nxt)
            or rows[u]["utterance_start_ts"] - rows[u - 1]["utterance_start_ts"] > END_FOLD_SEC):
        return matched
    if not all(items[j]["kind"] == "unreadable" or items[j].get("garbled") for j in nxt):
        return matched
    return [*matched[:-1], nxt[0]]


def _same(tok: tuple, t: tuple) -> bool:
    return tok == t or (tok[0] == "check_around" and t[0] == "check")


def restated(truth: list[dict], items: list[dict], matched: list[Optional[int]], rows: list[dict]) -> dict[int, str]:
    """どの行も受け持たない発話のうち、言い直しの発話 → その語のラベル（音で付けるラベル: 言い直しも同じ語）。
    読めた語（聞き違えたコールの語を除く）がすべて、`RESTATE_SEC` 以内に別の発話が受け持った行と同じ（ベット・レイズは
    額も同じ）か、読めない発話で、第 2 の耳の候補が隣の発話（`RESTATE_UNREAD_SEC` 以内）の受け持った行と同じ。"""
    tokens = [_token(r["action"], r.get("amount")) for r in truth]
    carried = {}                                    # 発話 → 受け持った行の (種類, 額) の並び
    for k, j in enumerate(matched):
        if j is not None:
            carried.setdefault(items[j]["u"], []).append(tokens[k])
    out: dict[int, str] = {}
    for u, row in enumerate(rows):
        own = [it for it in items if it["u"] == u]
        if u in carried or not own or any(it["kind"] in ("owner", "other") for it in own):
            continue
        at = row["utterance_start_ts"]
        if all(it["kind"] == "token" and not it.get("garbled") for it in own):
            kinds = {("check", 0) if it["tok"][0] == "check_around" else it["tok"] for it in own}
            if len(kinds) != 1:
                continue
            tok = next(iter(kinds))
            near = [v for v, toks in carried.items()
                    if abs(rows[v]["utterance_start_ts"] - at) <= RESTATE_SEC and any(_same(tok, t) for t in toks)]
        elif len(own) == 1 and own[0]["kind"] == "unreadable" and own[0]["hint"] is not None:
            tok = tuple(own[0]["hint"])
            near = [v for v in (u - 1, u + 1) if v in carried
                    and abs(rows[v]["utterance_start_ts"] - at) <= RESTATE_UNREAD_SEC
                    and any(_same(tok, t) for t in carried[v])]
        else:
            continue
        if near:
            out[u] = _label(["raise" if tok[0] == "wager" else tok[0]])
    return out


def whisper_label(row: dict) -> str:
    """Whisper の書き起こしだけ（第 2 の耳を重ねない）で読んだラベル: 賭けの語が無い = none、1 つならその種類
    （ベット・レイズ = amount、チェックアラウンド = check）、ほか（オールイン・2 つ以上）= other。"""
    text = (row.get("text") or "").strip()
    events = [] if _is_noise(row, text) else [e for e in parse_actions(text) if e.action in SPOKEN | {"check_around"}]
    if not events:
        return "none"
    if len(events) > 1:
        return "other"
    a = events[0].action
    return "amount" if a in ("bet", "raise") else "check" if a == "check_around" else a if a in sw.LABELS else "other"


def _label(actions: list[str]) -> str:
    if not actions:
        return "none"
    if len(actions) > 1:
        return "multi"
    a = actions[0]
    return "amount" if a in ("bet", "raise") else a if a in ("call", "check", "fold") else "other"


def session_utterances(si, use_labels: bool = True) -> list[Utterance]:
    """1 セッションの数えるハンドの発話とラベル。`use_labels`: オーナーのラベルを使う（False = 突き合わせだけ。
    突き合わせの規則をオーナーのラベルで確かめる）。"""
    rows = [r for r in si.transcripts if r.get("utterance_start_ts") is not None and not r.get("no_speech")]
    reparsed = replay_session(reparse_events(si.events, si.transcripts), si.setup, si.flags, si.session_id)
    windows = hand_windows(reparsed)
    labels = owner_labels(si.folder) if use_labels else {}
    out: list[Utterance] = []
    for hand in si.truth.get("hands") or []:
        hid = hand.get("hand_id")
        if is_excluded(hand) or hid not in windows:
            continue
        start, end = windows[hid]
        in_hand = sorted((r for r in rows if start <= r["utterance_start_ts"] < end),
                         key=lambda r: r["utterance_start_ts"])
        truth = truth_rows(hand)
        if not truth:
            continue
        items = heard_items(in_hand, labels)
        matched = align([_token(r["action"], r.get("amount")) for r in truth], items, [r["optional"] for r in truth])
        matched = end_fold(truth, items, matched, in_hand)
        per_u: dict[int, list[str]] = {}
        for k, j in enumerate(matched):
            if j is not None:
                per_u.setdefault(items[j]["u"], []).append(truth[k]["action"])
        again = restated(truth, items, matched, in_hand)
        live = heard_items(in_hand) if any(r["utterance_start_ts"] in labels for r in in_hand) else items
        for u, row in enumerate(in_hand):
            at = row["utterance_start_ts"]
            heard = tuple(it["tok"] for it in live if it["u"] == u and it["tok"] is not None)
            label = labels.get(at) or (_label(per_u[u]) if u in per_u else again.get(u, "none"))
            out.append(Utterance(si.session_id, hid, at, (row.get("text") or "").strip(), label, heard, row.get("ear"),
                                 whisper_label(row)))
    return out


def dataset(utts: list[Utterance], words: tuple[str, ...] = SHORT_WORDS) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """学習に使う発話（ラベルが 5 つのどれか・短い語の点数がある）→ (特徴量, ラベルの番号, utts の番号)。"""
    xs, ys, idx = [], [], []
    for i, u in enumerate(utts):
        if u.label not in sw.LABELS:
            continue
        x = sw.features(u.ear, words)
        if x is None:
            continue
        xs.append(x)
        ys.append(sw.LABELS.index(u.label))
        idx.append(i)
    return np.asarray(xs), np.asarray(ys, dtype=np.int64), idx


def whisper_confusion(utts: list[Utterance]) -> dict[str, dict[str, float]]:
    """P(Whisper の読みのラベル | 真のラベル)（1 ずつ足してならす）。推定器が Whisper の読みの項に使う。"""
    cols = (*sw.LABELS, "other")
    out: dict[str, dict[str, float]] = {}
    for label in sw.LABELS:
        counts = {c: 1.0 for c in cols}
        for u in utts:
            if u.label == label:
                counts[u.whisper if u.whisper in counts else "other"] += 1.0
        total = sum(counts.values())
        out[label] = {c: v / total for c, v in counts.items()}
    return out


def train(x: np.ndarray, y: np.ndarray, words: tuple[str, ...] = SHORT_WORDS, l2: float = L2,
          epochs: int = EPOCHS, rate: float = RATE) -> sw.Model:
    """多項ロジスティック回帰（全部の発話で 1 回に勾配を取る。決まった手順なので何度やっても同じ重み）。

    ラベルの数の偏りは重みでならす（どのラベルも同じだけ数える）。そうすると確率はラベルが一様に起きるとしたときの
    もの = 2 つのラベルの確率の比が、音がそのラベルから出た確からしさの比になる（推定器はこの比を使い、どのアクションが
    起きやすいかはハンドの筋で決める）。"""
    n = len(x)
    mean = x.mean(axis=0)
    std = x.std(axis=0) + 1e-6
    z = np.hstack([(x - mean) / std, np.ones((n, 1))])
    k = len(sw.LABELS)
    onehot = np.eye(k)[y]
    counts = np.bincount(y, minlength=k)
    balance = n / (k * np.maximum(1, counts))
    w = np.zeros((z.shape[1], k))
    for _ in range(epochs):
        logits = z @ w
        logits -= logits.max(axis=1, keepdims=True)
        p = np.exp(logits)
        p /= p.sum(axis=1, keepdims=True)
        grad = z.T @ ((p - onehot) * balance[y][:, None]) / n + l2 * w / n
        grad[-1] -= l2 * w[-1] / n                 # 切片はならさない
        w -= rate * grad
    prior = {label: float(counts[i] / n) for i, label in enumerate(sw.LABELS)}
    digest = hashlib.sha256(json.dumps([x.round(4).tolist(), y.tolist(), l2, epochs, rate]).encode()).hexdigest()[:12]
    return sw.Model({"version": digest, "words": list(words), "labels": list(sw.LABELS), "mean": mean.tolist(),
                     "std": std.tolist(), "weights": w.tolist(), "prior": prior})


def all_utterances() -> list[Utterance]:
    out: list[Utterance] = []
    for si in inputs_from_fixtures():
        if si.transcripts:
            out.extend(session_utterances(si))
    return out


def evaluate(utts: list[Utterance]) -> list[str]:
    """セッションを 1 つずつ抜いて学び、抜いたセッションの発話で当たりを数える。"""
    x, y, idx = dataset(utts)
    sessions = np.asarray([utts[i].session for i in idx])
    probs = np.zeros((len(y), len(sw.LABELS)))
    for s in sorted(set(sessions)):
        test = sessions == s
        model = train(x[~test], y[~test])
        for row, j in zip(np.flatnonzero(test), np.asarray(idx)[test]):
            p = model.predict(utts[j].ear)
            probs[row] = [p[label] for label in sw.LABELS]
    pred = probs.argmax(axis=1)
    lines = [f"発話 {len(y)}（{', '.join(f'{lab} {int((y == i).sum())}' for i, lab in enumerate(sw.LABELS))}）",
             "セッションを抜いた当たり（行 = 真、列 = 当てたもの）:",
             "          " + "".join(f"{lab:>8}" for lab in sw.LABELS)]
    for i, lab in enumerate(sw.LABELS):
        lines.append(f"{lab:>8}  " + "".join(f"{int(((y == i) & (pred == j)).sum()):>8}" for j in range(len(sw.LABELS))))
    # ライブの規則で読めなかった（読みが真と違う）アクションの発話と、雑談
    missed = [r for r, j in enumerate(idx) if y[r] != 0 and not _heard_right(utts[j])]
    chatter = [r for r in range(len(y)) if y[r] == 0]
    for t in (0.5, 0.7, 0.8, 0.9):
        got = sum(1 for r in missed if probs[r, y[r]] >= t)
        false = sum(1 for r in chatter if probs[r, 1:].max() >= t)
        lines.append(f"確率 {t:.1f} 以上: ライブの規則で読めなかったアクション {got}/{len(missed)}・雑談をアクションと"
                     f"する {false}/{len(chatter)}")
    # 較正（いちばん確からしいラベルの確率の幅ごとの当たり）
    top = probs.max(axis=1)
    for lo, hi in ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)):
        m = (top >= lo) & (top < hi)
        if m.any():
            lines.append(f"確率 {lo:.1f}〜{min(hi, 1.0):.1f}: {int(m.sum())} 発話・当たり {float((pred[m] == y[m]).mean()):.0%}"
                         f"（確率の平均 {float(top[m].mean()):.2f}）")
    return lines


def _heard_right(u: Utterance) -> bool:
    if u.label == "amount":
        return any(t[0] == "wager" for t in u.heard)
    return any(t[0] == u.label or (u.label == "check" and t[0] == "check_around") for t in u.heard)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="短い語の分類器を開発データから作る・確かめる")
    ap.add_argument("command", choices=("eval", "train", "labels"))
    ap.add_argument("--out", default=str(sw.MODEL_PATH), help="train: 重みを書く場所")
    args = ap.parse_args(argv)
    import logging

    logging.disable(logging.WARNING)            # 開発データの再生で出るエンジンの警告は要らない
    utts = all_utterances()
    if args.command == "labels":
        for u in utts:
            print(json.dumps({"session": u.session[:8], "hand": u.hand_id, "start": u.start, "text": u.text,
                              "label": u.label, "heard": [list(t) for t in u.heard],
                              "ear": (u.ear or {}).get("text")}, ensure_ascii=False))
        return 0
    if args.command == "eval":
        print("\n".join(evaluate(utts)))
        return 0
    x, y, idx = dataset(utts)
    model = train(x, y)
    model.whisper = whisper_confusion([utts[i] for i in idx])
    Path(args.out).write_text(json.dumps(model.to_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{args.out}: 発話 {len(y)}・版 {model.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
