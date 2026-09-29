"""audio/second_ear.py — 第 2 の耳（聞き間違いの根本対策の観測層, 2026-09-29 の検討）

日本語専用の音声認識 ReazonSpeech k2 v2（文字単位の RNN-T。k2-fsa が配る ONNX 版, Apache-2.0）で、発話ごとに:

1. 自由に聞いた文（貪欲な探索）と、その文の確からしさ
2. 決まった候補（アクションの語・額・その組み合わせ）ごとの確からしさ log P(候補 | 音声)（全アラインメントの和）

を出す。Whisper の書き起こしの文字列を読むのではなく、音にどの候補がいちばん合うかを確率で比べるので、
書き起こしの表記揺れは関係がない。1 発話 0.3 秒ほど（CPU, int8）。

ライブでは **Whisper がアクションとして読めなかった発話だけ**を聞き直し、第 2 の耳が自由に聞いた文そのものが
いちばん確からしい候補と同じアクションに読めるときだけ、その候補を使う（`rescue_events`。店舗 9/29 の評価:
真のアクションとの一致 73% → 80%, `docs/worklog/2026-09-29-second-ear-evaluation.md`）。Whisper が読めた発話は
変えない（第 2 の耳には席番号・ポジションの候補が無い）。

音の近さだけでは決まらない発話（ディーラーの短く崩した「コル」と「これ」）は使わない。候補と確からしさは記録して、
あとで卓の状態と合わせる（ADR-0056 追記 1 の推定器）。
"""
from __future__ import annotations

import shutil
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol

import numpy as np

SAMPLE_RATE = 16000
BLANK = 0
# k2-fsa の配布物（GitHub のリリース。int8 だけの配布は無いので、流しながら要るファイルだけ取り出す）
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01.tar.bz2")
MODEL_NAME = "reazonspeech-k2-v2"
ENCODER = "encoder-epoch-99-avg-1.int8.onnx"
DECODER = "decoder-epoch-99-avg-1.onnx"
JOINER = "joiner-epoch-99-avg-1.int8.onnx"
TOKENS = "tokens.txt"
MODEL_FILES = (ENCODER, DECODER, JOINER, TOKENS)
# 候補の上位いくつを記録するか
TOP_CANDIDATES = 8
# joiner を一度に計算する行数の上限（メモリ: 行 × 語彙 5224 × 4 バイト）
_JOINER_ROWS = 4096


def model_dir(root: Path) -> Path:
    return root / "models" / MODEL_NAME


def model_ready(folder: Path) -> bool:
    return all((folder / name).is_file() for name in MODEL_FILES)


class _Progress:
    """読んだバイト数を数えながら読むファイル（tarfile に渡す）。"""

    def __init__(self, raw, report: Optional[Callable[[int], None]]) -> None:
        self._raw = raw
        self._report = report
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.count += len(data)
        if self._report is not None:
            self._report(self.count)
        return data


def download_model(folder: Path, url: str = MODEL_URL, *, report: Optional[Callable[[int], None]] = None,
                   opener: Callable = urllib.request.urlopen) -> None:
    """配布物（tar.bz2, 約 713 MB）を流しながら、要る 4 ファイル（約 170 MB）だけ `folder` に取り出す。"""
    folder.mkdir(parents=True, exist_ok=True)
    wanted = set(MODEL_FILES)
    with opener(url) as response:
        stream = _Progress(response, report)
        with tarfile.open(fileobj=stream, mode="r|bz2") as archive:
            for member in archive:
                name = Path(member.name).name
                if not member.isfile() or name not in wanted:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                part = folder / (name + ".part")
                with part.open("wb") as out:
                    shutil.copyfileobj(source, out)
                part.replace(folder / name)
                wanted.discard(name)
                if not wanted:
                    break
    if wanted:
        raise RuntimeError(f"配布物に見つかりませんでした: {', '.join(sorted(wanted))}")


# ――― モデル ―――

class Model(Protocol):
    """音のモデル（テストでは小さな偽物に差し替える）。"""

    tokens: list[str]
    context_size: int

    def encode(self, samples: np.ndarray) -> np.ndarray:
        """(T, D) のエンコーダ出力。"""

    def decoder_out(self, contexts: np.ndarray) -> np.ndarray:
        """(N, C) の直前のトークン → (N, D)。"""

    def joiner_logits(self, enc: np.ndarray, dec: np.ndarray) -> np.ndarray:
        """(N, D) と (N, D) → (N, V) の logits。"""


def fbank(samples: np.ndarray) -> np.ndarray:
    """80 次元の対数メル（k2-fsa の学習と同じ: dither 0・snip_edges なし・high_freq -400）。"""
    import kaldi_native_fbank as knf  # type: ignore[import]

    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0.0
    opts.frame_opts.snip_edges = False
    opts.frame_opts.samp_freq = SAMPLE_RATE
    opts.mel_opts.num_bins = 80
    opts.mel_opts.high_freq = -400
    online = knf.OnlineFbank(opts)
    online.accept_waveform(SAMPLE_RATE, np.asarray(samples, dtype=np.float32).tolist())
    online.input_finished()
    frames = [online.get_frame(i) for i in range(online.num_frames_ready)]
    return np.asarray(frames, dtype=np.float32).reshape(-1, 80)


class OnnxModel:
    """k2-fsa の ONNX（encoder / decoder / joiner + tokens.txt）。"""

    def __init__(self, folder: Path, threads: int = 4) -> None:
        import onnxruntime as ort  # type: ignore[import]

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        options.inter_op_num_threads = 1

        def session(name: str):
            return ort.InferenceSession(str(folder / name), options, providers=["CPUExecutionProvider"])

        self._encoder = session(ENCODER)
        self._decoder = session(DECODER)
        self._joiner = session(JOINER)
        meta = self._decoder.get_modelmeta().custom_metadata_map
        self.context_size = int(meta.get("context_size", 2))
        self.tokens = read_tokens(folder / TOKENS)

    def encode(self, samples: np.ndarray) -> np.ndarray:
        feats = fbank(samples)
        if len(feats) == 0:
            return np.zeros((0, 1), dtype=np.float32)
        out, lens = self._encoder.run(None, {"x": feats[None], "x_lens": np.array([len(feats)], dtype=np.int64)})
        return out[0][: int(lens[0])]

    def decoder_out(self, contexts: np.ndarray) -> np.ndarray:
        return self._decoder.run(None, {"y": np.asarray(contexts, dtype=np.int64)})[0]

    def joiner_logits(self, enc: np.ndarray, dec: np.ndarray) -> np.ndarray:
        return self._joiner.run(None, {"encoder_out": enc.astype(np.float32), "decoder_out": dec.astype(np.float32)})[0]


def read_tokens(path: Path) -> list[str]:
    """tokens.txt（「文字 番号」の行）→ 番号順の文字。"""
    table: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.rsplit(maxsplit=1)
        if len(parts) == 2 and parts[1].isdigit():
            table[int(parts[1])] = parts[0]
        elif len(parts) == 1 and parts[0].isdigit():
            table[int(parts[0])] = " "      # 空白の文字
    return [table.get(i, "") for i in range(max(table) + 1)] if table else []


# ――― 候補 ―――

@dataclass(frozen=True)
class Candidate:
    spoken: str   # 音のモデルに与える文（句読点なし。モデルの書き方 = 漢数字）
    text: str     # 読み取り（parse_actions）に渡す文


_DIGITS = "〇一二三四五六七八九"


def kanji_number(n: int) -> str:
    """ReazonSpeech の書き方の漢数字（1000 = 千、1100 = 千百、10000 = 一万）。"""
    out = ""
    man, n = divmod(n, 10000)
    if man:
        out += kanji_number(man) + "万" if man > 1 else "一万"
    for unit, value in (("千", 1000), ("百", 100), ("十", 10)):
        q, n = divmod(n, value)
        if q:
            out += ("" if q == 1 else _DIGITS[q]) + unit
    if n:
        out += _DIGITS[n]
    return out


# ディーラーが言うアクションの語（正準の形だけ。崩れた言い方は音の確からしさで比べる）
ACTION_WORDS = ("フォールド", "コール", "チェック", "オールイン", "ショーダウン", "ヘッズアップ",
                "チェックアラウンド", "ハンド終了")
# 2 つ続けて言うことがある語（「フォールド、コール」）
_PAIR_WORDS = ("フォールド", "コール", "チェック", "オールイン")
AMOUNTS = tuple(range(100, 20001, 100)) + (25000, 30000, 40000, 50000)


def build_candidates(amounts: Iterable[int] = AMOUNTS) -> list[Candidate]:
    """ディーラーの読み上げの候補: アクションの語（+「です」）・額（+「点」）・レイズ/ベット + 額・
    2 つのアクション・額 + コール・コール + 額。"""
    out: list[Candidate] = []
    for word in ACTION_WORDS:
        out.append(Candidate(word, word))
        out.append(Candidate(word + "です", word + "です"))
    for amount in amounts:
        k = kanji_number(amount)
        out.append(Candidate(k, k))
        out.append(Candidate(k + "点", k + "点"))
        out.append(Candidate("レイズ" + k, "レイズ " + k))
        out.append(Candidate("ベット" + k, "ベット " + k))
        out.append(Candidate(k + "コール", k + "、コール"))
        out.append(Candidate("コール" + k, "コール " + k))
    for first in _PAIR_WORDS:
        for second in _PAIR_WORDS:
            out.append(Candidate(first + second, first + "、" + second))
    return out


# ――― 採点（RNN-T の前向きアルゴリズム）―――

@dataclass
class _Node:
    parent: int
    token: int
    depth: int
    context: tuple[int, ...]
    children: dict[int, int] = field(default_factory=dict)


class CandidateTrie:
    """候補の文字列の接頭木（同じ出だしの候補は計算を分け合う）。"""

    def __init__(self, candidates: list[Candidate], tokens: list[str], context_size: int) -> None:
        index = {t: i for i, t in enumerate(tokens) if t}
        self.nodes: list[_Node] = [_Node(-1, BLANK, 0, (BLANK,) * context_size)]
        self.candidates: list[Candidate] = []
        self.ends: list[int] = []
        for candidate in candidates:
            ids = [index.get(ch) for ch in candidate.spoken]
            if not ids or any(i is None for i in ids):
                continue                        # モデルの文字に無い（句読点など）
            node = 0
            for token in ids:
                child = self.nodes[node].children.get(token)
                if child is None:
                    parent = self.nodes[node]
                    context = (parent.context + (token,))[-context_size:]
                    self.nodes.append(_Node(node, token, parent.depth + 1, context))
                    child = len(self.nodes) - 1
                    parent.children[token] = child
                node = child
            self.candidates.append(candidate)
            self.ends.append(node)
        self.levels: list[list[int]] = []
        for i, node in enumerate(self.nodes[1:], start=1):
            while len(self.levels) < node.depth:
                self.levels.append([])
            self.levels[node.depth - 1].append(i)


def _log_softmax_columns(logits: np.ndarray, columns: np.ndarray) -> np.ndarray:
    """(N, V) の logits から、log-softmax の指定した列だけ（(N, len(columns))）。"""
    top = logits.max(axis=1, keepdims=True)
    norm = top[:, 0] + np.log(np.exp(logits - top).sum(axis=1))
    return logits[:, columns] - norm[:, None]


class Heard:
    """1 発話のエンコーダ出力と、直前のトークン（context）ごとの確率の置き場。"""

    def __init__(self, model: Model, enc: np.ndarray) -> None:
        self.model = model
        self.enc = np.asarray(enc, dtype=np.float32)
        self.frames = len(self.enc)
        self._full: dict[tuple[int, ...], np.ndarray] = {}

    def _dec(self, contexts: list[tuple[int, ...]]) -> np.ndarray:
        return self.model.decoder_out(np.asarray(contexts, dtype=np.int64))

    def full(self, context: tuple[int, ...]) -> np.ndarray:
        """(T, V) の log-softmax（貪欲な探索と、自由に聞いた文の採点に使う）。"""
        value = self._full.get(context)
        if value is None:
            dec = np.repeat(self._dec([context]), self.frames, axis=0)
            logits = self.model.joiner_logits(self.enc, dec)
            value = _log_softmax_columns(logits, np.arange(logits.shape[1]))
            self._full[context] = value
        return value

    def columns(self, requests: dict[tuple[int, ...], set[int]]) -> dict[tuple[int, ...], dict[int, np.ndarray]]:
        """context ごとに要る列（blank と次の文字）の log 確率 (T,)。まとめて joiner にかける。"""
        out: dict[tuple[int, ...], dict[int, np.ndarray]] = {}
        contexts = list(requests)
        per_batch = max(1, _JOINER_ROWS // max(1, self.frames))
        for start in range(0, len(contexts), per_batch):
            batch = contexts[start:start + per_batch]
            dec = np.repeat(self._dec(batch), self.frames, axis=0)
            enc = np.tile(self.enc, (len(batch), 1))
            logits = self.model.joiner_logits(enc, dec)
            for i, context in enumerate(batch):
                cols = np.array(sorted(requests[context] | {BLANK}), dtype=np.int64)
                rows = logits[i * self.frames:(i + 1) * self.frames]
                values = _log_softmax_columns(rows, cols)
                out[context] = {int(c): values[:, j] for j, c in enumerate(cols)}
        return out

    def greedy(self, context_size: int) -> list[int]:
        """1 フレームに 1 文字までの貪欲な探索（sherpa-onnx の greedy_search と同じ）。"""
        context = (BLANK,) * context_size
        out: list[int] = []
        for t in range(self.frames):
            token = int(self.full(context)[t].argmax())
            if token != BLANK:
                out.append(token)
                context = (context + (token,))[-context_size:]
        return out

    def score(self, ids: list[int], context_size: int) -> float:
        """log P(ids | 音声)（全アラインメントの和。1 フレームに複数の文字を出してよい）。"""
        contexts = [(BLANK,) * context_size]
        for token in ids:
            contexts.append((contexts[-1] + (token,))[-context_size:])
        frames = self.frames
        if frames == 0:
            return float("-inf")
        alpha = np.full(frames, -np.inf)
        alpha[0] = 0.0
        blank = self.full(contexts[0])[:, BLANK]
        for t in range(1, frames):
            alpha[t] = alpha[t - 1] + blank[t - 1]
        for u, token in enumerate(ids):
            emit = self.full(contexts[u])[:, token]
            blank = self.full(contexts[u + 1])[:, BLANK]
            nxt = np.full(frames, -np.inf)
            nxt[0] = alpha[0] + emit[0]
            for t in range(1, frames):
                nxt[t] = np.logaddexp(nxt[t - 1] + blank[t - 1], alpha[t] + emit[t])
            alpha = nxt
        return float(alpha[-1] + self.full(contexts[-1])[-1, BLANK])

    def score_trie(self, trie: CandidateTrie) -> np.ndarray:
        """接頭木のすべての候補の log P(候補 | 音声)。同じ深さの節をまとめて計算する。"""
        frames = self.frames
        if frames == 0 or not trie.candidates:
            return np.full(len(trie.candidates), -np.inf)
        requests: dict[tuple[int, ...], set[int]] = {}
        for node in trie.nodes:
            requests.setdefault(node.context, set()).update(node.children)
        probs = self.columns(requests)
        alpha = np.full((len(trie.nodes), frames), -np.inf)
        root_blank = probs[trie.nodes[0].context][BLANK]
        alpha[0, 0] = 0.0
        for t in range(1, frames):
            alpha[0, t] = alpha[0, t - 1] + root_blank[t - 1]
        for level in trie.levels:
            idx = np.asarray(level)
            parents = np.asarray([trie.nodes[i].parent for i in level])
            emit = np.stack([probs[trie.nodes[trie.nodes[i].parent].context][trie.nodes[i].token] for i in level])
            blank = np.stack([probs[trie.nodes[i].context][BLANK] for i in level])
            from_parent = alpha[parents] + emit
            cur = np.empty((len(level), frames))
            cur[:, 0] = from_parent[:, 0]
            for t in range(1, frames):
                cur[:, t] = np.logaddexp(cur[:, t - 1] + blank[:, t - 1], from_parent[:, t])
            alpha[idx] = cur
        ends = np.asarray(trie.ends)
        final_blank = np.stack([probs[trie.nodes[i].context][BLANK][-1] for i in trie.ends])
        return alpha[ends, -1] + final_blank


@dataclass
class EarResult:
    text: str                           # 自由に聞いた文
    logp: float                         # その文の確からしさ
    candidates: list[tuple[str, float]]  # (読み取りに渡す文, 確からしさ) の上位

    def to_dict(self) -> dict:
        return {"text": self.text, "logp": _round(self.logp),
                "candidates": [{"text": t, "logp": _round(s)} for t, s in self.candidates]}


def _round(value: float) -> Optional[float]:
    return None if not np.isfinite(value) else round(float(value), 3)


class SecondEar:
    """発話の音声 → 自由に聞いた文と、候補ごとの確からしさ。"""

    def __init__(self, model: Model, candidates: Optional[list[Candidate]] = None) -> None:
        self.model = model
        self.trie = CandidateTrie(candidates if candidates is not None else build_candidates(),
                                  model.tokens, model.context_size)

    @classmethod
    def load(cls, folder: Path, threads: int = 4) -> "SecondEar":
        return cls(OnnxModel(folder, threads=threads))

    def hear(self, samples: np.ndarray, top: int = TOP_CANDIDATES) -> EarResult:
        heard = Heard(self.model, self.model.encode(samples))
        ids = heard.greedy(self.model.context_size)
        text = "".join(self.model.tokens[i] for i in ids)
        free = heard.score(ids, self.model.context_size)
        scores = heard.score_trie(self.trie)
        order = np.argsort(-scores)[:top]
        return EarResult(text, free, [(self.trie.candidates[i].text, float(scores[i])) for i in order])


# ――― ライブの聞き直し ―――

# 第 2 の耳の候補から作ったアクションの印（要確認）と、聞き取りの自信（Whisper の自信は、読めなかった文・
# 幻聴のものなので使えない）
EAR_FLAG = "second_ear"
EAR_CONFIDENCE = 0.5


def _action_keys(text: str) -> list[tuple]:
    from audio.recognizer import parse_actions

    return [(e.action, e.amount, e.seat, e.position) for e in parse_actions(text)]


def agreed_candidate(ear: Optional[dict]) -> Optional[str]:
    """第 2 の耳が自由に聞いた文そのものが、いちばん確からしい候補と同じアクションに読めるとき、その候補の文。

    `ear` は `EarResult.to_dict()` の形。何も聞こえていない（自由に聞いた文が空）・候補が読めない・自由に聞いた文が
    違うアクションに読める（雑談「お願いしま」→ 候補「千」、チップを数える「三万四千四百点」→「四千四百点」）なら None。
    """
    if not ear:
        return None
    cands = ear.get("candidates") or []
    free = (ear.get("text") or "").strip()
    if not cands or not free:
        return None
    best = (cands[0].get("text") or "").strip()
    keys = _action_keys(best) if best else []
    return best if keys and _action_keys(free) == keys else None


def rescue_events(ear: Optional[dict], *, utterance_start_ts: Optional[float] = None) -> list:
    """`agreed_candidate` の文を読んだアクション（`second_ear` の印 = 要確認）。使えなければ空。"""
    from audio.recognizer import parse_actions

    text = agreed_candidate(ear)
    if text is None:
        return []
    events = parse_actions(text, confidence=EAR_CONFIDENCE, utterance_start_ts=utterance_start_ts)
    for event in events:
        event.parse_flags = (*event.parse_flags, EAR_FLAG)
    return events


def pcm16_samples(audio_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """PCM16（モノラル）→ 16 kHz の float32 [-1, 1]。"""
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if sample_rate != SAMPLE_RATE and len(audio):
        n = max(1, round(len(audio) * SAMPLE_RATE / sample_rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


def load_live(root: Path, audio_cfg: dict) -> tuple[Optional[SecondEar], str]:
    """ライブで使う第 2 の耳を読み込む（config `audio.second_ear` = {"enabled", "threads"}, 既定は使う）。

    (第 2 の耳 or None, CLI に出す一言) を返す。使えなくても聞き取りは Whisper だけで続ける。
    """
    cfg = audio_cfg.get("second_ear") or {}
    if not cfg.get("enabled", True):
        return None, "第 2 の耳は使いません（audio.second_ear.enabled=false）"
    folder = model_dir(root)
    if not model_ready(folder):
        return None, "第 2 の耳のモデルがありません（更新で取得します）。Whisper だけで聞き取ります"
    try:
        import kaldi_native_fbank  # type: ignore[import]  # noqa: F401 — 聞くときに使う。無ければここで分かる

        ear = SecondEar.load(folder, threads=int(cfg.get("threads", 4) or 4))
        ear.hear(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))    # 一度聞いてみる（壊れたモデルをここで見つける）
    except ImportError as e:
        return None, f"第 2 の耳の部品がありません（{e}。更新で入ります）。Whisper だけで聞き取ります"
    except Exception as e:  # noqa: BLE001 — 読み込めなくても聞き取りは止めない
        return None, f"第 2 の耳を読み込めませんでした（{type(e).__name__}: {e}）。Whisper だけで聞き取ります"
    return ear, "第 2 の耳（Whisper が読めなかった発話の聞き直し）を使います"
