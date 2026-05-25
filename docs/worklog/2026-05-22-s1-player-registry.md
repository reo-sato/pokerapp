# Worklog: Phase S1 — Player Registry Screen

## Date

2026-05-22

## Scope / Task

S1 (player registry): hand logger とは **別画面** の player 管理機能を実装する。
player を新規作成・一覧表示・display_name リネームでき、永続化し、後続フェーズ
(session / ledger / settlement) が参照できる安定 `player_id` を発行する。

## Goal

- hand logger 画面とは分離した Player Registry Screen を追加する。
- player の create / list / rename ＋ 永続化（再起動跨ぎで player_id 安定）。
- 空文字 / 前後空白のみ / 完全一致重複の validation をテストに固定する。
- `main.py` から hand logger と registry の両方を別々に起動できる。
- 既存 hand logger テストを壊さない。
- docs-as-code（CLAUDE.md / CHANGELOG / worklog / issue / decision-log）を更新する。

## Changed Files

- `core/player.py` — 新規。`Player` データクラス（`player_id` / `display_name` /
  `created_at`、to_dict / from_dict）。
- `core/player_repository.py` — 新規。`PlayerRepository`（create / list / get / rename）+
  JSON 永続化（アトミックリネーム）+ validation。例外
  `PlayerValidationError` / `EmptyDisplayNameError` / `DuplicateDisplayNameError` /
  `PlayerNotFoundError`。
- `gui/player_registry.py` — 新規。`PlayerRegistryWindow`（customtkinter, dashboard とは
  独立した別画面。list / add / rename / validation メッセージ）。
- `main.py` — `--players` フラグと `run_player_registry()` を追加（hand logger とは別起動）。
- `.gitignore` — `players.json`（player 永続ファイル, user data）を追加。
- `tests/test_player_repository.py` — 新規。domain / repository テスト。
- `tests/test_player_registry_gui.py` — 新規。GUI ロジックテスト（customtkinter をモック）。
- `CLAUDE.md` — § Player Registry (S1, 実装済) を追加、ディレクトリ構成 / 実装状況表 /
  future scope 表 / Phase candidates / コマンドを更新。
- `CHANGELOG.md` — Unreleased に S1 を追記。
- `docs/issues/0004-display-name-uniqueness-scope.md` — 新規。uniqueness 仕様の将来拡張を
  open question として登録。
- `docs/decision-log.md` — Major Issue Index に ISSUE-0004 を追加。

## Expected Behavior

- `PlayerRepository.create_player(name)` が UUID hex の安定 `player_id` を発行し、永続化する。
- 再起動相当（別インスタンスで再ロード）で player と player_id が保持される。
- `rename_player` が display_name を更新・永続化する。
- 空文字 / 前後空白のみ / 完全一致重複（前後空白除去後）は例外で拒否される。
- registry 画面は hand logger の `GameStateManager` / `JsonWriter` に依存しない。
- `python main.py` は hand logger、`python main.py --players` は registry を開く。

## Implemented Behavior

期待どおり実装:

- `create_player` は `uuid.uuid4().hex` を発行し、`players.json` にアトミック書き込み。
  `list_players()` は created_at → display_name 順で返す。
- `_load()` は破損 JSON / 欠損ファイルを握り潰して空状態で開始（hand logger の JsonWriter と
  同じ堅牢性方針）。
- validation は `_validate_name(raw, exclude_id)` に集約。rename は `exclude_id` で自分自身との
  一致を許容（no-op rename 可）。
- `PlayerRegistryWindow` は `_repo` のみに依存。command (`_cmd_add` / `_cmd_rename` /
  `_select_player`) はリポジトリ例外を捕捉し `_set_status(msg, error=...)` で UI 通知。
- `main.py --players` で `PlayerRepository()`（デフォルト `./players.json`）→
  `PlayerRegistryWindow(...).run()`。

## Test Results

- `python -m pytest tests/test_player_repository.py tests/test_player_registry_gui.py -q`
  → **26 passed**。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py`
  → **152 passed**（S1 前のベースライン 126 passed に対し +26、回帰なし）。
- `python -m main --help` → `--players` フラグが表示されることを確認。
- 注: 本実行環境は headless（DISPLAY なし、customtkinter 未インストール）のため、
  実 GUI の目視確認は未実施。GUI ロジックは customtkinter をモックしたユニットテストで検証した。

## Mismatches Found During Testing

None observed。新規テスト・既存テストとも初回実行で green。GUI ロジックはモックテストで
カバーしたが、ウィジェットの実描画は headless のため未検証（下記 Remaining Gaps 参照）。

## Fixes Applied

- なし（修正を要する不整合は発生しなかった）。

## Remaining Gaps / Out-of-Scope

- [ ] 実 GUI の目視確認（display 付き環境での add / rename / validation 表示）。headless の
      ため本タスクでは未実施。
- [ ] player 削除 / merge（S1 scope 外）。
- [ ] `display_name` 以外の属性、大文字小文字 / 全半角の同一視（`docs/issues/0002` 参照）。
- [ ] hand logger との自動接続、session / seat_assignment / ledger / settlement（S2 以降）。

## Related ADRs

- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`
  （S1 はこの ADR の player モデル定義の最小実装）。

## Related Issues

- `docs/issues/0004-display-name-uniqueness-scope.md`

## Related Commits

- 本 worklog と同じコミット（S1 player registry）
