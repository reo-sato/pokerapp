# Worklog: リリース版（`stable`）と、いま動いている版の記録

## Date

2026-10-11

## Scope / Task

製品化フェーズ 1（ADR-0063 D8）の 3 つ目「リリース版での更新」。ADR-0066。

## Goal

- 他の店には店で確かめた版だけが入る。テスト中の店は今までどおり開発版をすぐ試せる。
- どの店がどの版で動いているかを答えられる。

## Changed Files

- `tools/release.py` — 新規。`status` / `promote [--ref] [--dry-run]`（前に進めるだけ・日付のタグ・断る条件）
- `core/version.py` — 新規。`version_info()` / `describe()`（`installer/installed.json` か git）
- `api/server.py` — `/api/health` に `branch` / `commit` / `release`（additive）
- `api/store_server.py` — 起動ログに版
- `main.py` — `--version`
- `installer/install.ps1` — `-Commit`、`Resolve-Commit`（GitHub API でブランチの先頭）、そのコミットの zip を取る、
  `Write-InstalledVersion`（`installer/installed.json`）、更新後のインストーラへ `-Commit` を引き継ぐ
- `.gitignore` — `installer/installed.json`
- `docs/contracts/viewer-api.md` — `/api/health` の項目
- `tests/test_release.py` — 新規 12 件
- 文書: ADR-0066 / decision-log / CHANGELOG / CLAUDE.md / `docs/installation.md`（更新の節）

## Expected Behavior

- `promote` は `origin/verify-v1` の先頭（または `--ref`）を `stable` に進め、`release-YYYYMMDD[-N]` を push する。
  開発版に入っていないコミット・巻き戻し・既にリリース版・未コミットの変更は断る。`--dry-run` は何もしない。
- 店舗 PC の更新: コミットを確かめてその zip → 最後に `installed.json`。`main.py --version` は
  「リリース版 stable @ abc1234（… に更新）」/「開発版 verify-v1 @ …」。

## Implemented Behavior

- 期待どおり（使い捨ての bare リポジトリ + clone で promote・status を実際に動かして確認）。
- この checkout（`installed.json` なし）では git から「開発版 claude/release-channel @ 9ffc978」と出る。

## Test Results

- `tests/test_release.py` 12 passed。全体: 2853 passed, 5 skipped, 1 failed（推論側の既存の golden `postflop-street-transition`、変更前から）。`ruff check .` 通過。
- `tests/test_store_server.py` の「更新の前後で止める・起動する」の確かめ方を、隣り合う行ではなく順番で見るように直した（間に `Write-InstalledVersion` を足したため）。

## Mismatches / Fixes

- PowerShell は変数名の大文字・小文字を区別しないので、`Write-InstalledVersion` の中の `$commit` が引数の
  `$Commit` と同じ変数になっていた → ローカルを `$sha` に。
- 開発版に入っているかの確認を `merge-base --is-ancestor` の終了コードで判定する関数にまとめた。

## Remaining Gaps

- 店舗 PC（Windows）での確認（ADR-0066 の Validation）。GitHub の API に店舗 PC から届くか。
- 最初のリリースは、店舗で ADR-0064/0065 を確かめてから。
- 他の店に配る前に、インストーラの既定のブランチを `stable` にするか決める。

## Related Commits

- 本 worklog と同じ commit
