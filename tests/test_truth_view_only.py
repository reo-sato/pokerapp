"""お客さんの履歴に真のアクションだけを見せる（`viewer_api.show_truth = "only"`, オーナー 2026-11-19）。

`true` は真のアクション、無いハンドは推定（2026-11-09）。`"only"` は推定も出さない（今回の店舗テストのあいだ）。
"""
from __future__ import annotations

from core.truth_hand import truth_mode, truth_view


def test_modes():
    assert truth_mode("only") == "only" and truth_mode("ONLY ") == "only"
    assert truth_mode(True) is True and truth_mode(False) is False and truth_mode(None) is False
    assert truth_mode("false") is False


def test_only_hides_estimated_hands(monkeypatch):
    import core.truth_hand as th

    monkeypatch.setattr(th, "materialize_truth", lambda entry, hand: {"hand_id": hand["hand_id"], "truth": True})
    hands = [{"hand_id": 1}, {"hand_id": 2, "estimate": {"source": "estimator"}}, {"hand_id": 3}]
    truth = {1: {"actions": []}}
    assert [h["hand_id"] for h in truth_view(hands, truth)] == [1, 2]              # true: 無いハンドは推定
    assert [h["hand_id"] for h in truth_view(hands, truth, estimate=False)] == [1]  # only: 真のアクションだけ


def test_the_api_setting_reads_only():
    from api.server import auth_kwargs_from_config

    assert auth_kwargs_from_config({"show_truth": "only"})["show_truth"] == "only"
    assert auth_kwargs_from_config({"show_truth": True})["show_truth"] is True
    assert auth_kwargs_from_config({})["show_truth"] is False
