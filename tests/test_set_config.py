"""tests/test_set_config.py

ADR-0059: 店舗 PC で config.json の 1 項目（例: `session_layer.enabled`）をコマンド 1 つで変える。
PowerShell 5.1 の `Set-Content -Encoding UTF8` は BOM を付けるので、読む側も BOM を許す。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core.config import load_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import set_config  # noqa: E402

_CFG = {
    "session": {"num_seats": 6, "blinds": {"sb": 100, "bb": 200}},
    "session_layer": {"enabled": False, "_comment": "席とお客さんの記録"},
    "viewer_api": {"bind_port": 8788},
}


def _write(path: Path, data: dict, bom: bool = False) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)


def test_load_config_reads_a_bom(tmp_path: Path):
    path = tmp_path / "config.json"
    _write(path, _CFG, bom=True)
    assert load_config(path)["session_layer"]["enabled"] is False


def test_enabling_the_session_layer_keeps_everything_else(tmp_path: Path, capsys):
    path = tmp_path / "config.json"
    _write(path, _CFG, bom=True)
    assert set_config.main(["session_layer.enabled", "true"], path=path) == 0
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")             # BOM は付けない
    data = json.loads(raw.decode("utf-8"))
    assert data["session_layer"] == {"enabled": True, "_comment": "席とお客さんの記録"}
    assert data["session"] == _CFG["session"] and data["viewer_api"] == _CFG["viewer_api"]
    assert "session_layer.enabled: false -> true" in capsys.readouterr().out


def test_without_a_value_it_only_shows(tmp_path: Path, capsys):
    path = tmp_path / "config.json"
    _write(path, _CFG)
    before = path.read_bytes()
    assert set_config.main(["viewer_api.bind_port"], path=path) == 0
    assert capsys.readouterr().out.strip() == "viewer_api.bind_port = 8788"
    assert path.read_bytes() == before
    set_config.main(["no_such.key"], path=path)
    assert "（未設定）" in capsys.readouterr().out


@pytest.mark.parametrize("raw, value", [
    ("true", True), ("false", False), ("8790", 8790), ('"0.0.0.0"', "0.0.0.0"),
    ("0.0.0.0", "0.0.0.0"), ("null", None),
])
def test_values_are_read_as_json_or_kept_as_text(raw: str, value):
    assert set_config.parse_value(raw) == value


def test_missing_sections_are_created(tmp_path: Path):
    path = tmp_path / "config.json"
    _write(path, {"session": {}})
    set_config.main(["session_layer.enabled", "true"], path=path)
    assert load_config(path)["session_layer"] == {"enabled": True}
