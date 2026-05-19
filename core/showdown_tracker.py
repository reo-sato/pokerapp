"""core/showdown_tracker.py

Phase 2 予定の skeleton。

RFID / manual / 派生観測から ``RevealedHand`` を蓄積し、全 live seat の hole cards が
揃った時点で "showdown ready" を IntegrationThread に通知する責務を持つ予定。

設計方針:
- canonical な内部表現は ``RevealedHand`` (source, observed_at を持つ)。
- JSON 出力時は ``HandSummary.showdown_revealed_cards`` (簡略表現) に投影する。
- muck (一度見せて捨てた) 後でも finalization 用に保持する (上書きしない方針が有力)。

Phase 1 ではクラス・主要メソッド signature のみ定義し、本体は NotImplementedError stub。
"""
from __future__ import annotations

from typing import Optional

from core.hand_log import RevealedHand


class ShowdownTracker:
    """showdown 時に明かされた hole cards を seat 単位で蓄積するトラッカ。

    Phase 2 で実装する。
    """

    def __init__(self) -> None:
        self._revealed: dict[int, RevealedHand] = {}

    def observe(self, hand: RevealedHand) -> None:
        """1 seat 分の RevealedHand を取り込む。

        TODO Phase 2:
          - 同一 seat の再観測 (muck 後の RFID 再検出など) の扱いを決定:
            「最初の観測を canonical とする」「より信頼度が高い source で上書き」のどちらか
          - source の優先順位 (rfid > manual > derived) を明示
        """
        raise NotImplementedError("Phase 2 で実装予定")

    def revealed_hands(self) -> list[RevealedHand]:
        """これまでに観測した RevealedHand の集合を返す。"""
        return list(self._revealed.values())

    def is_showdown_ready(self, live_seats: list[int]) -> bool:
        """全 live seat の hole cards が揃ったかを判定する。

        TODO Phase 2:
          - live_seats に対して self._revealed のキーが部分集合関係を満たすかチェック
          - 部分情報 (一部 seat だけ revealed) のときは provisional 終局として扱う設計を検討
        """
        raise NotImplementedError("Phase 2 で実装予定")

    def project_to_summary_dict(self) -> dict[int, list[str]]:
        """``HandSummary.showdown_revealed_cards`` 用の簡略表現に投影する。

        canonical な RevealedHand の集合から source / observed_at を落とし
        ``{seat: [card, ...]}`` 形式に変換する。

        TODO Phase 2: 実装。Phase 1 では空 dict を返しても良いが finalizer の
        責務との切り分けは要検討。
        """
        raise NotImplementedError("Phase 2 で実装予定")

    def reset(self) -> None:
        """新ハンド開始時に状態をクリアする。"""
        self._revealed = {}
