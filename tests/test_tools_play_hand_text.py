"""tests/test_tools_play_hand_text.py

tools/play_hand_text.py（mic 不要のテキスト駆動ドライバ）のテスト。

- utterances_to_events: 空行/コメント/認識不能をスキップし、決定的 timestamp を付与
- run_text_hand(legacy): ハンドが確定し、アクションが記録される / 再現可能
- run_text_hand(pokerkit): 既定 backend でも確定する（importorskip）
"""
from __future__ import annotations

import pytest

from core.game_state import PlayerState
from tools.play_hand_text import run_text_hand, utterances_to_events

NARRATION = [
    "# 2-handed sample",
    "ハンド開始",
    "シート1 レイズ 600",
    "シート2 コール",
    "",
    "ショーダウン",
    "シート1 ウィナー",
]


def _players() -> list[PlayerState]:
    return [
        PlayerState(seat=1, name="P1", stack=30000),
        PlayerState(seat=2, name="P2", stack=30000),
    ]


class TestUtterancesToEvents:
    def test_parses_skips_and_timestamps(self):
        evs = utterances_to_events(NARRATION, start_ts=1000.0, step=1.0)
        assert [e.action for e in evs] == [
            "new_hand", "raise", "call", "showdown", "winner",
        ]
        assert evs[1].amount == 600
        assert evs[1].seat == 1
        assert evs[2].seat == 2
        assert [e.timestamp for e in evs] == [1000.0, 1001.0, 1002.0, 1003.0, 1004.0]

    def test_skips_unrecognized(self):
        assert utterances_to_events(["まったく無関係なテキスト"], start_ts=0.0) == []


class TestRunTextHandLegacy:
    def test_finalizes_hand(self, tmp_path):
        summaries = run_text_hand(
            NARRATION, backend="legacy", players=_players(),
            sb=100, bb=200, session_id="t1", out_dir=tmp_path,
            start_ts=1000.0, step=1.0,
        )
        assert len(summaries) == 1
        s = summaries[0]
        assert s.winner_seat == 1
        actions = [a.action for a in s.actions]
        assert "raise" in actions
        assert "call" in actions

    def test_writes_log_file(self, tmp_path):
        run_text_hand(
            NARRATION, backend="legacy", players=_players(),
            sb=100, bb=200, session_id="sess", out_dir=tmp_path,
            start_ts=1000.0, step=1.0,
        )
        assert (tmp_path / "sess.json").exists()

    def test_deterministic(self, tmp_path):
        common = dict(
            backend="legacy", players=_players(), sb=100, bb=200,
            start_ts=1000.0, step=1.0,
        )
        a = run_text_hand(NARRATION, session_id="a", out_dir=tmp_path / "a", **common)
        b = run_text_hand(NARRATION, session_id="b", out_dir=tmp_path / "b", **common)
        da, db = a[0].to_dict(), b[0].to_dict()
        # session_id 以外（timestamp 含む）は注入 clock により完全一致するはず
        da.pop("session_id")
        db.pop("session_id")
        assert da == db


class TestRunTextHandPokerkit:
    def test_finalizes_with_default_backend(self, tmp_path):
        pytest.importorskip("pokerkit")
        # check-facing-bet 相当: 3-handed で BB 直面の "チェック" → 合法手射影で call + needs_review
        narration = ["ハンド開始", "チェック", "シート1 ウィナー"]
        players = [
            PlayerState(seat=1, name="P1", stack=10000),
            PlayerState(seat=2, name="P2", stack=10000),
            PlayerState(seat=3, name="P3", stack=10000),
        ]
        summaries = run_text_hand(
            narration, backend="pokerkit", players=players,
            sb=100, bb=200, session_id="pk", out_dir=tmp_path,
            start_ts=1000.0, step=1.0,
        )
        assert len(summaries) == 1
        s = summaries[0]
        assert s.winner_seat == 1
        # rules-aware 経路: heard "check" が call に射影され review がつく
        assert s.review_required is True
        assert any(a.action == "call" for a in s.actions)
