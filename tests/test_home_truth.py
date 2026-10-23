"""家の PC で真のアクションを入れる道具（tools/home_truth.py）。"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from tools import home_truth
from tools.eval_store import open_input

SID = "d01277f800d54e0592b4d059ac7f19cb"
STAMP = "20261019_224945"


def _gt(note: str) -> bytes:
    return json.dumps({"session_id": SID, "hands": [{"hand_id": 2, "actions": [], "notes": note}]}).encode()


def _write_set(folder: Path, prefix: str = "") -> list[Path]:
    """pack_logs の分けた zip（2 つ）と同じ並び。"""
    folder.mkdir(parents=True, exist_ok=True)
    one = folder / f"{prefix}pokerlogs_{STAMP}_1of2.zip"
    two = folder / f"{prefix}pokerlogs_{STAMP}_2of2.zip"
    with zipfile.ZipFile(one, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"parts": [one.name, two.name]}))
        zf.writestr("pokerapp.log", "log")
        zf.writestr("config.json", "{}")
        zf.writestr(f"{SID}/{SID}.json", json.dumps({"session_id": SID, "hands": []}))
        zf.writestr(f"{SID}/{SID}.events.jsonl", "")
        zf.writestr(f"{SID}/{SID}.ground_truth.json", _gt("store"))
        zf.writestr(f"{SID}/{SID}.live_hand.json", "{}")
        zf.writestr(f"{SID}/audio/1.wav", b"RIFF1")
        zf.writestr("../evil.json", "x")
        zf.writestr(f"{SID}/../../evil2.json", "x")
    with zipfile.ZipFile(two, "w") as zf:
        zf.writestr(f"{SID}/audio/2.wav", b"RIFF2")
    return [one, two]


def test_pick_set_needs_every_part_and_takes_the_newest(tmp_path):
    one, two = _write_set(tmp_path, prefix="ca6fa185-")
    older = tmp_path / "pokerlogs_20261018_200000.zip"
    shutil.copy(one, older)
    stamp, parts = home_truth.pick_set([two, older, one, tmp_path / "other.zip"])
    assert stamp == STAMP and parts == [one, two]
    with pytest.raises(home_truth.SetError, match="2of2"):
        home_truth.pick_set([one])
    with pytest.raises(home_truth.SetError):
        home_truth.pick_set([tmp_path / "other.zip"])


def test_unpack_lays_out_the_entry_page_folder(tmp_path):
    zips = _write_set(tmp_path / "in")
    dest = tmp_path / "logs"
    sessions = home_truth.unpack(zips, dest)
    assert sessions[SID]["audio"] == 2 and sessions[SID]["store_truth"]
    assert (dest / f"{SID}.json").is_file() and (dest / f"{SID}.events.jsonl").is_file()
    assert (dest / "audio" / SID / "1.wav").read_bytes() == b"RIFF1"
    assert (dest / "audio" / SID / "2.wav").read_bytes() == b"RIFF2"
    # 進行中のハンド・アプリのログ・設定・外へ出る名前は置かない
    assert not (dest / f"{SID}.live_hand.json").exists()
    assert not (dest / "pokerapp.log").exists() and not (dest / "config.json").exists()
    assert not (tmp_path / "evil.json").exists() and not (tmp_path / "evil2.json").exists()
    assert json.loads((dest / f"{SID}.ground_truth.store.json").read_text())["hands"][0]["notes"] == "store"
    # 既定は最初から入れ直す: 店の分は入力の出発点にしない
    assert not (dest / f"{SID}.ground_truth.json").exists()


def test_continue_starts_from_the_store_entries(tmp_path):
    zips = _write_set(tmp_path / "in")
    dest = tmp_path / "logs"
    sessions = home_truth.unpack(zips, dest, keep_store=True)
    assert sessions[SID]["from_store"]
    assert json.loads((dest / f"{SID}.ground_truth.json").read_text())["hands"][0]["notes"] == "store"
    # あとから最初から入れ直すことにしたら、手つかずの店の分は外す
    home_truth.unpack(zips, dest)
    assert not (dest / f"{SID}.ground_truth.json").exists()


def test_unpack_again_keeps_what_was_entered_at_home(tmp_path):
    zips = _write_set(tmp_path / "in")
    dest = tmp_path / "logs"
    home_truth.unpack(zips, dest)
    (dest / f"{SID}.ground_truth.json").write_bytes(_gt("home"))
    sessions = home_truth.unpack(zips, dest)
    assert sessions[SID]["kept_truth"]
    assert json.loads((dest / f"{SID}.ground_truth.json").read_text())["hands"][0]["notes"] == "home"
    assert json.loads((dest / f"{SID}.ground_truth.store.json").read_text())["hands"][0]["notes"] == "store"


def test_pack_truth_is_read_after_the_log_zips(tmp_path):
    zips = _write_set(tmp_path / "in")
    dest = tmp_path / "logs"
    home_truth.unpack(zips, dest)
    (dest / f"{SID}.ground_truth.json").write_bytes(_gt("home"))
    out = home_truth.pack_truth(dest, STAMP, tmp_path / "out")
    assert out.name == f"pokertruth_{STAMP}.zip"
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == sorted(["truth_manifest.json", f"{SID}/{SID}.ground_truth.json"])
    root, tmp = open_input([*zips, out])
    try:
        gt = json.loads((root / SID / f"{SID}.ground_truth.json").read_text())
        assert gt["hands"][0]["notes"] == "home"
        assert json.loads((root / "manifest.json").read_text())["parts"]      # ログの manifest は残る
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pack_truth_without_entries(tmp_path):
    assert home_truth.pack_truth(tmp_path, STAMP, tmp_path / "out") is None


@pytest.fixture(autouse=True)
def _free_port(monkeypatch):
    # 8791 はほかの試験も使う（並列で取り合わない）
    monkeypatch.setattr(home_truth, "PORTS", range(0, 1))


def test_main_serves_and_packs_on_enter(tmp_path, monkeypatch):
    zips = _write_set(tmp_path / "in")
    monkeypatch.setattr("builtins.input", lambda *_: "")
    seen = {}
    real = home_truth.make_server

    def spy(*a, **kw):
        seen["evaluation"] = kw.get("evaluation")
        return real(*a, **kw)

    monkeypatch.setattr(home_truth, "make_server", spy)
    rc = home_truth.main([*map(str, zips), "--dest", str(tmp_path / "home"), "--out", str(tmp_path / "out"),
                          "--no-browser", "--continue"])
    assert seen["evaluation"] is False
    assert rc == 0
    assert (tmp_path / "home" / STAMP / f"{SID}.json").is_file()
    assert (tmp_path / "out" / f"pokertruth_{STAMP}.zip").is_file()


def test_main_reports_a_missing_part(tmp_path, capsys):
    one, _ = _write_set(tmp_path / "in")
    assert home_truth.main([str(one), "--dest", str(tmp_path / "home"), "--no-browser"]) == 1
    assert "2of2" in capsys.readouterr().out


def test_estimates_and_review_notes(tmp_path):
    zips = _write_set(tmp_path / "in")
    est = tmp_path / "in" / f"pokerestimate_{STAMP}.zip"
    with zipfile.ZipFile(est, "w") as zf:
        zf.writestr(f"{SID}/{SID}.estimate.shadow.json", json.dumps({"session_id": SID, "hands": {}}))
        zf.writestr("../evil.json", "x")
    dest = tmp_path / "logs"
    home_truth.unpack(zips, dest)
    assert home_truth.unpack_estimates([est], dest) == [SID]
    assert (dest / f"{SID}.estimate.shadow.json").is_file()
    (dest / f"{SID}.estimate_review.json").write_text(json.dumps({"hands": {"3": {"tags": ["額"]}}}))
    out = home_truth.pack_truth(dest, STAMP, tmp_path / "out")
    with zipfile.ZipFile(out) as zf:
        assert f"{SID}/{SID}.estimate_review.json" in zf.namelist()
