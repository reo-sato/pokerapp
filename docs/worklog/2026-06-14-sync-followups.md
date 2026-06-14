# Worklog: sync 後続（rename 伝播 / hand log union / auto-trigger, ADR-0032）

## Date

2026-06-14

## Scope / Task

ADR-0022 の残課題 3 点（player rename 伝播 / hand log の file-level union / 定期 auto-trigger）を
additive に実装する。実機不要。

## Goal

別端末の rename を収束させ、hand log も sync 対象にし、定期同期の純粋スケジューラを用意する。
すべて ADR-0022 の可換・冪等・収束を維持し、既定挙動は不変。

## Changed Files

- `core/player.py`: `Player.updated_at`（to_dict は created_at で既定埋め、from_dict 後方互換）。
- `core/player_repository.py`: create で updated_at=created_at、rename で updated_at=now。
- `docs/contracts/schemas/player.schema.json`: `1.1`→`1.2`（optional `updated_at` additive）。
- `core/sync.py`: `_resolve_player` を display=`updated_at` LWW × `merged_into`=monotonic × updated_at=max に
  合成（`_resolve_merged_into` 抽出）。`merge_hand_logs` + `_read_hand_logs` 追加。`build_snapshot` /
  `merge_snapshot_into` に optional `log_dir`（snapshot に `hand_logs`、merge は logs/{sid}.json をアトミック
  書き戻し、summary に `hand_logs_added`）。
- `api/server.py`: `_sync_paths()` に `log_dir` を追加（sync endpoint が hand log も同期）。
- `core/sync_scheduler.py`（新規）: `SyncScheduler`（interval / sync_fn / now_fn 注入、`due`/`tick`、
  callback 例外耐性）。
- `config_default.json`: `viewer_api.sync_auto_interval_sec`（既定 0）。
- tests: `tests/test_sync.py`（rename LWW + hand log union/ snapshot）/ `tests/test_sync_scheduler.py` /
  `tests/test_viewer_api_sync.py`（rename / hand log の API E2E）/ `tests/test_player_repository.py`
  （to_dict の updated_at 既定）。
- docs: ADR-0032 / decision-log / CLAUDE.md / CHANGELOG。

## Key Decisions（ADR-0032）

- rename は `updated_at` LWW で伝播。display と `merged_into` は直交フィールドとして合成（display=LWW /
  merge=monotonic）→ 可換・冪等で収束。**unmerge は sync 非伝播**（monotonic の既知帰結, 局所操作）。
- hand log は file-per-session の append-only union（`hand_id`）。`log_dir` opt-in で後方互換。
- auto-trigger は純粋スケジューラ + clock 注入でテスト可能化。常駐スレッド結線は運用タスク。

## Expected / Implemented Behavior

- 別端末で改名 → 双方向 sync で両ノードが新名に収束（古い updated_at は上書きしない）。
- node A/B が同一 session の別 hand を持つ → sync で両者が全 hand を保持。
- scheduler: interval 0 で off、初回 tick で即時実行、interval 経過で再実行、callback 例外でも継続。

## Test Results

- `pytest tests/test_sync.py tests/test_sync_scheduler.py tests/test_viewer_api_sync.py -q` — 緑。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **607 passed, 0 skipped**（既存 597 + 10）。
- `ruff check .` — clean。

## Mismatches Found During Testing

- `_resolve_player` が `updated_at` を補完するため、updated_at を持たない既存 sync テスト（idempotent /
  associative の exact-dict 比較）が破綻 → テストの `_player` helper に `updated_at` を追加し、旧
  「local 優先で rename 非伝播」テストを「`updated_at` LWW で伝播」に置換。
- `Player.to_dict` が updated_at を常時出すため `test_to_from_dict_roundtrip` が破綻 → updated_at 明示
  + 既定埋めの確認テストを追加。

## Remaining Gaps / Out-of-Scope

- auto-trigger の常駐スレッド結線（どのプロセスが `tick` を回すか）= 運用タスク（ADR-0029 会場主導）。
- unmerge の sync 伝播（monotonic 制約による既知の非伝播、ADR-0030 D4）。

## Related ADRs / Issues

- ADR-0032（本件）/ ADR-0022・0024（sync）/ ADR-0030（merge monotonic）/ ADR-0029（運用）/
  ADR-0019（schema additive 規則）

## Related Commits

- 本 commit
