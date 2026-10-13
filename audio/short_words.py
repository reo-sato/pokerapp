"""audio/short_words.py — 短い語の分類器（第 2 の耳の点数 → その発話は何を言ったか）

短い語の聞き間違いの進め方 2（`docs/worklog/2026-10-07-short-word-plan.md`）。Whisper は短く崩したアクションの語を
読めないことが多い（「こる」「こぅ」= コール、「これだ」= フォールド）。第 2 の耳（`audio/second_ear.py`）が全発話で
採点した短い語（`SHORT_WORDS`）・額・候補の確からしさから、その発話のラベル（雑談 / コール / チェック / フォールド /
額）の確率を出す。Whisper の読みは使わない（推定器が Whisper の読みと組み合わせるため、同じ証拠を二重に数えない）。

重み（`short_word_model.json`）は開発データ（`tests/fixtures/store`）の真のアクションから `tools/short_words.py train`
で作る（多項ロジスティック回帰）。読むだけのこのモジュールは numpy だけを使う。
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

LABELS = ("none", "call", "check", "fold", "amount")
MODEL_PATH = Path(__file__).resolve().with_name("short_word_model.json")
# 別の重みで確かめるとき（セッションを抜いて学んだ重みで推定器を回す, `tools/short_words.py`）。推定器の別の
# プロセスにも伝わるよう環境変数で渡す
MODEL_ENV = "POKERAPP_SHORT_WORD_MODEL"

# 確からしさの差（対数）を切る範囲（何も聞こえない語の −inf・自由に聞いた文より確からしい語）
_LOW, _HIGH = -40.0, 10.0


def _clip(value: Optional[float]) -> float:
    if value is None or not math.isfinite(float(value)):
        return _LOW
    return min(_HIGH, max(_LOW, float(value)))


def feature_names(words: tuple[str, ...]) -> list[str]:
    return [f"word:{w}" for w in words] + ["best_amount", "best_candidate", "frames", "free_chars", "free_logp"]


def features(ear: Optional[dict], words: tuple[str, ...]) -> Optional[np.ndarray]:
    """第 2 の耳の結果（`EarResult.to_dict()`）→ 特徴量。短い語の点数の無い古い形・自由に聞いた文の確からしさの無い
    結果は None。各語・額・候補は自由に聞いた文との確からしさの差。"""
    if not ear or ear.get("logp") is None or not isinstance(ear.get("words"), dict):
        return None
    free = float(ear["logp"])
    scores = ear["words"]
    out = [_clip(None if scores.get(w) is None else float(scores[w]) - free) for w in words]
    amounts = ear.get("amounts") or []
    out.append(_clip(float(amounts[0][1]) - free) if amounts and amounts[0][1] is not None else _LOW)
    cands = [c for c in ear.get("candidates") or [] if c.get("logp") is not None]
    out.append(_clip(float(cands[0]["logp"]) - free) if cands else _LOW)
    out.append(float(ear.get("frames") or 0) / 100.0)
    out.append(len((ear.get("text") or "").strip()) / 10.0)
    out.append(_clip(free) / 10.0)
    return np.asarray(out, dtype=np.float64)


class Model:
    """多項ロジスティック回帰（特徴量は学習データの平均・標準偏差でそろえる）。"""

    def __init__(self, data: dict) -> None:
        self.words: tuple[str, ...] = tuple(data["words"])
        self.labels: tuple[str, ...] = tuple(data["labels"])
        self.mean = np.asarray(data["mean"], dtype=np.float64)
        self.std = np.asarray(data["std"], dtype=np.float64)
        self.weights = np.asarray(data["weights"], dtype=np.float64)      # (特徴量 + 1, ラベル)
        self.prior = {k: float(v) for k, v in (data.get("prior") or {}).items()}
        self.version = str(data.get("version") or "")
        # P(Whisper の読みのラベル | 真のラベル)（`tools/short_words.whisper_confusion`）。推定器が Whisper の読みの項に使う
        self.whisper: dict[str, dict[str, float]] = {
            k: {c: float(v) for c, v in row.items()} for k, row in (data.get("whisper") or {}).items()}

    def predict(self, ear: Optional[dict]) -> Optional[dict[str, float]]:
        """ラベル → 確率。第 2 の耳の結果が使えなければ None。"""
        x = features(ear, self.words)
        if x is None or len(x) + 1 != len(self.weights):
            return None
        z = np.concatenate([(x - self.mean) / self.std, [1.0]]) @ self.weights
        z = np.exp(z - z.max())
        p = z / z.sum()
        return {label: float(v) for label, v in zip(self.labels, p)}

    def to_dict(self) -> dict:
        return {"version": self.version, "words": list(self.words), "labels": list(self.labels),
                "mean": [round(float(v), 6) for v in self.mean], "std": [round(float(v), 6) for v in self.std],
                "weights": [[round(float(v), 6) for v in row] for row in self.weights],
                "prior": {k: round(v, 6) for k, v in self.prior.items()},
                "whisper": {k: {c: round(v, 6) for c, v in row.items()} for k, row in self.whisper.items()}}

    def whisper_logp(self, heard: str, label: str) -> Optional[float]:
        """log P(Whisper がラベル `heard` に読んだ | 真のラベル `label`)。表が無ければ None。"""
        row = self.whisper.get(label)
        if not row:
            return None
        value = row.get(heard, row.get("other"))
        return math.log(value) if value else None


def load(path: Optional[str] = None) -> Optional[Model]:
    """学習した重み（無ければ None = 分類器を使わない）。"""
    return _load(str(Path(path or os.environ.get(MODEL_ENV) or MODEL_PATH)))


@lru_cache(maxsize=8)
def _load(path: str) -> Optional[Model]:
    p = Path(path)
    if not p.is_file():
        return None
    return Model(json.loads(p.read_text(encoding="utf-8")))
