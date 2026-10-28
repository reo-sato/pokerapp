"""audio/dealer_voice.py — ディーラーの声の照合（台帳 I, 2026-10-28。オフラインの確かめだけ・ライブは使わない）

発話の音声から「声の特徴」（話者の埋め込み = 192〜512 次元のベクトル）を作り、登録したディーラーの声との近さ
（コサイン類似度, -1〜1）を出す。マイクはディーラーの口元にあるので、近さが低い発話はお客さん・隣の卓・店員の声の
疑いがある。ディーラー本人の雑談は同じ声なので分けられない（`docs/worklog/2026-10-28-dealer-voice-plan.md`）。

モデルは k2-fsa（sherpa-onnx）が配る話者認識の ONNX（3D-Speaker の CAM++ が既定。CPU で 1 発話数十ミリ秒）。
入力は第 2 の耳と同じ 80 次元の対数メル（`audio.second_ear.fbank`）から時間方向の平均を引いたもの。

登録（`enroll`）は、そのディーラーの発話の埋め込みを平均して長さ 1 にしたもの。読み上げ集（`tools/read_corpus.py`）を
名前を入れて読んでもらうと、句ごとの音声がそのまま登録に使える。
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol

import numpy as np

from audio.second_ear import fbank

# 配布物（GitHub のリリース。タグの綴りは配布元のまま）
RELEASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
MODELS = {
    # 名前: (ファイル, 入力に掛ける倍率)。倍率は配布物の metadata の normalize_samples（1 = [-1, 1] のまま / 0 = PCM16 の値）
    "campplus": ("3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx", 1.0),
    "eres2net": ("3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx", 1.0),
    "wespeaker": ("wespeaker_zh_cnceleb_resnet34.onnx", 32768.0),
}
DEFAULT_MODEL = "campplus"
# これより短い発話は埋め込みが不安定なので測らない（10 ms のフレーム数 = 0.3 秒）
MIN_FRAMES = 30


def model_path(root: Path, name: str = DEFAULT_MODEL) -> Path:
    return root / "models" / "speaker" / MODELS[name][0]


def download_model(path: Path, name: str = DEFAULT_MODEL, *,
                   opener: Callable = urllib.request.urlopen) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with opener(RELEASE_URL + MODELS[name][0]) as response, part.open("wb") as out:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    part.replace(path)


class Embedder(Protocol):
    """声の特徴を作るもの（テストでは小さな偽物に差し替える）。"""

    name: str

    def embed(self, samples: np.ndarray) -> Optional[np.ndarray]:
        """16 kHz の float32 [-1, 1] → 長さ 1 のベクトル（短すぎれば None）。"""


class OnnxEmbedder:
    def __init__(self, path: Path, name: str = DEFAULT_MODEL, threads: int = 2) -> None:
        import onnxruntime as ort  # type: ignore[import]

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name
        self._scale = MODELS[name][1]
        self.name = name

    def embed(self, samples: np.ndarray) -> Optional[np.ndarray]:
        feats = fbank(np.asarray(samples, dtype=np.float32) * self._scale)
        if len(feats) < MIN_FRAMES:
            return None
        feats = feats - feats.mean(axis=0, keepdims=True)
        out = self._session.run(None, {self._input: feats[None].astype(np.float32)})[0][0]
        return unit(out)


def unit(vector: Iterable[float]) -> Optional[np.ndarray]:
    v = np.asarray(list(vector), dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else None


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


@dataclass
class Voice:
    """登録した 1 人の声。"""

    name: str
    center: np.ndarray       # 発話の埋め込みの平均（長さ 1）
    count: int
    spread: float            # 登録した発話と center の近さの中央値（その人の声のばらつきの目安）

    def to_dict(self) -> dict:
        return {"name": self.name, "count": self.count, "spread": round(self.spread, 4),
                "center": [round(float(x), 6) for x in self.center]}

    @classmethod
    def from_dict(cls, data: dict) -> "Voice":
        center = unit(data["center"])
        if center is None:
            raise ValueError("center が 0 です")
        return cls(str(data["name"]), center, int(data.get("count") or 0), float(data.get("spread") or 0.0))


def enroll(name: str, embeddings: Iterable[np.ndarray]) -> Optional[Voice]:
    """発話の埋め込みの平均で登録する。外れた発話（別の人の声・物音）を一度だけ除いて平均し直す。"""
    vectors = [v for v in embeddings if v is not None]
    if not vectors:
        return None
    center = unit(np.mean(vectors, axis=0))
    if center is None:
        return None
    sims = np.array([similarity(v, center) for v in vectors])
    if len(vectors) >= 5:
        keep = [v for v, s in zip(vectors, sims) if s >= np.quantile(sims, 0.1)]
        trimmed = unit(np.mean(keep, axis=0))
        center = trimmed if trimmed is not None else center
        sims = np.array([similarity(v, center) for v in keep])
    return Voice(name, center, len(vectors), float(np.median(sims)))


def nearest(embedding: np.ndarray, voices: Iterable[Voice]) -> tuple[Optional[str], dict[str, float]]:
    """いちばん近い登録の名前と、登録ごとの近さ。"""
    scores = {v.name: round(similarity(embedding, v.center), 4) for v in voices}
    if not scores:
        return None, scores
    return max(scores, key=lambda k: scores[k]), scores
