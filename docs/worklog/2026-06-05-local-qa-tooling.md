# Worklog: ローカル QA tooling（実機・Windows なしの検証手段）

## Date

2026-06-05

## Scope / Task

RFID 実機（ESP32 + PN532）や Windows ビルド環境が無い開発機で v1 の品質確認を進められるよう、
**手動 QA チェックリスト + 模擬ツール 2 本**を追加する。v1 リリーストラックの検証支援（実装トラックとは
独立、additive）。

## Goal

- 実機なしで「インストール → テスト → 再構築（replay / テキスト駆動）→ RFID 模擬 → PHH」まで一通り
  確認できる手順とツールが揃っている。
- 既存挙動・既存テストは一切変えない（純 additive）。CI 緑のまま skip 0。

## Changed Files

- `tools/__init__.py` — 新規。`tools` を import 可能なパッケージにする（テストから tool 関数を import）。
- `tools/simulate_rfid.py` — 新規。`RFIDHTTPReceiver` の `POST /rfid` に偽イベントを注入する CLI。
  サブコマンド `send` / `seat` / `board` / `status` / `register-demo`。`demo_tag_for_card`（決定的合成タグ）
  と `register_demo_deck`（合成デッキ登録）で物理タグ無しでもカード解決可能。stdlib `urllib` のみ。
- `tools/play_hand_text.py` — 新規。生テキスト（ディーラー読み上げ相当）を `parse_action` → 
  `integration/replay.py:replay_events` に流す mic 不要ドライバ。`utterances_to_events` / `run_text_hand` を公開。
- `tests/test_tools_simulate_rfid.py` — 新規。合成タグ / register-demo / 実 receiver への POST→queue（16 tests）。
- `tests/test_tools_play_hand_text.py` — 新規。parse/skip/timestamp / legacy 確定&再現性 / pokerkit smoke（6 tests）。
- `docs/manual-qa-checklist.md` — 新規。実機なしローカル QA の 9 項目チェックリスト。
- `CLAUDE.md` — 「よく使うコマンド」にローカル QA の 2 コマンドを追加。
- `CHANGELOG.md` — Unreleased に本追加を記載。

## Expected Behavior

- `tools/simulate_rfid.py`: 起動中アプリ（`rfid.enabled=true, transport=http`）に対し、席/ボードカードの
  タッチを HTTP POST で再現できる。物理タグ無しでも `register-demo` 後は `--card`/`board`/`seat` で
  カードがログに解決される。サーバ未起動時は graceful にエラー。
- `tools/play_hand_text.py`: マイク/Whisper なしで、テキストから parse → rules-aware 再構築 → 
  `logs/<session>.json` を生成。既定 backend は pokerkit（合法手射影 / silent-fold / side-pot が効く）。
  出力は `main.py --export-phh` でそのまま PHH 化できる。
- チェックリスト: 各項目のコマンドが実在し、ハード要否が明示される。

## Implemented Behavior

- 期待どおり。`play_hand_text` は `replay_events` を再利用するため、決定的 clock 注入による再現性を
  そのまま継承（同一入力 → 同一出力）。`simulate_rfid` の合成タグは "DEAD"+rank+suit の 16 進で
  `normalize_tag_id` 後に有効な CardMaster キー（例: Ah → `DE:AD:0C:02`）になり、カードごとに一意。

## Test Results

- `pytest tests/test_tools_simulate_rfid.py tests/test_tools_play_hand_text.py -v` — **22 passed**。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **311 passed, 0 skipped**（既存 289 + 新規 22、回帰なし）。
- Manual CLI smoke:
  - `printf 'ハンド開始\nチェック\nシート1 ウィナー\n' | python tools/play_hand_text.py - --seats 3 …`
    → 1 hand、`action=call amount=200 needs_review=true`、`review_required=true`（check→call 射影を再現）。
  - `python tools/simulate_rfid.py register-demo --cards-file /tmp/qa_cards.json` → 52 枚登録。
  - `python tools/simulate_rfid.py status`（サーバ無し）→ graceful error, exit=1。
  - `python main.py --export-phh /tmp/qa_logs/smoke.json` → `0001.phh` 生成（text→JSON→PHH の連鎖確認）。

## Mismatches Found During Testing

None observed.

## Fixes Applied

- なし（新規追加のみ）。テスト記述時に空キュー検出を `pytest.raises(queue.Empty)` に厳密化。

## Remaining Gaps / Out-of-Scope

- [ ] Windows ワンクリックインストーラ / コード署名 / クリーン Windows 実機 E2E（Phase H2/H3、要 Windows）。
- [ ] seat → player_id 選択 GUI（ISSUE-0006、Phase 2.3）。
- [ ] 実マイクでの音声 E2E は自動化せず手動項目（チェックリスト項目 5）に留める。

## Related ADRs

- `docs/adr/0011-deterministic-replay-harness.md` — `play_hand_text` が再利用する replay/clock 注入の設計。
- `docs/adr/0009-pokerkit-live-rules-authority.md` — テキスト駆動で検証する合法手射影 / silent-fold の根拠。

## Related Issues

- `docs/issues/0006-seat-selection-ux-at-hand-start.md` — seat→player_id 選択 GUI（ローカル QA では未結線として扱う）。

## Related Commits

- （このタスクの commit を追記）
