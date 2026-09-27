"""tools/rescore_audio.py

保存した発話の音声を、店舗 PC の Whisper で **採点し直す**（ADR-0056 追記 1 の S2 = 聞き取りの採点）。

ライブの書き起こしは 1 つの読み（ビーム探索の 1 位）しか残さない。事後の推定（S3）は「ほかにどう聞こえ得たか」と
「それぞれの確からしさ」を使う。発話ごとに:

1. 書き起こしの別候補（ビーム探索の上位 5 つ。ライブと同じプロンプト）
2. 候補の文ごとの確からしさ = その文を言い切って終わる確率の対数（文を Whisper に与え、各トークンと
   「終わり」の確率を足す）。候補 = 別候補・ライブの書き起こし・音の近さで読み替えた文・アクションの語だけ
   （コール・チェック・フォールド…）
3. いちばん確からしい文（`best`）

を `logs/<sid>.rescored.jsonl` に書く（1 発話 1 行。途中で止めても、次はその続きから）。音声は
`logs/audio/<sid>/`（pack_logs の zip を展開したフォルダなら `<sid>/audio/`）。評価は `tools/eval_store.py`
（`best` を今の読み取りで読み直して再生し、真のアクションとの一致率を比べる）。

使い方（店舗 PC。モデルはライブと同じ config の audio.whisper_model。1 発話 数秒かかる）:

    python tools/rescore_audio.py                         # logs/ の全セッション
    python tools/rescore_audio.py --session 8d08c010      # 1 セッション（ID の先頭でよい）
    python tools/rescore_audio.py --limit 5               # 試しに 5 発話
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from audio.phonetic import ACTION_WORDS  # noqa: E402
from audio.recognizer import phonetic_reading  # noqa: E402
from core.constants import WHISPER_PROMPT_JA  # noqa: E402

SAMPLE_RATE = 16000
RESCORED_SUFFIX = ".rescored.jsonl"
NBEST = 5
# 書き起こしに使うトークン数の上限（ライブと同じ考え方: 基本 + 音の長さあたり）
_TOKENS_BASE = 40
_TOKENS_PER_SEC = 20
_TOKENS_MAX = 200
# 候補の文を与えたあと、「終わり」の確率が出るまで生成させる余裕（生成の長さは与えた文を含めて数える）
_SCORE_EXTRA_STEPS = 8
_MAX_LENGTH = 448          # Whisper の上限（プロンプト + 文）


# ――― 入力 ―――

@dataclass
class SessionAudio:
    session_id: str
    transcripts: Path
    audio_dirs: tuple[Path, ...]

    @property
    def rescored(self) -> Path:
        return self.transcripts.with_name(self.session_id + RESCORED_SUFFIX)

    def audio_path(self, name: str) -> Optional[Path]:
        for folder in self.audio_dirs:
            path = folder / name
            if path.is_file():
                return path
        return None


def find_sessions(root: Path, only: Optional[list[str]] = None) -> list[SessionAudio]:
    """聞き取りの記録（`<sid>.transcripts.jsonl`）のあるセッション。"""
    out = []
    for path in sorted(root.rglob("*.transcripts.jsonl")):
        sid = path.name[: -len(".transcripts.jsonl")]
        if only and not any(sid.startswith(prefix) for prefix in only):
            continue
        out.append(SessionAudio(sid, path, (path.parent / "audio" / sid, path.parent / "audio")))
    return out


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_wav(path: Path) -> np.ndarray:
    """WAV（PCM16）を 16 kHz モノラルの float32 [-1, 1] にする。"""
    with wave.open(str(path), "rb") as src:
        rate, channels, width = src.getframerate(), src.getnchannels(), src.getsampwidth()
        data = src.readframes(src.getnframes())
    if width != 2:
        raise ValueError(f"PCM16 ではありません（{width * 8} bit）")
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(audio):
        n = max(1, round(len(audio) * SAMPLE_RATE / rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


def candidate_texts(heard: str) -> dict[str, list[str]]:
    """確からしさを測る文と、その出どころ（heard = ライブの書き起こし / fuzzy = 音の近さの読み替え / word）。"""
    out: dict[str, list[str]] = {}

    def add(text: Optional[str], source: str) -> None:
        text = (text or "").strip()
        if text:
            out.setdefault(text, [])
            if source not in out[text]:
                out[text].append(source)

    add(heard, "heard")
    add(phonetic_reading(heard) if heard else None, "fuzzy")
    for word in ACTION_WORDS:
        add(word, "word")
    return out


# ――― 採点（Whisper）―――

def log_softmax(row: Any) -> np.ndarray:
    values = np.asarray(row, dtype=np.float64).reshape(-1)
    top = values.max()
    return values - (top + math.log(float(np.exp(values - top).sum())))


def sequence_logprob(step_logits: list, tokens: list[int], eot: int) -> Optional[float]:
    """与えた文の各ステップの logits から、log P(文のトークン列 + 終わり)。ステップが足りなければ None。

    step_logits[i] は i 番目のトークンを予測する分布（与えた文の 1 つ目から。最後の 1 つは文のあとの分布）。
    """
    if len(step_logits) <= len(tokens):
        return None
    total = 0.0
    for i, token in enumerate([*tokens, eot]):
        total += float(log_softmax(step_logits[i])[token])
    return total


class WhisperRescorer:
    """faster-whisper の WhisperModel を使って、1 発話の別候補と候補の文の確からしさを出す。"""

    def __init__(self, model: Any, tokenizer: Any, prompt: Optional[str] = WHISPER_PROMPT_JA,
                 beam_size: int = 5, nbest: int = NBEST) -> None:
        self.model = model
        self.tok = tokenizer
        self.beam_size = max(beam_size, nbest)
        self.nbest = nbest
        prefix = [tokenizer.sot_prev, *tokenizer.encode(" " + prompt.strip())] if prompt else []
        self.start = [*prefix, *tokenizer.sot_sequence, tokenizer.no_timestamps]

    @classmethod
    def load(cls, model_size: str, language: str = "ja", prompt: Optional[str] = WHISPER_PROMPT_JA,
             beam_size: int = 5) -> "WhisperRescorer":
        from faster_whisper import WhisperModel  # type: ignore[import]
        from faster_whisper.tokenizer import Tokenizer  # type: ignore[import]

        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task="transcribe",
                              language=language)
        return cls(model, tokenizer, prompt=prompt, beam_size=beam_size)

    def encode(self, audio: np.ndarray) -> Any:
        """30 秒の窓に詰めたメルスペクトルを 1 回だけエンコードする（候補の採点はこの出力を使い回す）。"""
        features = self.model.feature_extractor(audio)
        frames = features.shape[-1] - 1
        return self.model.encode(_pad_or_trim(features[:, :frames]))

    def alternatives(self, encoded: Any, seconds: float) -> tuple[list[dict], Optional[float]]:
        """ビーム探索の上位の書き起こし（text / tokens / score = 1 トークンあたりの対数確率）と無音の確率。"""
        max_new = min(_TOKENS_MAX, _TOKENS_BASE + math.ceil(_TOKENS_PER_SEC * seconds))
        result = self.model.model.generate(
            encoded, [self.start], beam_size=self.beam_size, num_hypotheses=self.nbest,
            return_scores=True, return_no_speech_prob=True, max_length=len(self.start) + max_new,
        )[0]
        out = []
        for ids, score in zip(result.sequences_ids, result.scores or [None] * len(result.sequences_ids)):
            ids = [int(i) for i in ids if int(i) < self.tok.eot]
            out.append({"text": self.tok.decode(ids).strip(), "tokens": ids,
                        "score": None if score is None else round(float(score), 4)})
        no_speech = getattr(result, "no_speech_prob", None)
        return out, (None if no_speech is None else float(no_speech))

    def score(self, encoded: Any, token_lists: list[list[int]]) -> list[Optional[float]]:
        """文（トークン列）ごとに log P(文 + 終わり | 音声)。候補をまとめて 1 回の生成で測る。

        文を与えて生成させ、各ステップの分布（`return_logits_vocab`）から文のトークンと、文のあとの「終わり」の
        確率を拾う。文のあとのステップが出なかった候補だけ、長く生成させて測り直す。
        """
        scores = self._score_batch(encoded, token_lists, _SCORE_EXTRA_STEPS)
        missing = [i for i, value in enumerate(scores) if value is None]
        if missing:
            again = self._score_batch(encoded, [token_lists[i] for i in missing], 4 * _SCORE_EXTRA_STEPS)
            for i, value in zip(missing, again):
                scores[i] = value
        return scores

    def _score_batch(self, encoded: Any, token_lists: list[list[int]], extra: int) -> list[Optional[float]]:
        if not token_lists:
            return []
        prompts = [[*self.start, *tokens] for tokens in token_lists]
        results = self.model.model.generate(
            _tile(encoded, len(token_lists)), prompts, beam_size=1,
            max_length=min(_MAX_LENGTH, max(len(p) for p in prompts) + extra),
            return_logits_vocab=True, suppress_blank=False, suppress_tokens=[],
        )
        return [sequence_logprob(list(r.logits[0]) if r.logits else [], tokens, self.tok.eot)
                for tokens, r in zip(token_lists, results)]

    def rescore(self, audio: np.ndarray, heard: str) -> dict:
        encoded = self.encode(audio)
        alternatives, no_speech = self.alternatives(encoded, len(audio) / SAMPLE_RATE)
        texts = candidate_texts(heard)
        tokens: dict[str, list[int]] = {}
        for alt in alternatives:
            if alt["text"]:
                texts.setdefault(alt["text"], [])
                if "nbest" not in texts[alt["text"]]:
                    texts[alt["text"]].insert(0, "nbest")
                tokens.setdefault(alt["text"], alt["tokens"])
        for text in texts:
            tokens.setdefault(text, self.tok.encode(text))
        order = [t for t in texts if tokens[t]]
        scores = self.score(encoded, [tokens[t] for t in order])
        scored = sorted(
            ({"text": t, "logprob": round(s, 4), "tokens": len(tokens[t]), "from": texts[t]}
             for t, s in zip(order, scores) if s is not None),
            key=lambda row: -row["logprob"],
        )
        return {
            "no_speech_prob": None if no_speech is None else round(no_speech, 4),
            "nbest": [{"text": a["text"], "score": a["score"]} for a in alternatives],
            "scored": scored,
            "best": scored[0]["text"] if scored else None,
        }


def _pad_or_trim(features: np.ndarray, length: int = 3000) -> np.ndarray:
    """メルスペクトルを Whisper の 30 秒の窓（3000 フレーム）に詰める（faster_whisper.audio.pad_or_trim と同じ）。"""
    if features.shape[-1] > length:
        features = features[..., :length]
    if features.shape[-1] < length:
        pad = [(0, 0)] * features.ndim
        pad[-1] = (0, length - features.shape[-1])
        features = np.pad(features, pad)
    return features


def _tile(encoded: Any, n: int) -> Any:
    """エンコーダの出力を候補の数だけ並べる（候補ごとにエンコードし直さない）。"""
    array = np.asarray(encoded)
    tiled = np.repeat(array, n, axis=0)
    try:
        import ctranslate2  # type: ignore[import]
    except ImportError:
        return tiled
    return ctranslate2.StorageView.from_array(np.ascontiguousarray(tiled))


# ――― セッション ―――

def _row_key(row: dict) -> Optional[str]:
    return row.get("audio_file") or None


def rescore_session(
    session: SessionAudio, rescorer: Any, *, limit: Optional[int] = None, include_no_speech: bool = False,
    model_name: str = "", log: Callable[[str], None] = print,
) -> tuple[int, int]:
    """1 セッションの発話を採点し、`<sid>.rescored.jsonl` に追記する。(採点した数, 失敗した数) を返す。

    音声が保存されていない発話（`audio.save_audio` が off のときの記録）は飛ばす（行も書かない）。
    """
    done = {row.get("audio_file") for row in read_jsonl(session.rescored)}
    rows = [r for r in read_jsonl(session.transcripts)
            if _row_key(r) and r.get("audio_file") not in done and (include_no_speech or not r.get("no_speech"))]
    missing = [r for r in rows if session.audio_path(r["audio_file"]) is None]
    if missing:
        log(f"  音声の無い発話 {len(missing)} 個は飛ばします")
    rows = [r for r in rows if session.audio_path(r["audio_file"]) is not None]
    if limit is not None:
        rows = rows[:max(0, limit)]
    count = failed = 0
    for i, row in enumerate(rows, start=1):
        path = session.audio_path(row["audio_file"])
        out: dict[str, Any] = {
            "utterance_start_ts": row.get("utterance_start_ts"), "heard_at": row.get("heard_at"),
            "audio_file": row["audio_file"], "text": row.get("text") or "",
            "rescored_at": datetime.now().isoformat(timespec="seconds"), "model": model_name,
        }
        started = time.time()
        try:
            out.update(rescorer.rescore(load_wav(path), out["text"]))
        except Exception as e:  # noqa: BLE001 — 1 発話の失敗で全体を止めない
            out["error"] = f"{type(e).__name__}: {e}"
        out["sec"] = round(time.time() - started, 2)
        failed += 1 if "error" in out else 0
        count += 1
        with session.rescored.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
        shown = out.get("error") or f"「{out['text']}」→「{out.get('best') or ''}」"
        log(f"  [{i}/{len(rows)}] {row['audio_file']} {shown}（{out['sec']} 秒）")
    return count, failed


def _load_config() -> dict:
    try:
        from core.config import load_config

        return load_config()
    except Exception:  # noqa: BLE001
        return {}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="保存した発話の音声を Whisper で採点し直す（S2）")
    ap.add_argument("root", nargs="?", default=None, help="logs/ か pack_logs の zip を展開したフォルダ（既定 logs/）")
    ap.add_argument("--session", action="append", help="セッション ID（先頭でよい、複数回可）")
    ap.add_argument("--limit", type=int, default=None, help="セッションごとに採点する発話の数（試すとき）")
    ap.add_argument("--model", default=None, help="Whisper のモデル（既定 config の audio.whisper_model）")
    ap.add_argument("--no-prompt", action="store_true", help="ライブのプロンプトを付けずに採点する")
    ap.add_argument("--include-no-speech", action="store_true", help="声ではない音（VAD）も採点する")
    args = ap.parse_args(argv)

    config = _load_config()
    audio_cfg = config.get("audio") or {}
    root = Path(args.root) if args.root else ROOT / ((config.get("session") or {}).get("log_dir") or "logs")
    sessions = find_sessions(root, args.session)
    if not sessions:
        print(f"[rescore] 聞き取りの記録（*.transcripts.jsonl）が {root} にありません")
        return 1
    model_name = args.model or audio_cfg.get("whisper_model") or "medium"
    print(f"[rescore] Whisper {model_name} を読み込んでいます（初回はモデルの取得に時間がかかります）")
    try:
        rescorer = WhisperRescorer.load(
            model_name, language=audio_cfg.get("language") or "ja",
            prompt=None if args.no_prompt else WHISPER_PROMPT_JA, beam_size=int(audio_cfg.get("beam_size") or 5),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[rescore] Whisper を読み込めませんでした: {type(e).__name__}: {e}")
        return 1
    total = failed = 0
    for session in sessions:
        print(f"[rescore] {session.session_id}")
        n, bad = rescore_session(session, rescorer, limit=args.limit, include_no_speech=args.include_no_speech,
                                 model_name=model_name)
        total += n
        failed += bad
        if n == 0:
            print("  採点する発話はありません（採点済み、または音声が保存されていない）")
    print(f"[rescore] {total} 発話を採点しました（失敗 {failed}）。結果は各セッションの *{RESCORED_SUFFIX}")
    return 0 if failed == 0 or failed < total else 1


if __name__ == "__main__":
    sys.exit(main())
