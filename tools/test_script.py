"""tools/test_script.py — 台本のハンド（正解が先に分かっているハンド, テスト方針 週 1, 2026-09-30）

店舗の真のアクションは記録を見ながら入れるので、測る側の誤りが混ざり（`docs/worklog/2026-09-29-test-strategy.md`:
誤りの約 17%）、一人テストでは 3 人・まれな場面なしのデータしか増えない。台本のハンドは、アクションの列を先に作って
ディーラーがそれを読む = **正解は台本**（入力しない）。

- **声だけ**（`voice`）: 札は置かない 4〜9 人の仮想の卓。ハンドロガーを RFID なし・台本の卓の設定で起動し
  （`python main.py --cli --log-file --script voice`, ショートカット「台本のハンド (声だけ)」）、真のアクション入力の画面の
  「台本」（`/script`）にハンドを 1 つずつ出す。「このハンドを始める」で、ロガーは台本のボタンと持ち点で新しいハンドを
  始める（制御 `script_hand`。前のハンドの聞き違い・やり直しで持ち点やボタンがずれても、正解と同じ卓から始まる）。
  ディーラーは台本の行（「フォールド」「レイズ 600」…）を読むだけ。まれな場面（オールイン・サイドポット・ショーダウンで
  見せずに降りる・チョップ・チェックアラウンド・ヘッズアップ・続けて言う・ポジションを付けて言う）を決まった割合で入れる。
- **札の確認**（`cards`）: 実際の札と RFID の手順（配った順・持ち上げ・卓の中央へのマック・フロップを同時に・差し直し）。
  ロガーは席 4・5・6 の決まった卓で RFID ありで起動する（`--script cards`, ショートカット「札の確認 (RFID)」）。
- 画面の操作（始めた・やり直した・終わった・行を読んだ）は `logs/<セッション>.script_marks.jsonl` に時刻つきで残る。
  台本そのものは `logs/<セッション>.script.json`。`tools/eval_store.py` が台本を真のアクションとして評価する。

使い方（開発側）:

    python tools/test_script.py voice --seed 1 --hands 5     # 声だけの台本を表示（確かめる）
    python tools/test_script.py cards                         # 札の確認の手順
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.game_state import PlayerState  # noqa: E402
from core.positions import next_button  # noqa: E402

SCRIPT_VERSION = 1
SCRIPT_SUFFIX = ".script.json"
MARKS_SUFFIX = ".script_marks.jsonl"
DEFAULT_HANDS = 30
DEFAULT_SEATS = 6
SB, BB = 100, 200
BASE_STACK = 20000
# 1 ハンドのアクションの数の目安（読むのに 1 分かからないように）。超えたら大きなアクションを減らし、さらに超えたら畳む
_LONG_HAND = 12
_TOO_LONG = 18

# 場面の割合（声だけの台本）。normal 以外は店舗の一人テストではほとんど起きない場面
SCENARIOS = (
    ("normal", 0.50),
    ("multiway", 0.10),          # 何人もコールしてフロップへ（チェックアラウンド）
    ("allin", 0.10),             # 短いスタックのオールインにコール 1 人
    ("side_pot", 0.06),          # 短いスタックのオールインにコール 2 人 → サイドポット
    ("showdown_muck", 0.10),     # ショーダウンで見せずに降りる（「フォールド」）
    ("chop", 0.04),              # 2 人で分ける（「シート2 シート4 チョップ」）
    ("heads_up", 0.10),          # レイズとコールの 2 人（「ヘッズアップ」）
)
SCENARIO_LABELS = {
    "normal": "ふつう", "multiway": "何人もフロップへ", "allin": "オールイン", "side_pot": "サイドポット",
    "showdown_muck": "ショーダウンで見せずに降りる", "chop": "チョップ", "heads_up": "ヘッズアップ",
}
STREET_LABELS = {"preflop": "プリフロップ", "flop": "フロップ", "turn": "ターン", "river": "リバー",
                 "showdown": "ショーダウン"}
# ポジションを付けて言うときの言い方（読み取りが知っている言い方）
_SPOKEN_POSITION = {"BTN": "ボタン", "SB": "スモール", "BB": "ビッグ", "CO": "カットオフ", "HJ": "ハイジャック",
                    "UTG": "UTG"}
_WORDS = {"fold": "フォールド", "check": "チェック", "call": "コール", "allin": "オールイン"}


def _amount_text(n: int) -> str:
    from tools.read_corpus import amount_text

    return amount_text(n)


def _round100(x: float) -> int:
    return max(100, int(round(x / 100.0)) * 100)


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    items = [(k, w) for k, w in weights.items() if w > 0]
    total = sum(w for _, w in items)
    r = rng.random() * total
    for k, w in items:
        r -= w
        if r <= 0:
            return k
    return items[-1][0]


class _Hand:
    """1 ハンドをエンジン（本番と同じ pokerkit の卓）で進めながら、台本の行と正解の行を作る。"""

    def __init__(self, n: int, seats: list[int], stacks: dict[int, int], button: int, scenario: str,
                 rng: random.Random) -> None:
        from core.poker_engine import PokerkitGameState

        self.n, self.scenario, self.rng = n, scenario, rng
        self.start = dict(stacks)
        self.gs = PokerkitGameState([PlayerState(seat=s, name=f"P{s}", stack=stacks[s]) for s in seats], SB, BB)
        self.gs.set_button(button)
        self.gs.new_hand()
        self.button = self.gs.button_seat
        self.positions = self.gs.position_map()
        self.actions: list[dict] = []          # 正解（`replay_legal` と同じ形: コールは追加額、ベット・レイズはトータル）
        self.lines: list[dict] = []            # 読む行
        self.plan: dict[str, Any] = {}
        self._prepare_plan()

    # ――― 場面の段取り ―――

    def _prepare_plan(self) -> None:
        order = self.gs.seats_to_act()          # プリフロップの手番の順
        if self.scenario in ("allin", "side_pot"):
            # いちばん短いスタック（レイズできるだけある席）がオールイン。いなければふつうのハンド
            can_shove = sorted((s for s in order if self.start[s] >= 3 * BB), key=lambda s: (self.start[s], s))
            if not can_shove:
                self.scenario = "normal"
                return
            self.plan = {"shover": can_shove[0], "callers": 1 if self.scenario == "allin" else 2, "shoved": False}
        elif self.scenario in ("showdown_muck", "chop", "heads_up"):
            opener, caller = self.rng.sample(order, 2)
            self.plan = {"pair": (opener, caller)}
        elif self.scenario == "multiway":
            k = min(len(order), self.rng.choice([3, 4]))
            self.plan = {"limpers": set(self.rng.sample(order, k))}

    # ――― 1 つのアクション ―――

    def _weights(self, ctx: Any, street: str) -> dict[str, float]:
        legal = ctx.legal_actions
        facing = ctx.amount_to_call > 0
        raises = sum(1 for a in self.actions if a["street"] == street and a["action"] in ("bet", "raise", "allin"))
        if street == "preflop":
            if not facing:
                w = {"check": 0.75, "raise": 0.25}
            elif raises == 0:
                w = {"fold": 0.45, "call": 0.2, "raise": 0.35}
            elif raises == 1:
                # コールする人が多いほど降りる（9 人の卓で 7 人がフロップを見るような長いハンドにしない）
                callers = sum(1 for a in self.actions if a["street"] == street and a["action"] == "call")
                fold = min(0.85, 0.55 + 0.1 * callers)
                w = {"fold": fold, "call": 0.9 - fold, "raise": 0.10}
            else:
                w = {"fold": 0.6, "call": 0.3, "allin": 0.1}
        elif not facing:
            w = {"check": 0.6, "bet": 0.4}
        elif raises <= 1:
            w = {"fold": 0.5, "call": 0.38, "raise": 0.12}
        else:
            w = {"fold": 0.55, "call": 0.37, "allin": 0.08}
        if len(self.actions) >= _TOO_LONG:           # 読むのが長すぎる: ベットして、ほかは降りる
            w = {"fold": 0.85, "call": 0.15} if facing else {"check": 0.5, "bet": 0.5}
        elif len(self.actions) >= _LONG_HAND:        # 長くなってきたら大きなアクションを減らす
            w = {k: (v if k in ("fold", "check", "call") else v * 0.1) for k, v in w.items()}
        allowed = {"fold": "fold" in legal, "check": "check" in legal, "call": "call" in legal,
                   "bet": "bet" in legal, "raise": "raise" in legal, "allin": "allin" in legal}
        return {k: v for k, v in w.items() if allowed.get(k)}

    def _planned(self, seat: int, ctx: Any, street: str) -> Optional[str]:
        """場面の段取りで決まっているアクション（無ければ None = ふつうに選ぶ）。"""
        legal = ctx.legal_actions
        p = self.plan
        if self.scenario in ("allin", "side_pot") and street == "preflop":
            if not p["shoved"]:
                if seat == p["shover"] and "allin" in legal:
                    p["shoved"] = True
                    return "allin"
                if "check" in legal:
                    return "check"
                limp = "call" in legal and ctx.amount_to_call <= BB - ctx.committed and self.rng.random() < 0.3
                return "call" if limp else "fold"
            if p["callers"] > 0 and "call" in legal:
                p["callers"] -= 1
                return "call"
            return "fold" if "fold" in legal else "check"
        if self.scenario in ("showdown_muck", "chop", "heads_up"):
            opener, caller = p["pair"]
            if street == "preflop":
                if seat == opener and not any(a["action"] == "raise" for a in self.actions):
                    return "raise" if "raise" in legal else "call"
                if seat in (opener, caller):
                    return "call" if "call" in legal else "check"
                return "fold" if "fold" in legal else "check"
            if self.scenario in ("showdown_muck", "chop"):
                # 2 人でショーダウンまで: 降りない
                return None if "fold" not in legal else ("call" if "call" in legal else "check")
        if self.scenario == "multiway" and street == "preflop":
            if seat in p["limpers"]:
                return "call" if "call" in legal else "check"
            return "fold" if "fold" in legal else "check"
        if self.scenario == "multiway" and street == "flop":
            return "check" if "check" in legal else None
        return None

    def _amount(self, action: str, ctx: Any, street: str) -> tuple[str, int]:
        """ベット / レイズの額（トータル）。スタックの 7 割を超えるならオールインにする。"""
        rng = self.rng
        if action == "bet":
            to = _round100(self.gs.pot * rng.choice([0.33, 0.5, 0.5, 0.66, 0.75, 1.0]))
        else:
            current = ctx.committed + ctx.amount_to_call
            if street == "preflop" and current <= BB:
                to = rng.choice([400, 500, 600, 600])
            else:
                to = _round100(current * rng.choice([2.5, 3.0]))
        to = max(to, ctx.min_raise)
        if ctx.max_raise and to >= 0.7 * ctx.max_raise:
            return "allin", 0
        return action, to

    def step(self) -> bool:
        gs = self.gs
        ctx = gs.legal_context()
        seat = ctx.actor_seat
        if seat is None:
            return False
        street = gs.street
        action = self._planned(seat, ctx, street) or _pick(self.rng, self._weights(ctx, street))
        amount = 0
        if action in ("bet", "raise"):
            action, amount = self._amount(action, ctx, street)
        # 正解の行（`tools/ground_truth_ui.replay_legal` と同じ形）
        if action in ("check", "call"):
            row_amount = ctx.amount_to_call
            action = "call" if ctx.amount_to_call > 0 else "check"
        elif action == "allin":
            row_amount = ctx.max_raise if ctx.max_raise else ctx.amount_to_call
        else:
            row_amount = amount
        if action == "fold" and "fold" not in ctx.legal_actions:
            action, row_amount = "check", 0
        gs.apply_action(seat, action, amount)
        self.actions.append({"street": street, "seat": seat, "action": action, "amount": row_amount})
        self.lines.append(self._line(seat, action, amount, street))
        return True

    def _line(self, seat: int, action: str, amount: int, street: str) -> dict:
        pos = self.positions.get(seat, "")
        if action in ("bet", "raise"):
            say = f"{'ベット' if action == 'bet' else 'レイズ'} {_amount_text(amount)}"
        else:
            say = _WORDS[action]
        if action in ("bet", "raise", "allin") and pos in _SPOKEN_POSITION and self.rng.random() < 0.15:
            say = f"{_SPOKEN_POSITION[pos]} {say}"
        return {"say": say, "seats": [seat], "positions": [pos], "street": street, "actions": 1}

    # ――― ハンドの終わり ―――

    def finish(self) -> dict:
        gs = self.gs
        while self.step():
            pass
        remaining = gs.get_active_seats()
        winner_seats: list[int]
        showdown = len(remaining) >= 2
        if not showdown:
            winner_seats = list(remaining)
            gs.end_hand(remaining[0])
        elif self.scenario == "showdown_muck" and len(remaining) == 2:
            order = [s for s in gs.acting_order() if s in remaining]
            mucker, winner = order[0], order[1]
            self.actions.append({"street": "showdown", "seat": mucker, "action": "fold", "amount": 0})
            self.lines.append({"say": "フォールド", "seats": [mucker], "positions": [self.positions.get(mucker, "")],
                               "street": "showdown", "actions": 1, "note": "見せずに降りる（マック）"})
            winner_seats = [winner]
            gs.end_hand(winner)
        elif self.scenario == "chop" and len(remaining) == 2:
            order = [s for s in gs.acting_order() if s in remaining]
            winner_seats = order
            self.lines.append({"say": f"シート{order[0]} シート{order[1]} チョップ", "seats": order,
                               "positions": [self.positions.get(s, "") for s in order], "street": "showdown",
                               "actions": 0, "note": "2 人で分ける"})
            gs.end_hand_split(order)
        else:
            pots = gs.current_pots()
            covering = set(remaining)
            for pot in pots:
                covering &= set(pot["eligible_seats"])
            winner = self.rng.choice(sorted(covering or remaining))
            winner_seats = [winner]
            self.lines.append({"say": f"シート{winner} ウィナー", "seats": [winner],
                               "positions": [self.positions.get(winner, "")], "street": "showdown", "actions": 0,
                               "note": "勝った席" + ("（サイドポットも）" if len(pots) > 1 else "")})
            gs.end_hand(winner)
        self._decorate()
        return {
            "n": self.n, "button": self.button, "stacks": {str(s): v for s, v in sorted(self.start.items())},
            "positions": {str(s): p for s, p in sorted(self.positions.items())}, "scenario": self.scenario,
            "lines": self.lines, "actions": self.actions, "winner_seat": winner_seats[0],
            "winner_seats": winner_seats, "showdown": showdown,
            "end_stacks": {str(s): v for s, v in sorted(gs.get_stacks().items())},
        }

    def _decorate(self) -> None:
        """読み方の変化: チェックアラウンド・ヘッズアップ・続けて言う（本番のディーラーの言い方）。"""
        rng = self.rng
        lines = self.lines
        # 全員がチェックしたストリートは「チェックアラウンド」とまとめて言うことがある
        for street in ("flop", "turn", "river"):
            idx = [i for i, ln in enumerate(lines) if ln["street"] == street and ln.get("actions")]
            if len(idx) >= 2 and all(lines[i]["say"] == "チェック" for i in idx) and rng.random() < 0.35:
                merged = {"say": "チェックアラウンド", "seats": [s for i in idx for s in lines[i]["seats"]],
                          "positions": [p for i in idx for p in lines[i]["positions"]], "street": street,
                          "actions": len(idx), "note": "全員チェック"}
                lines[idx[0]:idx[-1] + 1] = [merged]
        # プリフロップが 2 人で終わったら「ヘッズアップ」と言うことがある
        pre = [a for a in self.actions if a["street"] == "preflop"]
        folded = {a["seat"] for a in pre if a["action"] == "fold"}
        in_hand = [s for s in self.start if s in self.positions and s not in folded]
        later = any(a["street"] in ("flop", "turn", "river") for a in self.actions)
        if len(in_hand) == 2 and later and rng.random() < 0.5:
            at = max(i for i, ln in enumerate(lines) if ln["street"] == "preflop") + 1
            lines.insert(at, {"say": "ヘッズアップ", "seats": [], "positions": [], "street": "preflop",
                              "actions": 0, "note": "残り 2 人"})
        # 続けて言う: 額の無いアクション（フォールド・チェック・コール）が 2〜3 つ続くところをまとめる
        out: list[dict] = []
        for ln in lines:
            prev = out[-1] if out else None
            joinable = ln.get("actions") == 1 and ln["say"] in ("フォールド", "チェック", "コール") and not ln.get("note")
            if (prev is not None and joinable and prev.get("joined") is not False and prev["street"] == ln["street"]
                    and prev.get("actions", 0) >= 1 and prev["say"].split("、")[-1] in ("フォールド", "チェック", "コール")
                    and not prev.get("note") and prev["actions"] < 3 and rng.random() < 0.3):
                prev["say"] += "、" + ln["say"]
                prev["seats"] = prev["seats"] + ln["seats"]
                prev["positions"] = prev["positions"] + ln["positions"]
                prev["actions"] += 1
                prev["note"] = "続けて言う"
                continue
            out.append(dict(ln))
        self.lines = out


def generate_voice_script(seed: int, hands: int = DEFAULT_HANDS, seats: int = DEFAULT_SEATS) -> dict:
    """声だけの台本（`hands` ハンド・`seats` 人の仮想の卓）。同じ seed なら同じ台本。"""
    if not 2 <= seats <= 9:
        raise ValueError("席は 2〜9")
    rng = random.Random(seed)
    seat_list = list(range(1, seats + 1))
    stacks = {s: BASE_STACK for s in seat_list}
    short = rng.sample(seat_list, min(2, len(seat_list) - 1))
    stacks[short[0]] = 4000
    if len(short) > 1:
        stacks[short[1]] = 7000
    deep = [s for s in seat_list if s not in short]
    if deep:
        stacks[rng.choice(deep)] = 40000
    table = {"seats": seat_list, "stacks": {str(s): v for s, v in stacks.items()}, "sb": SB, "bb": BB,
             "first_button": seat_list[-1], "names": {str(s): f"P{s}" for s in seat_list}}
    out_hands = []
    button_prev: Optional[int] = None
    names, weights = zip(*SCENARIOS)
    for n in range(1, hands + 1):
        button = seat_list[-1] if button_prev is None else next_button(seat_list, button_prev)
        scenario = rng.choices(names, weights=weights)[0]
        hand = _Hand(n, seat_list, stacks, button, scenario, rng).finish()
        out_hands.append(hand)
        button_prev = hand["button"]
        stacks = {int(s): v for s, v in hand["end_stacks"].items()}
        for s in seat_list:                    # 飛んだ席は元の持ち点に戻す（台本のハンドは持ち点も台本どおり）
            if stacks[s] <= 0:
                stacks[s] = BASE_STACK
    return {
        "tool": "test_script", "version": SCRIPT_VERSION, "kind": "voice", "seed": seed,
        "created_at": datetime.now().isoformat(timespec="seconds"), "table": table, "hands": out_hands,
    }


# ───────────────────────── 札の確認（RFID） ─────────────────────────

CARDS_TABLE = {"seats": [4, 5, 6], "stacks": {"4": 10000, "5": 10000, "6": 10000}, "sb": SB, "bb": BB,
               "first_button": 6, "names": {"4": "A", "5": "B", "6": "C"}}

# 手順（席 4・5・6。1 ハンド目のボタンは席 6 = SB は席 4、BB は席 5、プリフロップの最初は席 6）
CARDS_STEPS = (
    {"id": "deal_order", "title": "ふつうに配る（SB から 1 枚ずつ）",
     "do": ["ボタンを席 6 に置く", "SB（席 4）から 1 枚ずつ時計回りに 2 周配る（4→5→6→4→5→6）",
            "「コール」「コール」「チェック」→ フロップを 1 枚ずつ置く →「チェック」×3 → ターン →「ベット 400」、"
            "席 5 と席 6 は札を外して「フォールド」「フォールド」"],
     "expect": "ボタンは席 6。席 4 の勝ち。フォールドは札が離れたので付く"},
    {"id": "button_forgot", "title": "ボタンを動かし忘れる",
     "do": ["ボタンは席 6 のまま（動かさない）", "SB（席 4）から 1 枚ずつ 2 周配る",
            "席 6 と席 4 は札を外して「フォールド」「フォールド」"],
     "expect": "ロガーが「配った順からボタンを席 6 に直しました」と知らせる（CLI の ●）。席 5 の勝ち"},
    {"id": "lift", "title": "札を持ち上げて戻す",
     "do": ["ボタンを席 4 に動かす", "SB（席 5）から 1 枚ずつ 2 周配る",
            "席 4 の人が札を持ち上げて見て、2 秒以内に戻す（2 回）", "「コール」「コール」「チェック」→ フロップ →「チェック」×3",
            "ターン →「ベット 600」「コール」→ 席 4 は札を外して「フォールド」→ リバー →「チェック」「チェック」→「ハンド終了」"],
     "expect": "持ち上げてもフォールドにならない。勝者は手札で決まる"},
    {"id": "muck", "title": "卓の中央へマック",
     "do": ["ボタンを席 5 に動かす", "SB（席 6）から 1 枚ずつ 2 周配る",
            "「レイズ 600」（席 5）→ 席 6 は札を卓の中央（ボードのリーダーの上）を通して捨てる →「コール」（席 4）",
            "フロップ →「チェック」（席 4）→「ベット 800」（席 5）→ 席 4 も札を中央を通して捨てる"],
     "expect": "中央を通した札はマック（フォールド）として付く。席 5 の勝ち"},
    {"id": "flop_at_once", "title": "フロップを 3 枚同時に置く",
     "do": ["ボタンを席 6 に動かす", "SB（席 4）から 1 枚ずつ 2 周配る", "「コール」「コール」「チェック」",
            "フロップの 3 枚を重ねて持ち、同時に 3 台のリーダーに置く", "「チェック」×3 → ターン →「チェック」×3 → リバー →「チェック」×3 →「ハンド終了」"],
     "expect": "フロップ 3 枚・ターン・リバーが正しい位置に入る"},
    {"id": "redeal_turn", "title": "ターンを置き直す",
     "do": ["ボタンを席 4 に動かす", "SB（席 5）から 1 枚ずつ 2 周配る", "「コール」「コール」「チェック」→ フロップ →「チェック」×3",
            "ターンに違う札を置き、3 秒たったら外して正しい札を同じリーダーに置く", "「チェック」×3 → リバー →「チェック」×3 →「ハンド終了」"],
     "expect": "ターンが置き直した札に差し替わる（要確認が付く）"},
)


def cards_script() -> dict:
    return {"tool": "test_script", "version": SCRIPT_VERSION, "kind": "cards",
            "created_at": datetime.now().isoformat(timespec="seconds"), "table": CARDS_TABLE,
            "steps": [dict(s) for s in CARDS_STEPS]}


# ───────────────────────── ファイル ─────────────────────────


def session_table(script: dict) -> dict:
    """ハンドロガーのセッション設定（`main.py` の `_prompt_session_config` と同じ形）。"""
    table = script["table"]
    seats = [int(s) for s in table["seats"]]
    first = int(table.get("first_button") or seats[-1])
    return {
        "players": [{"seat": s, "name": table.get("names", {}).get(str(s), f"P{s}"),
                     "stack": int(table["stacks"][str(s)]), "named": False} for s in seats],
        "sb": int(table["sb"]), "bb": int(table["bb"]),
        # 内部の表現は「最初のハンドで 1 つ進める前の席」
        "button_seat": seats[(seats.index(first) - 1) % len(seats)],
        "log_dir": "./logs",
    }


def write_session_script(log_dir: Path, session_id: str, script: dict) -> Path:
    path = Path(log_dir) / f"{session_id}{SCRIPT_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(script, session_id=session_id, started_at=datetime.now().isoformat(timespec="seconds"))
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_marks(path: Path) -> list[dict]:
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_mark(path: Path, row: dict) -> None:
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_script_session(log_dir: Path) -> Optional[str]:
    """いちばん新しい台本のセッション（`<sid>.script.json` が最後に書かれたもの）。"""
    files = sorted(Path(log_dir).glob(f"*{SCRIPT_SUFFIX}"), key=lambda p: p.stat().st_mtime)
    return files[-1].name[: -len(SCRIPT_SUFFIX)] if files else None


# ───────────────────────── 画面（真のアクション入力の「台本」） ─────────────────────────


class ScriptApp:
    """台本の画面の中身（`/api/script/...`）。真のアクション入力のサーバが持つ（`ground_truth_ui`）。

    いちばん新しい台本のセッション（`<sid>.script.json`）を出し、操作を `<sid>.script_marks.jsonl` に残す。
    声だけの台本の「このハンドを始める」は、ロガーへ制御 `script_hand`（台本のボタンと持ち点）を送る。
    """

    def __init__(self, log_dir: Path, clock: Any = None) -> None:
        import time

        self.log_dir = Path(log_dir)
        self.clock = clock or time.time

    def _paths(self, sid: str) -> tuple[Path, Path, Path]:
        return (self.log_dir / f"{sid}{SCRIPT_SUFFIX}", self.log_dir / f"{sid}{MARKS_SUFFIX}",
                self.log_dir / f"{sid}.control.jsonl")

    def state(self) -> dict:
        sid = latest_script_session(self.log_dir)
        if sid is None:
            return {"session": None}
        script_path, marks_path, _ = self._paths(sid)
        script = read_json(script_path) or {}
        marks = read_marks(marks_path)
        record = read_json(self.log_dir / f"{sid}.json") or {}
        hands = [h for h in record.get("hands") or [] if isinstance(h, dict)]
        starts = [m for m in marks if m.get("event") == "start"]
        attempts: dict[str, int] = {}
        for m in starts:
            attempts[str(m.get("hand"))] = attempts.get(str(m.get("hand")), 0) + 1
        return {
            "session": sid, "kind": script.get("kind"), "table": script.get("table"),
            "hands": script.get("hands") or [], "steps": script.get("steps") or [],
            "current": starts[-1].get("hand") if starts else None, "attempts": attempts,
            "steps_done": {m.get("step"): m.get("result") for m in marks if m.get("event") == "step"
                           and m.get("result") in ("ok", "ng")},
            "ended": any(m.get("event") == "end" for m in marks),
            "recorded_hands": len(hands),
            "last_recorded": ({"hand_id": hands[-1].get("hand_id"), "winner_seat": hands[-1].get("winner_seat")}
                              if hands else None),
            "scenario_labels": SCENARIO_LABELS, "street_labels": STREET_LABELS,
        }

    def _mark(self, sid: str, row: dict) -> None:
        append_mark(self._paths(sid)[1], {"t": round(self.clock(), 3), **row})

    def act(self, verb: str, body: dict) -> tuple[int, dict]:
        from core.control_queue import ControlCommandLog

        sid = latest_script_session(self.log_dir)
        if sid is None:
            return 409, {"code": "no_script", "message": "台本のセッションがありません（ロガーを台本で起動してください）"}
        script = read_json(self._paths(sid)[0]) or {}
        if verb == "start":
            if script.get("kind") != "voice":
                return 400, {"code": "invalid", "message": "声だけの台本ではありません"}
            hand = body.get("hand")
            spec = next((h for h in script.get("hands") or [] if h.get("n") == hand), None)
            if spec is None:
                return 400, {"code": "invalid", "message": f"ハンド {hand} は台本にありません"}
            stacks = {int(s): int(v) for s, v in spec["stacks"].items()}
            ControlCommandLog(self._paths(sid)[2]).append("script_hand", {"button": spec["button"], "stacks": stacks})
            self._mark(sid, {"event": "start", "hand": hand, "redo": bool(body.get("redo"))})
        elif verb == "line":
            if not isinstance(body.get("hand"), int) or not isinstance(body.get("line"), int):
                return 400, {"code": "invalid", "message": "hand と line が要ります"}
            self._mark(sid, {"event": "line", "hand": body["hand"], "line": body["line"]})
        elif verb == "step":
            steps = {s.get("id") for s in script.get("steps") or []}
            if body.get("step") not in steps or body.get("result") not in ("start", "ok", "ng"):
                return 400, {"code": "invalid", "message": "手順と結果が要ります"}
            note = str(body.get("note") or "")[:500]
            self._mark(sid, {"event": "step", "step": body["step"], "result": body["result"], "note": note})
        elif verb == "end":
            self._mark(sid, {"event": "end"})
        else:
            return 404, {"code": "not_found", "message": verb}
        return 200, self.state()

    def route(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        rest = path[len("/api/script/"):].strip("/") if path.startswith("/api/script/") else ""
        if method == "GET" and rest == "state":
            return 200, self.state()
        if method == "POST" and rest in ("start", "line", "step", "end"):
            return self.act(rest, body if isinstance(body, dict) else {})
        return 404, {"code": "not_found", "message": path}


# ───────────────────────── 評価（台本 = 真のアクション） ─────────────────────────


def _epoch(iso: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def script_steps(script: dict, marks: list[dict]) -> list[dict]:
    """札の確認の手順ごとの結果とメモ（最後に押した「できた / ちがった」）。メモは毎回読む（オーナーの指示）。"""
    results: dict[str, dict] = {}
    for m in marks:
        if m.get("event") == "step" and m.get("result") in ("ok", "ng"):
            results[m.get("step")] = m
    out = []
    for step in script.get("steps") or []:
        m = results.get(step.get("id"))
        if m is not None:
            out.append({"id": step["id"], "title": step.get("title"), "result": m["result"],
                        "note": (m.get("note") or "").strip(), "t": m.get("t")})
    return out


def script_truth(script: dict, marks: list[dict], recorded_hands: list[dict]) -> dict:
    """台本と画面の操作から、記録のハンドごとの真のアクション（`ground_truth.json` の hands と同じ形）を作る。

    台本のハンドを始めた（`start`）時刻のあと最初に始まった記録のハンドがそのハンド。同じ台本のハンドをやり直したら
    最後に始めたものだけを使う（前の分は `redone` に数える）。返り値の `script_hands` は評価に使った台本のハンドの番号。
    """
    if script.get("kind") != "voice":
        return {"hands": [], "script_hands": [], "redone": 0, "unmatched": []}
    by_n = {int(h["n"]): h for h in script.get("hands") or []}
    starts = [m for m in marks if m.get("event") == "start" and isinstance(m.get("hand"), int)]
    latest: dict[int, dict] = {}
    for m in starts:
        latest[m["hand"]] = m                 # やり直したら後の方
    redone = len(starts) - len(latest)
    recorded = sorted((h for h in recorded_hands if _epoch(h.get("started_at")) is not None),
                      key=lambda h: _epoch(h.get("started_at")))
    hands, used, unmatched = [], [], []
    for n, mark in sorted(latest.items(), key=lambda kv: kv[1].get("t", 0)):
        t = float(mark.get("t") or 0)
        # 画面の操作からロガーがハンドを始めるまでは 1 秒もかからない（制御の読み取りは 0.2 秒ごと）
        match = next((h for h in recorded if _epoch(h["started_at"]) >= t - 1.0
                      and _epoch(h["started_at"]) <= t + 30.0), None)
        spec = by_n.get(n)
        if match is None or spec is None:
            unmatched.append(n)
            continue
        recorded.remove(match)
        hands.append({
            "hand_id": match.get("hand_id"), "actions": [dict(a) for a in spec["actions"]],
            "winner_seat": spec.get("winner_seat"), "board": [], "script_hand": n,
            "scenario": spec.get("scenario"),
            "players": [{"seat": int(s), "hole_cards": []} for s in spec.get("stacks") or {}],
        })
        used.append(n)
    return {"hands": hands, "script_hands": used, "redone": redone, "unmatched": unmatched}


# ───────────────────────── 表示（確かめる用） ─────────────────────────


def format_hand(hand: dict) -> list[str]:
    pos = " ".join(f"{s}:{p}" for s, p in hand["positions"].items())
    out = [f"ハンド {hand['n']}（{SCENARIO_LABELS.get(hand['scenario'], hand['scenario'])}）ボタン 席{hand['button']}  {pos}"]
    street = None
    for ln in hand["lines"]:
        if ln["street"] != street:
            street = ln["street"]
            out.append(f"  ― {STREET_LABELS.get(street, street)}")
        who = "・".join(f"席{s}" for s in ln["seats"])
        note = f"（{ln['note']}）" if ln.get("note") else ""
        out.append(f"    「{ln['say']}」 {who}{note}")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="台本のハンド（声だけ / 札の確認）")
    sub = ap.add_subparsers(dest="kind", required=True)
    v = sub.add_parser("voice", help="声だけの台本を作って表示する")
    v.add_argument("--seed", type=int, default=None)
    v.add_argument("--hands", type=int, default=DEFAULT_HANDS)
    v.add_argument("--seats", type=int, default=DEFAULT_SEATS)
    v.add_argument("--json", action="store_true")
    sub.add_parser("cards", help="札の確認の手順を表示する")
    args = ap.parse_args(argv)
    if args.kind == "cards":
        for i, step in enumerate(CARDS_STEPS, 1):
            print(f"{i}. {step['title']}")
            for d in step["do"]:
                print(f"   - {d}")
            print(f"   期待: {step['expect']}")
        return 0
    seed = args.seed if args.seed is not None else random.randrange(1_000_000)
    script = generate_voice_script(seed, args.hands, args.seats)
    if args.json:
        print(json.dumps(script, ensure_ascii=False, indent=1))
        return 0
    print(f"台本 seed={seed}・{args.seats} 人・{args.hands} ハンド")
    for hand in script["hands"]:
        for line in format_hand(hand):
            print(line)
    return 0


SCRIPT_PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>台本のハンド</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin:0; padding:12px 16px 40px; font:16px/1.5 system-ui,-apple-system,"Hiragino Sans",sans-serif;
        background:#12151a; color:#e8eaed; }
 h1 { font-size:18px; margin:0; }
 a { color:#58a6ff; }
 .muted { color:#9aa0a6; } .small { font-size:13px; }
 .top { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:baseline; gap:8px; margin-bottom:6px; }
 .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:8px 0; }
 button { font:inherit; color:#e8eaed; background:#2d333b; border:1px solid #3a414d; border-radius:8px;
        padding:8px 14px; min-height:44px; cursor:pointer; }
 button.primary { background:#1f6feb; border-color:#1f6feb; }
 button.ok { background:#238636; border-color:#238636; }
 button.danger { background:#6e3b3b; border-color:#6e3b3b; }
 .big { width:100%; min-height:72px; font-size:24px; font-weight:700; border-radius:14px; }
 .row2 { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px; }
 .row2 button { min-height:52px; }
 .table { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 10px; font-size:13px; font-variant-numeric:tabular-nums; }
 .seat { background:#1e232b; border:1px solid #2d333b; border-radius:8px; padding:3px 8px; white-space:nowrap; }
 .seat.btn { border-color:#e3b341; }
 .seat b { color:#58a6ff; font-weight:600; }
 .hand-h { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; margin-top:6px; }
 .hand-h .n { font-size:20px; font-weight:700; }
 .tag { display:inline-block; font-size:12px; padding:1px 8px; border-radius:999px; background:#23262b; color:#9aa0a6; }
 .tag.warn { background:#3a2f1f; color:#e3b341; }
 .street { color:#9aa0a6; font-size:13px; margin:12px 0 2px; letter-spacing:.04em; }
 .line { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 12px; padding:8px 10px; border-radius:10px;
         border:1px solid transparent; cursor:pointer; }
 .line .say { font-size:26px; font-weight:700; }
 .line .who { color:#9aa0a6; font-size:13px; }
 .line.cur { background:#1a2a1e; border-color:#238636; }
 .line.done { opacity:.45; }
 .panel { background:#161a20; border:1px solid #2d333b; border-radius:12px; padding:12px 14px; margin:10px 0; }
 .step h2 { font-size:17px; margin:0 0 6px; }
 .step ul { margin:4px 0 8px 18px; padding:0; }
 .step.done { opacity:.6; }
 .step textarea { width:100%; min-height:60px; font:inherit; color:#e8eaed; background:#1e232b; border:1px solid #3a414d;
         border-radius:8px; padding:8px; }
 .expect { color:#7ee787; font-size:14px; }
 .hidden { display:none !important; }
 #toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#b62324; color:#fff;
        padding:10px 18px; border-radius:999px; z-index:20; max-width:90vw; }
</style></head>
<body>
<div id="app">読み込み中…</div>
<div id="toast" class="hidden"></div>
<script>
const S = {st:null, hand:null, line:0, busy:false, timer:null, notes:{}, pending:false};
// 文字の欄・ロールダウンを触っている間は画面を作り直さない（キーボードが閉じる。店舗 2026-09-30）。離れたら作り直す
function editing(){
  const a = document.activeElement, app = document.getElementById("app");
  if (!a || !app || !app.contains(a)) return false;
  if (a.tagName === "SELECT" || a.tagName === "TEXTAREA") return true;
  return a.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "range"].includes((a.type || "").toLowerCase());
}
function renderWhenFree(){ if (editing()) S.pending = true; else { S.pending = false; render(); } }
document.addEventListener("focusout", () => setTimeout(() => { if (S.pending && !editing()) renderWhenFree(); }, 0));
const $ = (id) => document.getElementById(id);
function esc(s){ return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function toast(msg){ const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 6000); }
function fmt(n){ return Number(n || 0).toLocaleString("ja-JP"); }
const OFFLINE = "サーバにつながりません。PC の「真のアクション入力 (iPad から)」の黒い窓が開いているか確かめてください（閉じていたら起動し直して、この画面を再読み込み）";
async function api(path, body){
  const opts = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)};
  let r;
  try { r = await fetch("/api/script/" + path, opts); } catch (e) { throw new Error(OFFLINE); }
  let d = null; try { d = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((d && d.message) || ("HTTP " + r.status));
  return d;
}
function lineKey(){ return "script_line:" + (S.st && S.st.session) + ":" + S.hand; }
function saveLine(){ try { localStorage.setItem(lineKey(), String(S.line)); } catch (e) {} }
function loadLine(){ try { return parseInt(localStorage.getItem(lineKey()) || "0", 10) || 0; } catch (e) { return 0; } }
async function poll(){
  clearTimeout(S.timer);
  try {
    S.st = await api("state");
    // メモを書いている間は描き直さない（キーボードが閉じる・書いた文字が消える）
    renderWhenFree();
  } catch (e) { $("app").innerHTML = `<p>読み込めません: ${esc(e.message)}</p>`; }
  S.timer = setTimeout(poll, 3000);
}
async function send(verb, body){
  if (S.busy) return;
  S.busy = true;
  try { S.st = await api(verb, body || {}); render(); } catch (e) { toast(e.message); } finally { S.busy = false; }
}
function startHand(n, redo){ S.hand = n; S.line = 0; saveLine(); send("start", {hand:n, redo:!!redo}); window.scrollTo(0, 0); }
function setLine(i){ S.line = i; saveLine(); api("line", {hand:S.hand, line:i}).catch(() => {}); render(); }
function nextLine(){
  const h = (S.st.hands || []).find(x => x.n === S.hand); if (!h) return;
  if (S.line < h.lines.length) setLine(S.line + 1);
}
function renderVoice(st){
  const hands = st.hands || [];
  const cur = st.current;
  const shown = cur == null ? 1 : cur;
  if (S.hand !== shown) { S.hand = shown; S.line = loadLine(); }
  const h = hands.find(x => x.n === shown);
  if (!h) { $("app").innerHTML = "<p>台本にハンドがありません。</p>"; return; }
  const started = cur != null;
  const next = shown + 1;
  const tries = (st.attempts || {})[String(shown)] || 0;
  const seats = st.table.seats.map(s => `<span class="seat ${s === h.button ? "btn" : ""}">席${s} <b>${esc(h.positions[String(s)] || "")}</b> ${fmt(h.stacks[String(s)])}</span>`).join("");
  let street = null, rows = "";
  h.lines.forEach((ln, i) => {
    if (ln.street !== street) { street = ln.street; rows += `<div class="street">${esc(st.street_labels[street] || street)}</div>`; }
    const who = ln.seats.map((s, k) => `席${s} ${esc(ln.positions[k] || "")}`).join("・");
    rows += `<div class="line ${started && i === S.line ? "cur" : ""} ${started && i < S.line ? "done" : ""}" onclick="setLine(${i})">
      <span class="say">「${esc(ln.say)}」</span><span class="who">${who}${ln.note ? " — " + esc(ln.note) : ""}</span></div>`;
  });
  if (!h.lines.length) rows = '<p class="muted">（読む行はありません）</p>';
  const done = h.lines.length && S.line >= h.lines.length;
  const foot = !started
    ? `<button class="primary big" onclick="startHand(${shown})">ハンド ${shown} を始める</button>`
    : (st.ended ? `<div class="panel">台本はここまでです。ロガーを q で終えてから「ログをまとめる (音声付き・送付用)」をダブルクリックしてください。</div>`
      : `<button class="${done ? "primary" : "ok"} big" onclick="${done ? (next <= hands.length ? `startHand(${next})` : "send('end')") : "nextLine()"}">${
          done ? (next <= hands.length ? `ハンド ${next} を始める` : "終わる") : "次の行 ▶"}</button>
        <div class="row2">
          ${next <= hands.length ? `<button onclick="startHand(${next})">ハンド ${next} を始める</button>` : `<button onclick="send('end')">終わる</button>`}
          <button onclick="startHand(${shown}, true)">やり直す（ハンド ${shown} を最初から）</button></div>`);
  $("app").innerHTML = `<div class="top"><h1>台本のハンド（声だけ）</h1><a class="small" href="/">← 真のアクション入力</a></div>
    <p class="muted small">「始める」を押してから、行を上から順に読んでください（卓で配るときと同じ声・間で）。読んだら「次の行」
      （押さなくてもかまいません）。言い間違えたら「やり直す」。</p>
    <div class="hand-h"><span class="n">ハンド ${shown} / ${hands.length}</span>
      <span class="tag">${esc(st.scenario_labels[h.scenario] || h.scenario)}</span>
      ${tries > 1 ? `<span class="tag warn">やり直し ${tries - 1} 回</span>` : ""}
      <span class="muted small">ボタン 席${h.button}・記録 ${st.recorded_hands} ハンド</span></div>
    <div class="table">${seats}</div>
    ${started ? "" : '<p class="muted small">まだ始めていません（下のボタンでロガーがこのハンドを始めます）。</p>'}
    <div>${rows}</div>
    <div style="margin-top:14px">${foot}</div>`;
}
function stepAct(id, result){
  const note = ($("note_" + id) || {}).value || "";
  send("step", {step:id, result:result, note:note});
}
function renderCards(st){
  const steps = st.steps || [];
  const done = st.steps_done || {};
  const cur = steps.findIndex(s => !done[s.id]);
  const items = steps.map((s, i) => `<div class="panel step ${done[s.id] ? "done" : ""}">
      <h2>${i + 1}. ${esc(s.title)} ${done[s.id] ? (done[s.id] === "ok" ? "✓" : "✗") : ""}</h2>
      <ul>${s.do.map(d => `<li>${esc(d)}</li>`).join("")}</ul>
      <div class="expect">期待: ${esc(s.expect)}</div>
      ${i === cur ? `<div class="bar"><button onclick="stepAct('${s.id}','start')">始める</button>
        <button class="ok" onclick="stepAct('${s.id}','ok')">できた</button>
        <button class="danger" onclick="stepAct('${s.id}','ng')">ちがった</button></div>
        <textarea id="note_${s.id}" placeholder="気づいたこと（ちがったときは何が起きたか）"
          oninput="S.notes['${s.id}']=this.value">${esc(S.notes[s.id] || "")}</textarea>` : ""}
    </div>`).join("");
  $("app").innerHTML = `<div class="top"><h1>札の確認（席 4・5・6）</h1><a class="small" href="/">← 真のアクション入力</a></div>
    <p class="muted small">手順ごとに「始める」→ やってみる →「できた」か「ちがった」。記録 ${st.recorded_hands} ハンド。</p>
    ${items}${cur < 0 ? '<div class="panel">全部終わりました。ロガーを q で終えてから「ログをまとめる (音声付き・送付用)」。</div>' : ""}`;
}
function render(){
  const st = S.st; if (!st) return;
  if (!st.session) {
    $("app").innerHTML = `<div class="top"><h1>台本のハンド</h1><a class="small" href="/">← 真のアクション入力</a></div>
      <div class="panel">台本のセッションがありません。デスクトップの「台本のハンド (声だけ)」か「札の確認 (RFID)」で
      ハンドロガーを起動すると、ここに出ます。</div>`;
    return;
  }
  if (st.kind === "voice") renderVoice(st); else renderCards(st);
}
poll();
</script>
</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
