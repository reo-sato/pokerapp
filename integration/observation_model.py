"""integration/observation_model.py

ベイズ推定アクション推定レイヤ (v6.0+ M2) の観測モデル。

数学的中核:
  log P(E_t | a_i) + log P(a_i | state)
    = log φ_lex(audio, a_i) + log φ_amount(a_i.amount, a_i.action, state)
      + log P(a_i.action | position)

- φ_lex:    N-best の単語と Dirichlet 辞書 π(word|action) の重み付き和
- φ_amount: 金額の整合性 (BET/RAISE は log-Normal、CALL/CHECK/FOLD は degenerate)
- position prior: 早期/中期/後期バケットでの π(action|position)

ハード制約: legal_actions に違反する候補や、(seat=actor_seat) 不在中の FOLD などは
log_likelihood = NEG_INF (-inf)。

固定 prior は default_priors() で構築 (speech_normalization.json を再利用)。
将来 (v6.0+ B2) で profile.json への persist と Dirichlet posterior update に拡張する。
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional, Union

from core.events import AudioEvent, CameraEvent, RFIDEvent

if TYPE_CHECKING:
    from integration.action_inference import BettingState

logger = logging.getLogger(__name__)

NEG_INF: float = float("-inf")

EvidenceKind = Literal["audio", "rfid", "camera"]


@dataclass
class EvidenceInterval:
    """1 観測の正規化表現。t_start, t_end は絶対時刻 (unix time)。"""

    kind: EvidenceKind
    t_start: float
    t_end: float
    payload: Union[AudioEvent, RFIDEvent, CameraEvent]


@dataclass
class ActionHypothesis:
    """1 観測に対するアクション候補と評価結果。"""

    action: Optional[str]                 # "bet"/"call"/"raise"/"check"/"fold"/"allin"/None
    amount: int
    log_likelihood: float                 # log P(E|a) + log P(a|state) (未正規化、相対比較用)
    reason: str
    needs_review: bool = False
    # 既存 InferredAction との互換用フィールド (M2 アダプタが利用)
    confidence: float = 0.5
    raw_text: str = ""
    normalized_text: str = ""


@dataclass
class PriorParams:
    """固定 prior のパラメータ集約。M2 では学習なし。"""

    # 時刻整合 φ_time の遅延分布
    mu_audio: float = 0.4         # 秒
    sigma_audio: float = 0.6
    mu_rfid: float = 0.2
    sigma_rfid: float = 0.4

    # 語彙 π(word|action) Dirichlet
    lexicon: dict[str, dict[str, float]] = field(default_factory=dict)  # action -> word -> pseudocount
    lexicon_total: dict[str, float] = field(default_factory=dict)        # action -> 総 pseudocount (incl. unknown)
    alpha_unknown: float = 0.1                                            # 未知語 pseudocount

    # 位置別 prior π(action|position)
    position_prior: dict[str, dict[str, float]] = field(default_factory=dict)  # bucket -> action(UPPER) -> p

    # 金額 prior
    bet_raise_log_sigma: float = 0.7
    bet_raise_mean_factor: float = 2.0    # mean = factor * bb


# ────────────────────────────────────────────────────────────────────────────
# Helpers: math
# ────────────────────────────────────────────────────────────────────────────

def _safe_log(x: float) -> float:
    if x <= 0:
        return NEG_INF
    return math.log(x)


def _normal_log_pdf(x: float, mu: float, sigma: float) -> float:
    """N(x; mu, sigma^2) の log 密度。sigma は標準偏差。"""
    if sigma <= 0:
        return NEG_INF
    z = (x - mu) / sigma
    return -0.5 * z * z - math.log(sigma) - 0.5 * math.log(2.0 * math.pi)


def _lognormal_log_pdf(x: float, log_mean: float, sigma: float) -> float:
    """log-Normal(x; log_mean=ln(mean), sigma). x > 0。"""
    if x <= 0 or sigma <= 0:
        return NEG_INF
    z = (math.log(x) - log_mean) / sigma
    return -0.5 * z * z - math.log(sigma) - math.log(x) - 0.5 * math.log(2.0 * math.pi)


# ────────────────────────────────────────────────────────────────────────────
# Position bucket
# ────────────────────────────────────────────────────────────────────────────

def _position_bucket(seat: Optional[int], state: "BettingState") -> str:
    """button からの相対距離で early / middle / late を返す。

    button 直右 = SB → early、button 自身 = late。
    """
    if seat is None or state.button_seat is None or not state.active_seats:
        return "middle"
    active = state.active_seats
    if seat not in active or state.button_seat not in active:
        return "middle"
    n = len(active)
    if n <= 2:
        return "late"  # HU は全員 late 扱い
    idx_btn = active.index(state.button_seat)
    idx_seat = active.index(seat)
    rel = (idx_seat - idx_btn) % n
    if rel == 0:
        return "late"
    third = max(1, n // 3)
    if rel <= third:
        return "early"
    if rel <= 2 * third:
        return "middle"
    return "late"


# ────────────────────────────────────────────────────────────────────────────
# Likelihood components
# ────────────────────────────────────────────────────────────────────────────

def _word_prior(word: str, action: str, prior: PriorParams) -> float:
    """π(word | action). 未知語は alpha_unknown / total。"""
    a = action.lower()
    counts = prior.lexicon.get(a)
    total = prior.lexicon_total.get(a)
    if not counts or total is None or total <= 0:
        return prior.alpha_unknown
    return (counts.get(word, 0.0) + prior.alpha_unknown) / total


def _lexicon_log_likelihood(audio: AudioEvent, action: str, prior: PriorParams) -> float:
    """log φ_lex = log Σ_k c_k · π(w_k | action)。

    N-best (alternatives) があれば重み付け和、無ければ raw_text を単一仮説扱い。
    """
    accumulator = 0.0
    if audio.alternatives:
        for alt in audio.alternatives:
            text = (alt.text or "").strip()
            if not text:
                continue
            accumulator += float(alt.confidence) * _word_prior(text, action, prior)
    if accumulator <= 0:
        # フォールバック: raw_text を単一仮説として
        accumulator = _word_prior((audio.raw_text or "").strip(), action, prior)
    return _safe_log(max(accumulator, 1e-12))


def _amount_log_likelihood(
    amount: int,
    action: str,
    state: "BettingState",
    actor_seat: Optional[int],
    prior: PriorParams,
) -> float:
    """log ρ(amount | action)。"""
    a = action.lower()
    if a in ("check", "fold"):
        return 0.0 if amount == 0 else NEG_INF
    if a == "call":
        # current_bet の degenerate (CALL の amount は call 必要額)
        expected = state.current_bet
        return 0.0 if amount == expected else NEG_INF
    if a in ("bet", "raise", "allin"):
        if amount <= 0:
            return NEG_INF
        bb = max(1, state.bb_amount)
        log_mean = math.log(prior.bet_raise_mean_factor * bb)
        return _lognormal_log_pdf(float(amount), log_mean, prior.bet_raise_log_sigma)
    return NEG_INF


def _position_prior_log(action: str, seat: Optional[int], state: "BettingState", prior: PriorParams) -> float:
    bucket = _position_bucket(seat, state)
    p = prior.position_prior.get(bucket, {}).get(action.upper())
    if p is None or p <= 0:
        return math.log(0.01)
    return math.log(p)


def _time_alignment_log(evidence: EvidenceInterval, action_time: float, prior: PriorParams) -> float:
    """log φ_time. action_time との差を Normal で評価。camera は無情報 (0)。"""
    if evidence.kind == "audio":
        return _normal_log_pdf(action_time - evidence.t_end, prior.mu_audio, prior.sigma_audio)
    if evidence.kind == "rfid":
        return _normal_log_pdf(action_time - evidence.t_end, prior.mu_rfid, prior.sigma_rfid)
    return 0.0


# ────────────────────────────────────────────────────────────────────────────
# Hard constraints (legal_actions)
# ────────────────────────────────────────────────────────────────────────────

def is_legal(action: str, amount: int, state: "BettingState", actor_seat: Optional[int]) -> bool:
    """このアクションが state の legal_actions に入るか。M2 では BettingState のフィールドから判定。"""
    a = action.lower()
    if actor_seat is None:
        return True  # actor 未確定 → 制約をかけず上位で needs_review
    if actor_seat in state.folded_seats or actor_seat in state.all_in_seats:
        return False
    contrib = state.get_contrib(actor_seat)
    if a == "check":
        return state.current_bet <= contrib
    if a == "call":
        return state.current_bet > contrib and amount == state.current_bet
    if a == "bet":
        return (not state.is_opened) and amount > 0
    if a == "raise":
        return state.is_opened and amount > state.current_bet
    if a == "fold":
        return True
    if a == "allin":
        return amount > 0
    return False


def default_amount_for(action: str, state: "BettingState") -> int:
    """そのアクションが取られる場合の「典型的な」amount。代替仮説生成用。"""
    a = action.lower()
    if a in ("check", "fold"):
        return 0
    if a == "call":
        return state.current_bet
    if a == "bet":
        return max(state.bb_amount * 2, state.bb_amount, 1)
    if a == "raise":
        # min raise = current_bet + last_raise_increment ≈ current_bet * 2
        return max(state.current_bet * 2, state.current_bet + state.bb_amount, state.bb_amount * 3)
    if a == "allin":
        return state.current_bet  # placeholder
    return 0


# ────────────────────────────────────────────────────────────────────────────
# Combined log-likelihood (audio + position + amount)
# ────────────────────────────────────────────────────────────────────────────

def compute_log_likelihood(
    evidence: EvidenceInterval,
    action: str,
    amount: int,
    state: "BettingState",
    actor_seat: Optional[int],
    prior: PriorParams,
) -> float:
    """log P(observation | action) + log P(action | state).

    legal 違反は NEG_INF。
    """
    if not is_legal(action, amount, state, actor_seat):
        return NEG_INF

    log_pos = _position_prior_log(action, actor_seat, state, prior)
    log_amount = _amount_log_likelihood(amount, action, state, actor_seat, prior)
    if log_amount == NEG_INF:
        return NEG_INF

    if evidence.kind == "audio":
        audio: AudioEvent = evidence.payload  # type: ignore[assignment]
        log_lex = _lexicon_log_likelihood(audio, action, prior)
        return log_pos + log_amount + log_lex

    if evidence.kind == "rfid":
        # RFID 単独評価は粗い。FOLD なら φ_time のみ、それ以外は無情報。
        if action.lower() == "fold":
            log_time = _time_alignment_log(evidence, evidence.t_end, prior)
            return log_pos + log_amount + log_time
        return log_pos + log_amount

    return log_pos + log_amount


# ────────────────────────────────────────────────────────────────────────────
# Default priors loader
# ────────────────────────────────────────────────────────────────────────────

# 標準的なアクション語彙 (alias が出ない場合の保険)
_CANONICAL_VOCAB: dict[str, list[str]] = {
    "bet":   ["ベット", "bet", "ベッド"],
    "call":  ["コール", "call"],
    "raise": ["レイズ", "raise", "ライズ", "上げ", "上げる"],
    "check": ["チェック", "check"],
    "fold":  ["フォールド", "フォール", "fold", "降りる"],
    "allin": ["オールイン", "allin", "all in", "all-in"],
}

_DEFAULT_POSITION_PRIOR: dict[str, dict[str, float]] = {
    "early":  {"FOLD": 0.55, "CALL": 0.30, "RAISE": 0.10, "CHECK": 0.05, "BET": 0.05, "ALLIN": 0.02},
    "middle": {"FOLD": 0.40, "CALL": 0.35, "RAISE": 0.20, "CHECK": 0.05, "BET": 0.05, "ALLIN": 0.02},
    "late":   {"FOLD": 0.30, "CALL": 0.30, "RAISE": 0.30, "CHECK": 0.10, "BET": 0.10, "ALLIN": 0.03},
}


def default_priors(normalization_path: Optional[str] = None) -> PriorParams:
    """speech_normalization.json の action_aliases を Dirichlet 辞書に変換した固定 prior。

    各 alias word に pseudocount 5、未知語は α_0 = 0.1。
    """
    if normalization_path is None:
        normalization_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "speech_normalization.json",
        )

    pseudocount = 5.0
    lexicon: dict[str, dict[str, float]] = {}

    aliases: dict[str, Any] = {}
    try:
        with open(normalization_path, encoding="utf-8") as f:
            data = json.load(f) or {}
        aliases = data.get("action_aliases", {}) or {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not load %s; using canonical vocabulary only", normalization_path)

    for word, action in aliases.items():
        a = str(action).lower()
        lexicon.setdefault(a, {})[str(word)] = pseudocount

    # 正規化済みアクション語自体も常に登録
    for action, words in _CANONICAL_VOCAB.items():
        bucket = lexicon.setdefault(action, {})
        for w in words:
            bucket.setdefault(w, pseudocount)

    alpha_unknown = 0.1
    lexicon_total = {
        action: sum(counts.values()) + alpha_unknown
        for action, counts in lexicon.items()
    }

    return PriorParams(
        mu_audio=0.4,
        sigma_audio=0.6,
        mu_rfid=0.2,
        sigma_rfid=0.4,
        lexicon=lexicon,
        lexicon_total=lexicon_total,
        alpha_unknown=alpha_unknown,
        position_prior={k: dict(v) for k, v in _DEFAULT_POSITION_PRIOR.items()},
        bet_raise_log_sigma=0.7,
        bet_raise_mean_factor=2.0,
    )


def evidence_from_audio(event: AudioEvent) -> EvidenceInterval:
    """AudioEvent から EvidenceInterval を構築 (時刻情報がある時のみ意味を持つ)。"""
    t_end = event.t_end if event.t_end is not None else event.timestamp
    # t_start は最初の単語の start、なければ timestamp
    t_start = event.timestamp
    if event.word_timestamps:
        t_start = min(w.start for w in event.word_timestamps)
    return EvidenceInterval(kind="audio", t_start=t_start, t_end=t_end, payload=event)


def evidence_from_rfid(event: RFIDEvent) -> EvidenceInterval:
    t_end = event.t_end if event.t_end is not None else event.timestamp
    return EvidenceInterval(kind="rfid", t_start=event.timestamp, t_end=t_end, payload=event)


def evidence_from_camera(event: CameraEvent) -> EvidenceInterval:
    return EvidenceInterval(kind="camera", t_start=event.timestamp, t_end=event.timestamp, payload=event)
