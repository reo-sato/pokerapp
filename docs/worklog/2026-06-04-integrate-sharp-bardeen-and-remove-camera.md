# Worklog: sharp-bardeen 統合 + sprc_v4.docx 準拠の camera/vision 削除

## Date

2026-06-04

## Scope / Task

`claude/dazzling-brown-COUkU`（v4 設計ベース）へ作業の進んだ `claude/sharp-bardeen-dYeA6`
を統合し、続けて本ブランチの v4 設計（sprc_v4.docx）に合わせて温存されていた camera/vision を
削除する。2 部構成: (1) ブランチ統合、(2) v4 準拠の camera 削除（ADR-0011）。

## Goal

- sharp-bardeen の player registry / session+seating / contracts / config-gated 統合を
  本ブランチへ取り込む（hand logger の v4 挙動を壊さない）。
- sprc_v4.docx が「廃止」と明記する camera/vision をコード・依存・設定・テストから完全削除し、
  RFID + 音声の 2 ソース構成に確定する。
- docs-as-code（CLAUDE.md / CHANGELOG / ADR / decision-log / worklog）を揃える。
- 「done」= 全 camera 参照 0 件 + 全テスト green + docs 整合。

## Changed Files

### Part 1: 統合（merge commit, sharp-bardeen の差分をそのまま取り込み）

- `git merge --no-ff origin/claude/sharp-bardeen-dYeA6` — 81 files / +7986 / −6。
  - マージベース = 本ブランチ HEAD（sharp-bardeen は厳密な子孫）のためコンフリクト 0。
  - 取り込み: `CLAUDE.md` / `CHANGELOG.md` / `docs/`（ADR0003-0008, issues, worklog, contracts,
    templates）/ `core/player*.py` / `core/session*.py` / `gui/{player_registry,seat_assignment,session_viewer}.py`
    / `integration/engine.py` ・ `main.py` ・ `core/hand_log.py` の config-gated 追記 / 各 tests。

### Part 2: camera/vision 削除（ADR-0011）

- 削除: `vision/{__init__,camera,motion_detector,calibration}.py`、`tests/test_vision.py`。
- `core/events.py` — `CameraEvent` データクラスと numpy 依存・未使用 `field` import を削除。
- `core/event_queue.py` — `make_camera_queue` と `EventItem` の `CameraEvent` を削除。
- `core/hand_log.py` — `source` コメントを `{"audio","rfid"}` に、`board_source` から `"ocr"` を、
  confidence コメントから camera を除去。
- `integration/engine.py` — module docstring / `calc_confidence`（2 引数化）/ confidence 定数 /
  camera バッファ / `_drain_camera_queue` / `_pop_matching_camera_event` を削除。
  `CAMERA_BUFFER_TTL`→`BUFFER_TTL` 改名。`source` を `{"audio","rfid"}` に。
- `main.py` — run_cli / run_gui の camera スレッド・`camera_queue`・`cam_cfg` を削除、
  `--calibrate` サブコマンドを削除、`--cli` help 文言を更新。
- `gui/dashboard.py` — `camera_queue` DI・「カメラ」ソース表示・`start_threads` の camera_thread を削除。
- `config_default.json` — `camera` ブロックを削除。
- `requirements.txt` — `opencv-python` / `easyocr` を削除。
- tests — `test_phase7.py`（calc_confidence 2 ソース化, camera 照合テスト除去）、
  `test_integration.py`（audio 単独 confidence へ全面書き換え）、
  `test_gui.py` / `test_logger.py` / `test_phh_exporter.py`（source dict から camera キー除去）。
- docs — `CLAUDE.md`（概要 / confidence 行列 / ディレクトリ / 実装状況 / commands）、`CHANGELOG.md`、
  `docs/adr/0011-remove-camera-vision-per-v4-spec.md`（新規）、`docs/decision-log.md`（ADR-0011 登録）。

## Expected Behavior

- 統合後、`config.session_layer.enabled=false`（既定）では hand logger は本ブランチと同一挙動。
  player registry / session viewer は `--players` / `--sessions-viewer` の別エントリで起動。
- camera 削除後、通常運用（camera 未接続）の音声 + RFID 挙動・JSON/PHH 出力は不変。
  confidence は 2 ソース行列（RFID+audio=0.95 / RFID=0.70 / audio=0.50 / なし=0.00）。
- コード・設定・テストに camera/vision 参照が残らない。

## Implemented Behavior

期待どおり。

- 統合は fast-forward 可能な子孫を `--no-ff` で統合点を明示して取り込み（merge commit）。
  共有ファイルの sharp-bardeen 変更は全て additive / flag-gated であることを事前検証済み。
- camera 削除は共有インフラ（RFID 照合に使う `MATCH_WINDOW` / buffer 失効）を保持しつつ
  camera 専用部分のみ除去。`source` は sprc_v4.docx §6.1 と一致する `{"audio","rfid"}` に確定。

## Test Results

- `python -m py_compile $(git ls-files '*.py')` — 全 `.py` クリーンコンパイル。
- JSON 妥当性（`config_default.json` / `rfid_cards.json`）— OK。
- camera/vision の grep（`*.py` / `*.json`）— **0 件**（CLAUDE.md の「廃止済み」説明文を除く）。
- `pytest tests/` — **199 passed in ~14s**（pytest + jsonschema + numpy。pokerkit /
  faster-whisper / customtkinter / pyaudio / pyscard は未インストールだが各モジュールが遅延
  import のため全テスト collect・実行可）。
  - camera 削除の影響を直接受ける `test_phase7` / `test_integration` / `test_logger` /
    `test_gui` / `test_phh_exporter` を含め green。

## Mismatches Found During Testing

None observed. py_compile・grep・pytest いずれも期待どおり。

## Fixes Applied

- `test_integration.py` は camera 照合が主目的の suite だったため、audio 単独 confidence に
  焦点を絞って全面書き換え（RFID 照合は `test_phase7.py` の `TestRFIDSeatMatching` が担保）。
- `core/events.py` の `numpy` / `field` import は `CameraEvent` 専用だったため併せて除去。
- `CAMERA_BUFFER_TTL` は RFID buffer 失効にも使われていたため、削除せず `BUFFER_TTL` に改名して保持。

## Remaining Gaps / Out-of-Scope

- [ ] sprc_v4.docx が定義する v4 高度機能（PokerRuleEngine / legal_actions / 尤度ベース actor 推定 /
      ディーラーボタン自動管理）は **両ブランチとも未実装**。本タスクの範囲外（別 phase）。
- [ ] sharp-bardeen の S3 以降（ledger / point / settlement）・mobile・cross-app sync は planned のまま。
- [ ] camera を将来復活させる予定はない（必要時は git 履歴から復元）。

## Related ADRs

- `docs/adr/0011-remove-camera-vision-per-v4-spec.md` — camera/vision 削除の判断（本タスク）。
- `docs/adr/0003-...md` — hand logger を base とした domain 拡張（統合した sharp-bardeen 側）。
- `docs/adr/0008-...md` — hand logger × session の config-gated 統合（統合で取り込み）。

## Related Issues

- なし（新規 issue は起票していない）。

## Related Commits

- `<merge-commit>` — merge: integrate sharp-bardeen onto v4 base。
- `<removal-commit>` — refactor: remove camera/vision per sprc_v4.docx (ADR-0011)。
