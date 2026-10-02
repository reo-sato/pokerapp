"""integration/estimator.py — 推定器 v1: 生の観測の再生（world_replay）を直しながら、ハンドごとに筋の通る記録を探す

ADR-0056（事後・確率的推定）追記 1・2 と、Fable 5.1 の統計の監査（`docs/worklog/2026-09-30-estimator-audit.md`）の
最小モデル。いまの設計・監査への対応は `docs/estimator.md`（9/30 の設計と実測の記録は
`docs/worklog/2026-09-30-estimator-v1-design.md`）。

- 仮説 = 入力への「直し」の組: 発話の別の読み（第 2 の耳の候補など）/ 余計な語を 1 つ捨てる / 聞こえなかった
  アクション（フォールド・チェック・コール）を入れる / ボタンを変える / 札の離脱を持ち上げとみる。ハンドは
  ライブと同じ engine が組み直す（合法でない列は作られない）。
- 採点 = log 事後確率（定数を除く）: ボタンの事前 + アクション列の事前（合法手の種類の数で割るランダムウォーク）
  + 語の観測（言われた / 言われなかった / 余計な語 / 種類・額の取り違え）+ 札の離脱（時刻の密度: フォールドの声と
  ほぼ同時・言われないフォールドの幅・ショーダウンと片付け・持ち上げ）+ 残り人数の宣言 + ボードと勝者の整合。
  値は `PARAMS`（出所は design の worklog。店舗の真のアクションは開発にだけ使い、評価は新しいセッションで）。
- 探索 = ハンドごとのビーム（直しを 1 つずつ足す）。同じ記録になる直しは 1 つにまとめ、何も変えない直しは捨てる。
  ハンドは記録の持ち点・ボタンから始めるので互いに独立。
- 事後確率 = 候補の得点の softmax（温度つき）に「候補の外」の質量 λ を混ぜたもの。要確認 = 1 番と 2 番の差が
  小さい、直しを使った、または筋の通らない観測が残った（構造的な理由）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from core.events import AudioEvent, RFIDEvent
from core.game_state import PlayerState
from integration.replay import Event
from integration.world_replay import PresenceTimeline, SeatObservation, delivered_at, replay_world, world_events

logger = logging.getLogger(__name__)

ESTIMATOR_VERSION = "1.0"

# 確率の値（仮）。log にして足す。出所は design の worklog（店舗の真のアクション 18 ハンド = 開発データ）。
PARAMS: dict[str, float] = {
    # 語が言われない・聞こえない確率
    "p_miss_check": 0.04,        # 直前と違うチェック・コール（店舗 2/80 ほど）
    "p_miss_call": 0.04,
    "p_miss_repeat": 0.15,       # 直前の人と同じチェック・コール（店舗 7/50。2 回目を言わない旧い運用の名残）
    "p_miss_fold": 0.04,         # ハンドを終わらせないフォールド（店舗 0/14）
    "p_miss_last_fold": 0.45,    # ハンドを終わらせるフォールド（店舗 5/11。札の離脱と勝者で分かるので言わない）
    "p_miss_wager": 0.01,        # ベット・レイズ・オールイン（額は言う）
    "p_miss_muck": 0.05,         # ショーダウンで見せずに降りた（ディーラーは「フォールド」と言う = オーナー 2026-09-30）
    "p_phantom_short": 0.02,     # 短い発話（アクションの言葉だけ）の余計な語（読み上げ集の挿入 11/519 = 2.1%）
    "p_phantom": 0.10,           # 長い発話（雑談が混ざる）の余計な語
    "phantom_short_chars": 15,   # これ以下の文字数の発話を短いとみる
    "p_restate": 0.3,            # 言い直し: 同じアクションを言い方を変えて続けた（「コールします。コール。」「コール
                                 # です。コール800点。」= 店舗 2/2 が 1 つのアクション）・次の発話で額や「オールイン」を
                                 # 繰り返した・ショーダウンのあとのマックの声
    "p_restate_same": 0.05,      # 同じ語をそのまま繰り返した（「チェック、チェック。」店舗 1/2 が 1 つ、台本 0/15 = 合わせて
                                 # 1/17。掃引で店舗・台本の正誤が動かない 0.03〜0.1 の中）
    "p_sub": 0.02,               # 語の種類の取り違え（チェック ↔ コール など）
    "p_amount": 0.05,            # 額の聞き違い（寄せた・丸めた）
    "amount_prior_weight": 1.0,  # 賭けの額のポットに対する大きさの重み（`core.bet_sizing.size_prior` に掛ける。オーナー
                                 # 2026-10-01: 多くはポット以下、多くても 3 倍。真のアクションの賭け 92 のうちポット以下 75%）
    # 札の離脱（卓状態の履歴）。時刻の密度で比べる（どの仮説でも離脱 1 つに密度 1 つ）
    "p_nodepart": 0.03,          # フォールドしたのに札が離れない（店舗 0/25。その席の札がハンドで一度も読めなければ数えない）
    "fold_lag_mu": 0.1,          # 「フォールド」の話し始めから札が離れるまで（店舗 20 回の中央値 0.1 秒、[−1.35, +1.02]）
    "fold_lag_b": 0.4,           # その広がり（ラプラス分布の尺度, 秒。最尤は 0.3、狭いと崩れるので広め = 監査 2 回目）
    "fold_early_mix": 0.1,       # フォールドの札の離脱の裾（語が発話の後ろの方・手番より前に投げた札）の重み
    "fold_tail_sec": 20.0,       # その裾の幅（± 秒、一様）
    "flicker_sec": 3.0,          # これより早く戻った離脱（ちらつき・のぞき見）は数えない（engine がフォールドにしない 3 秒と同じ）
    "departure_after_end_sec": 60.0,   # 離脱を数える窓: 直しの無い再生のハンドの終わりからこの秒数まで
    "silent_fold_wait": 30.0,    # 言われないフォールドは前後の言われたアクションの間（最後なら前からこの秒数まで）
    "silent_fold_mean": 2.5,     # 言われないフォールドの札が離れるまでの考える時間（前の言われたアクションから, 指数分布
                                 # の平均。店舗の 5 回: 2.1〜4.7 秒・中央値 2.4。掃引で店舗の正誤が動かない 1〜3 の中）
    "other_departure_sec": 20.0,  # ショーダウン・片付けで札が離れる時刻の幅
    "lift_sec": 200.0,           # ベッティングの途中に残っている席の札が離れる（持ち上げ）の平均の間隔
    # そのほか
    "p_button": 0.05,            # ボタンが記録（ライブが回したボタン）と違う（隣の席 = 動かし忘れ・動かしすぎを 4 倍厚く）
    "p_unclosed": 0.01,          # ベッティングのラウンドが閉じないまま（手番の人が残ったまま）ハンドが終わった
    "p_players": 0.05,           # 残り人数の宣言（ヘッズアップ・N プレイヤーズ）が合わない
    "p_showdown_word": 0.03,     # ショーダウンの声（「ショーダウン」・役の名前）があるのに全員降りて終わった（真のアクションの
                                 # 92 ハンド: 声のあった 23 ハンドはすべてショーダウン、全員降りて終わった 45 ハンドで声 0）
    "p_unread_board": 0.03,      # 次のストリートのアクションなのにボードの札が読めていない（店舗 1 回 / 約 40）
    "p_street_time": 0.02,       # アクションの時刻がそのストリートの札の配布と合わない（前のストリートの札より
                                 # 前・次のストリートの札よりあと。札の読み取りの遅れ `street_slack_sec` は許す）
    "street_slack_sec": 2.0,
    "p_board_after_end": 0.02,   # 全員降りて終わったのに、そのあとのストリートの札が配られた
    "p_undetermined": 0.20,      # 勝者が決まらない・推し量った
    # 探し方・事後確率
    "beam": 4, "depth": 3, "expand": 12,
    "canned_near_sec": 15.0,     # 決まり文句の発話をアクションと読む直しは、読めた賭けの語からこの秒数以内の発話だけ（全
                                 # セッションで、穴を埋めた決まり文句 20 のうち 17 が 13 秒以内。遠い 3 つは 58〜286 秒 =
                                 # 偶然。ハンドの途中の長い雑談まで広げると 1 ハンドの推定が 3 分になった = 店舗 9d1d8536#4）
    "button_explore": 6.0,       # ボタンを変えた再生が既定よりこれ以上悪ければ、そのボタンでは探さない
    "temperature": 1.0,          # 事後確率の温度（較正する）
    "outside": 0.10,             # 正解が候補の外にある質量 λ（開発データから）
    "review_margin": 2.0,        # 1 番と 2 番の差（log）がこれより小さければ要確認
}

_WAGER = frozenset({"bet", "raise", "allin"})
_BETTING = frozenset({"fold", "check", "call", "bet", "raise", "allin"})
_WORD_SOURCES = frozenset({"engine_prior", "spoken_seat", "spoken_position"})
_RFID_FOLDS = frozenset({"rfid_departure", "rfid_muck"})
# engine が額を決めた印（ライブの記録では要確認）。推定の要確認の理由にもする（作業計画の監査 2026-10-01 §4: 推定を
# 記録の本体にすると、記録の要確認は推定の `review` で置き換わるため、これが無いと印が消える）。採点には足さない
_AMOUNT_READ_REASONS = frozenset({"phonetic_amount", "amount_restated", "legal_amount", "ambiguous_amount",
                                  "garbled_digits"})
_INSERTABLE = ("fold", "check", "call")
_INSERT_LEAD_SEC = 0.3          # 聞こえなかったアクションは次の語の少し前に置く
_STREETS = ("preflop", "flop", "turn", "river")


# 発話の読みの確からしさ（`tools/estimate.py` の `utterance_options`, v0 と共通）のうち v1 で置き直す値。
# 決まり文句（定型の幻聴「ご覧いただきありがとうございます。」・プロンプトの繰り返し）になり、ライブの規則でも
# 読めなかった発話は「雑談だった / アクションを言った」の 2 つの状態（`tools/estimate.py` の `_canned_options`）。
# アクションだった確率 0.2 は全セッションの数え直し（2026-10-02: 店舗の fixture・台本・9/30〜10/01 のログで、真の
# アクションの並びと読めた語を対応づけて残った穴を、読めた賭けの語から 15 秒以内の決まり文句の 17/51 = 0.33 が埋めた
# = 上限。雑談の発話でも 0.29 埋めるので偶然を全部引くと 0.06、監査役の目視 ≈ 0.11 → その間。前の値 = 捨てる −0.1 は
# 開発データの 1/23 から）。
# 第 2 の耳が自由に聞いた文が空でも、その候補を選択肢にする（`ear_empty_text`, 2026-10-02。空の文は耳の貪欲な探索が
# 何も書かなかっただけで、候補の確からしさはその空の文と比べられる。v0 が空を除いていた理由は記録に無い）。
READING_OVERRIDES: dict[str, float] = {"canned_action": 0.2, "canned_ear_temp": 2.0, "ear_empty_text": 1.0}


def reading_params() -> dict:
    from tools.estimate import PARAMS as V0

    return dict(V0, **READING_OVERRIDES)


def params_hash(params: dict[str, float] = PARAMS) -> str:
    """値の指紋。発話の読みの確からしさ（`reading_params`）も含める（監査 2 回目）。"""
    both = {"estimator": params, "reading": reading_params()}
    return hashlib.sha1(json.dumps(both, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _log(p: float) -> float:
    return math.log(min(max(p, 1e-9), 1.0))


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


# ───────────────────────── 直し ─────────────────────────


@dataclass(frozen=True)
class Edit:
    kind: str            # "read" | "drop" | "insert" | "button" | "lift"
    at: float            # 発話の始まり（read / drop）・入れる時刻（insert）・ハンドの始まり（button）・離れた時刻（lift）
    value: Any = None    # 読む文 / 捨てる語の番号 / アクション / 席 / (席, 戻った時刻)
    logp: float = 0.0    # 直しそのものの確からしさ（read の読みの確からしさ。ほかは採点で数える）
    label: str = ""

    def conflicts(self, other: "Edit") -> bool:
        """同じ発話への読みと語の捨て方、同じ時刻への 2 つ目の挿入、ボタンの 2 つ目は組み合わせない。"""
        if self.kind == "button" or other.kind == "button":
            return self.kind == other.kind
        if self.kind in ("read", "drop") and other.kind in ("read", "drop"):
            return self.at == other.at and not (self.kind == other.kind == "drop" and self.value != other.value)
        return self.kind == other.kind and self.at == other.at


@dataclass
class HandWindow:
    hand_id: int
    start: float
    end: float
    base: dict                       # 直しの無い再生のハンド（持ち点・ボタン・ブラインドの出所）

    @property
    def button(self) -> Optional[int]:
        return self.base.get("button_seat")


@dataclass
class Candidate:
    edits: tuple[Edit, ...]
    hand: dict
    score: float
    terms: list[tuple[str, float]]
    flags: list[str] = field(default_factory=list)     # 筋の通らない観測（要確認の理由）

    def key(self) -> tuple:
        return record_key(self.hand)


def record_key(hand: Optional[dict]) -> tuple:
    """記録として同じかどうか（ショーダウンのマックの行は見ない = 全部正しいハンドの物差しと同じ）。"""
    if not hand:
        return ()
    rows = tuple((a.get("street"), a.get("seat"), a.get("action"), a.get("amount") or 0)
                 for a in hand.get("actions") or [] if a.get("street") != "showdown")
    return rows, hand.get("winner_seat"), tuple(hand.get("board") or []), hand.get("button_seat")


@dataclass
class HandResult:
    window: HandWindow
    candidates: list[Candidate]      # 良い順・同じ記録は 1 つ（得点は logsumexp で足した事後確率に使う）
    posteriors: list[float]
    replays: int
    reasons: list[str] = field(default_factory=list)
    # 直しの無い再生（= 読み直し）。候補の中のその記録は、同じ記録のより良い説明（直しあり）に置き換わることがある
    base: Optional[Candidate] = None

    @property
    def best(self) -> Candidate:
        return self.candidates[0]

    @property
    def margin(self) -> Optional[float]:
        if len(self.candidates) < 2:
            return None
        return round(self.candidates[0].score - self.candidates[1].score, 3)


# ───────────────────────── 語とアクションの行の対応 ─────────────────────────


@dataclass
class _Row:
    street: str
    seat: int
    action: str
    t: float                          # 行の時刻（epoch）
    word: Optional[AudioEvent]        # その行を言った語（無ければ言われていない）
    collective: bool = False          # チェックアラウンド（1 語で複数の行）
    source: str = ""
    reasons: frozenset = frozenset()
    raw: Optional[dict] = None


def align_words(actions: list[dict], tokens: list[AudioEvent], inserted: dict[str, str]
                ) -> tuple[list[_Row], list[AudioEvent]]:
    """記録の行と語（書き起こしから読んだアクション）を対応づける。返り値: 行・使われなかった語。

    - 語から作った行（engine の手番の推定・席・ポジション）は時刻（ミリ秒）が語と同じ。
    - 声のフォールド（`spoken_fold`）は行の時刻が話し始め。
    - 札の離脱で作ったフォールドは、近くの「フォールド」の語がそれを言ったもの（engine は裏付けにして行を作らない）。
    - チェックアラウンドは 1 語で残りの全員のチェック。
    """
    betting = [t for t in tokens if t.action in _BETTING]
    by_ts: dict[str, AudioEvent] = {}
    for t in betting:
        by_ts.setdefault(_iso(t.timestamp), t)
    used: set[int] = set()
    rows: list[_Row] = []
    for a in actions:
        ts = a.get("timestamp") or ""
        t = _epoch(ts) or 0.0
        src = str(a.get("actor_source") or "")
        reasons = frozenset(str(a.get("reason") or "").split("+")) - {""}
        word: Optional[AudioEvent] = None
        collective = "check_around" in reasons
        if ts in inserted:
            pass
        elif collective or src in _WORD_SOURCES:
            word = by_ts.get(ts)
        elif src == "spoken_fold":
            word = min((x for x in betting if x.action == "fold" and id(x) not in used
                        and abs((x.utterance_start_ts or x.timestamp) - t) <= 1.0),
                       key=lambda x: abs((x.utterance_start_ts or x.timestamp) - t), default=None)
        elif src in _RFID_FOLDS:
            word = min((x for x in betting if x.action == "fold" and id(x) not in used
                        and -4.0 <= (x.utterance_start_ts or x.timestamp) - t <= 6.0),
                       key=lambda x: abs((x.utterance_start_ts or x.timestamp) - t), default=None)
        if word is not None and (id(word) not in used or collective):
            used.add(id(word))
        else:
            word = None if not collective else word
        rows.append(_Row(street=str(a.get("street") or ""), seat=int(a.get("seat") or 0),
                         action=str(a.get("action") or ""), t=t, word=word, collective=collective,
                         source=src, reasons=reasons, raw=a))
    return rows, [t for t in betting if id(t) not in used]


def _word_start(row: _Row) -> float:
    if row.word is not None and row.word.utterance_start_ts is not None:
        return float(row.word.utterance_start_ts)
    return row.t


# ───────────────────────── セッションの入力 ─────────────────────────


class SessionEstimator:
    """1 セッションの推定。入力は記録（events）・書き起こし・席の札の在否の履歴・卓の設定。"""

    def __init__(self, events: list[Event], transcripts: list[dict], presence: Optional[PresenceTimeline],
                 setup: dict, flags: dict, session_id: str, params: dict = PARAMS) -> None:
        self.events = sorted(events, key=lambda e: e.timestamp)
        self.transcripts = [r for r in transcripts if r.get("utterance_start_ts") is not None]
        self.presence = presence
        self.setup = setup
        self.flags = flags
        self.session_id = session_id
        self.params = params
        self.players = [PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"]) for p in setup["players"]]
        self.replays = 0
        self._parse_cache: dict = {}
        self._text_at = {r["utterance_start_ts"]: (r.get("text") or "").strip() for r in self.transcripts}
        self._row_at = {r["utterance_start_ts"]: r for r in self.transcripts}
        self._options_cache: dict[float, list] = {}

    def _options(self, start: float) -> list:
        """発話の読みの選択肢（[0] が既定, v0 と同じ `utterance_options`）。"""
        if start not in self._options_cache:
            from tools.estimate import utterance_options

            self._options_cache[start] = utterance_options(self._row_at[start], reading_params())
        return self._options_cache[start]

    @property
    def _default_logp(self) -> "_DefaultLogp":
        return _DefaultLogp(self)

    def _window_starts(self, w: HandWindow) -> list[float]:
        return [s for s in self._row_at if w.start - 0.5 <= s < w.end]

    def _button_logp(self, w: HandWindow, button: Optional[int]) -> float:
        """ボタンの事前: 記録のボタン 1 − p_button、ほかの席に p_button を分ける（隣の席 = 動かし忘れ・動かしすぎを
        4 倍厚く）。席の数で割るので卓の大きさによらず足して 1（監査 2 回目）。"""
        p = self.params["p_button"]
        if button == w.button:
            return _log(1 - p)
        seats = sorted({pl["seat"] for pl in w.base.get("players") or []})
        alts = [s for s in seats if s != w.button]
        if w.button not in seats or button not in alts:
            return _log(p / max(1, len(alts)))
        i = seats.index(w.button)
        near = {seats[i - 1], seats[(i + 1) % len(seats)]} - {w.button}
        weight = {s: 4.0 if s in near else 1.0 for s in alts}
        return _log(p * weight[button] / sum(weight.values()))

    def _p_phantom(self, start: Optional[float], words: tuple[tuple[str, str], ...] = (), k: int = -1) -> float:
        """その発話の k 番目の語が余計な語である確率。同じ発話で同じアクションが続けば言い直し（言い方が同じなら
        2 人分のことも多い）、短い発話（アクションの言葉だけ）は低い。`words`: その発話で読んだ (アクション, 文字)。"""
        p = self.params
        if 0 <= k < len(words):
            for j in (k - 1, k + 1):
                if 0 <= j < len(words) and words[j][0] == words[k][0]:
                    return p["p_restate_same" if words[j][1] == words[k][1] else "p_restate"]
        text = self._text_at.get(start, "")
        short = len(text) <= p["phantom_short_chars"]
        return p["p_phantom_short" if short else "p_phantom"]

    # ――― ハンドの窓 ―――

    def baseline(self) -> list[dict]:
        """直しの無い再生（セッション全体）。ハンドの窓と、持ち点・ボタンの出所。"""
        stacks = {int(h): {int(s): int(v) for s, v in seats.items()}
                  for h, seats in (self.setup.get("hand_stacks") or {}).items()}
        return replay_world(world_events(self.events, self.transcripts, cache=self._parse_cache), self.presence,
                            players=self.players, sb=self.setup["sb"], bb=self.setup["bb"],
                            session_id=self.session_id, button_seat=self.setup.get("button_prior"),
                            hand_stacks=stacks or None, **self.flags)

    def windows(self, hands: Optional[list[dict]] = None) -> list[HandWindow]:
        hands = [h for h in (hands if hands is not None else self.baseline()) if _epoch(h.get("started_at"))]
        hands.sort(key=lambda h: _epoch(h["started_at"]))
        out = []
        last = max((e.timestamp for e in self.events), default=0.0) + 60.0
        for i, h in enumerate(hands):
            start = _epoch(h["started_at"])
            end = _epoch(hands[i + 1]["started_at"]) if i + 1 < len(hands) else last
            out.append(HandWindow(hand_id=h["hand_id"], start=start, end=end, base=h))
        return out

    def _window_inputs(self, w: HandWindow) -> tuple[list[Event], list[dict]]:
        rows = [r for r in self.transcripts if w.start - 0.5 <= r["utterance_start_ts"] < w.end]
        starts = {r["utterance_start_ts"] for r in self.transcripts}
        events = []
        for e in self.events:
            if isinstance(e, AudioEvent) and e.utterance_start_ts in starts:
                continue                      # 書き起こしから読み直す（窓の中の行だけ）
            if isinstance(e, RFIDEvent) and e.kind == "deal":
                t = e.observed_at if e.observed_at is not None else e.timestamp
                if w.start - 1.0 <= t < w.end - 0.5:
                    events.append(e)
                continue
            if w.start - 2.0 <= e.timestamp < w.end:
                events.append(e)
        return events, rows

    # ――― 再生 ―――

    def replay(self, w: HandWindow, edits: tuple[Edit, ...] = ()) -> tuple[Optional[dict], list, dict]:
        """直しを入れてこのハンドだけ再生する。返り値: ハンド・通知されたアクション・語の数え方の材料。"""
        events, rows = self._window_inputs(w)
        texts = {e.at: e.value for e in edits if e.kind == "read"}
        drops: dict[float, set[int]] = {}
        for e in edits:
            if e.kind == "drop":
                drops.setdefault(e.at, set()).add(int(e.value))
        inserted: dict[str, str] = {}
        extra: list[Event] = []
        for k, e in enumerate(sorted((x for x in edits if x.kind == "insert"), key=lambda x: x.at)):
            at = e.at + 0.0001 * k
            extra.append(AudioEvent(action=e.value, amount=0, timestamp=at, raw_text="", confidence=1.0,
                                    utterance_start_ts=at))
            inserted[_iso(at)] = e.value
        presence = self.presence
        lifts = [e for e in edits if e.kind == "lift"]
        if presence is not None and lifts:
            presence = _masked(presence, [(e.value[0], e.at, e.value[1]) for e in lifts])
        button = next((e.value for e in edits if e.kind == "button"), w.button)
        stacks = {p["seat"]: p["stack_start"] for p in w.base.get("players") or [] if "stack_start" in p}
        blinds = w.base.get("blinds") or {}
        records: list = []
        world = world_events(events, rows, texts, extra=extra, drops=drops, cache=self._parse_cache)
        hands = replay_world(
            world, presence, players=self.players, sb=int(blinds.get("sb") or self.setup["sb"]),
            bb=int(blinds.get("bb") or self.setup["bb"]), session_id=self.session_id,
            hand_stacks={1: stacks} if stacks else None,
            hand_buttons={1: button} if button is not None else None,
            on_action=records.append, **self.flags,
        )
        self.replays += 1
        hand = next((h for h in hands if _epoch(h.get("started_at")) is not None
                     and abs(_epoch(h["started_at"]) - w.start) < 5.0), hands[0] if hands else None)
        if hand is not None:
            hand = dict(hand, hand_id=w.hand_id)
        tokens = [e for e in world if isinstance(e, AudioEvent) and e.utterance_start_ts is not None
                  and w.start - 0.5 <= e.utterance_start_ts < w.end and not _is_inserted(e, inserted)]
        return hand, records, {"tokens": tokens, "inserted": inserted, "button": button}

    # ――― 採点 ―――

    def score(self, w: HandWindow, hand: Optional[dict], records: list, info: dict,
              edits: tuple[Edit, ...]) -> tuple[float, list[tuple[str, float]], list[str]]:
        """log 事後確率（定数を除く）・その内訳・筋の通らない観測（要確認の理由）。"""
        p = self.params
        terms: list[tuple[str, float]] = []
        flags: list[str] = []
        if hand is None:
            return -1e6, [("ハンドが作れない", -1e6)], ["ハンドが作れない"]
        # 発話の読み: 各発話でちょうど 1 つの読みを選び、その確からしさを足す（直しの読み or 既定の読み。既定の読みも
        # 第 2 の耳で聞き直した・音の近さで読んだ・定型の幻聴を捨てた、などで確からしさが違う = 監査 2 回目）
        read_at = {e.at: e for e in edits if e.kind == "read"}
        for start in self._window_starts(w):
            e = read_at.get(start)
            logp = e.logp if e is not None else self._default_logp.get(start, 0.0)
            if logp:
                terms.append((f"読み: {e.label}" if e is not None else f"既定の読み「{self._text_at.get(start, '')}」",
                              logp))
        terms.append(("ボタン", self._button_logp(w, info["button"])))
        if hand.get("betting_open_at_end"):
            # 実卓のハンドは全員が降りるかショーダウンでしか終わらない（語を捨てて短くしたハンドが、最後の人が
            # 行動しないまま勝者の操作で終わるのを止める = 監査 2 回目の要約 2）
            terms.append(("ラウンドが閉じないまま終わった", _log(p["p_unclosed"])))
            flags.append("ベッティングが閉じないまま終わった")
        rows, unused = align_words(hand.get("actions") or [], info["tokens"], info["inserted"])
        betting = [r for r in rows if r.street != "showdown"]
        terms += self._action_terms(hand, betting, flags)
        flags += self._amount_readings(betting, edits)
        # ショーダウンで見せずに降りた（マック）: ディーラーは「フォールド」と言う
        for r in rows:
            if r.street == "showdown" and r.action == "fold":
                said = r.word is not None
                terms.append((f"{'言われた' if said else '言われない'}マック（席{r.seat}）",
                              _log(1 - p["p_miss_muck"]) if said else _log(p["p_miss_muck"])))
                if not said:
                    flags.append(f"言われないマック（席{r.seat}）")
        # 語: 使われなかった語 = 余計な語（雑談・言い直し・幻聴）。直しで捨てた語も
        by_utt: dict[Optional[float], list[AudioEvent]] = {}
        for t in sorted(info["tokens"], key=lambda x: x.timestamp):
            by_utt.setdefault(t.utterance_start_ts, []).append(t)
        said_end = max((_word_start(r) for r in betting if r.word is not None), default=w.start)
        showdown = hand.get("winner_source") in ("cards", "announced") or any(r.street == "showdown" for r in rows)
        for t in unused:
            same = by_utt.get(t.utterance_start_ts) or [t]
            k = next((i for i, x in enumerate(same) if x is t), -1)
            p_ph = self._p_phantom(t.utterance_start_ts, tuple((x.action, x.raw_text) for x in same), k)
            if _restates(t, rows) or (showdown and t.action == "fold"
                                      and (t.utterance_start_ts or t.timestamp) > said_end):
                p_ph = max(p_ph, p["p_restate"])   # 前の語の言い直し・ショーダウンのあとのマックの声
            terms.append((f"余計な語「{t.raw_text}」", _log(p_ph)))
            if (t.action in _WAGER or t.action == "fold") and p_ph < p["p_restate"]:
                flags.append(f"使えなかった語「{t.raw_text}」")
        for e in edits:
            if e.kind == "drop":
                parsed = tuple((x.action, x.raw_text) for x in self._parse_cache.get((e.at, None)) or ())
                terms.append((f"捨てた語: {e.label}", _log(self._p_phantom(e.at, parsed, int(e.value)))))
            elif e.kind == "read" and not (e.value or "").strip():
                terms.append((f"捨てた語: {e.label}", _log(self._p_phantom(e.at))))
        # 残り人数の宣言
        for t in info["tokens"]:
            if t.action in ("heads_up", "players_left"):
                want = 2 if t.action == "heads_up" else int(t.amount or 0)
                have = _remaining_at(hand, t.utterance_start_ts)
                ok = have is None or want == have
                terms.append(("人数の宣言" + ("" if ok else f"が合わない（{want} 人 / {have} 人）"),
                              _log(1 - p["p_players"]) if ok else _log(p["p_players"])))
                if not ok:
                    flags.append(f"人数の宣言が合わない（{want} 人と言ったが {have} 人）")
        # ショーダウンの声（「ショーダウン」・役の名前）は全員降りて終わったハンドでは言われない（声の数によらず 1 つ。
        # 店舗 42f7b964 ハンド 2: リバーのコールが決まり文句になり、言われないフォールドで終わる別解が役の名前を
        # 説明しないまま 1 番だった）
        if any(t.action == "showdown" or (t.action == "end_hand" and t.hand_name) for t in info["tokens"]):
            foldout = hand.get("winner_source") == "fold"
            terms.append(("ショーダウンの声" + ("があるのに全員降りて終わった" if foldout else ""),
                          _log(p["p_showdown_word"]) if foldout else _log(1 - p["p_showdown_word"])))
            if foldout:
                flags.append("ショーダウンの声があるのに全員降りて終わった")
        terms += self._departure_terms(w, hand, betting, flags)
        terms += self._board_terms(hand, flags)
        terms += self._street_time_terms(hand, betting, flags)
        if hand.get("winner_source") in ("estimated", "undetermined") or hand.get("winner_seat") is None:
            terms.append(("勝者が決まらない", _log(p["p_undetermined"])))
            flags.append("勝者が決まらない")
        return round(sum(v for _, v in terms), 4), terms, list(dict.fromkeys(flags))

    def _action_terms(self, hand: dict, betting: list[_Row], flags: list[str]) -> list[tuple[str, float]]:
        """アクション列の事前（合法手の種類の数）と、各行が言われた / 言われなかった確率。"""
        from core.bet_sizing import hand_wager_ratios, size_prior

        p = self.params
        terms: list[tuple[str, float]] = []
        last_fold = _last_fold(hand, betting)
        prev: dict[str, Optional[str]] = {}
        facing = {"preflop": True}
        ratios = (hand_wager_ratios([r.raw or {} for r in betting], hand.get("blinds") or {},
                                    hand.get("position_map") or {}) if p.get("amount_prior_weight") else {})
        for i, r in enumerate(betting):
            n_legal = 3 if facing.get(r.street, False) else 2
            terms.append(("手の事前", -math.log(n_legal)))
            size = p.get("amount_prior_weight", 0.0) * size_prior(ratios.get(i))
            if size < 0:
                terms.append(("賭けの大きさ", size))      # ポットに対して大きすぎる額ほど低い（弱い事前）
            if r.action in _WAGER:
                facing[r.street] = True
            miss = self._p_miss(r.action, prev.get(r.street), i == last_fold)
            if r.word is not None or r.collective:
                terms.append((f"言われた {r.action}", _log(1 - miss)))
                if (r.raw or {}).get("corrected_from") or r.reasons & {"check_facing_bet", "check_illegal_fold"} or any(
                        x.endswith(("_illegal_to_call", "_illegal_to_fold")) or x.startswith("heard_")
                        for x in r.reasons):
                    terms.append(("語の取り違え", _log(p["p_sub"])))
                    flags.append(f"語の取り違え（{r.street} 席{r.seat} {r.action}）")
                if r.reasons & {"amount_snapped", "no_amount_heard", "rounded_to_bb"}:
                    terms.append(("額の聞き違い", _log(p["p_amount"])))
                    flags.append(f"額の聞き違い（{r.street} 席{r.seat}）")
                if r.reasons & _AMOUNT_READ_REASONS:
                    flags.append(f"額の読み（{r.street} 席{r.seat} {(r.raw or {}).get('amount')}）")
            else:
                terms.append((f"言われない {r.action}", _log(miss)))
                if i != last_fold and "players_left_call" not in r.reasons:     # 「N プレイヤーズ」で言われた
                    # 言われないのが普通なのはハンドを終わらせるフォールドだけ（店舗 5/11）。ほかは 1 割未満で、
                    # 手番の並べ方がほかにもありうる（店舗 d0f055fb ハンド 3: 補ったコールの代わりに無言のチェック
                    # とコール = ベットした人が違う）
                    flags.append(f"言われない {r.action}（{r.street} 席{r.seat}）")
            prev[r.street] = r.action
        return terms

    def _amount_readings(self, betting: list[_Row], edits: tuple[Edit, ...]) -> list[str]:
        """賭けの額を読んだ発話に、別の額になる読み（第 2 の耳の候補など）が、選んだ読みから `review_margin` 以内の
        確からしさである。採点には足さず要確認の理由だけ（監査 2 回目: 合法な額の聞き違いは、寄せ・丸めが無いと何の
        印も付かない）。店舗の 15 発話（第 2 の耳が額を読んだ）では、別の額の候補はどれも 2.75 以上離れていた。"""
        margin = self.params["review_margin"]
        chosen = {e.at: e.value for e in edits if e.kind == "read"}
        out: list[str] = []
        seen: set[float] = set()
        for r in betting:
            start = r.word.utterance_start_ts if r.word is not None else None
            if r.action not in _WAGER or start is None or start in seen or start not in self._row_at:
                continue
            seen.add(start)
            options = self._options(start)
            mine = next((o for o in options if o.source != "drop" and o.text == chosen.get(start)), options[0])
            amounts = _wager_amounts(mine.keys)
            others = sorted({a for o in options if o is not mine and o.logp >= mine.logp - margin
                             for a in _wager_amounts(o.keys)} - amounts)
            if amounts and others:
                out.append(f"額の読みが 2 通り（{r.street} 席{r.seat} {'・'.join(map(str, sorted(amounts)))} / "
                           f"{'・'.join(map(str, others))}）")
        return out

    def _p_miss(self, action: Optional[str], previous: Optional[str], last_fold: bool = False) -> float:
        p = self.params
        if action in _WAGER:
            return p["p_miss_wager"]
        if action == "fold" and last_fold:
            return p["p_miss_last_fold"]
        if action in ("check", "call", "fold") and previous == action:
            return max(p["p_miss_repeat"], p[f"p_miss_{action}"])
        return p.get(f"p_miss_{action}", p["p_miss_check"])

    def _departure_terms(self, w: HandWindow, hand: dict, betting: list[_Row],
                         flags: list[str]) -> list[tuple[str, float]]:
        """札の離脱（窓の中のすべて）を仮説ごとに説明する: フォールドの声とほぼ同時・言われないフォールド・
        ショーダウンと片付け・持ち上げ。どの仮説でも離脱 1 つに時刻の密度 1 つ（比べられる）。"""
        p = self.params
        if self.presence is None:
            return []
        seats = {pl["seat"] for pl in hand.get("players") or []}
        # 窓はどの仮説でも同じ（ハンドの始まりから、直しの無い再生のハンドの終わり + `departure_after_end_sec`
        # または次のハンドの始まりまで）。すぐ戻った離脱（ちらつき・のぞき見 = engine が 3 秒でフォールドにしない
        # のと同じ）はどの仮説でも数えない（監査 2 回目: フォールドの証拠に流用されない）
        stop = min(w.end, (_epoch(w.base.get("ended_at")) or w.end) + p["departure_after_end_sec"])
        deps = [d for d in self.presence.departures(w.start, stop)
                if d[0] in seats and not (d[2] is not None and d[2] - d[1] < p["flicker_sec"])]
        end = _epoch(hand.get("ended_at")) or stop
        terms: list[tuple[str, float]] = []
        explained: set[int] = set()
        folded_at: dict[int, float] = {}
        eps, tail = p["fold_early_mix"], p["fold_tail_sec"]
        # 時刻の分かる行（言われた語の話し始め・札の離脱で作ったフォールドの離脱の時刻）。言われないフォールドは
        # 前後の時刻の分かる行の間のどこか（最後のフォールドなら前の行から `silent_fold_wait` 秒まで）
        anchors = [(_word_start(r) if r.word is not None else r.t)
                   if (r.word is not None or r.collective or r.source in _RFID_FOLDS) else None for r in betting]
        bet_end = max((a for a in anchors if a is not None), default=w.start)
        for i, r in enumerate(betting):
            if r.action != "fold":
                continue
            folded_at.setdefault(r.seat, r.t)
            if not self._seat_read(r.seat, w.start, stop):
                continue                          # そのハンドで一度も読めていない席: 離脱は証拠にならない
            spoken = r.word is not None
            if spoken:
                at = _word_start(r)
                lo, hi = at - tail, at + tail
                nxt: list[float] = []
            else:
                prev = max((a for a in anchors[:i] if a is not None), default=w.start)
                nxt = [a for a in anchors[i + 1:] if a is not None]
                after = min(nxt) if nxt else prev + p["silent_fold_wait"]
                lo, hi = prev - tail, after + 1.0          # 手番より前に投げた札（早いマック）も裾で受ける
                at = r.t
            # フォールドした札はそのハンドの中では戻らない（戻った離脱は持ち上げ）
            best = min((k for k, d in enumerate(deps) if d[0] == r.seat and k not in explained
                        and lo <= d[1] <= hi and (d[2] is None or d[2] >= end)),
                       key=lambda k: abs(deps[k][1] - at), default=None)
            if best is None:
                terms.append((f"席{r.seat} のフォールドに札の離脱が無い", _log(p["p_nodepart"])))
                flags.append(f"札が離れないフォールド（{r.street} 席{r.seat}）")
                continue
            explained.add(best)
            folded_at[r.seat] = min(folded_at[r.seat], deps[best][1])
            if not spoken:
                bet_end = max(bet_end, deps[best][1])   # 言われないフォールドの時刻 = 札が離れた時刻
            t_dep = deps[best][1]
            if spoken:
                # 話し始めのほぼ同時（ラプラス）+ 裾（語が発話の後ろの方・早いマック: ±`fold_tail_sec` の一様）
                lag = t_dep - at
                core = math.exp(-abs(lag - p["fold_lag_mu"]) / p["fold_lag_b"]) / (2 * p["fold_lag_b"])
                density = (1 - eps) * core + eps / (2 * tail)
                terms.append((f"席{r.seat} の離脱 = 声のフォールド（{lag:+.1f} 秒）", _log(density)))
            else:
                # 前の言われたアクションからの考える時間（指数分布、次の言われたアクションで打ち切る。打ち切りの
                # 幅は 3 秒より狭くしない）+ 裾（早いマック）
                mean = p["silent_fold_mean"]
                wait = t_dep - prev
                span = max(3.0, (after - prev)) if nxt else math.inf
                core = math.exp(-max(0.0, wait) / mean) / mean / (1 - math.exp(-span / mean)) if wait >= -1.0 else 0.0
                density = (1 - eps) * core + eps / max(1.0, hi - lo)
                terms.append((f"席{r.seat} の離脱 = 言われないフォールド（{wait:+.1f} 秒）", _log(density)))
            terms.append(("フォールドの札が離れた", _log(1 - p["p_nodepart"])))
        for k, (seat, t, back) in enumerate(deps):
            if k in explained:
                continue
            if (seat in folded_at and folded_at[seat] <= t) or t >= bet_end - 2.0:
                terms.append((f"席{seat} の離脱（ショーダウン・片付け）", _log(1 / p["other_departure_sec"])))
            else:
                terms.append((f"席{seat} の離脱（残っているのに）", _log(1 / p["lift_sec"])))
                if back is None or back >= end:
                    flags.append(f"残っている席{seat} の札が離れた")
        return terms

    def _seat_read(self, seat: int, t0: float, t1: float) -> bool:
        """その席の札がハンドの中で一度でも載っていたか（読めない席のフォールドを札の離脱で罰しない）。"""
        obs = (self.presence._seats or {}).get(seat) if self.presence is not None else None   # noqa: SLF001
        if not obs:
            return False
        before = [o for o in obs if o.t <= t0]
        if before and before[-1].present:
            return True
        return any(o.present for o in obs if t0 < o.t <= t1)

    def _board_terms(self, hand: dict, flags: list[str]) -> list[tuple[str, float]]:
        p = self.params
        board = [c for c in hand.get("board") or [] if c and c != "??"]
        streets = {a.get("street") for a in hand.get("actions") or []}
        terms = []
        needed = {"flop": 3, "turn": 4, "river": 5}
        for street, n in needed.items():
            if street in streets and len(board) < n and hand.get("board"):
                terms.append((f"{street} の札が読めていない", _log(p["p_unread_board"])))
                flags.append(f"{street} の札が読めていない")
        dealt_to = max((n for s, n in needed.items() if len(board) >= n), default=0)
        last = next((s for s in ("river", "turn", "flop", "preflop") if s in streets), "preflop")
        if hand.get("winner_source") == "fold" and dealt_to and _STREETS.index(last) < [0, 0, 0, 1, 2, 3][dealt_to]:
            terms.append(("全員降りたあとに札が配られた", _log(p["p_board_after_end"])))
            flags.append("全員降りたあとに札が配られた")
        return terms

    def _street_time_terms(self, hand: dict, betting: list[_Row], flags: list[str]) -> list[tuple[str, float]]:
        """時刻の分かる行（言われた語・札の離脱）が、そのストリートの札が配られてから次のストリートの札が配られる
        までの間にあるか（ディーラーはベッティングが終わってから次の札を配る）。"""
        p = self.params
        dealt: dict[int, float] = {}
        for item in hand.get("board_timeline") or []:
            t = _epoch(item.get("dealt_at"))
            if t is not None and isinstance(item.get("index"), int):
                dealt[item["index"]] = t
        opened = {"preflop": None, "flop": _street_open(dealt, 3), "turn": _street_open(dealt, 4),
                  "river": _street_open(dealt, 5)}
        closes = {"preflop": opened["flop"], "flop": opened["turn"], "turn": opened["river"], "river": None}
        slack = p["street_slack_sec"]
        terms = []
        for r in betting:
            if r.word is None and r.source not in _RFID_FOLDS:
                continue                          # 言われない行の時刻は分からない
            at = _word_start(r)
            start, end = opened.get(r.street), closes.get(r.street)
            if start is not None and at < start - slack:
                terms.append((f"{r.street} の{r.action}（席{r.seat}）が札より前", _log(p["p_street_time"])))
                flags.append(f"{r.street} のアクションが札より前（席{r.seat} {r.action}）")
            elif end is not None and at > end + slack:
                terms.append((f"{r.street} の{r.action}（席{r.seat}）が次の札よりあと", _log(p["p_street_time"])))
                flags.append(f"{r.street} のアクションが次の札よりあと（席{r.seat} {r.action}）")
        return terms

    # ――― 直しの候補 ―――

    def candidate_edits(self, w: HandWindow, base_info: dict) -> list[Edit]:
        edits: list[Edit] = []
        _events, rows = self._window_inputs(w)
        by_start = {r["utterance_start_ts"]: r for r in rows}
        heard = [t.utterance_start_ts for t in base_info["tokens"]
                 if t.action in _BETTING and t.utterance_start_ts is not None]
        near = self.params["canned_near_sec"]
        # 決まり文句の発話（アクションだったかもしれない）は、読めた賭けの語の近くのものだけ選択点にする
        canned = {s for s in by_start if any(o.source == "canned" for o in self._options(s))
                  and any(abs(s - t) <= near for t in heard)}
        # 別の読み
        for start, row in by_start.items():
            options = self._options(start)       # 読みの選択肢（第 2 の耳の候補など）は v0 と同じ
            for opt in options[1:]:
                if opt.source == "drop" or (opt.source == "canned" and start not in canned):
                    continue                        # 語を捨てるのは drop で
                edits.append(Edit("read", start, opt.text, logp=opt.logp, label=f"{row.get('text')!r} → {opt.label()}"))
        # 余計な語を捨てる（その発話で読んだアクションの何番目か = world_events の drops と同じ数え方）
        per_utt: dict[float, int] = {}
        betting_times: list[float] = []
        for t in sorted(base_info["tokens"], key=lambda x: x.timestamp):
            k = per_utt.get(t.utterance_start_ts, 0)
            per_utt[t.utterance_start_ts] = k + 1
            if t.action not in _BETTING:
                continue
            betting_times.append(t.utterance_start_ts)
            edits.append(Edit("drop", t.utterance_start_ts, k,
                              label=f"「{by_start.get(t.utterance_start_ts, {}).get('text')}」の {k + 1} 語目を捨てる"))
        # 聞こえなかったアクションを入れる（次の語の少し前・最後の語のあと）。選択点にした決まり文句の発話の前も（店舗
        # 4c252c77 ハンド 2: 決まり文句になったレイズの前に、言われない SB のコール）
        times = sorted(set(betting_times))
        spots = [s - _INSERT_LEAD_SEC for s in sorted(set(times) | set(canned))]
        if times and times[-1] in by_start:
            spots.append(delivered_at(by_start[times[-1]]) + 1.0)
        for at in spots:
            for act in _INSERTABLE:
                edits.append(Edit("insert", at, act, label=f"聞こえなかった {act}（{_iso(at)[11:19]} の前）"))
        # ボタン
        seats = sorted({pl["seat"] for pl in w.base.get("players") or []})
        if w.button in seats and len(seats) > 2:
            for seat in seats:
                if seat != w.button:
                    edits.append(Edit("button", w.start, seat, label=f"ボタン 席{seat}"))
        # 札の離脱を持ち上げとみる
        if self.presence is not None:
            end = _epoch(w.base.get("ended_at")) or w.end
            for seat, t, back in self.presence.departures(w.start, end):
                edits.append(Edit("lift", t, (seat, back if back is not None else end + 60.0),
                                  label=f"席{seat} の離脱（{_iso(t)[11:19]}）は持ち上げ"))
        return edits

    # ――― 探索 ―――

    def estimate_hand(self, w: HandWindow) -> HandResult:
        p = self.params
        seen: dict[frozenset, Candidate] = {}

        def evaluate(edits: tuple[Edit, ...]) -> Candidate:
            key = frozenset(edits)
            if key not in seen:
                hand, records, info = self.replay(w, edits)
                score, terms, flags = self.score(w, hand, records, info, edits)
                seen[key] = Candidate(edits, hand or {}, score, terms, flags)
            return seen[key]

        base = evaluate(())
        _hand, _records, base_info = self.replay(w, ())
        pool = self.candidate_edits(w, base_info)
        distinct: dict[tuple, Candidate] = {base.key(): base}

        def keep(c: Candidate, parent: Candidate) -> bool:
            """同じ記録になる直しは得点の良い 1 つ。記録を変えない直しは候補を広げないが、得点が良ければその記録の
            説明としては残す（エンジンが補ったコールを、近くの決まり文句の発話の読みで説明する = 店舗 42f7b964
            ハンド 2）。"""
            if not c.hand:
                return False
            if c.key() not in distinct or c.score > distinct[c.key()].score:
                distinct[c.key()] = c
            return c.key() != parent.key()

        def search(root: Candidate, edits: list[Edit]) -> None:
            """root の直しに 1 つずつ足すビーム。"""
            singles: dict[tuple, Candidate] = {}
            for e in edits:
                if any(e.conflicts(x) for x in root.edits):
                    continue
                c = evaluate(_ordered((*root.edits, e)))
                if keep(c, root) and (c.key() not in singles or c.score > singles[c.key()].score):
                    singles[c.key()] = c
            ranked = sorted(singles.values(), key=lambda c: -c.score)
            useful = [next(x for x in c.edits if x not in root.edits) for c in ranked[:int(p["expand"])]]
            beam = sorted([root, *ranked], key=lambda c: -c.score)[:int(p["beam"])]
            for _depth in range(2, int(p["depth"]) + 1):
                grown: list[Candidate] = []
                for rank, c in enumerate(beam):
                    # いちばん良い候補にはすべての直しを試す（単独では点が低くても、ほかの直しのあとで効く直しが
                    # ある = 店舗 fded6f75 ハンド 1: リバーを直したあとのフロップの無言のチェック）
                    for e in (edits if rank == 0 else useful):
                        if e in c.edits or any(e.conflicts(x) for x in c.edits):
                            continue
                        child = evaluate(_ordered((*c.edits, e)))
                        if keep(child, c):
                            grown.append(child)
                if not grown:
                    break
                beam = sorted({c.key(): c for c in [*beam, *grown]}.values(), key=lambda c: -c.score)[:int(p["beam"])]

        # ボタンはハンド全体の手番を変えるので、ボタンごとに直しを探す（単独では点が低くても、ほかの直しと
        # 合わせて正しくなる = 店舗 9d1d8536 ハンド 4: ボタンの置き忘れ + 「チェック、チェック」の言い直し）。
        # ボタンを変えた再生が既定より `button_explore` 以上悪ければ探さない
        others = [e for e in pool if e.kind != "button"]
        search(base, others)
        for b in (e for e in pool if e.kind == "button"):
            root = evaluate((b,))
            if root.hand and root.score >= base.score - p["button_explore"]:
                keep(root, base)
                search(root, others)
        groups = sorted(distinct.values(), key=lambda c: -c.score)
        tau = p["temperature"]
        top = groups[0].score / tau if groups else 0.0
        weights = [math.exp(c.score / tau - top) for c in groups]
        total = sum(weights) or 1.0
        posteriors = [(1 - p["outside"]) * wgt / total for wgt in weights]
        result = HandResult(window=w, candidates=groups, posteriors=posteriors, replays=len(seen), base=base)
        result.reasons = self._review_reasons(result)
        return result

    def _review_reasons(self, r: HandResult) -> list[str]:
        reasons = []
        if r.margin is not None and r.margin < self.params["review_margin"]:
            reasons.append(f"次点との差が小さい（{r.margin:.1f}）")
        for e in r.best.edits:
            reasons.append(e.label or e.kind)
        reasons += r.best.flags
        return list(dict.fromkeys(reasons))

    def estimate(self, on_hand: Optional[Callable[[HandResult], None]] = None, workers: int = 1,
                 windows: Optional[list[HandWindow]] = None) -> list[HandResult]:
        """セッションのハンドを推定する。ハンドどうしは独立なので `workers` > 1 なら別のプロセスで並べて回す
        （結果は同じ。店舗 PC で 1 セッション 1 分以内 = ADR-0056）。"""
        windows = self.windows() if windows is None else windows
        out: list[HandResult] = []
        if workers <= 1 or len(windows) < 2:
            for w in windows:
                r = self.estimate_hand(w)
                out.append(r)
                if on_hand:
                    on_hand(r)
            return out
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor

        args = (self.events, self.transcripts, self.presence, self.setup, self.flags, self.session_id, self.params)
        with ProcessPoolExecutor(max_workers=min(workers, len(windows)), mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_init_worker, initargs=(args,)) as pool:
            for r in pool.map(_estimate_in_worker, windows):
                out.append(r)
                if on_hand:
                    on_hand(r)
        return out


# ───────────────────────── 補助 ─────────────────────────

_WORKER: Optional[SessionEstimator] = None
_KEEP_CANDIDATES = 50          # 別のプロセスから返す候補の数（受け渡しを軽くする。1 番・次点・事後確率は変わらない）


def _init_worker(args: tuple) -> None:
    global _WORKER
    logging.disable(logging.CRITICAL)             # 候補の再生で出るエンジンのログは要らない
    _WORKER = SessionEstimator(*args)


def _estimate_in_worker(w: HandWindow) -> HandResult:
    assert _WORKER is not None
    r = _WORKER.estimate_hand(w)
    r.candidates = r.candidates[:_KEEP_CANDIDATES]
    r.posteriors = r.posteriors[:_KEEP_CANDIDATES]
    return r


def _restates(t: AudioEvent, rows: list[_Row]) -> bool:
    """使われなかった語が、少し前に言われた行の言い直しか（同じアクション。ベット・レイズは額も同じ、オールインは
    額を問わない。ディーラーは額や「オールイン」を次の発話で繰り返す）。"""
    start = t.utterance_start_ts or t.timestamp
    wager = t.action in _WAGER
    for r in rows:
        if r.word is None or r.word is t:
            continue
        gap = start - _word_start(r)
        if not 0.0 <= gap <= (30.0 if wager else 15.0):
            continue
        if t.action == "allin" and r.action == "allin":
            return True
        if wager and r.action in _WAGER and t.amount and int((r.raw or {}).get("amount") or 0) == int(t.amount):
            return True
        if not wager and r.action == t.action:
            return True
    return False


def _ordered(edits: tuple[Edit, ...]) -> tuple[Edit, ...]:
    return tuple(sorted(edits, key=lambda x: (x.kind, x.at, str(x.value))))


class _DefaultLogp:
    """発話の既定の読みの確からしさ（`utterance_options` の [0]）。"""

    def __init__(self, est: SessionEstimator) -> None:
        self._est = est

    def get(self, start: float, default: float = 0.0) -> float:
        try:
            options = self._est._options(start)          # noqa: SLF001
        except KeyError:
            return default
        return float(options[0].logp) if options else default


def _wager_amounts(keys: tuple) -> set[int]:
    """読みの選択肢のアクション（`tools/estimate.py` の `_keys`）のうち賭けの額。"""
    return {int(k[1]) for k in keys if k[0] in _WAGER and k[1]}


def _street_open(dealt: dict[int, float], n: int) -> Optional[float]:
    """そのストリートが始まった時刻: フロップは 3 枚のうち最初に読んだ札（1 枚の読み遅れでストリートの窓ごとずれ
    ないように = 監査 2 回目）、ターン・リバーはその札。"""
    group = {3: (1, 2, 3), 4: (4,), 5: (5,)}.get(n, tuple(range(1, n + 1)))
    times = [dealt[i] for i in group if i in dealt]
    return min(times) if times else None


def _last_fold(hand: dict, betting: list[_Row]) -> Optional[int]:
    """ハンドを終わらせたフォールド（最後のベッティングの行がフォールドで、全員降りて終わった）の番号。"""
    if hand.get("winner_source") != "fold" or not betting or betting[-1].action != "fold":
        return None
    if any(a.get("street") == "showdown" for a in hand.get("actions") or []):
        return None                            # ショーダウンのマックで終わった
    return len(betting) - 1


def _is_inserted(e: AudioEvent, inserted: dict[str, str]) -> bool:
    return _iso(e.timestamp) in inserted and not e.raw_text


def _remaining_at(hand: dict, t: Optional[float]) -> Optional[int]:
    """その時刻に残っている人数（それまでのフォールドを引く）。"""
    if t is None:
        return None
    players = [pl["seat"] for pl in hand.get("players") or []]
    if not players:
        return None
    folded = {a["seat"] for a in hand.get("actions") or []
              if a.get("action") == "fold" and (_epoch(a.get("timestamp")) or 0.0) <= t + 0.5}
    return len([s for s in players if s not in folded])


def _masked(presence: PresenceTimeline, lifts: list[tuple[int, float, float]]) -> PresenceTimeline:
    """札の離脱を持ち上げとみた在否（その間も載っていたことにする）。"""
    seats: dict[int, list[SeatObservation]] = {}
    for seat, obs in presence._seats.items():      # noqa: SLF001
        seats[seat] = list(obs)
    for seat, t0, t1 in lifts:
        obs = seats.get(seat)
        if not obs:
            continue
        kept = [o for o in obs if not (t0 - 0.05 <= o.t < t1)]
        kept.append(SeatObservation(t0, True, None, None))
        seats[seat] = sorted(kept, key=lambda o: o.t)
    masked = PresenceTimeline(seats)
    masked._reads = presence._reads                # noqa: SLF001
    return masked
