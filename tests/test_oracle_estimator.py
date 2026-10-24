"""tests/test_oracle_estimator.py

推定器 v1 のオラクルの採点（`tools/oracle_estimator.py`, 台帳 B の ①）: 項の群・真との近さ・最初に違う行（速い組）と、
開発データのハンドでの分け方（遅い組）。推定器の値・規則は変えない（推定器はこの道具を import しない）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tools.oracle_estimator import OTHER, closeness, first_difference, group_terms, term_group

ROOT = Path(__file__).resolve().parent.parent


def _hand(*rows, winner=None):
    return {"actions": [{"street": s, "seat": seat, "action": a, "amount": m} for s, seat, a, m in rows],
            "winner_seat": winner}


class TestTermGroups:
    @pytest.mark.parametrize("label, group", [
        ("読み: 「レイズ」→ コール", "読み"),
        ("既定の読み「ベット」", "読み"),
        ("捨てた語: 「コール」の 1 語目を捨てる", "捨てた語"),
        ("余計な語「チェック」", "余計な語"),
        ("言われない call", "言われない"),
        ("言われないマック（席3）", "言われない"),
        ("言われた fold", "言われた"),
        ("額の聞き違い", "取り違え"),
        ("手の事前", "手の事前"),
        ("席3 の離脱（残っているのに）", "持ち上げ"),
        ("席3 の離脱 = 言われないフォールド（+2.0 秒）", "札の離脱"),
        ("フォールドの札が離れた", "札の離脱"),
        ("ボタン", "ボタン"),
        ("turn の札より前のアクション（4 秒）", "札の時刻"),
        ("人数の宣言が合わない（2 人 / 3 人）", "宣言"),
        ("ラウンドが閉じないまま終わった", "終わり方"),
    ])
    def test_group(self, label, group):
        assert term_group(label) == group

    def test_every_label_the_estimator_writes_has_a_group(self):
        """推定器が書く項のラベル（`terms.append` の固定の部分）が「その他」に落ちない。"""
        import re

        src = (ROOT / "integration/estimator.py").read_text(encoding="utf-8")
        # 「席{seat} …」で始まるラベルは固定の部分が「席」だけになるので、形を下に足して確かめる
        heads = set(re.findall(r'terms\.append\(\(f?"([^"{]+)', src)) - {"席"}
        heads |= {"席3 の離脱（残っているのに）", "席3 のフォールドに札の離脱が無い",
                  "席3 はフォールドのあとも札が席に載っていた", "flop の札より前のアクション（3 秒）",
                  "flop の call（席3）が次の札よりあと", "flop の札が読めていない"}
        assert not {h for h in heads if term_group(h) == OTHER}

    def test_group_sums(self):
        assert group_terms([("言われない call", -1.5), ("言われない fold", -2.0), ("ボタン", -0.5)]) == {
            "言われない": -3.5, "ボタン": -0.5}


class TestCloseness:
    truth = _hand(("preflop", 1, "raise", 600), ("preflop", 2, "call", 600), winner=1)

    def test_truth_is_the_closest(self):
        wrong = _hand(("preflop", 1, "raise", 600), ("preflop", 2, "fold", 0), winner=1)
        short = _hand(("preflop", 1, "raise", 600), winner=1)
        assert closeness(self.truth, self.truth) > closeness(self.truth, wrong)
        assert closeness(self.truth, self.truth) > closeness(self.truth, short)
        assert closeness(self.truth, None) < closeness(self.truth, short)

    def test_winner_counts(self):
        other = _hand(("preflop", 1, "raise", 600), ("preflop", 2, "call", 600), winner=2)
        assert closeness(self.truth, other) == closeness(self.truth, self.truth) - 1

    def test_first_difference(self):
        wrong = _hand(("preflop", 1, "raise", 600), ("preflop", 2, "fold", 0), winner=1)
        assert first_difference(self.truth, wrong) == "2 行目: 真 preflop 席2 call 600 / 得た preflop 席2 fold"
        assert first_difference(self.truth, self.truth) == ""
        assert first_difference(self.truth, _hand(*[("preflop", 1, "raise", 600), ("preflop", 2, "call", 600)],
                                                  winner=2)).startswith("勝者")


# ───────────────────── 開発データ（09-27 027e4b15: 物差しで「点で負けた」ハンド） ─────────────────────


@pytest.mark.slow
def test_outscored_hand_reaches_the_truth_and_names_the_losing_terms():
    from tools.bench_hands import v1_inputs
    from tools.oracle_estimator import run_session

    logging.disable(logging.CRITICAL)
    try:
        pairs = [(i, p) for _n, ss in v1_inputs(store=True, script=False) for i, p in ss
                 if i.session_id.startswith("027e4b15")]
        assert pairs, "fixture 2026-09-27-027e4b15 が無い"
        (result,) = run_session(*pairs[0], only={"027e4b15#1"})
    finally:
        logging.disable(logging.NOTSET)
    assert result.category == "outscored" and result.in_candidates
    assert result.gap is not None and result.gap <= 0 and result.oracle_edits
    assert result.lost_groups(1)[0][0] == "言われない"


def test_the_estimator_does_not_import_the_oracle():
    """道具は推定器の内容の指紋に入らない（推定器・再生器から import されない）。"""
    for rel in ("integration/estimator.py", "integration/world_replay.py", "tools/estimate.py"):
        assert "oracle_estimator" not in (ROOT / rel).read_text(encoding="utf-8")
