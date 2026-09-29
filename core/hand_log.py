from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# 読めなかった札（ボードの位置は分かるが札が分からない。PHH の未知の札と同じ表記で、pokerkit も読める）
UNKNOWN_CARD = "??"


@dataclass
class ActionRecord:
    """1プレイヤー・1回のアクションを表す。

    監査フィールド（ADR-0047 G2, action schema の optional）は rules-aware 経路のみが埋める。
    None のフィールドは to_dict に出さない（legacy 経路の出力は従来どおり不変）。
    """

    hand_id: int
    timestamp: str  # ISO 8601 形式
    street: str
    seat: int
    player_name: str
    action: str
    amount: int
    pot_after: int
    stack_after: int
    source: dict  # {"camera": bool, "audio": bool, "rfid": bool}
    needs_review: bool
    confidence: float = 0.0  # 0.0–1.0 (FR-42: RFID+audio+camera 合意度)
    # そのアクションを行った席の **ポジション名**（BTN/SB/BB/UTG…, 仕様 §6.2, ISSUE-0032）。
    # ボタンを持たない backend（legacy）では空文字。additive。
    position: str = ""
    # ――― 監査フィールド（ADR-0009 §7 / ADR-0047 G2。needs_review の理由を逆引き可能にする）―――
    # actor_source: "spoken_seat" | "spoken_position" | "engine_prior" | "unresolved"。
    # "rfid" は旧記録にのみ現れる（RFID の検出は actor の証拠にしない = ISSUE-0033 / ADR-0056）。
    actor_source: Optional[str] = None
    corrected_from: Optional[str] = None    # 射影で action が変わった場合の修復前 raw ASR action
    reason: Optional[str] = None            # 射影/合成/競合の短い理由（"+区切りで複合）
    asr_confidence: Optional[float] = None  # Whisper 信頼度（欠測は None のまま）
    apply_ok: Optional[bool] = None         # pokerkit が受理したか（False = state 非反映のレコード）
    # そのアクションになった発話（Whisper の書き起こし、または CLI で打った読み上げ文）。
    # 合成した fold には無い。音声テストで「何と聞こえて何になったか」を追うため（ADR-0060, additive）。
    raw_text: Optional[str] = None

    def to_dict(self) -> dict:
        data = {
            "hand_id": self.hand_id,
            "timestamp": self.timestamp,
            "street": self.street,
            "seat": self.seat,
            "player_name": self.player_name,
            "action": self.action,
            "amount": self.amount,
            "pot_after": self.pot_after,
            "stack_after": self.stack_after,
            "source": self.source,
            "needs_review": self.needs_review,
            "confidence": self.confidence,
            "position": self.position,
        }
        if self.actor_source is not None:
            data["actor_source"] = self.actor_source
        if self.corrected_from is not None:
            data["corrected_from"] = self.corrected_from
        if self.reason:
            data["reason"] = self.reason
        if self.asr_confidence is not None:
            data["asr_confidence"] = self.asr_confidence
        if self.apply_ok is not None:
            data["apply_ok"] = self.apply_ok
        if self.raw_text:
            data["raw_text"] = self.raw_text
        return data


@dataclass
class HandSummary:
    """ハンド終了後に確定した情報を表す。"""

    hand_id: int
    session_id: str
    started_at: str  # ISO 8601
    ended_at: str  # ISO 8601
    blinds: dict  # {"sb": int, "bb": int}
    board: list[str]  # ショーダウン時のボードカード（未確定時は空リスト）
    board_source: str  # ボード情報のソース: "rfid" | "ocr" | "manual" | ""
    players: list[dict]  # {seat, name, hole_cards, stack_start, stack_end, result}
    pot_total: int
    winner_seat: Optional[int]  # None = 勝者が分からずチップを動かしていない（winner_source=undetermined）
    actions: list[ActionRecord]
    review_required: bool  # いずれかのアクションに needs_review=True があれば True
    # main/side pot スナップショット [{"amount": int, "eligible_seats": [int,...]}]。
    # rules-aware backend が end_hand 時に算出（legacy は []）。additive（F3 / R5）。
    pots: list = field(default_factory=list)
    # このハンドの **ディーラーボタンの席**（仕様 FR-05b, ISSUE-0032）。ボタンを持たない
    # backend（legacy）では None。`position_map` は seat → ポジション名（仕様 §6.1）。additive。
    button_seat: Optional[int] = None
    position_map: dict = field(default_factory=dict)
    # ボード各枚の **配布時刻**（RFID が最初にそのカードを検出した時刻）。
    # [{"index": 1..5, "card": "Qc", "dealt_at": ISO8601}]。index 昇順。
    # ターン/リバーの配布時刻はベッティングラウンドの区切りとして**アクションの時刻と対応**するため
    # 記録する（音声の時系列とハンド履歴を突き合わせて再生するため, ADR-0055）。
    # フロップは 3 枚の最小値がラウンドの開始。RFID 以外のソースでは空リスト。additive。
    board_timeline: list = field(default_factory=list)
    # split pot（チョップ）時の授与内訳 [{"seat": int, "amount": int}]（ADR-0050 S7, additive）。
    # 単独勝者の従来ハンドでは None = 出力に含めない（後方互換）。ショーダウンを手札で判定して
    # 2 人以上に配ったとき（引き分け・side pot の勝者が別）も入る（ADR-0062）。
    pot_awards: Optional[list] = None
    # 勝者を自動で決めたときの決まり方（ADR-0062, additive）: "fold"（ほかが全員フォールド / マック）|
    # "cards"（ショーダウンを RFID の手札とボードで判定。読めていない札があってもどの札でも同じ勝者）|
    # "estimated"（決まらないまま次の手札が配られた = 仮, 要確認）| "undetermined"（ショーダウンで札が読めず
    # 勝者が分からない = チップを動かさない, winner_seat は None, 要確認, オーナー 2026-09-29）。
    # ディーラーの宣言（`w` / 「ウィナー」）で決めたハンドは None = 出力に含めない。
    winner_source: Optional[str] = None
    # ショーダウンで見せた手札の役 [{"seat", "hole_cards", "hand", "best"}]（winner_source="cards" のとき）。
    showdown: Optional[list] = None
    # ディーラーが言った勝った役の名前（pokerkit の役名, 2026-09-26 additive）。言わなければ None = 出力に
    # 含めない。判定と違えば review_required。winner_source="announced" = 役名から勝者を決めた（要確認）。
    announced_hand: Optional[str] = None

    def to_dict(self) -> dict:
        data = {
            "hand_id": self.hand_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "blinds": self.blinds,
            "board": self.board,
            "board_source": self.board_source,
            "board_timeline": self.board_timeline,
            "button_seat": self.button_seat,
            "position_map": {str(k): v for k, v in self.position_map.items()},
            "players": self.players,
            "pot_total": self.pot_total,
            "pots": self.pots,
            "winner_seat": self.winner_seat,
            "actions": [a.to_dict() for a in self.actions],
            "review_required": self.review_required,
        }
        if self.pot_awards is not None:
            data["pot_awards"] = self.pot_awards
        if self.winner_source is not None:
            data["winner_source"] = self.winner_source
        if self.showdown is not None:
            data["showdown"] = self.showdown
        if self.announced_hand is not None:
            data["announced_hand"] = self.announced_hand
        return data


# ――― 表示用: コールの額をトータルで見せる（オーナー, 2026-09-29）―――
#
# 記録の `amount` はコールなら追加額（ベット・レイズ・オールインはトータル）。画面はコールもトータル
# （その人がそのストリートで出した合計）で見せる。記録は変えない（真のアクション・計測は追加額のまま）。

_BET_LIKE = frozenset({"bet", "raise", "allin", "all_in"})
_CALL_LIKE = frozenset({"call", "allin", "all_in"})


def _field(a: Any, key: str) -> Any:
    return a.get(key) if isinstance(a, Mapping) else getattr(a, key, None)


def _blind_of(
    seat: int, position: Any, blinds: Optional[Mapping[str, Any]], position_map: Optional[Mapping[Any, Any]],
) -> Optional[int]:
    """プリフロップにその席が払ったブラインド（ポジションから）。分からなければ None。"""
    positions = {str(k): v for k, v in (position_map or {}).items()}
    pos = position or positions.get(str(seat))
    sb, bb = (blinds or {}).get("sb"), (blinds or {}).get("bb")
    if not pos:
        return None
    if pos == "BTN":
        if not positions:
            return None                                # ヘッズアップ（ボタンが SB）か分からない
        pos = "BTN" if "SB" in positions.values() else "SB"
    if pos in ("SB", "BB"):
        blind = sb if pos == "SB" else bb
        return blind if isinstance(blind, int) else None
    return 0


def _after(action: Any, amount: int, prior: Optional[int]) -> Optional[int]:
    """アクションのあとの合計（記録の額から）。コールは前に出していた額 + 追加額。"""
    if action == "call":
        return None if prior is None else prior + amount
    if action in _BET_LIKE:
        return amount
    return prior                                       # チェック / フォールド


def street_flow(
    actions: Sequence[Any],
    stack_start: Mapping[Any, Any],
    *,
    blinds: Optional[Mapping[str, Any]] = None,
    position_map: Optional[Mapping[Any, Any]] = None,
) -> list[tuple[Optional[int], Optional[int]]]:
    """各アクションの (前に出していた額, あとの合計) — その人がそのストリートで出した額（表示用）。

    合計 = ストリートの始めの持ち点 − アクションのあとの持ち点（`stack_after`）。プリフロップはブラインドも入る
    （`stack_start` はブラインドを払う前, ADR-0047）。訂正した行（`_original`, ADR-0036）は持ち点が訂正前のままなので、
    前に出していた額 + 訂正後の額から出す。分からない値は None。`actions` は dict でも ActionRecord でもよい（記録の順）。
    """
    last: dict[int, int] = {}
    for seat, stack in (stack_start or {}).items():
        try:
            last[int(seat)] = int(stack)
        except (TypeError, ValueError):
            continue
    begin: dict[int, int] = {}
    put: dict[int, int] = {}                           # このストリートで出した額（記録どおり）
    street: Any = object()
    out: list[tuple[Optional[int], Optional[int]]] = []
    for a in actions:
        if _field(a, "street") != street:
            street = _field(a, "street")
            begin, put = dict(last), {}
        seat, after, action = _field(a, "seat"), _field(a, "stack_after"), _field(a, "action")
        if not isinstance(seat, int):
            out.append((None, None))
            continue
        original = _field(a, "_original")
        original = original if isinstance(original, Mapping) else {}
        was_action = original.get("action", action)
        was_amount = original.get("amount", _field(a, "amount")) or 0
        recorded = begin[seat] - after if isinstance(after, int) and seat in begin else None
        prior = put.get(seat)
        if prior is None:
            if street != "preflop":
                prior = 0
            elif recorded is not None and was_action == "call":
                prior = recorded - was_amount
            elif recorded is not None and was_action in ("check", "fold"):
                prior = recorded
            else:
                prior = _blind_of(seat, _field(a, "position"), blinds, position_map)
        if isinstance(after, int):
            last[seat] = after
        total = recorded if recorded is not None else _after(was_action, was_amount, prior)
        if total is not None:
            put[seat] = total
        if "action" in original or "amount" in original:
            total = _after(action, _field(a, "amount") or 0, prior)
        out.append((prior, total))
    return out


def street_totals(actions: Sequence[Any], stack_start: Mapping[Any, Any]) -> list[Optional[int]]:
    """各アクションのあと、その人がそのストリートで出した額の合計（`street_flow` の合計だけ）。"""
    return [total for _, total in street_flow(actions, stack_start)]


def hand_street_flow(hand: Mapping[str, Any]) -> list[tuple[Optional[int], Optional[int]]]:
    """ハンドの記録（`HandSummary.to_dict()`, 訂正を重ねたものも可）の `street_flow`。"""
    actions = [a for a in hand.get("actions") or [] if isinstance(a, Mapping)]
    return street_flow(
        actions, hand_stack_start(hand), blinds=hand.get("blinds"), position_map=hand.get("position_map"),
    )


def shown_amount(action: str, amount: int, total: Optional[int]) -> int:
    """画面に出す額: コール（とオールイン）はトータル（そのストリートで出した合計）、ほかは記録の額（ベット・レイズは
    もともとトータル。足りないオールインのコールは記録が追加額）。トータルが分からない・合わないときは記録の額。"""
    if action in _CALL_LIKE and total is not None and total >= (amount or 0):
        return total
    return amount or 0


def hand_stack_start(hand: Mapping[str, Any]) -> dict[int, int]:
    """ハンドの記録（`HandSummary.to_dict()`）の `players[].stack_start`。"""
    out: dict[int, int] = {}
    for p in hand.get("players") or []:
        if isinstance(p, Mapping) and isinstance(p.get("seat"), int) and isinstance(p.get("stack_start"), int):
            out[p["seat"]] = p["stack_start"]
    return out
