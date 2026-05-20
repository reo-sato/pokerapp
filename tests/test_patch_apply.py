"""tests/test_patch_apply.py

Phase 5-G: ``core.patch_apply`` の pure helper のユニットテスト。

検証対象:
  - ``apply_patch_proposal_to_summary``: whitelist field のみ適用、元 summary を
    mutate しない、int key 正規化、proposal が dict 形でも動く
  - ``applicable_patch_fields``: ``PATCH_APPLY_FIELDS`` の subset を抽出
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.hand_log import HandSummary
from core.patch_apply import (
    PATCH_APPLY_FIELDS,
    FieldDiffView,
    applicable_patch_fields,
    apply_patch_proposal_to_summary,
    summarize_patch_proposal_for_view,
)
from core.patch_proposal import FieldPatch, HandPatchProposal


def _make_summary(**overrides) -> HandSummary:
    """テスト用に最小限の HandSummary を組み立てる。"""
    base = dict(
        hand_id=1, session_id="t",
        started_at="", ended_at="",
        blinds={"sb": 100, "bb": 200},
        board=[], board_source="",
        players=[],
        pot_total=300,
        winner_seat=1,
        actions=[],
        review_required=False,
        folded_seats=[], all_in_seats=[],
        resolution_status="final",
        resolution_type="fold_win",
        seat_payouts={1: 300},
    )
    base.update(overrides)
    return HandSummary(**base)


# ────────────────────────────────────────────────────────────────────────────
# whitelist 内の field は適用される
# ────────────────────────────────────────────────────────────────────────────


class TestWhitelistedFieldsApply:
    def test_resolution_type_applied(self) -> None:
        summary = _make_summary(resolution_type="fold_win")
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="resolution_type",
                                online="fold_win", offline="showdown")],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.resolution_type == "showdown"

    def test_seat_payouts_applied(self) -> None:
        summary = _make_summary(seat_payouts={1: 300})
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="seat_payouts",
                                online={1: 300}, offline={2: 600})],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.seat_payouts == {2: 600}

    def test_seat_payouts_int_key_normalization(self) -> None:
        """JSON round-trip で str key になっていても int 化する。"""
        summary = _make_summary()
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="seat_payouts",
                                online={"1": 300},
                                offline={"2": 600, "3": 400})],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        # int key 正規化
        assert patched.seat_payouts == {2: 600, 3: 400}
        assert all(isinstance(k, int) for k in patched.seat_payouts.keys())

    def test_showdown_revealed_cards_applied(self) -> None:
        summary = _make_summary()
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="showdown_revealed_cards",
                                online={}, offline={1: ["Ah", "Kh"]})],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.showdown_revealed_cards == {1: ["Ah", "Kh"]}

    def test_showdown_revealed_cards_int_key_normalization(self) -> None:
        summary = _make_summary()
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="showdown_revealed_cards",
                                online={}, offline={"3": ["Qs", "Qd"]})],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.showdown_revealed_cards == {3: ["Qs", "Qd"]}

    def test_blinds_applied(self) -> None:
        summary = _make_summary(blinds={"sb": 100, "bb": 200})
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="blinds",
                                online={"sb": 100, "bb": 200},
                                offline={"sb": 200, "bb": 400})],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.blinds == {"sb": 200, "bb": 400}

    def test_pots_in_whitelist_but_not_yet_in_practice(self) -> None:
        """``pots`` は whitelist には入っているが、現状の
        ``compute_patch_proposal`` / ``_apply_blind_mismatch_advisory`` は ``pots``
        を patch 対象にしていない (= future-proof な whitelist entry)。
        ただし手動で proposal に pots を入れた場合は apply される。
        """
        from core.hand_log import PotSettlement
        original_pots: list = []
        new_pots = [PotSettlement(amount=600, eligible_seats=[1, 2],
                                    winning_seats=[2], payouts={2: 600})]
        summary = _make_summary(pots=original_pots)
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="pots", online=original_pots, offline=new_pots)],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert len(patched.pots) == 1
        assert patched.pots[0].amount == 600
        assert patched.pots[0].payouts == {2: 600}


# ────────────────────────────────────────────────────────────────────────────
# whitelist 外の field は無視される
# ────────────────────────────────────────────────────────────────────────────


class TestNonWhitelistedFieldsIgnored:
    def test_winner_seat_ignored(self) -> None:
        summary = _make_summary(winner_seat=1)
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="winner_seat", online=1, offline=2)],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.winner_seat == 1   # 無視されて変わらない

    def test_pot_total_ignored(self) -> None:
        summary = _make_summary(pot_total=300)
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="pot_total", online=300, offline=600)],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.pot_total == 300

    def test_actions_ignored(self) -> None:
        summary = _make_summary(actions=[])
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="actions",
                                online=[], offline=[(1, "fold", 0)])],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.actions == []

    def test_mixed_whitelist_and_non_whitelist_partial_apply(self) -> None:
        """whitelist 内のものだけ反映、外は無視される。"""
        summary = _make_summary(resolution_type="fold_win", winner_seat=1,
                                pot_total=300, seat_payouts={1: 300})
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[
                FieldPatch(field="resolution_type",
                            online="fold_win", offline="showdown"),       # ✓
                FieldPatch(field="winner_seat", online=1, offline=2),       # ✗
                FieldPatch(field="pot_total", online=300, offline=600),     # ✗
                FieldPatch(field="seat_payouts",
                            online={1: 300}, offline={2: 600}),            # ✓
            ],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.resolution_type == "showdown"   # 適用
        assert patched.winner_seat == 1                 # 無視
        assert patched.pot_total == 300                 # 無視
        assert patched.seat_payouts == {2: 600}        # 適用


# ────────────────────────────────────────────────────────────────────────────
# 元 summary を mutate しない
# ────────────────────────────────────────────────────────────────────────────


class TestNoMutation:
    def test_original_summary_unchanged_simple(self) -> None:
        summary = _make_summary(resolution_type="fold_win")
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="resolution_type",
                                online="fold_win", offline="showdown")],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert summary.resolution_type == "fold_win"
        assert patched is not summary

    def test_original_summary_mutable_field_not_aliased(self) -> None:
        """seat_payouts は dict なので、apply 後に patched 側を mutate しても
        original には影響しないこと (deep copy セマンティクス)。
        """
        summary = _make_summary(seat_payouts={1: 300})
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="seat_payouts",
                                online={1: 300}, offline={2: 600})],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        # patched 側を mutate しても original は変わらない
        patched.seat_payouts[3] = 999
        assert 3 not in summary.seat_payouts
        assert summary.seat_payouts == {1: 300}


# ────────────────────────────────────────────────────────────────────────────
# proposal None / 不正形式 でも安全
# ────────────────────────────────────────────────────────────────────────────


class TestRobustness:
    def test_none_proposal_returns_deep_copy(self) -> None:
        summary = _make_summary()
        patched = apply_patch_proposal_to_summary(summary, None)
        assert patched is not summary
        assert patched.resolution_type == summary.resolution_type

    def test_dict_form_proposal_works(self) -> None:
        """JSONL から読み戻した dict 形 proposal でも apply できる。"""
        summary = _make_summary()
        proposal_dict = SimpleNamespace(
            fields=[
                {"field": "resolution_type",
                 "online": "fold_win",
                 "offline": "showdown"},
            ],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal_dict)
        assert patched.resolution_type == "showdown"

    def test_malformed_field_entry_is_skipped(self) -> None:
        """field 名が文字列でないなど壊れた entry は skip して他は適用する。"""
        summary = _make_summary(resolution_type="fold_win")
        proposal = SimpleNamespace(
            fields=[
                {"field": None, "offline": "ignored"},                       # 壊れ
                {"field": "unknown_field", "offline": "ignored"},            # 非 whitelist
                FieldPatch(field="resolution_type",
                            online="fold_win", offline="showdown"),         # 正常
            ],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.resolution_type == "showdown"

    def test_seat_payouts_non_dict_offline_skipped(self) -> None:
        """seat_payouts の offline が dict でない場合 skip。"""
        summary = _make_summary(seat_payouts={1: 300})
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="seat_payouts",
                                online={1: 300}, offline="not-a-dict")],
        )
        patched = apply_patch_proposal_to_summary(summary, proposal)
        assert patched.seat_payouts == {1: 300}  # 変わらない


# ────────────────────────────────────────────────────────────────────────────
# applicable_patch_fields
# ────────────────────────────────────────────────────────────────────────────


class TestApplicablePatchFields:
    def test_filters_to_whitelist_only(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[
                FieldPatch(field="resolution_type", online="a", offline="b"),
                FieldPatch(field="winner_seat", online=1, offline=2),
                FieldPatch(field="seat_payouts",
                            online={}, offline={1: 100}),
                FieldPatch(field="pot_total", online=0, offline=100),
            ],
        )
        names = applicable_patch_fields(proposal)
        # PATCHABLE_FIELDS の順 + whitelist filter
        assert names == ["resolution_type", "seat_payouts"]

    def test_none_proposal_returns_empty(self) -> None:
        assert applicable_patch_fields(None) == []

    def test_empty_fields_returns_empty(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False, fields=[],
        )
        assert applicable_patch_fields(proposal) == []

    def test_dict_form_proposal(self) -> None:
        proposal_dict = SimpleNamespace(
            fields=[
                {"field": "blinds",
                 "online": {"sb": 100, "bb": 200},
                 "offline": {"sb": 200, "bb": 400}},
                {"field": "actions", "online": [], "offline": []},
            ],
        )
        names = applicable_patch_fields(proposal_dict)
        assert names == ["blinds"]   # actions は whitelist 外


class TestWhitelistConstant:
    def test_whitelist_contents(self) -> None:
        """``PATCH_APPLY_FIELDS`` の中身が仕様通り。"""
        assert PATCH_APPLY_FIELDS == frozenset({
            "resolution_type",
            "seat_payouts",
            "pots",
            "showdown_revealed_cards",
            "blinds",
        })


# ────────────────────────────────────────────────────────────────────────────
# Phase 5-H: summarize_patch_proposal_for_view
# ────────────────────────────────────────────────────────────────────────────


class TestSummarizePatchProposalForView:
    """Phase 5-H: ``summarize_patch_proposal_for_view`` の挙動。"""

    def test_returns_empty_for_none_proposal(self) -> None:
        assert summarize_patch_proposal_for_view(None, None) == []

    def test_returns_empty_for_proposal_without_fields(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False, fields=[],
        )
        assert summarize_patch_proposal_for_view(None, proposal) == []

    def test_includes_all_fields_with_is_applicable_flag(self) -> None:
        """whitelist 内 / 外を ``is_applicable`` フラグで判別、順序は維持。"""
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[
                FieldPatch(field="resolution_type",
                            online="fold_win", offline="showdown"),
                FieldPatch(field="winner_seat", online=1, offline=2),
                FieldPatch(field="seat_payouts",
                            online={1: 300}, offline={2: 600}),
                FieldPatch(field="actions",
                            online=[(1, "fold", 0)], offline=[]),
            ],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        # field 順序は proposal.fields の順
        assert [v.field for v in views] == [
            "resolution_type", "winner_seat", "seat_payouts", "actions",
        ]
        # is_applicable は PATCH_APPLY_FIELDS にあるかで決まる
        assert views[0].is_applicable is True   # resolution_type
        assert views[1].is_applicable is False  # winner_seat
        assert views[2].is_applicable is True   # seat_payouts
        assert views[3].is_applicable is False  # actions

    def test_value_formatting_for_str(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="resolution_type",
                                online="fold_win", offline="showdown")],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        assert views[0].online_repr == "fold_win"
        assert views[0].offline_repr == "showdown"

    def test_value_formatting_for_int(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="winner_seat", online=1, offline=2)],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        assert views[0].online_repr == "1"
        assert views[0].offline_repr == "2"

    def test_value_formatting_for_seat_payouts_dict(self) -> None:
        """int キーの dict (seat_payouts) は昇順 sort で deterministic に整形。"""
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(
                field="seat_payouts",
                online={2: 600, 1: 300},   # 順不同
                offline={3: 100, 1: 50, 2: 150},
            )],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        # int キーは昇順 sort
        assert views[0].online_repr == "{1: 300, 2: 600}"
        assert views[0].offline_repr == "{1: 50, 2: 150, 3: 100}"

    def test_value_formatting_for_blinds_dict(self) -> None:
        """str キーの dict (blinds) は挿入順を維持 (= sb/bb の意味的順序)。"""
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(
                field="blinds",
                online={"sb": 100, "bb": 200},
                offline={"sb": 200, "bb": 400},
            )],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        assert views[0].online_repr == "{sb: 100, bb: 200}"
        assert views[0].offline_repr == "{sb: 200, bb: 400}"

    def test_value_formatting_for_list(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(
                field="actions",
                online=[(1, "fold", 0)],
                offline=[(2, "call", 200), (1, "fold", 0)],
            )],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        # list は [v1, v2] 形 (tuple は repr で表示)
        assert "(1, 'fold', 0)" in views[0].online_repr
        assert "(2, 'call', 200)" in views[0].offline_repr

    def test_value_formatting_for_none(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="resolution_type",
                                online=None, offline="showdown")],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        # None は em-dash で表示
        assert views[0].online_repr == "—"

    def test_note_is_propagated_when_present(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(
                field="resolution_type", online="fold_win", offline="showdown",
                note="resolution_type differs (online=fold_win, offline=showdown)",
            )],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        assert views[0].note is not None
        assert "differs" in views[0].note

    def test_note_none_when_missing(self) -> None:
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="resolution_type",
                                online="a", offline="b", note=None)],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        assert views[0].note is None

    def test_dict_form_proposal_works(self) -> None:
        """JSONL から読み戻した dict 形 proposal でも動く (duck-typed)。"""
        from types import SimpleNamespace
        proposal_dict = SimpleNamespace(
            fields=[
                {"field": "resolution_type",
                 "online": "fold_win", "offline": "showdown",
                 "note": "differs"},
                {"field": "seat_payouts",
                 "online": {"1": 300}, "offline": {"2": 600}},
            ],
        )
        views = summarize_patch_proposal_for_view(None, proposal_dict)
        assert len(views) == 2
        assert views[0].field == "resolution_type"
        assert views[0].is_applicable is True
        assert views[0].note == "differs"
        # str-keyed dict (JSON round-trip) も整形される
        assert "'1': 300" in views[1].online_repr or "1: 300" in views[1].online_repr

    def test_malformed_entry_is_skipped(self) -> None:
        """field 名が無い / 空文字列の entry は skip。残りは表示される。"""
        from types import SimpleNamespace
        proposal = SimpleNamespace(
            fields=[
                {"field": None, "online": "x", "offline": "y"},      # 壊れ
                {"field": "", "online": "x", "offline": "y"},         # 空
                FieldPatch(field="resolution_type",
                            online="fold_win", offline="showdown"),
            ],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        # 正常 entry だけが view に乗る
        assert len(views) == 1
        assert views[0].field == "resolution_type"

    def test_summary_argument_is_accepted_but_unused(self) -> None:
        """``summary`` 引数は将来用なので、何が渡されても挙動が変わらない。"""
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="resolution_type",
                                online="a", offline="b")],
        )
        from types import SimpleNamespace
        v1 = summarize_patch_proposal_for_view(None, proposal)
        v2 = summarize_patch_proposal_for_view(
            SimpleNamespace(resolution_type="x"), proposal,
        )
        # 同じ proposal なら summary に関わらず同じ
        assert [vv.field for vv in v1] == [vv.field for vv in v2]
        assert v1[0].online_repr == v2[0].online_repr
        assert v1[0].offline_repr == v2[0].offline_repr

    def test_returns_field_diff_view_instances(self) -> None:
        """戻り値の型が ``FieldDiffView`` であること。"""
        proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="seat_payouts",
                                online={1: 300}, offline={2: 600})],
        )
        views = summarize_patch_proposal_for_view(None, proposal)
        assert len(views) == 1
        assert isinstance(views[0], FieldDiffView)
