"""tests/test_review_page.py

開発データのハンドを真のアクションの入力画面と同じ形で見直すページ（`tools/review_page.py`, 2026-10-08）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools import review_page as rp

STORE = Path(__file__).resolve().parent / "fixtures" / "store"
FOLDER = STORE / "2026-10-06-e82f5005"


@pytest.fixture(scope="module")
def hand():
    pytest.importorskip("pokerkit")
    return rp.build_hand(FOLDER, 15, "リバーで席6 はフォールドしましたか？", estimate=False)


def test_the_hand_material(hand):
    assert hand["key"] == "e82f5005_15" and hand["hand_id"] == 15
    assert [s["seat"] for s in hand["seats"]] == [5, 6, 7, 8]
    assert hand["truth"]["winner_seat"] == 5 and hand["truth"]["actions"][-1]["action"] == "fold"
    assert hand["record"]["actions"] and hand["record"]["board"]
    assert hand["estimate"] is None                         # 推定器を回さない指定


def test_the_timeline_numbers_the_utterances_and_shows_the_owners_labels(hand):
    speech = [i for i in hand["timeline"] if i["kind"] == "speech"]
    assert [i["no"] for i in speech] == list(range(1, len(speech) + 1))
    assert all(i["wav"].startswith(f"e82f5005_h15_{i['no']:02d}_+") and i["wav"].endswith("s.wav") for i in speech)
    labelled = [i for i in speech if i["label"]]
    assert any(i["label_item"] == 54 and i["ear"] == "これそら" for i in labelled)
    assert any(i["kind"] == "board" for i in hand["timeline"])
    times = [i["t"] for i in hand["timeline"]]
    assert times == sorted(times)


def test_the_page_embeds_the_material_and_no_audio(hand, tmp_path):
    page = rp.render([hand])
    data = json.loads(re.search(r'<script id="review-data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["hands"][0]["key"] == "e82f5005_15"
    assert "<title>" in page.split("<style>")[0]              # 公開の作法: 先頭にタイトル
    assert "data:audio" not in page and "/audio/" not in page   # 音声の中身や置き場は入れない


def test_script_end_in_the_text_does_not_break_the_page():
    page = rp.render([{"key": "x_1", "question": "</script><b>", "timeline": []}])
    assert "</script><b>" not in page


def test_reviews_are_checked_like_the_input_screen():
    ok = {"id": "e82f5005_15", "data": {"hand": {"board": ["4s"], "actions": [
        {"seat": 5, "action": "bet", "amount": 8100, "street": "river"},
        {"seat": 6, "action": "call", "amount": 8100, "street": "river"}],
        "players": [{"seat": 6, "hole_cards": ["2h", "5c"]}], "winner_seat": 6}}}
    bad = {"id": "d0f055fb_8", "data": {"hand": {"board": [], "actions": [{"seat": 0, "action": "dance"}]}}}
    out = rp.load_reviews([ok, bad])
    assert list(out) == ["e82f5005_15"] and out["e82f5005_15"]["winner_seat"] == 6


def test_audio_is_copied_under_the_numbered_names(hand, tmp_path):
    speech = [i for i in hand["timeline"] if i["kind"] == "speech"]
    src = tmp_path / "logs" / "e82f5005abc" / "audio"
    src.mkdir(parents=True)
    (src / f"{int(speech[0]['start'] * 1000)}.wav").write_bytes(b"RIFF")
    copied = rp.copy_audio([hand], [tmp_path / "logs"], tmp_path / "out")
    assert [p.name for p in copied] == [speech[0]["wav"]]
