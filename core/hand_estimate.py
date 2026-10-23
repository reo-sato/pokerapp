"""core/hand_estimate.py — 推定したハンドを記録に重ねる（ADR-0056 D1: live ⊕ estimate ⊕ staff corrections）

推定器（`integration/estimator.py`）は、ハンドが終わるたびに記録した観測（発話・札・操作）から**ハンド全体で筋の通る
アクションの列**を探し、`logs/<sid>.estimate.json` に書く。読む側（お客さん向けの画面・スタッフの画面・真のアクション
入力・PHH の書き出し）は、ライブの記録（`logs/<sid>.json`, 暫定）にこれを重ねたものを**記録の本体**として読み、
その上にスタッフの訂正（ADR-0036）を重ねる。元の記録は変えない（推定し直せば上書きされる）。

推定のファイルの形（`hands` は hand_id → 推定）:

    {"tool": "estimator", "estimator_version": "...", "params_hash": "...", "session_id": "...",
     "hands": {"3": {"hand_id": 3, "started_at": "...", "estimated_at": "...", "changed": true,
                     "margin": 2.1, "posterior": 0.89, "hand": {…記録と同じ形のハンド…},
                     "alternatives": [...], "notes": [...]}}}
"""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ESTIMATE_SUFFIX = ".estimate.json"
# 推定のハンドで置き換えない（ライブの記録のまま）項目: 記録の識別・時刻・札の読み取り
_LIVE_FIELDS = ("session_id", "hand_id", "started_at", "ended_at", "board_timeline", "board_source")


def estimate_path(log_dir: str | Path, session_id: str) -> Path:
    return Path(log_dir) / f"{session_id}{ESTIMATE_SUFFIX}"


def load_estimates(log_dir: str | Path, session_id: str) -> dict[int, dict]:
    """hand_id → 推定（ハンドの丸ごとがあるものだけ）。無い・壊れているときは空。"""
    path = estimate_path(log_dir, session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("推定のファイルを読めません（記録のまま使います）: %s (%s)", path, e)
        return {}
    hands = data.get("hands") if isinstance(data, dict) else None
    out: dict[int, dict] = {}
    if isinstance(hands, dict):
        for key, entry in hands.items():
            if isinstance(entry, dict) and isinstance(entry.get("hand"), dict):
                try:
                    out[int(key)] = dict(entry, estimator_version=data.get("estimator_version"),
                                         params_hash=data.get("params_hash"))
                except (TypeError, ValueError):
                    continue
    return out


def _matches(hand: dict, entry: dict) -> bool:
    """同じハンドの推定か（hand_id と始まりの時刻）。記録し直した・別のセッションの推定は使わない。"""
    est = entry.get("hand") or {}
    if est.get("hand_id") != hand.get("hand_id"):
        return False
    started = entry.get("started_at")
    return not started or not hand.get("started_at") or started == hand.get("started_at")


def apply_estimate(hand: dict, entry: Optional[dict]) -> dict:
    """ライブの記録のハンドに推定を重ねたハンド（元は変えない）。推定が無い・合わないときはライブのまま。

    - アクション・勝者・ポット・持ち点の結果・ボタンとポジションは推定のもの。
    - 記録の識別・時刻・札の読み取り（`_LIVE_FIELDS`）と、席の人（player_id・名前）はライブの記録のもの。
    - `estimate` に推定の由来（版・差・事後確率・次点）、`_live` にライブの記録のアクションと勝者を残す
      （画面では「ライブではこう出ていた」を出せる）。
    """
    if not entry or not _matches(hand, entry):
        return hand
    est = deepcopy(entry["hand"])
    out: dict[str, Any] = dict(est)
    for key in _LIVE_FIELDS:
        if key in hand:
            out[key] = deepcopy(hand[key])
    live_players = {p.get("seat"): p for p in hand.get("players") or [] if isinstance(p, dict)}
    for p in out.get("players") or []:
        live = live_players.get(p.get("seat"))
        if live is None:
            continue
        for key in ("player_id", "name"):
            if key in live:
                p[key] = live[key]
    out["estimate"] = {
        "estimator_version": entry.get("estimator_version"),
        "params_hash": entry.get("params_hash"),
        "estimated_at": entry.get("estimated_at"),
        "changed": bool(entry.get("changed")),
        "margin": entry.get("margin"),
        "posterior": entry.get("posterior"),     # 較正していない（確率として画面に出さない = 監査 2 回目）
        "alternatives": deepcopy(entry.get("alternatives") or []),
        "notes": list(entry.get("notes") or []),
    }
    # 記録の本体の「要確認」= 推定の要確認（理由が 1 つでもある）。推定のファイルに無ければ推定のハンドのまま
    if "review" in entry:
        out["review_required"] = bool(entry.get("review"))
    out["_live"] = {"actions": deepcopy(hand.get("actions") or []), "winner_seat": hand.get("winner_seat"),
                    "button_seat": hand.get("button_seat")}
    return out


def overlay_log(log: dict, estimates: dict[int, dict]) -> dict:
    """セッションの記録（`{"session_id", "hands": [...]}`）の各ハンドに推定を重ねた記録。"""
    if not estimates:
        return log
    out = dict(log)
    out["hands"] = [apply_estimate(h, estimates.get(h.get("hand_id"))) if isinstance(h, dict) else h
                    for h in log.get("hands") or []]
    return out


def load_record(log_dir: str | Path, session_id: str) -> Optional[dict]:
    """記録の本体（ライブの記録 ⊕ 推定）。記録が無い・壊れているときは None。"""
    path = Path(log_dir) / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        log = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Could not load hand log %s (%s), treating as absent.", path, e)
        return None
    if not isinstance(log, dict):
        return None
    return overlay_log(log, load_estimates(log_dir, session_id))
