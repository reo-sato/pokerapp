# Worklog: review hardening — 全体レビューで検出した堅牢化（ISSUE-0012 ほか）

## Date

2026-06-08

## Scope / Task

プロジェクト全体レビュー（ビジネス/技術、3 Explore agent + GitHub 状況調査）で検出した
「小さく安全に直せる技術的欠陥」を修正する。ユーザー承認済みプランの項目 2〜5 と 7。
レビュー基準ツリーは `v1-integration` HEAD `99bb9bd`。

## Goal

1. **T1**: GUI/CLI スレッドからの `GameStateManager` 直接変更（レース・規約違反）を queue 経由に一元化。
2. **T2**: RFID HTTP 受信の堅牢化（`bind_host` 既定 `127.0.0.1`、`Content-Length` 上限）。
3. **T3**: pokerkit 未導入時の起動クラッシュを legacy フォールバックで解消。
4. **T4（最小着手）**: `parse_amount` 系の直接ユニットテスト追加 + CI に ruff 最小ゲート。

## Changed Files

- `integration/engine.py` — `_handle_audio_event` に `rebuy` 分岐 + `_handle_rebuy` 追加
  （IntegrationThread 内で `gs.rebuy()` 適用、`on_action` 通知のみで `HandSummary.actions` には積まない）。
- `gui/dashboard.py` — `_cmd_rebuy` を queue 経由に（GUI 側は入力 validation のみ。winner と同パターン）。
- `main.py` — CLI `n` / `r` を queue 経由に（`w` と同パターン）。`bind_host` フォールバック既定も
  `127.0.0.1` に同期。
- `core/poker_engine.py` — `create_game_state("pokerkit")` の ImportError で warning + legacy 返却。
  ログの「(preview)」表記を削除（Phase G で既定化済みのため）。
- `rfid/http_receiver.py` — `MAX_CONTENT_LENGTH = 16KB`（超過 413 / 不正ヘッダ 400）、
  コンストラクタ既定 `bind_host="127.0.0.1"`、docstring の設定例更新。
- `config_default.json` — `rfid.bind_host` 既定 `127.0.0.1` + `_bind_host_comment`。
- `docs/installation.md` / `docs/usage.md` / `docs/troubleshooting.md` — bind_host 手順/表/注意を追記。
- `.github/workflows/ci.yml` + `pyproject.toml` + `requirements-dev.txt` — ruff（`select=["F","E9"]`,
  vision 除外）。`ruff check . --fix` で未使用 import 17 件（全 F401）を除去。
- tests（新規/追記）: `tests/test_engine_rebuy.py`（5）/ `tests/test_poker_engine_fallback.py`（2）/
  `tests/test_recognizer_amounts.py`（31）/ `tests/test_rfid_http.py` に `TestPayloadLimits`（3）。
- docs: `CHANGELOG.md` / `docs/issues/0012-gui-thread-rebuy-race.md`（Fixed）/
  `docs/decision-log.md` / `CLAUDE.md`（エラーハンドリング方針 3 行）。

## Expected vs Implemented

期待どおり。実装中に追加で判明・対応した点:

- **CLI `n` の実バグ**（レビューでは未検出）: `game_state.new_hand()` 直呼びのため
  IntegrationThread のハンドバッファ（`_current_actions` / `_stack_start` / board / hole_cards /
  session `assign_seat`）がリセットされていなかった。queue 経由化で `_start_new_hand` を通り解消。
  ISSUE-0012 の Actual に追記。
- **レビュー所見の訂正**: 「`audio/recognizer.py` に専用テストがほぼ無い」は部分的に不正確。
  `apply_corrections` は `tests/test_phase_d_corrections.py` で 20 件カバー済みだった。
  実際に薄かったのは `parse_amount` / `_kanji_to_int` / 席除去（`test_parser.py` の基本 6 件のみ）で、
  本タスクの 31 件はそこを固定した。
- CLI の rebuy 確定表示は同期（直呼び時の即時 print）から非同期（`on_action` 経由の表示）に変わる。
  ユーザー可視の挙動変化として CHANGELOG に記載。

## Test Results

- `pytest tests/ --ignore=tests/test_vision.py -q` → **330 passed, 0 skipped**（289 → +41、回帰なし）。
- `ruff check .` → All checks passed（修正前 17 errors / 全 F401）。
- 413/400 経路は `tests/test_rfid_http.py:TestPayloadLimits` で実サーバ起動して検証。
- pokerkit フォールバックは `sys.modules["pokerkit"]=None` パッチで ImportError を再現して検証。

## Mismatches Found During Testing

None（全テスト初回 green）。

## Remaining Gaps / Out-of-Scope

- [ ] HTTP 受信の認証（トークン/HMAC）— bind 既定の安全化のみ実施。LAN 公開時は依然無認証
      （docs に明記）。v1 後の検討事項。
- [ ] `core/config.py` の schema validation / `main.py` ユニットテスト / sleep 依存テストの同期化 /
      mypy / pytest-cov — レビュー指摘の残り（次段）。
- [ ] E3 ブランチ（`claude/phaseE3-seat-selection-gui`）のマージと issue/PR 棚卸し — ユーザー確認待ち。
      なお E3 の `gui/dashboard.py` 変更と本タスクの `_cmd_rebuy` 変更は同ファイルだが別領域で、
      マージ時の競合は軽微の見込み。

## Related Issues / ADRs

- `docs/issues/0012-gui-thread-rebuy-race.md`（本タスクで新規・Fixed）。
- ADR-0009/0012（pokerkit backend — フォールバックは既定切替 Phase G の運用補強）。

## Related Commits

- 本タスクのコミット（push 先: `claude/review-hardening`）。
