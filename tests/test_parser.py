# tests/test_parser.py
from audio.recognizer import parse_amount, parse_action

def test_parse_amount_basic_numbers():
    assert parse_amount("レイズ 800") == 800
    assert parse_amount("レイズ 1,200 です") == 1200

def test_parse_amount_k_and_man():
    assert parse_amount("レイズ 5K") == 5000
    assert parse_amount("レイズ 3万") == 30000
    assert parse_amount("レイズ 1万2千") == 12000
    assert parse_amount("レイズ 五千") == 5000

def test_parse_action_basic():
    ev = parse_action("シート3 レイズ 800")
    assert ev is not None
    assert ev.action == "raise"
    assert ev.amount == 800

    ev = parse_action("シート2 コール")
    assert ev is not None
    assert ev.action == "call"
    assert ev.amount == 0
