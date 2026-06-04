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
_MODELS = ["player", "session", "seat_assignment", "hand_ref"]


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
