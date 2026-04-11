"""output/phh_exporter.py

Phase 5: HandSummary を PHH (Poker Hand History) TOML 形式にエクスポートする。

PHH フォーマット仕様: https://arxiv.org/abs/2312.11753
  - TOML ファイル (.phh)
  - variant, antes, blinds_or_straddles, starting_stacks, actions, players 等

アクション変換表:
  bet / raise / allin  → "p{i} cbr {amount}"  (complete/bet/raise to total)
  call                 → "p{i} cc"             (call)
  check                → "p{i} cc"             (check)
  fold                 → "p{i} f"              (fold)

ストリート遷移時にボードカードのデール行を自動挿入する:
  preflop → flop  : "d db {card3}"  (3 枚、未知時は "??????")
  flop    → turn  : "d db {card1}"  (1 枚、未知時は "??")
  turn    → river : "d db {card1}"  (1 枚、未知時は "??")

ホールカードは常に不明 ("????") として扱う（RFID 対応は Phase 7）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from core.constants import STREET_ORDER
from core.hand_log import ActionRecord, HandSummary

try:
    from pokerkit import HandHistory as _PKHandHistory
    _POKERKIT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _POKERKIT_AVAILABLE = False

logger = logging.getLogger(__name__)

# PHH variant コード（No-Limit Texas Hold'em）
VARIANT_NT = "NT"

# ストリート → 新規ボードカード枚数
_STREET_NEW_CARDS = {"flop": 3, "turn": 1, "river": 1}


class PHHExporter:
    """HandSummary を PHH (TOML) 形式の文字列またはファイルに変換する。"""

    def __init__(self, author: str = "PokerHandLogger") -> None:
        self._author = author

    # ――― 公開 API ―――

    def export(self, summary: HandSummary) -> str:
        """HandSummary を PHH TOML 文字列に変換して返す。"""
        data = self._build_phh_dict(summary)
        return _to_toml(data)

    def write(self, summary: HandSummary, path: Path) -> None:
        """PHH TOML ファイルに書き出す。"""
        content = self.export(summary)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("PHH exported: %s", path)

    def export_hand(self, summary: HandSummary) -> str:
        """PokerKit HandHistory API を使って HandSummary を PHH TOML 文字列に変換する。

        FR-37: PokerKit の公式 HandHistory クラスを使用して TOML を生成する。
        hole_cards が None（非公開）の席は 1 枚あたり "??" で埋める。
        pokerkit が未インストールの場合は self.export() へフォールバックする。
        """
        if not _POKERKIT_AVAILABLE:
            logger.warning("pokerkit not available; falling back to custom TOML exporter")
            return self.export(summary)

        players_info = summary.players
        num_players = len(players_info)
        if num_players == 0:
            return self.export(summary)

        antes = tuple(0 for _ in range(num_players))
        sb = summary.blinds.get("sb", 0)
        bb = summary.blinds.get("bb", 0)
        blinds: list[int] = [0] * num_players
        if num_players >= 1:
            blinds[0] = sb
        if num_players >= 2:
            blinds[1] = bb
        blinds_tuple = tuple(blinds)

        starting_stacks = tuple(p.get("stack_start", 0) for p in players_info)
        player_names = tuple(p.get("name", f"Player{i+1}") for i, p in enumerate(players_info))

        phh_actions = tuple(_build_phh_actions(summary))

        kwargs: dict = {
            "variant": VARIANT_NT,
            "ante_trimming_status": True,
            "antes": antes,
            "blinds_or_straddles": blinds_tuple,
            "min_bet": summary.blinds.get("bb", 0),
            "starting_stacks": starting_stacks,
            "actions": phh_actions,
        }
        if player_names:
            kwargs["players"] = player_names
        if summary.hand_id:
            kwargs["hand"] = summary.hand_id
        if summary.started_at:
            kwargs["time"] = summary.started_at
        if self._author:
            kwargs["author"] = self._author
        if summary.review_required:
            kwargs["note"] = "contains actions requiring review"

        try:
            hh = _PKHandHistory(**kwargs)
            return hh.dumps()
        except Exception as exc:
            logger.warning("PokerKit HandHistory failed (%s); falling back to custom exporter", exc)
            return self.export(summary)

    def write_session(self, summaries: list[HandSummary], directory: Path) -> list[Path]:
        """セッション内の全ハンドを {hand_id:04d}.phh として書き出す。"""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for s in summaries:
            out = directory / f"{s.hand_id:04d}.phh"
            self.write(s, out)
            paths.append(out)
        return paths

    # ――― 内部 ―――

    def _build_phh_dict(self, summary: HandSummary) -> dict:
        players_info = summary.players  # list[dict]: seat, name, stack_start, ...
        seats = [p["seat"] for p in players_info]
        num_players = len(seats)

        antes = [0] * num_players
        blinds = _build_blinds_list(summary.blinds, num_players)
        starting_stacks = [p.get("stack_start", 0) for p in players_info]
        player_names = [p.get("name", f"Player{i+1}") for i, p in enumerate(players_info)]

        actions = _build_phh_actions(summary)

        data: dict = {
            "variant":               VARIANT_NT,
            "ante_trimming_status":  True,
            "antes":                 antes,
            "blinds_or_straddles":   blinds,
            "min_bet":               summary.blinds.get("bb", 0),
            "starting_stacks":       starting_stacks,
            "actions":               actions,
        }

        if player_names:
            data["players"] = player_names
        if summary.hand_id:
            data["hand"] = summary.hand_id
        if summary.started_at:
            data["time"] = summary.started_at
        if self._author:
            data["author"] = self._author
        if summary.review_required:
            data["note"] = "contains actions requiring review"

        return data


# ――― PHH アクション生成 ―――

def _build_phh_actions(summary: HandSummary) -> list[str]:
    """ActionRecord リストを PHH アクション文字列のリストに変換する。"""
    players_info = summary.players
    if not players_info:
        return []

    seats = [p["seat"] for p in players_info]
    seat_to_idx = {seat: i for i, seat in enumerate(seats)}

    actions: list[str] = []

    # プリフロップ: 全プレイヤーにホールカードをデール（未知）
    for i in range(len(seats)):
        actions.append(f"d dh p{i} ????")

    # ボードカードのトラッキング（summary.board が既知の場合に使用）
    board_cards = list(summary.board) if summary.board else []
    board_used = 0

    current_street = "preflop"

    for record in summary.actions:
        if record.action in ("showdown", "winner", "new_hand"):
            continue

        # ストリート遷移 → ボードカードをデール
        if record.street != current_street:
            transitions = _streets_between(current_street, record.street)
            for street in transitions:
                if street not in _STREET_NEW_CARDS:
                    continue
                n = _STREET_NEW_CARDS[street]
                known = board_cards[board_used: board_used + n]
                board_used += n
                if len(known) == n:
                    card_str = "".join(known)
                else:
                    card_str = "??" * n
                actions.append(f"d db {card_str}")
            current_street = record.street

        # アクション変換
        idx = seat_to_idx.get(record.seat)
        if idx is None:
            logger.warning("Unknown seat %d in ActionRecord, skipping", record.seat)
            continue
        phh_action = _record_to_phh(record, idx)
        if phh_action:
            actions.append(phh_action)

    return actions


def _record_to_phh(record: ActionRecord, player_idx: int) -> Optional[str]:
    """ActionRecord 1 件を PHH アクション文字列に変換する。"""
    p = f"p{player_idx}"
    action = record.action.lower()

    if action in ("bet", "raise", "allin"):
        return f"{p} cbr {record.amount}"
    if action == "call":
        return f"{p} cc"
    if action == "check":
        return f"{p} cc"
    if action == "fold":
        return f"{p} f"

    # 未知アクションはコメントとして残す
    logger.debug("Unknown action %r for PHH, skipping", action)
    return None


def _streets_between(from_street: str, to_street: str) -> list[str]:
    """from_street の次から to_street（含む）までのストリートを返す。"""
    try:
        start = STREET_ORDER.index(from_street) + 1
        end = STREET_ORDER.index(to_street) + 1
    except ValueError:
        return []
    return list(STREET_ORDER[start:end])


def _build_blinds_list(blinds: dict, num_players: int) -> list[int]:
    """{"sb": 100, "bb": 200} → [100, 200, 0, 0, ...] (num_players 長)。"""
    sb = blinds.get("sb", 0)
    bb = blinds.get("bb", 0)
    result = [0] * num_players
    if num_players >= 1:
        result[0] = sb
    if num_players >= 2:
        result[1] = bb
    return result


# ――― 最小限の TOML シリアライザ ―――

def _toml_value(val: object) -> str:
    """Python 値を TOML 値文字列に変換する（PHH で使う型のみ対応）。"""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(val, list):
        if not val:
            return "[]"
        items = [_toml_value(v) for v in val]
        # アクションリストなど長いリストは複数行
        if any(isinstance(v, str) and len(v) > 4 for v in val):
            inner = ",\n  ".join(items)
            return f"[\n  {inner},\n]"
        return "[" + ", ".join(items) + "]"
    raise TypeError(f"Unsupported TOML value type: {type(val)!r} for value {val!r}")


def _to_toml(data: dict) -> str:
    """dict を TOML 文字列に変換する（トップレベルのキーのみ）。"""
    lines = []
    for key, val in data.items():
        lines.append(f"{key} = {_toml_value(val)}")
    return "\n".join(lines) + "\n"
