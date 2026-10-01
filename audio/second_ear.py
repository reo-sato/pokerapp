"""audio/second_ear.py — 第 2 の耳（聞き間違いの根本対策の観測層, 2026-09-29 の検討）

日本語専用の音声認識 ReazonSpeech k2 v2（文字単位の RNN-T。k2-fsa が配る ONNX 版, Apache-2.0）で、発話ごとに:

1. 自由に聞いた文（貪欲な探索）と、その文の確からしさ
2. 決まった候補（アクションの語・額・その組み合わせ）ごとの確からしさ log P(候補 | 音声)（全アラインメントの和）

を出す。Whisper の書き起こしの文字列を読むのではなく、音にどの候補がいちばん合うかを確率で比べるので、
書き起こしの表記揺れは関係がない。1 発話 0.3 秒ほど（CPU, int8）。

ライブでは **Whisper がアクションとして読めなかった発話**を聞き直し、第 2 の耳が自由に聞いた文そのものが
いちばん確からしい候補と同じアクションに読めるときだけ、その候補を使う（`rescue_events`。店舗 9/29 の評価:
真のアクションとの一致 73% → 80%, `docs/worklog/2026-09-29-second-ear-evaluation.md`）。Whisper が読めた発話は
アクションを変えない（第 2 の耳には席番号・ポジションの候補が無い）。ただし Whisper が読んだベット・レイズに
額が無い・100 未満のとき（「レイズ3 ハピック」）は、第 2 の耳の額だけを入れる（`fill_amounts`。読み上げ集
2026-09-30）。どの発話をどう聞き直すかは `wants_ear` / `apply_ear` の 1 か所で決める（ライブ・読み直し・推定器）。

音の近さだけでは決まらない発話（ディーラーの短く崩した「コル」と「これ」）は使わない。候補と確からしさは記録して、
あとで卓の状態と合わせる（ADR-0056 追記 1 の推定器）。
"""
from __future__ import annotations

import shutil
import tarfile
import urllib.request
from dataclasses import dataclass, field, replace
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
    amount: int = 0   # 額の候補ならその額（額ごとの点数の表を作る）


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
# ショーダウンで言う役の名前（第 2 の耳の書き方 → 読み取りに渡す語）。Whisper は短い役名を幻聴にしやすいが、
# 第 2 の耳は聞けていた（読み上げ集 2026-09-30:「ワンペア」→「本日はここまでです。」「完璧だ!」、
# 「ツーペア」→「つぺよ!」を第 2 の耳は「ワンペア」「二ペア」）
HAND_NAME_WORDS = (("ワンペア", "ワンペア"), ("二ペア", "ツーペア"), ("ツーペア", "ツーペア"),
                   ("スリーカード", "スリーカード"), ("ストレート", "ストレート"), ("フラッシュ", "フラッシュ"),
                   ("フルハウス", "フルハウス"), ("フォーカード", "フォーカード"),
                   ("ストレートフラッシュ", "ストレートフラッシュ"))
# 2 つ続けて言うことがある語（「フォールド、コール」）
_PAIR_WORDS = ("フォールド", "コール", "チェック", "オールイン")
AMOUNTS = tuple(range(100, 20001, 100)) + (25000, 30000, 40000, 50000)
# 第 2 の耳の候補にある一番小さい額。Whisper が読んだ額がこれ未満なら聞き違い（「レイズ3 ハピック」= 1800）
EAR_MIN_AMOUNT = min(AMOUNTS)


def build_candidates(amounts: Iterable[int] = AMOUNTS) -> list[Candidate]:
    """ディーラーの読み上げの候補: アクションの語（+「です」）・役の名前・額（+「点」）・レイズ/ベット + 額・
    2 つのアクション・額 + コール・コール + 額。"""
    out: list[Candidate] = []
    for word in ACTION_WORDS:
        out.append(Candidate(word, word))
        out.append(Candidate(word + "です", word + "です"))
    for spoken, text in HAND_NAME_WORDS:
        out.append(Candidate(spoken, text))
    for amount in amounts:
        k = kanji_number(amount)
        out.append(Candidate(k, k, amount))
        out.append(Candidate(k + "点", k + "点", amount))
        out.append(Candidate("レイズ" + k, "レイズ " + k, amount))
        out.append(Candidate("ベット" + k, "ベット " + k, amount))
        out.append(Candidate(k + "コール", k + "、コール", amount))
        out.append(Candidate("コール" + k, "コール " + k, amount))
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
    # 額ごとの点数（その額のどの言い方でもいちばん確からしいもの）の上位 `AMOUNT_TABLE_SIZE`。engine がその場面で
    # 使える額だけに絞って選ぶ（オーナー 2026-10-01「可能なベット / レイズ額の空間を事前に用意しておけば」）
    amounts: list[tuple[int, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"text": self.text, "logp": _round(self.logp),
               "candidates": [{"text": t, "logp": _round(s)} for t, s in self.candidates]}
        if self.amounts:
            out["amounts"] = [[a, _round(s)] for a, s in self.amounts if np.isfinite(s)]
        return out


def _round(value: float) -> Optional[float]:
    return None if not np.isfinite(value) else round(float(value), 3)


# 記録に残す額の数（点数の上位）。使える額の最も良いものは、ほぼこの中にある（ほかは音が遠い）
AMOUNT_TABLE_SIZE = 40


def amount_scores_of(candidates: list[Candidate], scores: np.ndarray,
                     size: int = AMOUNT_TABLE_SIZE) -> list[tuple[int, float]]:
    """候補ごとの点数から、額ごとの点数（言い方のうち最も確からしいもの）の上位 `size`。"""
    best: dict[int, float] = {}
    for cand, score in zip(candidates, scores):
        if cand.amount and np.isfinite(score) and score > best.get(cand.amount, float("-inf")):
            best[cand.amount] = float(score)
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:size]


def amount_table(ear: Optional[dict]) -> dict[int, float]:
    """`EarResult.to_dict()` の額ごとの点数（無ければ空。古い記録には無い）。"""
    table: dict[int, float] = {}
    for item in (ear or {}).get("amounts") or []:
        try:
            amount, logp = int(item[0]), item[1]
        except (TypeError, ValueError, IndexError):
            continue
        if logp is not None and amount > 0:
            table[amount] = float(logp)
    return table


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
        return EarResult(text, free, [(self.trie.candidates[i].text, float(scores[i])) for i in order],
                         amounts=amount_scores_of(self.trie.candidates, scores))


# ――― ライブの聞き直し ―――

# 第 2 の耳の候補から作ったアクションの印（要確認）と、聞き取りの自信（Whisper の自信は、読めなかった文・
# 幻聴のものなので使えない）
EAR_FLAG = "second_ear"
EAR_CONFIDENCE = 0.5


def _action_keys(text: str) -> list[tuple]:
    from audio.recognizer import parse_actions

    return [(e.action, e.amount, e.seat, e.position, e.hand_name) for e in parse_actions(text)]


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


def _amountless(events: Iterable) -> list[int]:
    """ベット・レイズなのに額が無い・`EAR_MIN_AMOUNT` 未満の位置。"""
    return [i for i, e in enumerate(events) if e.action in ("bet", "raise") and (e.amount or 0) < EAR_MIN_AMOUNT]


def agreed_amount(ear: Optional[dict]) -> Optional[tuple[int, str]]:
    """第 2 の耳が自由に聞いた文と、いちばん確からしい候補が、同じ額（`EAR_MIN_AMOUNT` 以上）を 1 つだけ含むとき、
    (額, 候補の文)。アクションの語は問わない（読み上げ集 2026-09-30:「レイズ 800」を自由に「レーズ八百句」、
    候補は「八百」と聞いた）。額を入れるだけなので、アクションは Whisper の読みを使う。"""
    from audio.recognizer import parse_actions

    if not ear:
        return None
    cands = ear.get("candidates") or []
    free = (ear.get("text") or "").strip()
    best = (cands[0].get("text") or "").strip() if cands else ""
    if not free or not best:
        return None
    amounts = [[e.amount for e in parse_actions(t) if e.amount] for t in (best, free)]
    if len(amounts[0]) == 1 and amounts[0] == amounts[1] and amounts[0][0] >= EAR_MIN_AMOUNT:
        return amounts[0][0], best
    return None


def fill_amounts(events: list, ear: Optional[dict]) -> Optional[tuple[list, str]]:
    """Whisper が読んだアクションのうち、額の無い・`EAR_MIN_AMOUNT` 未満のベット・レイズがちょうど 1 つなら、第 2 の耳の
    額（`agreed_amount`）を入れる（`second_ear` の印 = 要確認）。(アクション, 候補の文) か、入れられなければ None。

    読み上げ集 2026-09-30: Whisper が「ベッド サンビュアック」「レイズ3 ハピック」「ディレイズ 4 セント」と額だけを
    崩した句で、第 2 の耳は「ベッド三百」「レイズ千八百」「リレーズ四千」と聞けていた。額を言わずに次の発話で言った
    ときは、第 2 の耳の自由に聞いた文にも額が無いので入れない。
    """
    targets = _amountless(events)
    if len(targets) != 1:
        return None
    got = agreed_amount(ear)
    if got is None:
        return None
    amount, text = got
    out = list(events)
    event = out[targets[0]]
    confidence = EAR_CONFIDENCE if event.confidence is None else min(event.confidence, EAR_CONFIDENCE)
    out[targets[0]] = replace(event, amount=amount, confidence=confidence,
                              parse_flags=(*event.parse_flags, EAR_FLAG))
    return out, text


# Whisper の自信がこれ未満の読みは、第 2 の耳がアクションを何も聞いていなければ捨てる（短い音への幻聴。読み上げ集
# 2026-09-30: 句の前の「あ」を「コール」0.06、無音を「コール」0.10、「はい」を「オールイン」0.12。読み上げ集 3 つと
# 店舗の書き起こし 960 発話で、正しい読みの自信は 0.155 以上）
EAR_VETO_CONFIDENCE = 0.15
EAR_HEARD_NOTHING = "（聞こえない）"


def _whisper_confidence(events: list, confidence: Optional[float]) -> Optional[float]:
    if confidence is not None:
        return confidence
    values = [e.confidence for e in events if getattr(e, "confidence", None) is not None]
    return min(values) if values else None


def _doubtful(events: list, confidence: Optional[float]) -> bool:
    conf = _whisper_confidence(events, confidence)
    return bool(events) and conf is not None and conf < EAR_VETO_CONFIDENCE


def wants_ear(events: Iterable, text: str, question: bool = False, confidence: Optional[float] = None) -> bool:
    """ライブで第 2 の耳に聞き直させるか。Whisper がアクションとして読めなかった発話（確認の問い・ポットや
    ブラインドの読み上げは除く）と、読んだベット・レイズに額が無い・`EAR_MIN_AMOUNT` 未満の発話と、自信の
    とても低い読み（`EAR_VETO_CONFIDENCE` 未満）。`confidence` を省くとアクションに付いた Whisper の自信を使う。"""
    from audio.recognizer import is_announcement

    events = list(events)
    if not events:
        return not question and not is_announcement(text)
    return bool(_amountless(events)) or _doubtful(events, confidence)


def wants_amount_scores(events: Iterable) -> bool:
    """額を読んだベット・レイズのある発話（Whisper がはっきり読んでも）。第 2 の耳の額ごとの点数を残し、engine が
    いま使えない額だったときに使える額から選び直せるようにする（第 2 の耳は 1 発話 0.1〜0.2 秒）。"""
    return any(e.action in ("bet", "raise") and e.amount for e in events)


def _with_amount_scores(events: list, ear: Optional[dict]) -> list:
    """発話の中のベット・レイズがちょうど 1 つなら、第 2 の耳の額ごとの点数を付ける（2 つ以上ならどの額の点数か
    分からないので付けない）。音で読んだ額（`phonetic_amount`）は自分の候補の点数を持っているのでそのまま。"""
    table = amount_table(ear)
    wagers = [e for e in events if e.action in ("bet", "raise")]
    if table and len(wagers) == 1 and not wagers[0].amount_scores:
        wagers[0].amount_scores = tuple(sorted(table.items(), key=lambda kv: (-kv[1], kv[0])))
    return events


# 意味のない単発の語（「ゼニューク」）を額と読むとき（オーナー 2026-10-01, `recognizer.garbled_word`）: 第 2 の耳の額の
# 候補のうち、確からしさが自由に聞いた文から `EAR_AMOUNT_LOGP_GAP` 以内（音声が額でも同じくらい説明できる）で、Whisper の
# 語に音が近い（`EAR_AMOUNT_MAX_DISTANCE` 以下）もの。確からしさと音の近さを足して並べる（距離 0〜1 を
# `EAR_AMOUNT_PHONETIC_WEIGHT` 倍して確からしさ（log）から引く）。店舗 b0a27270:「ゼニューク」の第 2 の耳は何も聞かず
# （-3.5）、候補は 三百 -4.0・千二百 -4.2・二百 -5.1 → 音の近さで 千二百（正解）。「はい」「ナイス」「これぞ」は自由に
# 聞いた文がその語で、額の候補は 10 以上低いので読まない。
EAR_AMOUNT_LOGP_GAP = 3.0
EAR_AMOUNT_MAX_DISTANCE = 0.6
EAR_AMOUNT_PHONETIC_WEIGHT = 10.0


def garbled_amount_events(text: str, ear: Optional[dict], *,
                          utterance_start_ts: Optional[float] = None) -> tuple[list, Optional[str]]:
    """意味のない単発の語を、第 2 の耳の額の候補と音の近さで額と読む。(アクション, 使った候補の文) か ([], None)。"""
    from audio.phonetic import amount_distance
    from audio.recognizer import garbled_word, parse_actions, phonetic_amount_event

    word = garbled_word(text)
    free = (ear or {}).get("logp")
    if word is None or free is None:
        return [], None
    scored: dict[int, tuple[float, str]] = {}
    for cand in ear.get("candidates") or []:
        logp, cand_text = cand.get("logp"), (cand.get("text") or "").strip()
        if logp is None or logp < free - EAR_AMOUNT_LOGP_GAP:
            continue
        read = parse_actions(cand_text)
        if len(read) != 1 or "amount_only" not in read[0].parse_flags or not read[0].amount:
            continue
        d = amount_distance(word, read[0].amount)
        if d > EAR_AMOUNT_MAX_DISTANCE:
            continue
        score = logp - EAR_AMOUNT_PHONETIC_WEIGHT * d
        if score > scored.get(read[0].amount, (float("-inf"), ""))[0]:
            scored[read[0].amount] = (score, cand_text)
    if not scored:
        return [], None
    order = sorted(scored.items(), key=lambda item: (-item[1][0], item[0]))
    options = tuple(amount for amount, _ in order)
    event = phonetic_amount_event(text, options, EAR_CONFIDENCE, utterance_start_ts, flags=(EAR_FLAG,),
                                  scores=tuple((amount, round(score, 3)) for amount, (score, _) in order))
    return [event], order[0][1][1]


def apply_ear(events: Iterable, text: str, ear: Optional[dict], *, question: bool = False,
              utterance_start_ts: Optional[float] = None,
              confidence: Optional[float] = None) -> tuple[list, Optional[str]]:
    """Whisper の読み（`events`, 文 `text`）に第 2 の耳の結果を重ねる。(アクション, 使った候補の文 or None)。

    ベット・レイズがちょうど 1 つの発話には、第 2 の耳の額ごとの点数を付ける（`_with_amount_scores`。engine が
    使えない額だったとき・音で読んだ額の候補から選ぶとき、その場面で使える額に絞って使う）。
    """
    used, used_text = _apply_ear(events, text, ear, question=question, utterance_start_ts=utterance_start_ts,
                                 confidence=confidence)
    return _with_amount_scores(used, ear), used_text


def _apply_ear(events: Iterable, text: str, ear: Optional[dict], *, question: bool = False,
               utterance_start_ts: Optional[float] = None,
               confidence: Optional[float] = None) -> tuple[list, Optional[str]]:
    """`apply_ear` の本体（額ごとの点数を付ける前）。

    ライブ（`AudioThread`）・書き起こしの読み直し（`tools/eval_store.py`）・読み上げ集・推定器が同じ規則を使う。
    読めなかった発話は `rescue_events`（ポット・ブラインドの読み上げ =「ポット1万2000です。」は、第 2 の耳が額だけを
    聞いてもアクションにしない）、それでも読めない意味のない単発の語は `garbled_amount_events`（額の候補と音の近さ）、
    額の無いベット・レイズは `fill_amounts`。自信のとても低い読みは、第 2 の耳がアクションを何も聞いていなければ捨てる
    （使った文 = 第 2 の耳が聞いた文、空なら `EAR_HEARD_NOTHING`）。第 2 の耳が無ければ（`ear` = None）、意味のない
    単発の語を音の近さだけで額と読む（`recognizer.parse_garbled_amount`、使った文は None）。
    """
    from audio.recognizer import parse_garbled_amount

    events = list(events)
    if not ear:
        if not events and not question and text:
            garbled = parse_garbled_amount(text, confidence=confidence, utterance_start_ts=utterance_start_ts)
            if garbled is not None:
                return [garbled], None
        return events, None
    if not wants_ear(events, text, question, confidence):
        return events, None
    if not events:
        rescued = rescue_events(ear, utterance_start_ts=utterance_start_ts)
        if rescued:
            return rescued, agreed_candidate(ear)
        return garbled_amount_events(text, ear, utterance_start_ts=utterance_start_ts)
    if _doubtful(events, confidence):
        free = (ear.get("text") or "").strip()
        cands = ear.get("candidates") or []
        best = (cands[0].get("text") or "").strip() if cands else ""
        whisper_keys = [(e.action, e.amount, e.seat, e.position, e.hand_name) for e in events]
        # 自由に聞いた文にアクションが無く、候補でも Whisper と同じ読みにならない（「こる」= 候補「コール」は残す）
        if not (_action_keys(free) if free else []) and not (best and _action_keys(best) == whisper_keys):
            return [], free or EAR_HEARD_NOTHING
    filled = fill_amounts(events, ear)
    return (events, None) if filled is None else filled


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
