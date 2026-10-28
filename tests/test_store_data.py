"""店舗のテスト記録をリポジトリに置く（`tools/store_data.py`, オーナー 2026-10-28）。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from tools import store_data

SID = "2bbd480e439a42fb805094ff979c9d9c"


def _zip(path: Path, files: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _logs(tmp: Path) -> list[Path]:
    record = {"session_id": SID, "hands": [{"hand_id": 1, "started_at": "2026-10-26T20:18:19"},
                                           {"hand_id": 2, "started_at": "2026-10-26T20:19:44"}]}
    gt = {"hands": [{"hand_id": 1, "actions": [], "notes": "デッドボタン"}]}
    return [
        _zip(tmp / "f9f3586b-pokerlogs_20261026_215250_1of2.zip", {
            "manifest.json": "{}", f"{SID}/{SID}.events.jsonl": "", f"{SID}/{SID}.json": json.dumps(record),
            f"{SID}/{SID}.ground_truth.json": json.dumps(gt), f"{SID}/audio/1793013500784.wav": b"RIFF"}),
        _zip(tmp / "345ed500-pokerlogs_20261026_215250_2of2.zip", {
            f"{SID}/audio/1793013600000.wav": b"RIFF", f"{SID}/{SID}.transcripts.jsonl": ""}),
    ]


def _truth(tmp: Path, prefix: str, created: str, hands: int) -> Path:
    gt = {"hands": [{"hand_id": i, "actions": []} for i in range(1, hands + 1)]}
    return _zip(tmp / f"{prefix}-pokertruth_20261026_215250.zip", {
        "truth_manifest.json": json.dumps({"created_at": created}), f"{SID}/{SID}.ground_truth.json": json.dumps(gt)})


def test_add_keeps_the_text_and_leaves_the_audio_to_the_data_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(store_data, "FIXTURES", tmp_path / "fixtures")
    store = tmp_path / "store"
    (store).mkdir()
    (store / "README.md").write_text(f"# 記録\n{store_data.INDEX_START}\n{store_data.INDEX_END}\n", encoding="utf-8")
    labels = tmp_path / "c010ee5b-listen_2bbd480e.json"
    labels.write_text("{}", encoding="utf-8")
    store_data.add(_logs(tmp_path),
                   [_truth(tmp_path, "aaaaaaaa", "2026-10-28T11:00:00", 1),
                    _truth(tmp_path, "bbbbbbbb", "2026-10-28T11:45:03", 2)], [labels], store)
    day = store / "2026-10-26"
    with zipfile.ZipFile(day / "pokerlogs_20261026_215250_text.zip") as zf:
        names = set(zf.namelist())
    assert not any(n.endswith(".wav") for n in names)                       # 音声は入れない
    assert {f"{SID}/{SID}.json", f"{SID}/{SID}.transcripts.jsonl", "manifest.json"} <= names   # 分割の両方から
    audio = json.loads((day / "pokerlogs_20261026_215250_audio.json").read_text(encoding="utf-8"))
    assert audio["branch"] == "store-data" and audio["zips"] == [
        "pokerlogs_20261026_215250_1of2.zip", "pokerlogs_20261026_215250_2of2.zip"]   # アップロードの名前の頭は外す
    with zipfile.ZipFile(day / "pokertruth_20261026_215250.zip") as zf:     # 新しい版
        assert json.loads(zf.read("truth_manifest.json"))["created_at"] == "2026-10-28T11:45:03"
    assert (day / "listen_2bbd480e.json").exists()
    readme = (store / "README.md").read_text(encoding="utf-8")
    assert "| 2026-10-26 | 2bbd480e | 2 | 2（pokertruth_20261026_215250.zip） | — | listen_2bbd480e.json | 入れていない |" \
        in readme


def test_the_audio_is_taken_out_of_the_data_branch_without_switching():
    assert store_data.archive_args("origin/store-data", "2026-10-26") == [
        "archive", "--format=tar", "origin/store-data", "audio/2026-10-26"]


def test_names():
    assert store_data.stamp_of("pokerlogs_20261026_215250_3of9.zip") == ("pokerlogs", "2026-10-26", "20261026_215250")
    assert store_data.clean_name(Path("/x/f9f3586b-pokerlogs_20261026_215250_1of9.zip")) == \
        "pokerlogs_20261026_215250_1of9.zip"
