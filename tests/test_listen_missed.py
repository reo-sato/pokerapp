"""読めなかったコール・フォールドを聞いて確かめるページ（`tools/listen_missed.py`, 2026-10-28）。"""
from __future__ import annotations

import json

from tools.listen_missed import build_page, missed_spots, read_labels, with_streets

T0 = 1_793_000_000.0


def _rows(texts):
    return [{"utterance_start_ts": T0 + at, "audio_sec": 1.0, "text": t, "audio_file": f"{int((T0 + at) * 1000)}.wav"}
            for at, t in texts]


HAND = {"hand_id": 3, "actions": [
    {"seat": 4, "action": "raise", "amount": 600, "street": "preflop"},
    {"seat": 5, "action": "fold", "street": "preflop"},
    {"seat": 6, "action": "call", "amount": 600, "street": "preflop"},
    {"seat": 2, "action": "check", "street": "flop"},
    {"seat": 4, "action": "bet", "amount": 1000, "street": "flop"},
    {"seat": 2, "action": "fold", "street": "flop"},
]}


def test_a_missed_call_shows_the_utterances_between_the_words_read_around_it():
    rows = _rows([(2, "600"), (5, "今日は寒いですね"), (8, "ご視聴ありがとうございました。"), (12, "チェック"),
                  (15, "1000")])
    spots = missed_spots(HAND, rows, (T0, T0 + 60))
    # フォールド（席5）とコール（席6）は 600 とチェックのあいだ。最後のフォールドはハンドを終わらせるので数えない
    assert len(spots) == 1
    (spot,) = spots
    assert [m["text"] for m in spot["missed"]] == ["プリフロップ 席5 フォールド", "プリフロップ 席6 コール"]
    assert [u["text"] for u in spot["utterances"]] == ["600", "今日は寒いですね", "ご視聴ありがとうございました。", "チェック"]
    assert [u["kind"] for u in spot["utterances"]] == ["action", "chatter", "stock", "action"]   # 前後の読めた発話も
    assert [c["read"] for c in spot["context"]] == [True, False, False, True, True]


def test_streets_come_from_the_first_blind_entry():
    hand = {"actions": [{"seat": 4, "action": "raise", "amount": 600}],
            "blind_entry": {"actions": [{"seat": 4, "action": "raise", "amount": 600, "street": "preflop"}]}}
    assert with_streets(hand, None)["actions"][0]["street"] == "preflop"


def test_the_page_and_the_labels(tmp_path):
    spots = missed_spots(HAND, _rows([(2, "600"), (12, "チェック"), (15, "1000")]), (T0, T0 + 60))
    page = build_page("abcdef0123", "2026-10-26 20:18", spots, {})
    assert "聞き取りの確認 abcdef01" in page and "フォールド 1・コール 1" in page and "</script>" in page
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"session_id": "abcdef0123", "labels": [
        {"action": "call", "label": "said", "utterance_kind": "stock"},
        {"action": "fold", "label": "not_said"}, {"action": "fold", "label": "not_said"}]}), encoding="utf-8")
    out = read_labels(labels)
    assert "コール: 言った 1" in out and "フォールド: 言っていない 2" in out and "幻聴（定型句） 1" in out
