"""tests/test_contracts.py

Phase 0a: contract drift detection (最小 bootstrap, ISSUE-0003 対策)。

検査内容:
- docs/contracts/schemas/*.schema.json が valid JSON で $id / version を持つ。
- 各 model の fixtures が対応 schema と整合する
  (canonical / valid-* は通過、invalid-* は必ず違反)。
- core が生成する Player が player schema に適合する (code ↔ contract drift 検知)。

jsonschema 未導入環境では skip する (CI には jsonschema を入れる方針。
docs/contracts/versioning-and-freeze.md § drift detection 参照)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

_CONTRACTS = Path(__file__).parent.parent / "docs" / "contracts"
_SCHEMAS = _CONTRACTS / "schemas"
_FIXTURES = _CONTRACTS / "fixtures"

# 1 model = 1 schema = 1 fixtures dir
# player: S1 freeze 候補。session / seat_assignment / hand_ref: S2 draft (ADR-0006, 未 freeze)。
# reconstruction_event: R1 (ADR-0010) record/replay の envelope (additionalProperties:false)。
# hand / action: hand core (ADR-0010 R5, ISSUE-0011)。additionalProperties:true で 1.0。
# ledger_entry / point_ledger_entry / session_settlement: S3 draft (ADR-0016, 未 freeze)。
_MODELS = [
    "player", "session", "seat_assignment", "hand_ref", "reconstruction_event",
    "hand", "action", "ledger_entry", "point_ledger_entry", "session_settlement",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_schemas_are_valid_json_with_id_and_version():
    schema_files = sorted(_SCHEMAS.glob("*.schema.json"))
    assert schema_files, "no schema files found under docs/contracts/schemas/"
    for sf in schema_files:
        schema = _load(sf)
        assert "$id" in schema, f"{sf.name} missing $id"
        assert "version" in schema, f"{sf.name} missing version"
        # schema 自身が draft 2020-12 として正当であること
        jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("model", _MODELS)
def test_fixtures_match_schema(model: str):
    schema = _load(_SCHEMAS / f"{model}.schema.json")
    validator = jsonschema.Draft202012Validator(schema)

    fixture_dir = _FIXTURES / model
    fixtures = sorted(fixture_dir.glob("*.json"))
    assert fixtures, f"no fixtures found under {fixture_dir}"

    for fx in fixtures:
        data = _load(fx)
        errors = list(validator.iter_errors(data))
        if fx.name.startswith("invalid-"):
            assert errors, f"{model}/{fx.name} should violate schema but passed"
        else:
            assert not errors, f"{model}/{fx.name} should match schema: {errors}"


def test_core_player_matches_contract(tmp_path: Path):
    """core が生成する Player が player schema に適合する (code↔contract drift)。"""
    from core.player_repository import PlayerRepository

    repo = PlayerRepository(path=tmp_path / "players.json")
    player = repo.create_player("Alice")

    schema = _load(_SCHEMAS / "player.schema.json")
    jsonschema.Draft202012Validator(schema).validate(player.to_dict())


def test_core_ledger_matches_contract(tmp_path: Path):
    """core が生成する LedgerEntry / PointLedgerEntry が schema に適合する
    (code↔contract drift, S3 draft)。"""
    from core.ledger_repository import LedgerRepository
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    session = sessions.create_session()
    repo = LedgerRepository(
        path=tmp_path / "ledger.json", session_repo=sessions, player_repo=players
    )

    grant = repo.grant_points(alice.player_id, 3000, "manual_grant", idempotency_key="g1")
    entry = repo.add_entry(
        session.session_id, alice.player_id, "order",
        cash_amount=200, point_amount=1000,
        order={"item_name": "ジントニック", "unit_amount": 600, "quantity": 2},
    )
    spend = repo.list_point_entries(alice.player_id)[-1]

    ledger_schema = _load(_SCHEMAS / "ledger_entry.schema.json")
    jsonschema.Draft202012Validator(ledger_schema).validate(entry.to_dict())
    point_schema = _load(_SCHEMAS / "point_ledger_entry.schema.json")
    validator = jsonschema.Draft202012Validator(point_schema)
    validator.validate(grant.to_dict())
    validator.validate(spend.to_dict())


def test_core_hand_action_match_contract():
    """core が生成する HandSummary / ActionRecord が hand / action schema に適合する
    (code↔contract drift, ISSUE-0011 freeze)。"""
    from core.hand_log import ActionRecord, HandSummary

    action = ActionRecord(
        hand_id=1, timestamp="2026-06-05T04:00:00.000", street="preflop",
        seat=3, player_name="P3", action="call", amount=200,
        pot_after=500, stack_after=9800,
        source={"camera": False, "audio": True, "rfid": True},
        needs_review=True, confidence=0.9,
    )
    action_schema = _load(_SCHEMAS / "action.schema.json")
    jsonschema.Draft202012Validator(action_schema).validate(action.to_dict())

    summary = HandSummary(
        hand_id=1, session_id="0123456789abcdef0123456789abcdef",
        started_at="2026-06-05T04:00:00.000", ended_at="2026-06-05T04:00:10.000",
        blinds={"sb": 100, "bb": 200}, board=[], board_source="",
        players=[{"seat": 1, "name": "P1", "hole_cards": None, "hole_cards_source": "",
                  "stack_start": 1000, "stack_end": 100, "result": -900}],
        pot_total=6700, winner_seat=2, actions=[action], review_required=True,
        pots=[{"amount": 3000, "eligible_seats": [1, 2, 3]}],
    )
    hand_schema = _load(_SCHEMAS / "hand.schema.json")
    jsonschema.Draft202012Validator(hand_schema).validate(summary.to_dict())
