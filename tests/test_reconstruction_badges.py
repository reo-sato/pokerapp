"""tests/test_reconstruction_badges.py

Phase 4-C2: gui.reconstruction_badges の純粋ユニットテスト。
``customtkinter`` 不要 (GUI 描画を mock せずに helper の判定ロジックだけ検証する)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from gui.reconstruction_badges import (
    ReconstructionBadgeState,
    format_history_line,
    summarize_reconstruction,
)


# ────────────────────────────────────────────────────────────────────────────
# 軽量 fake (HandReconstructionResult duck-typed)
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeResult:
    """`HandReconstructionResult` の duck-typed テスト stub。

    summarize_reconstruction が getattr で読む field だけ持てば十分。
    """

    needs_review: bool = False
    reason: str = ""
    diff: Optional[dict] = None
    summary: Optional[Any] = None
    bootstrap_source: Optional[str] = None
    bootstrap_meta: Optional[dict] = field(default_factory=lambda: None)


def _ok_result(**overrides) -> _FakeResult:
    base = _FakeResult(
        needs_review=False,
        reason="reconstructed_no_diff",
        diff=None,
        summary=object(),               # non-None で OK
        bootstrap_source="online_summary",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _review_result(diff: dict, **overrides) -> _FakeResult:
    base = _FakeResult(
        needs_review=True,
        reason="reconstructed_with_diff",
        diff=diff,
        summary=object(),
        bootstrap_source="raw",
        bootstrap_meta={"button_inferred": True},
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _skipped_result(**overrides) -> _FakeResult:
    base = _FakeResult(
        needs_review=False,
        reason="reconstruction_skipped",
        diff=None,
        summary=None,
        bootstrap_source=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ────────────────────────────────────────────────────────────────────────────
# summarize_reconstruction: status 判定
# ────────────────────────────────────────────────────────────────────────────


class TestStatusDecision:
    def test_none_result_is_skipped(self) -> None:
        state = summarize_reconstruction(None)
        assert state.status == "skipped"
        assert state.show_raw_badge is False
        assert state.reason is None
        assert state.diff_fields == []

    def test_summary_none_yields_skipped(self) -> None:
        state = summarize_reconstruction(_skipped_result())
        assert state.status == "skipped"
        # reason は保持する (デバッグ用)
        assert state.reason == "reconstruction_skipped"

    def test_reason_reconstruction_skipped_yields_skipped(self) -> None:
        # summary が non-None でも reason が skipped なら skipped 優先
        result = _FakeResult(
            needs_review=False, reason="reconstruction_skipped",
            summary=object(), bootstrap_source=None,
        )
        state = summarize_reconstruction(result)
        assert state.status == "skipped"

    def test_needs_review_true_yields_review(self) -> None:
        result = _review_result(diff={"resolution_type": {}})
        state = summarize_reconstruction(result)
        assert state.status == "review"

    def test_reason_reconstructed_with_diff_yields_review(self) -> None:
        # needs_review が立ってなくても reason だけで review に倒れる
        result = _FakeResult(
            needs_review=False, reason="reconstructed_with_diff",
            diff={"pot_total": {}}, summary=object(),
            bootstrap_source="online_summary",
        )
        state = summarize_reconstruction(result)
        assert state.status == "review"

    def test_otherwise_yields_ok(self) -> None:
        state = summarize_reconstruction(_ok_result())
        assert state.status == "ok"


# ────────────────────────────────────────────────────────────────────────────
# summarize_reconstruction: RAW バッジ + diff_fields + button_inferred
# ────────────────────────────────────────────────────────────────────────────


class TestRawBadge:
    def test_raw_bootstrap_sets_show_raw_badge(self) -> None:
        state = summarize_reconstruction(_review_result(diff={"actions": {}}))
        assert state.bootstrap_source == "raw"
        assert state.show_raw_badge is True

    def test_online_summary_bootstrap_no_raw_badge(self) -> None:
        state = summarize_reconstruction(_ok_result())
        assert state.bootstrap_source == "online_summary"
        assert state.show_raw_badge is False

    def test_initial_state_bootstrap_no_raw_badge(self) -> None:
        state = summarize_reconstruction(_ok_result(bootstrap_source="initial_state"))
        assert state.show_raw_badge is False

    def test_skipped_no_raw_badge_even_if_source_is_raw_string(self) -> None:
        # skipped 経路で bootstrap_source は通常 None だが、防御的に確認
        result = _skipped_result(bootstrap_source="raw")
        state = summarize_reconstruction(result)
        assert state.status == "skipped"
        # skipped でも raw_badge は単独で計算される設計だが、
        # フィールド分岐: skipped path では show_raw_badge は default(False) のまま
        assert state.show_raw_badge is False


class TestDiffFields:
    def test_diff_keys_sorted_alphabetically(self) -> None:
        state = summarize_reconstruction(_review_result(diff={
            "seat_payouts":    {},
            "actions":         {},
            "resolution_type": {},
        }))
        assert state.diff_fields == ["actions", "resolution_type", "seat_payouts"]

    def test_none_diff_yields_empty_list(self) -> None:
        state = summarize_reconstruction(_ok_result(diff=None))
        assert state.diff_fields == []

    def test_empty_diff_dict_yields_empty_list(self) -> None:
        state = summarize_reconstruction(_ok_result(diff={}))
        assert state.diff_fields == []

    def test_non_dict_diff_yields_empty_list(self) -> None:
        # 防御的: diff 型が dict 以外 (壊れた JSON 等) → 空 list で破綻しない
        state = summarize_reconstruction(_ok_result(diff="bogus"))  # type: ignore[arg-type]
        assert state.diff_fields == []


class TestButtonInferredFlag:
    def test_button_inferred_propagated_from_meta(self) -> None:
        state = summarize_reconstruction(_review_result(
            diff={"resolution_type": {}},
        ))
        # _review_result の default は button_inferred=True
        assert state.button_inferred is True

    def test_no_meta_yields_false(self) -> None:
        state = summarize_reconstruction(_ok_result(bootstrap_meta=None))
        assert state.button_inferred is False

    def test_meta_without_button_inferred_key_yields_false(self) -> None:
        state = summarize_reconstruction(_ok_result(
            bootstrap_meta={"source": "online_summary"},
        ))
        assert state.button_inferred is False


# ────────────────────────────────────────────────────────────────────────────
# Phase 5-A: patch_fields
# ────────────────────────────────────────────────────────────────────────────


class TestPatchFields:
    def test_patch_fields_empty_when_no_proposal(self) -> None:
        # _ok_result の default は patch_proposal なし (= None)
        state = summarize_reconstruction(_ok_result())
        assert state.patch_fields == []

    def test_patch_fields_from_dataclass_proposal(self) -> None:
        """live hook 経由: ``HandPatchProposal`` dataclass を直接渡す。"""
        from core.patch_proposal import FieldPatch, HandPatchProposal
        proposal = HandPatchProposal(
            hand_id=2, can_patch_automatically=False,
            fields=[
                FieldPatch(field="resolution_type",
                           online="fold_win", offline="showdown"),
                FieldPatch(field="seat_payouts",
                           online={1: 300}, offline={2: 300}),
            ],
        )
        result = _review_result(
            diff={"resolution_type": {}, "seat_payouts": {}},
        )
        result.patch_proposal = proposal       # type: ignore[attr-defined]
        state = summarize_reconstruction(result)
        assert state.patch_fields == ["resolution_type", "seat_payouts"]

    def test_patch_fields_from_dict_proposal(self) -> None:
        """JSONL 経由: ``patch_proposal`` が dict (asdict 後) でも読める。"""
        result = _review_result(diff={"resolution_type": {}})
        result.patch_proposal = {                # type: ignore[attr-defined]
            "hand_id": 1, "can_patch_automatically": False,
            "fields": [
                {"field": "resolution_type", "online": "fold_win",
                 "offline": "showdown", "note": "..."},
            ],
        }
        state = summarize_reconstruction(result)
        assert state.patch_fields == ["resolution_type"]

    def test_patch_fields_skipped_when_proposal_malformed(self) -> None:
        """proposal が壊れていても落ちず空 list で抜ける。"""
        result = _review_result(diff={"resolution_type": {}})
        result.patch_proposal = "bogus"          # type: ignore[attr-defined]
        state = summarize_reconstruction(result)
        assert state.patch_fields == []


# ────────────────────────────────────────────────────────────────────────────
# format_history_line: 出力テキスト
# ────────────────────────────────────────────────────────────────────────────


class TestFormatHistoryLine:
    def test_ok_line(self) -> None:
        state = ReconstructionBadgeState(
            status="ok", show_raw_badge=False,
            reason="reconstructed_no_diff",
            bootstrap_source="online_summary", diff_fields=[],
        )
        line = format_history_line(1, winner_seat=2, pot_total=300, badge_state=state)
        assert "#1" in line
        assert "winner=seat2" in line
        assert "pot=300" in line
        assert "[OK]" in line
        assert "[RAW]" not in line
        assert "bootstrap=online_summary" in line
        assert "diff=" not in line       # OK には diff= 出さない (empty)

    def test_review_with_raw_and_diff(self) -> None:
        state = ReconstructionBadgeState(
            status="review", show_raw_badge=True,
            reason="reconstructed_with_diff",
            bootstrap_source="raw",
            diff_fields=["resolution_type", "seat_payouts"],
            button_inferred=True,
        )
        line = format_history_line(2, winner_seat=1, pot_total=600, badge_state=state)
        assert "#2" in line
        assert "[REVIEW]" in line
        assert "[RAW]" in line
        assert "bootstrap=raw" in line
        assert "diff=resolution_type,seat_payouts" in line
        assert "button inferred" in line

    def test_skipped_with_unknown_winner_and_pot(self) -> None:
        state = ReconstructionBadgeState(
            status="skipped", show_raw_badge=False,
            reason="reconstruction_skipped",
            bootstrap_source=None, diff_fields=[],
        )
        line = format_history_line(3, winner_seat=None, pot_total=None, badge_state=state)
        assert "#3" in line
        assert "winner=?" in line
        assert "pot=?" in line
        assert "[SKIPPED]" in line
        assert "bootstrap=None" in line
