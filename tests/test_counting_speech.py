"""チップを数える声はアクションではない・「3人です」は残りの人数（オーナー 2026-10-23。10/19 a64ec4ad ハンド 11 の
「5万、6万、7万、8万」をベット 4 つにしていた）。額だけの語が 3 つ以上続けて大きくなる並びは数え上げ。"""
from __future__ import annotations

import pytest

from audio.recognizer import parse_actions


@pytest.mark.parametrize("text", ["5万、6万、7万、8万", "7千、8千、9千。", "1万、2万、3万、4万、5万、6万、7万、8万、9万、10万"])
def test_counting_is_not_an_action(text):
    assert parse_actions(text) == []


def test_the_actions_around_the_counting_stay():
    assert [(e.action, e.amount) for e in parse_actions("フォールド、5万、6万、7万")] == [("fold", 0)]


@pytest.mark.parametrize("text, expected", [
    ("2万、4万", [("bet", 20000), ("bet", 40000)]),               # 2 つはベットとレイズのことがある
    ("1万、2千、フォールド。", [("bet", 10000), ("bet", 2000), ("fold", 0)]),   # 大きくなっていかない
    ("レイズ800、8000", [("raise", 800), ("bet", 8000)]),
])
def test_other_amounts_are_read(text, expected):
    assert [(e.action, e.amount) for e in parse_actions(text)] == expected


@pytest.mark.parametrize("text, expected", [
    ("3人です。", [("players_left", 3)]), ("4人です", [("players_left", 4)]), ("3人でーす。", [("players_left", 3)]),
    ("2人です", [("heads_up", 0)]),
    ("レイズ1万5千、フォールド、フォールド、コール。3人です。",
     [("raise", 15000), ("fold", 0), ("fold", 0), ("call", 0), ("players_left", 3)]),
])
def test_n_people_is_the_players_left(text, expected):
    """「3人です」= 残りの人数（10/19 a64ec4ad ハンド 21 の注釈）。"""
    assert [(e.action, e.amount) for e in parse_actions(text)] == expected


@pytest.mark.parametrize("text", ["7人", "はい、10人です。", "12人です", "犬2人と子供2人だから。"])
def test_other_n_people_is_not(text):
    assert [e.action for e in parse_actions(text)] == []
