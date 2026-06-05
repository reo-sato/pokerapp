"""tests/test_reconstruction.py

Phase F1 (#8) — golden-fixture replay 回帰 + round-trip 決定性 (R4)。

各 `tests/fixtures/reconstruction/<case>/` は:
  - setup.json: backend / blinds / players（replay の初期状態）
  - events.jsonl: 記録済み reconstruction_event 列（replay 入力）
  - expected_hand.json: replay 後の HandSummary.to_dict() を正規化したもの

green ケースは D1/D2a で既に正しく再構築できる挙動を固定する。silent-fold / out-of-turn-rfid は
D2b（fold_through 合成）、unequal-allin は F3（side-pot 連携）で fixtures + 実装を追加する。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")  # green ケースは pokerkit backend で再構築する

from integration.replay import replay_fixture  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures" / "reconstruction"
_SCHEMA = (
    Path(__file__).parent.parent
    / "docs" / "contracts" / "schemas" / "reconstruction_event.schema.json"
)

# D1/D2a で既に緑にできるケース。
GREEN_CASES = ["check-facing-bet", "call-amount-from-state"]

# 後続フェーズで追加するケース（実装と同じ増分で fixtures を authoring する）。
PENDING_CASES = {
    "silent-fold": "D2b: fold_through 合成で未宣言 fold を補う",
    "out-of-turn-rfid": "D2b: RFID seat で prior を上書き（合法なら fold 合成）",
    "unequal-allin": "F3: side-pot を HandSummary に連携",
}


def _normalize(d: dict) -> dict:
    """wall-clock 由来フィールドを除去し confidence を丸める（machine/timezone 非依存比較）。"""
    d = copy.deepcopy(d)
    d["started_at"] = None
    d["ended_at"] = None
    for a in d["actions"]:
        a["timestamp"] = None
        if a.get("confidence") is not None:
            a["confidence"] = round(a["confidence"], 3)
    return d


@pytest.mark.parametrize("case", GREEN_CASES)
def test_golden_replay_matches_expected(case: str, tmp_path: Path):
    summaries = replay_fixture(_FIXTURES / case, tmp_path)
    assert len(summaries) == 1, f"{case}: 1 ハンド確定を期待"
    actual = _normalize(summaries[0].to_dict())
    expected = json.loads((_FIXTURES / case / "expected_hand.json").read_text(encoding="utf-8"))
    assert actual == expected


@pytest.mark.parametrize("case", GREEN_CASES)
def test_round_trip_determinism(case: str, tmp_path: Path):
    """同一 events.jsonl を 2 回 replay → 完全一致（timestamp 込み、DoD #3）。"""
    s1 = replay_fixture(_FIXTURES / case, tmp_path)
    s2 = replay_fixture(_FIXTURES / case, tmp_path)
    assert s1[0].to_dict() == s2[0].to_dict()


def test_check_facing_bet_corrected_to_call():
    """中核アサーション: 非合法 'check' が 'call'+needs_review に射影される。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        summaries = replay_fixture(_FIXTURES / "check-facing-bet", d)
    betting = [a for a in summaries[0].to_dict()["actions"] if a["action"] == "call"]
    assert len(betting) == 1
    assert betting[0]["needs_review"] is True


def test_call_amount_taken_from_state_not_heard():
    """中核アサーション: heard 9999 ではなく engine の call 額が採用される。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        summaries = replay_fixture(_FIXTURES / "call-amount-from-state", d)
    call = [a for a in summaries[0].to_dict()["actions"] if a["action"] == "call"][0]
    assert call["amount"] != 9999 and call["amount"] > 0
    assert call["needs_review"] is False


def test_fixture_events_match_reconstruction_schema():
    """fixtures の events.jsonl が reconstruction_event schema に適合する（contract tie-in）。"""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for case in GREEN_CASES:
        for line in (_FIXTURES / case / "events.jsonl").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                validator.validate(json.loads(line))


def test_same_timestamp_sensor_processed_before_audio(tmp_path: Path):
    """同一 timestamp の RFID(sensor) が audio より先に処理され corroboration に間に合う。

    audio を入力リストで rfid より前に置いても、tie-break (camera→rfid→audio) により sensor が先。
    tie-break が無いと（安定ソートで audio 先）→ source.rfid=False になり本テストが落ちる。
    """
    from core.events import AudioEvent, RFIDEvent
    from core.game_state import PlayerState
    from integration.replay import replay_events

    players = [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(3)]
    t = 5000.0
    events = [
        AudioEvent("new_hand", 0, t, "新しいハンド"),
        AudioEvent("call", 0, t + 1, "コール"),  # ← rfid より前に置く
        RFIDEvent(tag_id="X", card="Ah", reader_id="seat_3", role="seat",
                  seat=3, timestamp=t + 1, raw_tag_id="X"),  # 同 ts・先頭 actor(=seat 3)
        AudioEvent("winner", 0, t + 2, "シート1 ウィナー"),
    ]
    summaries = replay_events(
        events, backend="pokerkit", players=players,
        sb=100, bb=200, session_id="tie-break", out_dir=tmp_path,
    )
    call = [a for a in summaries[0].to_dict()["actions"] if a["action"] == "call"][0]
    assert call["seat"] == 3
    assert call["source"]["rfid"] is True


@pytest.mark.parametrize("case", list(PENDING_CASES))
@pytest.mark.skip(reason="後続フェーズ（D2b / F3）で fixtures + 実装を追加")
def test_pending_cases_placeholder(case: str):  # pragma: no cover
    pass
