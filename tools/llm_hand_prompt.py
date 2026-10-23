#!/usr/bin/env python3
"""tools/llm_hand_prompt.py — LLM に渡す 1 ハンド分の入力と、固定の説明文（LLM の提案の実験）

推定器（ADR-0056）の候補に LLM の解釈を足す実験（`tools/llm_candidates.py`）の入力側。推定器の窓（`HandWindow`）
と同じ範囲の観測を、決まった順の JSON にする:

- 卓: 配られた席・ボタン・ブラインド・始めの持ち点・手番の順・手札（RFID）
- RFID: 配った時刻・ボードの札（何枚目・時刻）・席の札の離脱（離れた時刻・戻った時刻）
- 発話: 窓の中の書き起こし（Whisper の文と確からしさ・第 2 の耳の文と上位の候補・いまの読み取りの読み）

**送らないもの**: 名前（席番号だけ）・真のアクション・メモ（`notes`）・音声。時刻はハンドの始まりからの秒。

推定器（`integration/estimator.py`）からは import しない = 推定器の内容の指紋（`content_hash`）に入らない。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 説明文や入力の形を変えたら上げる（キャッシュの鍵に入る = 古い答えを使い回さない）
PROMPT_VERSION = "llm-proposer-v1"

SYSTEM_PROMPT = """\
あなたは日本のアミューズメントポーカー店（ノーリミット・テキサスホールデム）のハンド記録を復元する係です。
卓のディーラーの口元のマイクを音声認識した書き起こしと、RFID で読んだ札の記録から、1 ハンドのアクションの列を
復元してください。

# 入力の読み方
- `utterances`: ディーラーの発話。`text` は Whisper の書き起こしで、聞き間違い・幻聴・雑談・言い直しを多く含む。
  `ear` は別の音声認識（第 2 の耳）の文と上位の候補、`ear_amounts` はその耳が聞いた額の候補。`read` はいまの
  読み取り規則がその発話から読んだアクション（誤っていることがある）。`conf` は Whisper の確からしさ（0〜1）。
- `table`: 配られた席・ボタン・ブラインド・始めの持ち点。`preflop_order` / `postflop_order` が手番の順。
- `rfid.board`: ボードの札（`index` 1〜3 がフロップ、4 がターン、5 がリバー）と読んだ時刻。
- `rfid.departures`: 席の札がリーダーから離れた時刻（`back` は戻った時刻、null は戻らなかった）。
  戻らなかった離脱はフォールドのことが多い。ただしプレイヤーが札を持ち上げただけのこともある。
  ショーダウンで札を見せる・片付けるときも離れる。
- 時刻はすべてハンドの始まりからの秒。

# この店の言い方
- ディーラーは席番号をほとんど言わない。誰のアクションかは手番の順で決まる。
- 額は「2千500」「1万6千100」「5千点」のように言う。「センテン」「先天」「宣伝」は「千点」の聞き間違いのことがある。
- 「チェック、アンド」「チェックアラウンド」は残り全員がチェック（ストリートが変わる）。「N ウェイ」は人数の宣言。
- 「失礼しました」は直前の言い間違いの訂正の合図。同じアクションや額を言い直すこともある（2 回目は新しい
  アクションではない）。
- 役の名前（「ツーペア」「フラッシュ」など）はショーダウンで札を見せた意味。見せたあとの「フォールド」は次の人の
  マック（見せずに降りる）。
- 「ご視聴ありがとうございました」「これで終わりです」のような文は音声認識の幻聴で、アクションではない。
- 人数の多い卓では、ディーラーはフォールド・コール・チェックを毎回は言わない（言われないアクションがある）。
  フォールドは札の離脱で分かることが多い。
- そのストリートのボードの札が出る前に、そのストリートのアクションは無い。

# 規則
- ルールに合うアクションの列だけを答える（手番の順・最小レイズ・持ち点を超えない）。
- 額の書き方: call は**そのアクションで足したチップ**。bet / raise / allin は**そのストリートで
  そのプレイヤーが出した合計**（レイズは「いくらにレイズしたか」）。fold / check は 0。
  持ち点を全部出すアクションは allin（額 = そのストリートで出した合計）。
- 勝者が決まらない（全員降りたかショーダウンかが分からない）ときは winner_seat を null にする。

# 答え方
解釈を確からしい順に 1〜3 個。各解釈で:
- `utterances`: 入力のすべての発話について、何だったか（`kind`）と、アクションなら言われたアクション（`actions`）。
  `kind`: action（ベッティングのアクションを言った）/ muck（ショーダウンで見せずに降りた人の「フォールド」）/
  noise（雑談・幻聴・人数や役の宣言などアクションでない）/ restated（前のアクションの言い直し）/
  correction（訂正の合図）。1 つの発話に複数のアクションが入ることがある（「レイズ8千、フォールド、
  フォールド」）。`seat` は分かるときだけ（手番の順から決まるなら書いてよい）。
- `silent_actions`: 言われなかった fold / check / call と、どの発話の直後か（`after_index`。最初の発話より前なら -1）。
- `actions`: ハンド全体のアクションの列（ストリート・席・種類・額）。ブラインドの支払いは入れない。
- `button_seat`: ボタンの席（入力と違うと思うときだけ書き換える）。
- `winner_seat`、`confidence`（0〜1）、`note`（判断の根拠を 1〜2 文）。
"""

_ACTION_WORDS = {"fold": "フォールド", "check": "チェック", "call": "コール", "bet": "ベット",
                 "raise": "レイズ", "allin": "オールイン"}


def prompt_hash() -> str:
    """説明文の版（キャッシュの鍵・記録の出所）。"""
    return hashlib.sha256((PROMPT_VERSION + "\n" + SYSTEM_PROMPT).encode("utf-8")).hexdigest()[:12]


def input_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _r(x: Optional[float], nd: int = 1) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _reading(events: list) -> list[str]:
    """いまの読み取りが発話から読んだアクション（"raise 8000" の形）。"""
    out = []
    for e in events:
        amount = int(e.amount or 0)
        out.append(f"{e.action} {amount}" if amount else str(e.action))
    return out


def hand_input(est, w) -> dict:
    """推定器の窓 1 つ分の観測を、LLM に渡す決まった形の dict にする。"""
    from core.positions import seat_order_from_button
    from core.events import RFIDEvent
    from integration.world_replay import read_utterance

    t0 = float(w.start)
    events, rows = est._window_inputs(w)
    players = sorted(w.base.get("players") or [], key=lambda p: p["seat"])
    seats = [p["seat"] for p in players]
    blinds = w.base.get("blinds") or {}
    button = w.button
    order = seat_order_from_button(seats, button) if seats and button is not None else seats
    if len(order) == 2:
        preflop = list(reversed(order))       # ヘッズアップはボタン（= SB）から
    else:
        preflop = order[2:] + order[:2]
    table = {
        "seats": seats,
        "button_seat": button,
        "sb": int(blinds.get("sb") or est.setup.get("sb") or 0),
        "bb": int(blinds.get("bb") or est.setup.get("bb") or 0),
        "bb_ante": bool(blinds.get("bb_ante")),
        "stacks": {str(p["seat"]): p.get("stack_start") for p in players},
        "preflop_order": preflop,
        "postflop_order": order,
        "hole_cards": {str(p["seat"]): p.get("hole_cards") for p in players if p.get("hole_cards")},
    }
    board = []
    deal_at = None
    for e in events:
        if not isinstance(e, RFIDEvent):
            continue
        if e.kind == "deal" and deal_at is None:
            deal_at = _r((e.observed_at if e.observed_at is not None else e.timestamp) - t0)
        if e.kind == "card" and e.role == "board" and e.board_index and w.start <= e.timestamp < w.end:
            board.append({"index": int(e.board_index), "card": e.card, "t": _r(e.timestamp - t0)})
    seen: set[int] = set()
    board = [b for b in sorted(board, key=lambda b: (b["index"], b["t"]))
             if not (b["index"] in seen or seen.add(b["index"]))]
    departures = []
    if est.presence is not None:
        p = est.params
        for seat, t, back in est.presence.departures(w.start, w.end, p["blip_sec"], p["flicker_sec"]):
            departures.append({"seat": seat, "t": _r(t - t0), "back": _r(back - t0) if back is not None else None})
    departures.sort(key=lambda d: (d["t"], d["seat"]))
    utterances = []
    for i, row in enumerate(sorted(rows, key=lambda r: r["utterance_start_ts"])):
        ear = row.get("ear") or {}
        utterances.append({
            "i": i,
            "t": _r(row["utterance_start_ts"] - t0),
            "dur": _r(row.get("audio_sec")),
            "text": row.get("text") or "",
            "conf": _r(row.get("confidence"), 2),
            "ear": ear.get("text"),
            "ear_top": [c.get("text") for c in (ear.get("candidates") or [])[:3]],
            "ear_amounts": [int(a[0]) for a in (ear.get("amounts") or [])[:3]],
            "read": _reading(read_utterance(row)),
        })
    return {
        "hand_id": w.hand_id,
        "table": table,
        "rfid": {"deal_t": deal_at, "board": board, "departures": departures},
        "utterances": utterances,
    }


def window_utterance_starts(est, w) -> list[float]:
    """`hand_input` の `utterances[i]` の i → 発話の始まりの時刻（推定器の read / insert の `at`）。"""
    _events, rows = est._window_inputs(w)
    return [r["utterance_start_ts"] for r in sorted(rows, key=lambda r: r["utterance_start_ts"])]


def spoken_text(actions: list[dict], with_seat: bool = True) -> str:
    """LLM の言ったアクションを、いまの読み取り（`parse_actions`）が読める文にする（例「シート5 レイズ 8000」）。"""
    parts = []
    for a in actions:
        word = _ACTION_WORDS.get(a.get("action") or "")
        if word is None:
            continue
        seat = a.get("seat")
        amount = int(a.get("amount") or 0)
        text = (f"シート{int(seat)} " if with_seat and seat else "") + word
        if amount and a.get("action") in ("bet", "raise", "allin"):
            text += f" {amount}"
        parts.append(text)
    return "、".join(parts)
