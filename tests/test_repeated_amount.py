"""同じ額を 2 回以上区切って言った発話（店舗 2026-10-26 2bbd480e ハンド 7）。

ディーラーは額を確かめて言い直す（「1600? 1600。大丈夫です。」「どうする、あおりすぎ 3200 3200 ポールド」）。どちらも
真のアクションのベット・レイズだったが、会話が混ざっているので読んでいなかった。会話が混ざっていても同じ額を繰り返して
いればその額のベットかレイズと読む（要確認 = `repeated_amount`）。
"""
from __future__ import annotations

import pytest

from audio.recognizer import REPEATED_AMOUNT, parse_actions


def _read(text: str) -> list[tuple]:
    return [(e.action, e.amount, e.parse_flags) for e in parse_actions(text)]


@pytest.mark.parametrize("text, amount", [
    ("1600? 1600。大丈夫です。", 1600),
    ("どうする、あおりすぎ 3200 3200 ポールド", 3200),
    ("2千点、2千点です。お願いします", 2000),
])
def test_a_repeated_amount_is_a_wager(text, amount):
    assert _read(text) == [("bet", amount, ("amount_only", REPEATED_AMOUNT))]


@pytest.mark.parametrize("text", [
    "3000円も払ったの? 3000円も",     # 額だけの区切りが 1 つ
    "1万 2000",                          # 違う額
    "1600? 1600?",                       # 確かめただけ（疑問形だけ）
    "5、5、ちょっと待って",              # 小さい数
    "今日は寒いですね",
])
def test_other_talk_is_not_read(text):
    assert _read(text) == []


def test_an_amount_said_once_is_read_as_before():
    assert _read("1600") == [("bet", 1600, ("amount_only",))]
    assert _read("1600? 1600。") == [("bet", 1600, ("amount_only",))]


def test_a_repeated_amount_is_flagged_for_review(tmp_path):
    """記録では要確認（`repeated_amount` は読み方の情報ではない）。"""
    from integration.engine import _INFO_PARSE_FLAGS

    assert REPEATED_AMOUNT not in _INFO_PARSE_FLAGS
