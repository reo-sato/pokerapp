"""聞き取りの振り返り（ディーラーごと・1 時間ごと, `tools/dealer_report.py`, ADR-0074）。"""
from __future__ import annotations

from datetime import datetime

from tools.dealer_report import NO_NAME, advice, format_brief, format_rows, tally_session

T0 = datetime(2026, 11, 19, 20, 0).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat()


RECORD = {"session_id": "s1", "hands": [
    {"hand_id": 1, "dealer": "佐藤", "started_at": _iso(T0), "ended_at": _iso(T0 + 50), "actions": [
        {"action": "raise", "amount": 600, "reason": None},
        {"action": "fold", "actor_source": "rfid_departure"},
        {"action": "fold", "actor_source": "rfid_departure"},
        {"action": "call", "actor_source": "implied"},
        {"action": "check", "actor_source": "engine_prior"},
        {"action": "bet", "amount": 200, "reason": "no_amount_heard"},
        {"action": "fold", "street": "showdown"},                    # ショーダウンのマックは数えない
    ]},
    {"hand_id": 2, "started_at": _iso(T0 + 60), "ended_at": _iso(T0 + 90), "actions": [
        {"action": "fold", "actor_source": "rfid_departure"},
    ]},
]}
TRANSCRIPTS = [
    {"utterance_start_ts": T0 + 5, "audio_sec": 1.0, "text": "600", "events": [{"action": "raise"}]},
    {"utterance_start_ts": T0 + 9, "audio_sec": 5.0, "text": "フォールドコール",
     "events": [{"action": "fold"}, {"action": "call"}]},
    {"utterance_start_ts": T0 + 20, "audio_sec": 2.0, "text": "今日は寒い", "events": []},
    {"utterance_start_ts": T0 + 70, "audio_sec": 1.0, "text": "フォールド", "events": [{"action": "fold"}]},
]


def test_counts_by_dealer_and_hour():
    out = tally_session(RECORD, TRANSCRIPTS)
    sato = out[("佐藤", "11/19 20時")]
    assert len(sato.hands) == 1 and (sato.folds, sato.fold_words) == (2, 1)
    assert (sato.implied, sato.check_call) == (1, 2) and (sato.no_amount, sato.wagers) == (1, 2)
    assert (sato.multi_utts, sato.action_utts, sato.cut_utts) == (1, 2, 1)
    other = out[(NO_NAME, "11/19 20時")]
    assert (other.folds, other.fold_words) == (1, 1)
    text = "\n".join(format_rows({"佐藤": sato}))
    assert "フォールドの声 1/2（50%）" in text and "続けて言った発話 1/2（50%）" in text and "5 秒で切れた発話 1" in text


def test_the_brief_names_the_one_thing_to_work_on():
    sato = tally_session(RECORD, TRANSCRIPTS)[("佐藤", "11/19 20時")]
    assert advice(sato) == "フォールドは札が離れたのを見てから、1 人ずつ「フォールド」と言う"   # 50% が最大（同率は先）
    brief = "\n".join(format_brief({"佐藤": sato}))
    assert "佐藤（1 ハンド）" in brief and "次に気をつける" in brief and "フォールドコール" not in brief  # 文は出さない
