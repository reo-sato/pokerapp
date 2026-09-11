# Worklog: S5 双方向 sync（state-based merge, ADR-0022）

- **Date**: 2026-06-13
- **Branch**: `claude/merge-s3-verify-v1`
- **Related**: ADR-0022（source of truth）/ ADR-0020（更新）/ ADR-0021（staff token gate 再利用）

## Goal

複数の運営ノード（LAN）が全ストアのレプリカを **state-based merge** で相互最新化できるようにする。
マージは可換・結合・冪等にし、どのノードがどの順で何度マージしても同じ状態に収束させる
（ADR-0022 §Decision のルールを exactly 実装、独自の衝突ポリシーは入れない）。

## Changed files

- **`core/sync.py`（新規）**: 純粋マージ関数（`merge_players` / `merge_ledger_entries` /
  `merge_point_entries` / `merge_order_requests` / `merge_settlements` / `merge_sessions`）+
  snapshot I/O（`build_snapshot` / `merge_snapshot_into`, アトミックリネーム書き戻し）。
- **`core/ledger_repository.py`**: `path` property + public `reload()`（in-memory リセット + `_load`）。
- **`core/order_request_repository.py`**: `path` property + public `reload()`（既存 `_reload` に委譲）。
- **`core/player_repository.py` / `core/session_repository.py`**: `path` property（`reload()` は既存）。
- **`api/server.py`**: `GET /api/staff/sync/snapshot`（need_write=False）/ `POST /api/staff/sync/merge`
  （need_write=True, merge 後に 4 repo を reload）。`_staff_guard` を再利用。
- **`api/client.py`**: `ViewerApiClient.pull_sync_snapshot` / `push_sync_merge` / `sync_bidirectional`。
- **docs**: `decision-log.md`（ADR-0022 行 + ADR-0020 back-ref）/ `viewer-api.md`（sync API 節）/
  `repository-interfaces.md`（sync メソッド + path/reload）/ `CLAUDE.md`（S5 行・roadmap・tree）/ `CHANGELOG.md`。
- **tests**: `tests/test_sync.py`（純粋 17）/ `tests/test_viewer_api_sync.py`（HTTP 3）。

## Expected vs implemented

ADR-0022 §Decision.2 の per-store ルールを 1:1 実装:

- players: create-only union（既存 id は local 保持・rename 非伝播）。✅
- ledger / point: entry_id で union（衝突なし）。✅
- order_request: union + status 解決（pending<終端、confirmed が rejected 優先、両 confirmed は
  resolved_at 早い方）。✅
- settlement: committed>uncommitted・paid>unpaid・settled_at 早い方。✅
- session: closed>open（ended_at は closed 側）・label/blinds local 優先・入れ子 hands/seats union
  （seat 衝突は local 優先）。✅

すべて wall-clock を使わずレコードの timestamp のみで解決（決定的・順序非依存）。

## Test results

- `python -m pytest tests/ --ignore=tests/test_vision.py -q` → **511 passed, 0 skipped**
  （baseline 491 + 新規 20）。
- `ruff check .` → clean。

## Mismatches / fixes

- 当初の収束 property テストで player "a" を両ノードで別名にしていたため非収束で fail。これは
  **rename 非伝播（local 優先）という ADR-0022 の意図した caveat** なので、収束テストでは
  conflicting rename を入れず、別途 `test_players_create_only_union_keeps_local_rename` で
  rename 非伝播を検証する形に修正（同じく (hand,seat) の同一 seat_no 並行衝突も収束テストから除外）。
- `list_entries` は `LedgerEntry` を返す（dict ではない）ため、テストの属性アクセスを修正。

## Remaining gaps

- player rename 伝播（`updated_at` additive 版数が必要）。
- hand log（`logs/*.json`）の file-level union（v1 sync 対象外）。
- 定期 auto-trigger（v1 は手動 / on-demand）。
- 同一 (hand, seat_no) の並行衝突は local 優先の「一方が勝つ」semantics（document 済、非収束 caveat）。

## Related commits

- 本 worklog と同じ commit（`feat(s5): bidirectional state-based sync (ADR-0022)`）。
