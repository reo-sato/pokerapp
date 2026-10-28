"""ディーラーの声の照合（台帳 I1, `audio/dealer_voice.py` / `tools/dealer_voice.py`）。モデルは偽物に差し替える。"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from audio import dealer_voice as dv
from tools import dealer_voice as tool


def _wav(path: Path, level: float) -> None:
    """偽の埋め込みが読む「声」: 最初のサンプルの値で声を表す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.full(16000, level, dtype=np.float32)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes((x * 32767).astype(np.int16).tobytes())


class FakeEmbedder:
    """声の値 a を (cos a, sin a) にする。値が近いほど近い声。"""

    name = "fake"

    def embed(self, samples):
        a = float(samples[0]) * 3
        return dv.unit([np.cos(a), np.sin(a)])


def test_enroll_drops_an_outlier():
    same = [dv.unit([1.0, 0.01 * (i - 4)]) for i in range(9)]
    voice = dv.enroll("A", same + [dv.unit([-1.0, 0.0])])
    assert voice is not None and voice.count == 10
    assert dv.similarity(voice.center, dv.unit([1.0, 0.0])) > 0.99


def test_voice_round_trips():
    voice = dv.enroll("A", [dv.unit([1.0, 2.0])])
    back = dv.Voice.from_dict(json.loads(json.dumps(voice.to_dict())))
    assert back.name == "A" and dv.similarity(back.center, voice.center) > 0.999


def test_nearest_picks_the_closer_voice():
    a = dv.enroll("A", [dv.unit([1.0, 0.0])])
    b = dv.enroll("B", [dv.unit([0.0, 1.0])])
    best, scores = dv.nearest(dv.unit([0.9, 0.1]), [a, b])
    assert best == "A" and scores["A"] > scores["B"]


def test_category():
    assert tool.category({"text": "コール", "audio_sec": 1.0}) == tool.ACTION
    assert tool.category({"text": "ご視聴ありがとうございました", "audio_sec": 1.0}) == tool.CANNED
    assert tool.category({"text": "ええと", "audio_sec": 1.0}) == tool.OTHER


def test_auc():
    assert tool.auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert tool.auc([0.5], [0.5]) == 0.5
    assert tool.auc([], [0.1]) is None


def _layout(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    corpus = logs / "corpus" / "20261028_100000_A"
    corpus.mkdir(parents=True)
    (corpus / "meta.json").write_text(json.dumps({"tool": "read_corpus", "speaker": "A"}), encoding="utf-8")
    for i in range(4):
        _wav(corpus / f"p{i}_t1_m1.wav", 0.10)
        _wav(corpus / f"p{i}_t1_m2.wav", 0.50)     # 2 本目のマイクは登録に使わない
    rows = []
    for i, (text, level) in enumerate([("コール", 0.10), ("チェック", 0.11), ("フォールド", 0.45), ("ええと", 0.10)]):
        name = f"{1790000000000 + i * 1000}.wav"
        _wav(logs / "audio" / "s1" / name, level)
        rows.append({"audio_file": name, "utterance_start_ts": 1790000000 + i, "audio_sec": 1.0, "text": text})
    rows.append({"audio_file": "missing.wav", "audio_sec": 1.0, "text": "コール"})
    (logs / "s1.transcripts.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                                               encoding="utf-8")
    return logs


def test_score_flags_an_action_in_another_voice(tmp_path):
    logs = _layout(tmp_path)
    corpora = tool.find_corpora(logs)
    assert list(corpora) == ["A"] and all(p.name.endswith("_m1.wav") for p in corpora["A"])
    embedder = FakeEmbedder()
    voices = tool.enroll_corpora(embedder, corpora, log=lambda _: None)
    session = tool.find_sessions(logs)[0]
    items = tool.score_session(session, embedder, voices, tmp_path / "out", log=lambda _: None)
    assert len(items) == 4                                   # 音声の無い発話は測らない
    by_text = {it["text"]: it for it in items}
    assert by_text["コール"]["similarity"] > 0.99 and by_text["フォールド"]["similarity"] < 0.5
    lines: list[str] = []
    summary = tool.report("s1", items, low=0.9, listing=1, labels={}, log=lines.append)
    assert summary["low_actions"] == 1
    assert any("フォールド" in line for line in lines)
    assert (tmp_path / "out" / "s1.voice.jsonl").is_file()


def test_score_without_a_corpus_enrolls_from_the_session(tmp_path):
    logs = _layout(tmp_path)
    session = tool.find_sessions(logs)[0]
    items = tool.score_session(session, FakeEmbedder(), [], tmp_path / "out", log=lambda _: None)
    assert {it["best"] for it in items} == {"auto:s1"}
