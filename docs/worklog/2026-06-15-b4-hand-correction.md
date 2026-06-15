# Worklog: ハンド訂正（B4 / ADR-0036）

## Date

2026-06-15

## Scope / Task

v1.0 ローンチレビュー B4。音声は自動記録のまま、誤認識の訂正を **iPad などのアプリ（staff 操作）**で行えるよう
にする。hand log が write-once だった問題を、append-only オーバーレイで解消する。

## Goal

元 hand log を mutate せず、訂正レコードを別ストアに append し、read 時に重ねた訂正済みビューを viewer/mobile が
返す。元の ASR 記録は監査・再学習のため保持。認可は staff。

## Changed Files

- `core/hand_correction.py`（新）: `HandCorrection` dataclass + `apply_hand_corrections`（read 時オーバーレイ。
  対象 `(session_id, hand_id, action_index)`、field=action/amount/winner_seat、元値 `_original` 保持・
  `corrected` 付与・`needs_review` 解除・hand に `_corrections` 監査痕。元 dict 不変、範囲外/未知 field は無視）。
- `core/hand_correction_repository.py`（新）: append-only ストア（`hand_corrections.json`, atomic+fsync,
  RLock, 破損退避）+ field/value validation。
- `api/read_models.py`: `get_hand` / `list_player_hands` に `correction_repo` を追加しオーバーレイ適用。
- `api/server.py`: `correction_repo` DI + `POST /api/staff/sessions/{sid}/hands/{hid}/corrections`
  （staff write, hand 存在 + action_index 範囲チェック, 400 `invalid_correction` / 404 `not_found`）+
  hand 読み取り endpoint に correction_repo を結線。
- `api/client.py`: `add_hand_correction`。
- mobile: `types.ts`（`HandCorrection`/`HandCorrectionInput`）/ `repository.ts`（`addHandCorrection`）/
  `httpRepository.ts`（staff token + staff POST）/ `mockRepository.ts`（store + overlay 適用）/
  `mockRepository.test.ts`（2 件）。
- `.gitignore`: `hand_corrections.json`。
- docs: ADR-0036 / decision-log / error-shapes（`invalid_correction`）/ viewer-api / CLAUDE.md / CHANGELOG /
  review doc。

## Key Decisions（ADR-0036）

- **append-only オーバーレイ**（元 hand log を mutate しない）→ live hand-logger プロセスの hand log write と
  別ファイル（`hand_corrections.json`）で競合せず、元記録を保持（監査・再学習）。ledger の append-only 思想と一貫。
- 対象キーは **action_index**（アクションに安定 ID が無いため hand 内の位置）。
- 認可は **staff**（権威的編集）。player は閲覧のみ。
- 訂正可能 field は v1.0 で action/amount/winner_seat に限定（board/hole/seat 帰属は将来）。

## Test Results

- `pytest tests/test_hand_correction.py tests/test_viewer_api_correction.py -q` — 13 passed。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **655 passed, 0 skipped**（既存 642 + 13）。`ruff` clean。
- mobile: `npm run typecheck` clean + `npm test` 13 passed（+2 correction）。

## Mismatches Found During Testing

- API E2E テストで `Session.path` を誤参照 → `log_dir` を env に持たせて修正（コードではなくテストの誤り）。
- mobile typecheck で `ActionRecord as Record<...>` が型エラー → `as unknown as Record<...>` に修正。

## Remaining Gaps / 残課題

- **iPad 訂正 UI 画面**（mobile, 反復実装）。data path（repo/型/overlay）は整備済み。
- **PHH export へのオーバーレイ適用**（現状 export は元記録のまま）。
- 訂正の取り消し（unwind）、board/hole/seat 帰属の訂正。
- B8（実機 Phase H E2E, 来週）/ B5・hosted（SaaS フェーズ）。

## Related

- ADR-0036 / ADR-0009（再構築・needs_review）/ ADR-0016（append-only）/ ADR-0021（staff write）/
  ADR-0017（viewer/mobile）/ ISSUE-0011（hand schema additionalProperties）/ v1.0 ローンチレビュー
