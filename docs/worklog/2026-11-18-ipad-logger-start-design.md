# Worklog: iPad の操作だけでロガーを起動する — 達成度と設計（ADR-0073）

## Date

2026-11-18

## Scope / Task

オーナー「最終的に、iPad アプリからの操作だけでロガーを起動するようにします。Fable の監査を受けながら、現状の達成度と
設計計画を考えてください」。設計だけで、コードは変えていない。

## 進め方

1. いまのコードを調べた。
   - 起動の流れ: `main.py:run_cli`・`_session_config`・`_open_session_layer`。
   - 制御: `core/control_queue.py`・`integration/control_consumer.py`・`api/server.py` の `staff_hand_control`。
   - 店舗サーバー（`api/store_server.py`・タスクの登録）、卓の設定画面（`tools/session_setup_ui.py`）、スタッフ画面
     （`staff/src/screens`）、RDP とマイクの注意書き。
2. 草稿（`57d759d`）を書いた。
3. Fable に、コードと突き合わせた監査を頼んだ（読むだけ）。
4. 指摘のうち、コードで確かめられるものを確かめた。どれもそのとおりだった。
   - iPad の制御は 3 種類だけ。
   - `name` と `rr` は queue の外の副作用。
   - `SessionRepository._flush` は書く前に読み直さない。
   - `atomic_io` の一時ファイルの名前は固定。
   - 制御の読み手は末尾から読み始める。
5. 改訂版（`bfbb807`）を書いて再監査を頼み、残りの指摘を反映した。

## 結果

- 達成度はおよそ 35〜40%（Fable の独立の見積もりと同じ）。
  - 土台（店舗サーバー・認可・RFID の中継・制御の通り道・卓モニタ・真のアクションの画面）はほぼ済み。
  - 起動・設定・状態・停止・見張りは 0。
  - iPad から送れる操作は 3 種類。
- 計画（ADR-0073 の段階）:
  - 段階 0: 店舗 PC で実測。
  - 単独: `sessions.json` の読み直しとロック（いまある不具合）。
  - A1: 状態とお知らせを iPad に。
  - A2: 制御の共通化と記録の同一性。
  - B: `--headless`・段階のある終了。
  - C: 見張り役・起動と終了の画面。
  - A3: 操作の画面。
  - D: 店舗テスト。
  - E: 再開など。

## 監査で分かった、いまある不具合（設計とは別に直せる）

- ロガーは `sessions.json` を起動時の内容のまま書き直す。そのため、プレー中に iPad で行った変更（公開範囲・別の卓・
  席の割当）が、次のハンドで消える（ADR-0073 D5）。

## Changed Files

- `docs/adr/0073-start-logger-from-ipad.md`（新規, Proposed）
- `docs/decision-log.md`・`docs/plan-board.md`（トラック I）・CHANGELOG

## Remaining Gaps

- オーナーの確認（段階の順番・段階 0 の実測の日）。
- 段階 0 の実測（コードなし。タスクから `tools/audio_check.py listen` を動かす手順を用意する）。
