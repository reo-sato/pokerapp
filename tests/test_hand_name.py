"""tests/test_hand_name.py

ショーダウンでディーラーが言う **役名**（オーナー 2026-09-30: アウトオブポジションが見せ、ディーラーが役名を言う。
見せると札がリーダーから外れるので、役名は見せるたびに必ず言う運用。マックする人は素早くマックし、ディーラーが
「フォールド」と言う。役名のあと、まれにインポジションが勝っている手をマックすることもある）:

- 役名（「ツーペア」「フラッシュ」…）= 次に見せる人（アウトオブポジションから）が見せた。ハンドはまだ終わらない。
  手札で役名に合う席が見せていない中に 1 つだけあれば、その席が見せた。`AudioEvent.hand_name` に pokerkit の
  役名を載せ、events.jsonl にも残す（reconstruction_event 0.6）。
- 見せたあとの「フォールド」= まだ見せていない次の人のマック（手札が強くても負け、要確認）。
- 残った全員が見せたら手札で決める。見せる・マックが `SHOWDOWN_MUCK_SEC` 無ければ、見せたとして手札で決める
  （次の配布まで待たない。記録した `showdown_end` で replay も同じ）。
- 役名は見せた席の手札の判定と突き合わせる: 違えば要確認。役名が全員にあって役名だけで決まる勝者が判定と違えば
  その席の勝ち（`winner_source="announced"`）。手札が読めていない席があるときも、役名から勝者を決める（要確認）。
- 閉じていないベッティングは聞き取れなかったとみて閉じる。確定したあとの役名は、見せた手のどれとも違えば知らせる。
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from audio.recognizer import parse_action, parse_actions
from audio.recorder import describe_event
from core.events import RFIDEvent
from integration.replay import event_from_envelope
from output.event_recorder import event_to_envelope

pytest.importorskip("pokerkit")

from integration.engine import SHOWDOWN_MUCK_SEC  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402

_SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "contracts" / "schemas" / "reconstruction_event.schema.json"


class TestParsing:
    @pytest.mark.parametrize("text, name", [
        ("ツーペア", "Two pair"),
        ("ツーペア、エースとテン", "Two pair"),
        ("2ペアです", "Two pair"),
        ("ワンペア", "One pair"),
        ("スリーカード", "Three of a kind"),
        ("トリップス", "Three of a kind"),
        ("ストレート", "Straight"),
        ("フラッシュ", "Flush"),
        ("フルハウス", "Full house"),
        ("フォーカード", "Four of a kind"),
        ("ストレートフラッシュ", "Straight flush"),
        ("ロイヤルフラッシュ", "Straight flush"),
        ("ハイカード、エースハイ", "High card"),
        ("ふらっしゅ", "Flush"),
    ])
    def test_a_hand_name_ends_the_hand(self, text, name):
        (event,) = parse_actions(text)
        assert (event.action, event.hand_name, event.amount) == ("end_hand", name, 0)

    def test_plain_end_of_hand_has_no_name(self):
        assert parse_action("ハンド終了").hand_name is None

    @pytest.mark.parametrize("text", ["キングハイにクイーンハイ", "キングハイとクイーンハイ"])
    def test_two_hands_named_together(self, text):
        # 店舗 2026-10-06 05cccd6c ハンド 16: 2 人の手を「に」でつないで言った（読めずに降りた手とみていた）
        events = parse_actions(text)
        assert [(e.action, e.hand_name, e.raw_text) for e in events] == [
            ("end_hand", "High card", "キングハイ"), ("end_hand", "High card", "クイーンハイ")]

    @pytest.mark.parametrize("text", ["キングハイに", "キングハイにして"])
    def test_a_joiner_needs_a_second_hand(self, text):
        assert parse_actions(text) == []

    @pytest.mark.parametrize("text", ["セット", "リセットします", "フル"])
    def test_loose_words_are_not_hand_names(self, text):
        assert parse_actions(text) == []

    def test_described_for_the_cli(self):
        assert describe_event(parse_action("ツーペア")) == "見せた（ツーペア）"

    @pytest.mark.parametrize("text, name", [
        # 店舗のディーラーの言い方（オーナー 2026-10-01）: 札の名前 + 役の言い方
        ("キングヒット", "One pair"), ("キング ヒット", "One pair"), ("Kヒット", "One pair"), ("7ヒット", "One pair"),
        ("テンヒットです", "One pair"), ("ポケットエース", "One pair"), ("エースエース", "One pair"),
        ("キングのペア", "One pair"),
        ("エースファイブツーペア", "Two pair"), ("クイーンジャックツーペア", "Two pair"),
        ("クイーンジャック", "High card"), ("クイーン ジャック", "High card"), ("QJ", "High card"),
        ("エースハイ", "High card"), ("エースキングハイ", "High card"),
        ("セブンのセット", "Three of a kind"),
        ("キングハイストレート", "Straight"), ("キングハイ、ストレート", "Straight"),
        ("エースハイフラッシュ", "Flush"),
        ("エースキングフル", "Full house"), ("エースキング、フル", "Full house"), ("AKフル", "Full house"),
        ("エースフル", "Full house"), ("エースキングフルハウス", "Full house"),
        # スリーカード: 「トリップス」「ナナのセット」「セットオブセブン」「スリーカード」/ フォーカード:「クワッズ」
        ("ナナのセット", "Three of a kind"), ("7のセット", "Three of a kind"), ("セットオブセブン", "Three of a kind"),
        ("セットオブセブンズ", "Three of a kind"), ("クアッズ", "Four of a kind"),
        # ポケットペアの呼び名: 「エーシーズ」「キングス」…「Xポケ」「ポケットX(s)」
        ("エーシーズ", "One pair"), ("キングス", "One pair"), ("クイーンズ", "One pair"), ("ジャックス", "One pair"),
        ("テンズ", "One pair"), ("ナインズ", "One pair"), ("ナナポケ", "One pair"), ("Kポケ", "One pair"),
        ("ポケットキングス", "One pair"), ("ポケットエーシーズ", "One pair"), ("キングのポケット", "One pair"),
        # 長い札の名前のツーペア・複数形のフルハウス
        ("キングスアンドセブンズ、ツーペア", "Two pair"), ("二ペア", "Two pair"),
        ("エーシーズフルオブキングス", "Full house"),
    ])
    def test_the_store_wording_with_card_names(self, text, name):
        (event,) = parse_actions(text)
        assert (event.action, event.hand_name, event.amount) == ("end_hand", name, 0)

    @pytest.mark.parametrize("text", ["エース", "キング", "エースキングでしょ", "エースキング対クイーンクイーン",
                                      "セットアップです。", "600点", "スリーベット", "サンセット", "ポケモン",
                                      "キングスパーク", "ベトナナ"])
    def test_card_names_alone_or_in_talk_are_not_hand_names(self, text):
        assert all(e.action != "end_hand" for e in parse_actions(text))

    def test_a_hand_name_then_a_muck_in_one_utterance(self):
        assert [(e.action, e.hand_name) for e in parse_actions("キングヒット、フォールド")] == [
            ("end_hand", "One pair"), ("fold", None)]

    @pytest.mark.parametrize("text, rank", [
        ("5ヒット", "5"), ("ナナヒット", "7"), ("キングヒット", "K"), ("十ヒット", "T"), ("エースヒット", "A"),
    ])
    def test_a_hit_carries_the_paired_rank(self, text, rank):
        (event,) = parse_actions(text)
        assert (event.action, event.hand_name, event.hit_rank) == ("end_hand", "One pair", rank)

    def test_the_hit_rank_is_recorded_and_replayed(self):
        (event,) = parse_actions("5ヒット", utterance_start_ts=5.0)
        envelope = event_to_envelope(event)
        jsonschema.Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8"))).validate(envelope)
        assert envelope["hit_rank"] == "5" and event_from_envelope(envelope).hit_rank == "5"
        assert "hit_rank" not in event_to_envelope(parse_action("ワンペア"))

    def test_recorded_and_replayed(self):
        event = parse_action("フラッシュ", confidence=0.7, utterance_start_ts=5.0)
        envelope = event_to_envelope(event)
        assert envelope["hand_name"] == "Flush"
        jsonschema.Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8"))).validate(envelope)
        assert event_from_envelope(envelope).hand_name == "Flush"
        assert "hand_name" not in event_to_envelope(parse_action("ハンド終了"))


# ボタン 席6: プリフロップは 6 → 4 → 5、フロップ以降は 4 → 5 → 6 の順。
FLUSH_BOARD = ["Jd", "9d", "3d", "5h", "2c"]
HOLES = {4: ["6s", "Qs"], 5: ["Jh", "9c"], 6: ["Kd", "Qd"]}   # 席4 ハイカード / 席5 ツーペア / 席6 フラッシュ


def _board(tb: _Table, cards: list[str]) -> None:
    for index, card in enumerate(cards, start=len(tb.t._board_positions) + 1):   # noqa: SLF001
        ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                       timestamp=tb.now, raw_tag_id=card, board_index=index)
        tb.recorder.record(ev)
        tb.t._process_rfid_event(ev)         # noqa: SLF001
    tb.tick(tb.now + 1.0)


def _to_river(tb: _Table, holes=HOLES, checks_on_river: bool = True) -> None:
    tb.deal(holes)
    for text in ("コール", "コール", "チェック"):
        tb.say(text)
        tb.tick(tb.now + 1.0)
    _board(tb, FLUSH_BOARD[:3])
    for _ in range(3):
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
    _board(tb, FLUSH_BOARD[3:4])
    for _ in range(3):
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
    _board(tb, FLUSH_BOARD[4:])
    if checks_on_river:
        for _ in range(3):
            tb.say("チェック")
            tb.tick(tb.now + 1.0)


class TestShowdown:
    """3 人: 席4 ハイカード / 席5 ツーペア / 席6 フラッシュ。見せる順は 4 → 5 → 6。"""

    def test_each_show_is_announced_and_the_best_hand_wins(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("ハイカード")
        assert tb.hands == [] and tb.notices[-1] == "席4 が見せました（ハイカード）"
        tb.say("ツーペア")
        assert tb.hands == []
        tb.say("フラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source, hand.announced_hand) == (6, "cards", "Flush")
        assert not hand.review_required
        assert [(s["seat"], s.get("announced")) for s in hand.to_dict()["showdown"]] == [
            (4, "High card"), (5, "Two pair"), (6, "Flush")]

    def test_folds_after_a_show_are_the_next_players(self, tmp_path):
        # 席4 が見せたあと、席5・席6 が見せずにマック（席6 はフラッシュ = まれに勝っている手をマック）
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("ハイカード")
        tb.say("フォールド")
        assert tb.hands == [] and tb.t._showdown_mucks == [5]   # noqa: SLF001
        tb.say("フォールド")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (4, "fold")
        assert [(a.seat, a.action, a.street) for a in hand.actions[-2:]] == [
            (5, "fold", "showdown"), (6, "fold", "showdown")]
        assert "mucked_stronger_hand" in hand.actions[-1].reason and hand.review_required

    def test_nobody_mucks_for_a_while_then_the_cards_decide(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("ハイカード")
        tb.tick(tb.now + SHOWDOWN_MUCK_SEC - 1.0)
        assert tb.hands == []
        tb.tick(tb.now + 1.5)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards")
        assert any(isinstance(e, RFIDEvent) and e.kind == "showdown_end" for e in tb.recorder.events)

    def test_no_hand_name_and_no_muck_also_decides_by_the_cards(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.tick(tb.now + SHOWDOWN_MUCK_SEC + 0.5)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards")

    def test_it_waits_for_speech_said_before_the_deadline(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("ハイカード")
        spoken = tb.now + 2.0
        tb.speech_since = spoken                     # 「フォールド」を認識中
        tb.tick(tb.now + SHOWDOWN_MUCK_SEC + 5.0)
        assert tb.hands == []
        tb.speech_since = None
        tb.say("フォールド", spoken_at=spoken)
        assert tb.hands == [] and tb.t._showdown_mucks == [5]   # noqa: SLF001

    def test_a_hand_name_that_only_one_unshown_seat_has_says_who_showed(self, tmp_path):
        # 最初の役名が席6 の手（フラッシュ）: 席6 が見せた。そのあとの「フォールド」は席4（まだ見せていない先頭）
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("フラッシュ")
        assert tb.notices[-1] == "席6 が見せました（フラッシュ）"
        tb.say("フォールド")
        assert tb.t._showdown_mucks == [4]           # noqa: SLF001

    def test_a_hand_name_that_differs_from_the_cards_is_flagged(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        for name in ("ハイカード", "ツーペア", "ツーペア"):      # 席6 は手札ではフラッシュ
            tb.say(name)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards") and hand.review_required
        assert any("席6 の役名「ツーペア」が手札の判定（フラッシュ）と違います" in n for n in tb.notices)

    def test_the_hand_names_decide_when_they_disagree_with_the_cards(self, tmp_path):
        # 役名: 席4 ハイカード / 席5 ストレート / 席6 ツーペア → 役名では席5 の勝ち（札の読み違いの疑い）
        tb = _Table(tmp_path)
        _to_river(tb)
        for name in ("ハイカード", "ストレート", "ツーペア"):
            tb.say(name)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (5, "announced")
        assert hand.review_required and hand.pot_total == 600
        assert any("席5 の勝ちにします" in n for n in tb.notices)

    def test_an_unreadable_seat_is_resolved_by_the_hand_names(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb, holes={4: HOLES[4], 5: HOLES[5], 6: ["Kd"]})    # 席6 の札は 1 枚しか読めていない
        for name in ("ハイカード", "ツーペア", "フラッシュ"):
            tb.say(name)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "announced") and hand.review_required

    def test_a_hand_name_closes_the_betting(self, tmp_path):
        # リバーのチェックが聞き取れないまま役名が言われた = ベッティングは終わっている
        tb = _Table(tmp_path)
        _to_river(tb, checks_on_river=False)
        tb.say("フラッシュ")
        assert tb.hands == []                        # 席6 が見せた。ほかはまだ
        tb.tick(tb.now + SHOWDOWN_MUCK_SEC + 0.5)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards") and hand.review_required
        assert [a.seat for a in hand.actions if a.street == "river"] == [4, 5, 6]
        assert all(a.action == "check" and a.reason == "implied_before_showdown"
                   for a in hand.actions if a.street == "river")

    def test_a_hand_name_before_the_river_does_not_end_the_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HOLES)
        tb.say("フラッシュ")
        assert tb.hands == [] and any("ベッティングの途中" in n for n in tb.notices)

    def test_a_late_hand_name_that_differs_is_reported(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        for name in ("ハイカード", "ツーペア", "フラッシュ"):
            tb.say(name)
        tb.say("ストレート")
        assert len(tb.hands) == 1
        assert any("役名「ストレート」が判定（フラッシュ）と違います" in n for n in tb.notices)

    def test_a_late_hand_name_of_a_shown_hand_is_quiet(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        for name in ("ハイカード", "ツーペア", "フラッシュ"):
            tb.say(name)
        before = len(tb.notices)
        tb.say("ツーペア")
        assert len(tb.notices) == before

    def test_replay_reproduces_the_decision_by_time(self, tmp_path):
        from core.game_state import PlayerState
        from integration.replay import replay_events

        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("ハイカード")
        tb.tick(tb.now + SHOWDOWN_MUCK_SEC + 0.5)
        (live,) = tb.hands
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert [(h.winner_seat, h.winner_source) for h in replayed] == [(live.winner_seat, live.winner_source)]


class TestHeadsUp:
    """2 人: 席4（BB, アウトオブポジション）ハイカード / 席6（ボタン = SB）フラッシュ。"""

    HOLES = {4: HOLES[4], 6: HOLES[6]}

    def _to_river(self, tb: _Table) -> None:
        tb.deal(self.HOLES)
        for text in ("コール", "チェック"):
            tb.say(text)
            tb.tick(tb.now + 1.0)
        for street in (FLUSH_BOARD[:3], FLUSH_BOARD[3:4], FLUSH_BOARD[4:]):
            _board(tb, street)
            tb.say("チェック チェック")
            tb.tick(tb.now + 1.0)

    def test_in_position_mucks_a_winning_hand_after_the_show(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        self._to_river(tb)
        tb.say("ハイカード")                         # 席4 が見せた
        tb.say("フォールド")                         # 席6 がマック（手札はフラッシュ）
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (4, "fold") and hand.review_required
        assert (hand.actions[-1].seat, hand.actions[-1].street) == (6, "showdown")

    def test_both_show(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        self._to_river(tb)
        tb.say("ハイカード")
        tb.say("フラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards") and not hand.review_required

    def test_a_fold_before_any_show_is_out_of_position(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        self._to_river(tb)
        tb.say("フォールド")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "fold")

    @pytest.mark.parametrize("shown", ["クイーンシックス", "クイーンハイ"])
    def test_the_hole_card_name_is_the_show_before_the_muck(self, tmp_path, shown):
        # 店舗の言い方（オーナー 2026-10-01）: 役の無い手は手札の名前。読めないと「フォールド」が見せた席のマックになる
        tb = _Table(tmp_path, seats=(4, 6))
        self._to_river(tb)
        tb.say(shown)                                 # 席4（Q6）が見せた
        tb.say("フォールド")                          # 席6 がマック
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (4, "fold")
        assert (hand.actions[-1].seat, hand.actions[-1].action) == (6, "fold")

    def test_a_hit_is_a_pair(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        self.HOLES = {4: ["Jc", "Ac"], 6: HOLES[6]}   # 席4 ジャックのワンペア（ボードの J と組）
        self._to_river(tb)
        tb.say("ジャックヒット")
        tb.say("キングハイフラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards") and not hand.review_required
        assert [(s["seat"], s.get("announced")) for s in hand.to_dict()["showdown"]] == [
            (4, "One pair"), (6, "Flush")]


class TestHits:
    """「Nヒット」= 手札の N とボードの N で組にした（ワンペア）。ボードにペアがあれば、それを無視して N だけを言う
    こともある = 2 ペア（オーナー 2026-10-06）。言った N で、見せた席を手札と突き合わせる。"""

    def _river(self, tb: _Table, holes: dict, board: list[str]) -> None:
        tb.deal(holes)
        tb.say("コール チェック" if len(holes) == 2 else "コール コール チェック")
        for cards in (board[:3], board[3:4], board[4:]):
            _board(tb, cards)
            tb.say(" ".join(["チェック"] * len(holes)))
            tb.tick(tb.now + 1.0)

    def test_a_hit_with_the_board_pair_ignored_is_two_pair(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        self._river(tb, {4: ["Jc", "Ac"], 6: ["Kd", "Qd"]}, ["Jd", "9d", "3d", "9c", "2s"])
        tb.say("ジャックヒット")                       # 席4 = ジャックと 9 の 2 ペア（ボードの 9 のペアは言わない）
        tb.say("キングハイフラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards") and not hand.review_required
        assert not any("違います" in n for n in tb.notices)

    def test_the_rank_says_who_showed(self, tmp_path):
        # 席4（3 のワンペア）より先に席5（5 のワンペア）が見せた: 役の名前だけでは 2 人に合う
        tb = _Table(tmp_path)
        self._river(tb, {4: ["3c", "Ks"], 5: ["5c", "Ah"], 6: ["Kd", "Qd"]}, FLUSH_BOARD)
        tb.say("5ヒット")
        assert tb.t._showdown_shown == {5: "One pair"}   # noqa: SLF001
        tb.say("3ヒット")
        tb.say("フラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards") and not hand.review_required


class TestHandNameAfterTheLastFold:
    """ベットに向き合った人の札が離れて最後のフォールドを待っているときの役名。ショーダウンではアウトオブポジションが
    先に見せる（オーナー 2026-09-30）ので、先に見せる人が見せる前に離れた札は、見せたのではなく降りた = 役名は降りた
    手のこと（見せながら降りた・ディーラーが降りた手を見て言った）。店舗 2026-10-06 e82f5005 ハンド 15:「5ヒット」で
    フォールドを取り消して、降りた人の勝ちにしていた。"""

    HOLES = {4: ["Kc", "Qs"], 6: ["5c", "Ah"]}       # 席4（BB = アウトオブポジション）キングハイ / 席6 5 のワンペア

    def _river(self, tb: _Table) -> None:
        tb.deal(self.HOLES)
        tb.say("コール チェック")
        for cards in (FLUSH_BOARD[:3], FLUSH_BOARD[3:4], FLUSH_BOARD[4:]):
            _board(tb, cards)
            if len(tb.t._board_positions) < 5:        # noqa: SLF001
                tb.say("チェック チェック")
                tb.tick(tb.now + 1.0)

    def test_in_position_left_first_so_the_name_is_the_folded_hand(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        self._river(tb)
        tb.say("ベット 1000")                          # 席4
        tb.lift(6)                                     # 席6 は降りた（札を前へ）
        tb.tick(tb.now + 3.5)
        assert tb.t._foldout_pending is not None       # noqa: SLF001
        tb.say("5ヒット")                              # 降りた手を見て言った
        assert tb.hands == [] and "降りたまま" in tb.notices[-1]
        tb.tick(tb.now + 11.0)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (4, "fold") and hand.review_required
        assert [(a.seat, a.action) for a in hand.actions if a.street == "river"] == [(4, "bet"), (6, "fold")]

    def test_a_name_of_the_remaining_hand_is_a_show(self, tmp_path):
        # コールが聞き取れず、降りたとみた席6 の札が先に離れたが、ディーラーは残った席4 の手を言った = ショーダウン
        tb = _Table(tmp_path, seats=(4, 6))
        self._river(tb)
        tb.say("ベット 1000")
        tb.lift(6)
        tb.tick(tb.now + 3.5)
        tb.say("キングハイ")                           # 席4 の手
        tb.say("5ヒット")                              # 席6 の手
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards")
        assert [(a.seat, a.action) for a in hand.actions if a.street == "river"] == [(4, "bet"), (6, "call")]

    def test_out_of_position_left_first_so_the_name_is_a_show(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        self._river(tb)
        tb.say("チェック")                             # 席4
        tb.say("ベット 1000")                          # 席6
        tb.lift(4)                                     # 席4 はコールして見せた（コールは聞き取れなかった）
        tb.tick(tb.now + 3.5)
        tb.say("キングハイ")
        tb.say("5ヒット")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards")
        assert [(a.seat, a.action) for a in hand.actions if a.street == "river"] == [
            (4, "check"), (6, "bet"), (4, "call")]


class TestAllInBeforeTheRiver:
    """オールインで手を開いたとき、手札の名前（「エースキング」「クイーンクイーン」）が言われても、ボードが出きる
    までは役名で勝者を決めない（ボードで AK が勝つことがある）。"""

    @pytest.mark.parametrize("queens", ["クイーンクイーン", "クイーンズ", "ポケットクイーンズ"])
    def test_the_board_decides_not_the_names(self, tmp_path, queens):
        # オールインで見せたら、その場で手札の名前を言う（オーナー 2026-10-01）
        tb = _Table(tmp_path, seats=(4, 6))
        tb.deal({4: ["As", "Kh"], 6: ["Qc", "Qh"]})
        tb.say("オールイン")                          # 席6（ボタン = SB）
        tb.tick(tb.now + 1.0)
        tb.say("コール")                              # 席4
        tb.tick(tb.now + 1.0)
        tb.say("エースキング")
        tb.say(queens)
        assert tb.hands == []
        for street in (["Ad", "7c", "3s"], ["9h"], ["2d"]):
            _board(tb, street)
        tb.tick(tb.now + SHOWDOWN_MUCK_SEC + 1.0)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (4, "cards")
        assert not hand.review_required
        assert all("announced" not in s for s in hand.to_dict()["showdown"])   # 開いたときの名前は役名ではない

    def test_the_hand_name_after_the_river_decides_at_once(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 6))
        tb.deal({4: ["As", "Kh"], 6: ["Qc", "Qh"]})
        tb.say("オールイン")
        tb.tick(tb.now + 1.0)
        tb.say("コール")
        tb.tick(tb.now + 1.0)
        tb.say("エースキング")
        tb.say("クイーンクイーン")
        for street in (["Ad", "Kc", "3s"], ["9h"], ["2d"]):
            _board(tb, street)
        tb.say("エースキングツーペア")                # 勝った席の役（待たずに決める）
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (4, "cards") and not hand.review_required
        assert [(s["seat"], s.get("announced")) for s in hand.to_dict()["showdown"]] == [(4, "Two pair"), (6, None)]
