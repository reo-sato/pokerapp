"""tests/test_showdown_unknown_cards.py

ショーダウンで読めていない札（ボードの `??`・手札の不足）があるとき（オーナー 2026-09-29, 店舗 d0f055fb ハンド 5 のメモ）:
どの札でも配当が同じなら勝者は決まる。読めていない札しだいで変わるなら決まらない（エンジンはチップを動かさない）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pokerkit")

from core.showdown import award_with_unknown_cards  # noqa: E402

POT = [{"amount": 1000, "eligible_seats": [4, 5]}]


def test_an_unknown_flop_card_that_decides_the_winner_is_undetermined():
    # d0f055fb ハンド 5: 3c Kh ?? Kc 9s。席4 8h7d は 8・7 でツーペア、それ以外は席5 2dTc（T のキッカー）
    assert award_with_unknown_cards(
        {4: ["8h", "7d"], 5: ["2d", "Tc"]}, ["3c", "Kh", "??", "Kc", "9s"], POT, [5, 4],
        excluded=["9d", "Td"],
    ) is None


def test_an_unknown_card_that_cannot_change_the_winner_decides():
    # 席4 はフォーカード（A）: 残りのどの札でも勝つ
    assert award_with_unknown_cards(
        {4: ["As", "Ad"], 5: ["Kd", "Qd"]}, ["Ah", "Ac", "??", "7d", "8c"], POT, [5, 4],
    ) == ({4: 1000}, [[4]])


def test_a_missing_river_counts_as_unknown():
    # 5 枚に足りないボード（リバーが読めていない）も読めていない札として埋める
    assert award_with_unknown_cards(
        {4: ["As", "Ad"], 5: ["Kd", "Qd"]}, ["Ah", "Ac", "7d", "8c"], POT, [5, 4],
    ) == ({4: 1000}, [[4]])


def test_a_missing_hole_card():
    # 席5 の 1 枚しか読めていない: Kd と何を組み合わせても、席4 のフォーカード（Q）に勝てない
    assert award_with_unknown_cards(
        {4: ["Qd", "Qs"], 5: ["Kd"]}, ["Qh", "Qc", "7c", "2h", "3d"], POT, [5, 4],
    ) == ({4: 1000}, [[4]])
    # ボードがフォーカード + 席4 の A キッカー: 席5 の読めていない札が A なら引き分け = 決まらない
    assert award_with_unknown_cards(
        {4: ["As", "2d"], 5: ["Kd"]}, ["Qh", "Qc", "Qd", "Qs", "7c"], POT, [5, 4],
    ) is None
    # 席5 の手札しだいで変わる（Qh + ペアになる札なら席5、2 などなら A ハイの席4）
    assert award_with_unknown_cards(
        {4: ["As", "Kd"], 5: ["Qh"]}, ["3h", "5c", "8d", "Jc", "7c"], POT, [5, 4],
    ) is None


def test_too_many_unknown_cards_are_not_enumerated():
    assert award_with_unknown_cards(
        {4: ["As", "Ad"], 5: []}, ["??", "??", "??", "7d", "8c"], POT, [5, 4],
    ) is None
