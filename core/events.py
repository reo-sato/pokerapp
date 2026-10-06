from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

# numpy は CameraEvent.frame の型注釈だけで使う。`from __future__ import annotations` により
# 注釈は実行時評価されない文字列なので、実行時に numpy を import する必要はない。TYPE_CHECKING
# ガードに入れることで、RFID canonical 経路（rfid.reader_thread / tools/probe_pcsc.py watch）が
# numpy 未導入の最小環境（pyscard だけ）でも動く。numpy が要るのは camera(legacy)/audio 経路のみ。
if TYPE_CHECKING:
    import numpy as np


@dataclass
class RFIDEvent:
    """RFID リーダースレッドが検出したカードタッチイベント。"""

    tag_id: str                  # リーダーから受け取った UID（正規化済み）
    card: str                    # "Ah", "Kd" など（カードマスター未登録時は空文字）
    reader_id: str               # "seat_1", "board_3" など設定ファイルのキー
    role: str                    # "seat" | "board"
    seat: Optional[int]          # role="seat" 時の席番号、role="board" 時は None
    timestamp: float             # time.time()
    raw_tag_id: str              # デバッグ用の生タグ ID
    board_index: Optional[int] = None  # role="board" 時のボード位置 (1=flop1…5=river)
    # 配り直しで差し替えた前の札（カード名。ADR-0058）。席ならその席の手札のうちこの札を置き換え、
    # ボードなら同じ board_index の札を置き換える。None = 通常の配布（追加）。
    replaces: Optional[str] = None
    # 種類（2026-09-25, フォールドは札の離脱で決める = オーナー決定）。"card" = 札を読んだ（従来）。
    # 以下は engine が席の在否から作って記録する（replay で同じ判断を再現するため）:
    # "leave" = 席の札が離れたまま戻らない / "muck" = 席の札が卓の中央を通過 / "return" = 離れた札が
    # 戻った（それまでフォールドではない）/ "confirm" = 最後の 1 人を残すフォールドを確定。
    kind: str = "card"
    # "leave" / "muck" の札が離れた時刻（`timestamp` は engine がそれを反映した時刻 = replay の順序）。
    observed_at: Optional[float] = None
    # "deal" = 在否で決めた配布（observed_at = 手札が載り始めた時刻、cards = "席:札" の並び）/ "hand_start" =
    # 配布のハンドを始めた（配る前の発話を待ったあと）。replay はこの 2 つで live と同じ時点にハンドを始める。
    # "deal_order" = 手札を最初に読んだ時刻（cards = "席:札"、times = 同じ並びの時刻）。配った順からボタンの
    # 置き忘れを見つける（2026-09-29）。
    cards: tuple[str, ...] = ()
    times: tuple[float, ...] = ()


@dataclass
class CameraEvent:
    """カメラスレッドが検出したチップ動作イベント。"""

    seat: int
    timestamp: float  # time.time()
    frame: Optional[np.ndarray] = field(default=None, repr=False)  # Phase 2 以降で使用


@dataclass
class AudioEvent:
    """音声認識スレッドが検出したアクションイベント。"""

    action: str  # "bet"/"call"/"raise"/"check"/"fold"/"allin"/"showdown"/"winner"/"new_hand"
    amount: int  # 金額なしの場合は 0
    timestamp: float  # time.time()
    raw_text: str
    # 以下は additive (R3/R4 用)。既存経路は未使用 = 挙動不変。
    seat: Optional[int] = None        # 明示発話された席番号（"シート3"）。actor 推定/replay 用
    confidence: Optional[float] = None  # Whisper per-segment 信頼度 [0,1]（派生 confidence の入力）
    # 明示発話された **ポジション名**（"BTN、コール" の BTN, 正準名。仕様 §7 / FR-26, ISSUE-0032）。
    # 席への解決はボタンを知っている engine 側が行う（`seat` が無いときの代替証拠）。
    position: Optional[str] = None
    # additive (ADR-0047/0048): パース時に検出した曖昧性（"ambiguous_amount"/"multi_action_keywords"）。
    # 非空なら engine が needs_review を付ける。
    parse_flags: tuple[str, ...] = ()
    # additive (ADR-0048 T1): 発話キャプチャの開始時刻（epoch 秒）。ASR デコード遅延に依らず
    # センサー照合窓の始端に使う。None なら timestamp を始端に使う（旧記録の後方互換）。
    utterance_start_ts: Optional[float] = None
    # additive (2026-09-26): ディーラーが言った **勝った役の名前**（pokerkit の役名, action="end_hand" のとき）。
    # ショーダウンの判定との突き合わせに使う。役名を言わない「ハンド終了」では None。
    hand_name: Optional[str] = None
    # additive (2026-10-06): 「Nヒット」の N（ボードの札と組にした札の数字 A K Q J T 9..2）。ワンペアか、ボードのペアを
    # 無視した 2 ペア（オーナー 2026-10-06）。engine が見せた席を手札と突き合わせる
    hit_rank: Optional[str] = None
    # additive (2026-10-01): 意味のない単発の語を額の音で読んだとき（parse_flags "phonetic_amount"）の額の候補
    # （確からしい順。`amount` は先頭）。
    amount_options: tuple[int, ...] = ()
    # additive (2026-10-01): 額の候補ごとの音の点数 (額, log の確からしさ)。第 2 の耳の額ごとの点数（ベット・レイズが
    # 1 つの発話）か、音で読んだ額の候補の点数。engine が使える額（最小ベット・レイズ〜オールイン）に絞り、ポットに
    # 対する大きさの重みを足して選ぶ（`integration/engine.py:_choose_amount`）。
    amount_scores: tuple[tuple[int, float], ...] = ()
