# Worklog: magical-ritchie（R系 reconstruction）統合 + 番号衝突解消 + camera 整合

## Date

2026-06-05

## Scope / Task

ユーザーの「本ブランチ = `claude/magical-ritchie-nX58s`」という訂正を受け、前タスクで
`claude/dazzling-brown-COUkU`（= v4 base + sharp-bardeen + camera 削除）に作った状態へ、
兄弟ブランチ `claude/magical-ritchie-nX58s` の R系作業（rules-aware hand reconstruction）を
統合する。camera は削除維持し、magical 側を audio+RFID の 2 ソースへ改修する。

## Goal

- magical の R1（event recorder）/ R2（pokerkit backend）/ 再構築契約・ADR・issue を取り込む。
- magical と sharp の **番号衝突**（ADR-0009, ISSUE-0008）を解消する。
- camera 削除（ADR-0011）を維持し、magical の `CameraEvent` 依存を除去して破綻させない。
- 全テスト green + 全 conflict marker 解消 + docs 整合。

## Changed Files

### マージ + 競合解決

- `git merge --no-ff origin/claude/magical-ritchie-nX58s`（共通祖先 `bc97470`）。
- `integration/engine.py` — event_recorder DI（`_record`）と session レイヤ DI を両立。
  `_record` の型から `CameraEvent` を除去、magical の `_drain_camera_queue` は取り込まない。
- `main.py` — `_init_session_layer`（sharp）/ `_make_event_recorder` `_make_game_state`（magical）を
  3 関数とも保持。IntegrationThread に session_* と event_recorder を両方 DI。
- `config_default.json` — `session_layer`(sharp) + `recording` `engine`(magical) を保持、camera 無し。
- `CLAUDE.md` / `CHANGELOG.md` / `docs/decision-log.md` — 両系列のエントリ/行を統合。

### camera 整合（ADR-0011 維持, magical 側を 2 ソース化）

- `output/event_recorder.py` — `CameraEvent` import / `RecordableEvent` / camera envelope branch を削除。
- `docs/contracts/schemas/reconstruction_event.schema.json` — `type` enum と camera if/then を除去。
- `docs/contracts/fixtures/reconstruction_event/valid-camera.json` — 削除。
  `invalid-unknown-field.json` — `type:"camera"` → 有効な `rfid` に（未知フィールドのみで invalid に）。
- `tests/test_event_recorder.py` — camera テスト除去、camera→rfid に差し替え。
- `docs/contracts/event-replay.md` / `hand-reconstruction.md` — camera 廃止注記 + schema/コード参照を 2 ソース化。

### 番号衝突解消

- ADR: 私の camera ADR `0009` → **`0011`**（magical の ADR-0009 pokerkit / ADR-0010 を温存）。
  `docs/adr/0011-remove-camera-vision-per-v4-spec.md` にリネーム + 本文タイトル + 参照を更新。
- ISSUE: sharp の viewer issue `0008` → **`0012`**（magical の issue 0008..0011 を温存）。
  `docs/issues/0012-session-viewer-enhancements.md` にリネーム + 参照（CLAUDE.md / CHANGELOG /
  decision-log / hand-integration.md / viewer worklog）を更新。
- `CLAUDE.md` 実装状況表 / ディレクトリツリーに R1（`output/event_recorder.py`）/
  R2（`core/poker_engine.py`）を追記。

## Expected Behavior

- `magical + sharp` の和集合。hand logger は flag 既定（session_layer off / recording off /
  engine=legacy）で従来挙動。camera は完全に存在しない（2 ソース）。
- R1 recorder は `recording.enabled=true` で opt-in、R2 pokerkit backend は `engine.backend=pokerkit`
  で opt-in（pokerkit 未導入時は legacy / テストは skip）。

## Implemented Behavior

期待どおり。共通祖先 `bc97470` からの 3-way マージで code 5 ファイルが衝突。すべて
「両機能を保持しつつ camera を除く」方針で解決。magical の `event_recorder` を 2 ソース化し、
`CameraEvent` 依存による ImportError を回避。

## Test Results

- 全 `.py` `py_compile` green。conflict marker 0（repo 全体）。camera コード参照 0。
- `pytest tests/` — **209 passed, 10 skipped**。
  - skip 10 = `tests/test_poker_engine.py`（pokerkit 未インストール, importorskip。preview backend）。
  - camera 削除影響（`test_phase7` / `test_integration` / `test_event_recorder`）含め green。

## Mismatches Found During Testing

None。skip は pokerkit 依存のみで、私の変更起因ではない。

## Fixes Applied

- `_record` 型ヒント・recorder envelope から `CameraEvent` を除去（camera 削除との整合）。
- `invalid-unknown-field` fixture の type を camera→rfid に（camera が enum から消えたため、
  「未知フィールドのみで invalid」のテスト意図を維持）。
- ADR/ISSUE 番号衝突を renumber（camera→ADR-0011, viewer→ISSUE-0012）。magical/pokerkit 側の
  番号（ADR-0009/0010, ISSUE-0008..0011）は温存し参照を取り違えないよう個別に判定。

## Remaining Gaps / Out-of-Scope

- [ ] R3（actor 推定 / corrections / fusion）以降は magical でも planned。未着手。
- [ ] pokerkit backend は preview（default-off）。`pip install pokerkit` 環境での実走は未検証
      （本環境は未インストールで skip）。
- [ ] hand-reconstruction.md / event-replay.md の深い設計 prose に残る camera 記述は historical として
      冒頭注記で flag（全文リライトはしない）。

## Related ADRs

- `docs/adr/0011-remove-camera-vision-per-v4-spec.md`（camera 削除, 本統合で renumber）。
- `docs/adr/0009-pokerkit-live-rules-authority.md` / `0010-contract-first-hand-core-record-replay.md`
  （magical 由来, 取り込み）。

## Related Issues

- `docs/issues/0012-session-viewer-enhancements.md`（sharp 由来, renumber）。
- `docs/issues/0008-0011`（magical 由来 reconstruction, 取り込み）。

## Related Commits

- `<merge-commit>` — merge magical-ritchie (R-series) + camera 整合 + 番号衝突解消。
