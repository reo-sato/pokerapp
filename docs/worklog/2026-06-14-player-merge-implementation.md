# Worklog: player merge の実装（ADR-0030）

## Date

2026-06-14

## Scope / Task

ADR-0030（player merge = alias/tombstone + read-time canonicalization）を実装する。L2 の前提ブロッカー
解消 + LAN 単独の重複 player 掃除。

## Goal

同一人物の複数 player_id を、既存不変条件（ledger append-only / 収束 sync / player_id 不変）を壊さずに
1 会計へ統合する。既定挙動（merge なし）は不変。

## Changed Files

- `docs/contracts/schemas/player.schema.json`: `1.0`→`1.1`（optional `merged_into`/`merged_at` additive）。
  `docs/contracts/fixtures/player/valid-merged.json` 追加。
- `core/player.py`: `Player` に `merged_into`/`merged_at` + `is_merged`。`to_dict` は未 merge では従来形。
- `core/player_repository.py`: `PlayerMergeError` / `resolve_canonical`（チェーン・サイクル・深度ガード）/
  `equivalence_class` / `merge_players`（自己 merge・サイクル拒否・冪等・survivor canonical 化）/ `unmerge` /
  `list_players(include_merged=False)`（tombstone を既定で隠す）。
- `core/ledger_repository.py`: `player_repo` property + `_canonical`。`_derive_settlement_rows` を canonical
  グルーピング、`point_balance` / `list_entries` を equivalence class 合算。
- `core/order_request_repository.py`: `list_requests` の player filter を equivalence class に。
- `api/read_models.py`: `list_player_sessions`/`list_player_hands` を equivalence class で seat 突合、
  `get_player_session_ledger` を canonical query に。
- `api/server.py`: `auth_login` は canonical principal を発行、`_require_player` は両辺 canonical 比較、
  `POST /api/staff/players/merge`（write 所有のみ、`invalid_merge`/`not_found`）。
- `api/client.py`: `merge_players`。
- `core/sync.py`: `_resolve_player`（`merged_into` monotonic 伝播 + survivor 最小 tiebreak）を `merge_players`
  に組込み（従来の create-only union + local 優先は維持）。
- `gui/player_registry.py`: merge パネル（統合先設定→統合）。
- tests: `tests/test_player_merge.py`（13）/ `tests/test_viewer_api_merge.py`（5）/
  `tests/test_player_registry_gui.py::TestMergePlayers`（3）/ `tests/test_sync.py`（merge 2）。
- docs: ADR-0030（Status→実装済 + Validation チェック）/ decision-log / error-shapes（`invalid_merge`）/
  viewer-api（merge endpoint）/ CLAUDE.md / CHANGELOG。

## Key Decisions（実装で確定）

- **履歴を書き換えない**（D1）: ledger/session/order の player_id は不変。merge は registry の `merged_into`
  のみ。read 側が `resolve_canonical`/`equivalence_class` で survivor に解決。
- **canonicalize は core 集約**: 各 repo が `self._player_repo` 経由で解決。`player_repo=None` は実質発生
  しない（既定 registry を構築）ため identity degrade は不要だった。
- **既定不変**: merge が無ければ `equivalence_class` は単集合・`resolve_canonical` は identity ⇒ 既存
  557 テストは無改修で緑（実装後 580 passed）。

## Expected / Implemented Behavior

- merge 後: settlement は survivor 1 行に合算（複数 ID の buy_in 等）、point 残高・ledger entry・order・
  viewer per-player・login principal が survivor 視点に解決。`/api/players` と registry 一覧は tombstone を隠す。
  `players.json` には absorbed レコードが残る（可逆・履歴保全）。
- sync: 片方だけ merged なら両方向で merged 版に収束、両 merged 別 survivor は survivor 最小で収束。

## Test Results

- `pytest tests/test_player_merge.py tests/test_viewer_api_merge.py tests/test_player_registry_gui.py
  tests/test_sync.py tests/test_contracts.py -q` — 緑。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **580 passed, 0 skipped**（既存 557 + 23）。
- `ruff check .` — clean。
- live smoke: staff merge → settlement 2 行→1 行（15000 合算）/ `/api/players` は 1 件 / `players.json` は
  2 件（履歴保全）を確認。

## Mismatches Found During Testing

- `core/sync.py:merge_players` が create-only union（local 優先）で **merged_into を伝播しなかった**ため、
  `_resolve_player` で monotonic 解決 + tiebreak を追加。

## Remaining Gaps / Out-of-Scope

- `current_seating` 表示の canonicalize（同一 hand 2 席の歴史的アーティファクト）は staff 検査画面のみ・
  低影響で未対応（ADR-0030 D6, follow-up）。
- L2 本体（ADR-0028 OIDC）/ cloud モード config（ADR-0029）は別タスク。

## Related ADRs / Issues

- ADR-0030（本件）/ ADR-0028・0029（L2 前提）/ ADR-0004（player_id 不変）/ ADR-0016（append-only）/
  ADR-0022/0024（収束 sync）/ ADR-0019（schema freeze の additive 規則）/ ADR-0027（principal）

## Related Commits

- 本 commit
