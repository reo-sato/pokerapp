# Worklog: Phase A — コア堅牢化（RFID 未解決カードの review 伝播）

## Date

2026-06-05

## Scope / Task

v1 リリーストラックの Phase A（GitHub issue #5）。小負債のうち **A1**（RFID カード未解決時に
`needs_review` をハンドに伝播）を実装し、**A2**（faster-whisper 未導入時の起動）を検証する。
統合ブランチ `v1-integration`、作業ブランチ `claude/phaseA-hardening`。

## Goal

- A1: board / seat RFID イベントの `card` がカードマスター未解決（空文字）のままハンドが
  進んだ場合、その `HandSummary.review_required` が `True` になる。従来は `logger.warning`
  のみで、オペレーターが検出失敗に気付けなかった。
- A2: faster-whisper 未導入環境でアプリ起動がクラッシュしないことを確認する。
- 既存テストの回帰なし。

## Changed Files

- `integration/engine.py` — `IntegrationThread` に `_hand_needs_review` フラグを additive 追加。
  board(`_handle_board_rfid`) / seat(`_handle_seat_rfid`) の card 未解決分岐でフラグを立て、
  `_start_new_hand` でリセット、`_finalize_hand` の `review_required` 算出に OR で合成。
- `tests/test_phase_a_hardening.py`（新規）— A1 の回帰テスト 4 件。

## Expected Behavior

- 未解決 board/seat RFID → そのハンドの `review_required=True`。
- 解決済みカード（valid）では `review_required` を立てない（誤検知ガード）。
- `new_hand` でフラグがリセットされ、後続ハンドに引きずらない。
- A2: `audio/recognizer.py` の `from faster_whisper import WhisperModel` は
  `WhisperTranscriber.__init__` の `try/except ImportError` 内にあり、未導入なら `_model=None`。

## Implemented Behavior

- 期待どおり。`_hand_needs_review` を `__init__` で `False` 初期化、card 未解決の 2 分岐で
  `True` 化、`_start_new_hand` で `False` リセット、`_finalize_hand` で
  `self._hand_needs_review or any(a.needs_review for a in ...)` として出力。
- A2: コードを精査した結果、`audio/recognizer.py` に **top-level の faster_whisper import は無い**
  （import は `__init__` の try 内のみ。line 193）。未導入でも `audio.recognizer` の import /
  `WhisperTranscriber()` 構築はクラッシュしない。**コード変更は不要**と判断（A3 = pokerkit
  `pots()`→HandSummary 連携は Phase F に委譲、本タスク対象外）。

## Test Results

- `python -m pytest tests/test_phase_a_hardening.py -v` — **4 passed**。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **192 passed, 10 skipped**
  （従来 188 + 新規 4。skip は pokerkit 未導入分で期待どおり）。回帰なし。

## Mismatches Found During Testing

- None observed.
- セットアップ上の発見: `v1-integration` を当初 default ブランチ `claude/poker-hand-logger-mPPkv`
  （古い状態、S1/S2/R1/R2 未含）から切ってしまっていた。最新作業の tip
  `claude/dazzling-wozniak-KDYsC` への fast-forward（非破壊）で是正済み。

## Fixes Applied

- `_hand_needs_review` フラグによる review 伝播（上記）。

## Remaining Gaps / Out-of-Scope

- [ ] A3（pokerkit `pots()`→`HandSummary` side-pot 連携）は Phase F（#8）で実施。
- [ ] 未解決理由の構造化（どの reader/tag が未解決か）の出力は今回 scope 外。必要なら follow-up。

## Related ADRs

- なし（小負債修正。挙動方針は CLAUDE.md「エラーハンドリング方針」§ needs_review 付与に整合）。

## Related Issues

- GitHub issue #5（Phase A）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
