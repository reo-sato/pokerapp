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
_SCHEMAS_DIR = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"

# 再構築が正しく緑にできるケース（D1/D2a: 射影、D2b: silent-fold 合成、F3: side-pot、
# ADR-A/B/C/D: 復元正当性バッチで 5→13 に拡充）。
GREEN_CASES = [
    "check-facing-bet",      # D1/D2a: 非合法 check → call + review
    "call-amount-from-state",  # D1/D2a: heard 額無視 → state の call 額
    "silent-fold",           # D2b: 明示席へ向け中間席を fold 合成（audio 駆動）
    "out-of-turn-rfid",      # D2b: RFID seat で prior を上書きし fold 合成（RFID 駆動）
    "unequal-allin",         # F3: スタック差 all-in → main/side pot を HandSummary.pots に
    "postflop-street-transition",  # RFID board でストリート遷移 + postflop アクション
    "full-ring-6max",        # 6 人卓の fold 回し + explicit seat raise
    "multi-hand-session",    # 2 ハンド連続（stack_start がブラインド post 前 = S5 の跨ぎ検証）
    "rfid-vs-spoken-seat-conflict",  # RFID > 明示発話席 の優先順位（ADR-0009 §4）
    "camera-corroboration",  # camera 照合で confidence 向上
    "low-whisper-confidence",  # 低信頼 ASR → REVIEW_THRESHOLD 未満で needs_review
    "cap-exceeded-negative",   # SILENT_FOLD_CAP 超過 → prior 維持 + review（負例）
    "split-pot-chop",        # ADR-D S7: チョップ → pot_awards + 等分
]

# 後続フェーズで追加するケース（実装と同じ増分で fixtures を authoring する）。
PENDING_CASES: dict[str, str] = {}


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
    expected = json.loads((_FIXTURES / case / "expected_hand.json").read_text(encoding="utf-8"))
    if isinstance(expected, list):  # multi-hand fixture（例: multi-hand-session）
        assert [_normalize(s.to_dict()) for s in summaries] == expected
    else:
        assert len(summaries) == 1, f"{case}: 1 ハンド確定を期待"
        assert _normalize(summaries[0].to_dict()) == expected


@pytest.mark.parametrize("case", GREEN_CASES)
def test_round_trip_determinism(case: str, tmp_path: Path):
    """同一 events.jsonl を 2 回 replay → 完全一致（timestamp 込み、DoD #3）。"""
    s1 = replay_fixture(_FIXTURES / case, tmp_path)
    s2 = replay_fixture(_FIXTURES / case, tmp_path)
    assert [s.to_dict() for s in s1] == [s.to_dict() for s in s2]


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


@pytest.mark.parametrize("case", GREEN_CASES)
def test_golden_output_conforms_to_hand_action_schema(case: str, tmp_path: Path):
    """各 green ケースの再構築出力（非正規化・実 timestamp）が hand / action schema に適合する
    （ISSUE-0011 freeze の回帰: code↔contract↔golden を結ぶ）。"""
    jsonschema = pytest.importorskip("jsonschema")
    hand_v = jsonschema.Draft202012Validator(json.loads((_SCHEMAS_DIR / "hand.schema.json").read_text(encoding="utf-8")))
    action_v = jsonschema.Draft202012Validator(json.loads((_SCHEMAS_DIR / "action.schema.json").read_text(encoding="utf-8")))
    for summary in replay_fixture(_FIXTURES / case, tmp_path):
        hd = summary.to_dict()
        hand_v.validate(hd)
        for a in hd["actions"]:
            action_v.validate(a)


def test_unequal_allin_main_and_side_pots(tmp_path: Path):
    """F3: スタック差 all-in で HandSummary.pots に main/side pot が入る。"""
    summaries = replay_fixture(_FIXTURES / "unequal-allin", tmp_path)
    pots = summaries[0].to_dict()["pots"]
    assert len(pots) == 2
    main, side = pots
    assert main["amount"] == 3000 and main["eligible_seats"] == [1, 2, 3]   # 全員 eligible
    assert side["amount"] == 4000 and side["eligible_seats"] == [2, 3]       # 短スタック除外
    assert sum(p["amount"] for p in pots) == 7000


def test_pending_cases_all_green():
    """既知バグ 5 ケースが全て GREEN（pending なし）。"""
    assert PENDING_CASES == {}
