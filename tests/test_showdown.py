

def test_unknown_cards_with_six_board_cards_is_not_decided():
    """6 枚のボード（片付けの札を足した記録）は判定しない（落ちない, 店舗 2026-10-19）。"""
    from core.showdown import award_with_unknown_cards
    pots = [{"amount": 1000, "eligible_seats": [1, 2]}]
    board = ["Ad", "Qs", "Jc", "Qd", "4s", "3c"]
    assert award_with_unknown_cards({1: ["Ah"], 2: ["Kh", "Qh"]}, board, pots, [1, 2]) is None
