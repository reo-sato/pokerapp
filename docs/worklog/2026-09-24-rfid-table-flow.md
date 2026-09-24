# Worklog: RFID を卓の流れに合わせて解釈する（フォールドの通過 / 配り直しを入力なしで）

## Date

2026-09-24

## Scope / Task

店舗 PC での実卓テストで、オーナーから次の報告を受けた（ISSUE-0035）。

- 別の札を置き直したとき、新しい札が読めない。
- ディーリングの構造上、フォールドした手札はボードのリーダーの上を通る。
- ミスディールで新しい札を配るときに入力を求めることは、進行の都合でできない。

## Goal

- フォールドした手札がボードの札として位置を取らない。
- 配り直しがディーラーの入力なしで記録に反映される。
- ISSUE-0026 の壊れ方（読み落ちで同じ札が 2 か所・枚数の水増し）を再発させない。

## Changed Files

- `rfid/reader_thread.py` — 卓の流れに合わせた解釈（`commit_sec` / `gap_sec` / `release_sec`）。場所ごとの
  「載り続けている期間」（`_Run`）、そのハンドで席に記録した UID（`_seat_owner`）、マック（`_mucked_at`）、
  ボードの差し替え（ストリート単位, `_choose_board_slot`）、席の差し替え・移動（`_decide_seat`）。従来の
  解釈（`_poll_immediate`）はコンストラクタ既定として残し、手札の除外だけ両方で有効。状態の更新は RLock。
- `core/events.py` — `RFIDEvent.replaces`（additive）。
- `integration/engine.py` — 席の差し替え・別席からの移動（デッキ整合）・ボード位置の差し替え（配布時刻も更新）、
  いずれも `needs_review`。
- `output/event_recorder.py` / `integration/replay.py` / `docs/contracts/schemas/reconstruction_event.schema.json`
  （`0.4`）/ `docs/contracts/versioning-and-freeze.md` — `replaces` の記録と再生。通常の配布では省略
  （既存 golden は不変）。
- `core/table_state.py` / `tools/table_monitor.py` — 席の「マック」表示。
- `main.py` / `config_default.json` — 本番の起動経路に既定値（2.0 / 1.5 / 6.0）で結線。キーが無くても有効。
- `tests/test_rfid_table_flow.py`（新規 24 件）/ `tests/test_table_state.py`（presence に `mucked_at`）。
- docs: ADR-0058 / ISSUE-0035 / ADR-0053・0054 の Status / 契約 v1.5 §4・§10 / decision-log / CLAUDE.md /
  usage.md / CHANGELOG。

## Expected Behavior

- 席 1 の手札がボードの上を通っても、flop は 1〜3 枚目、turn は 4 枚目。卓モニタで席 1 が「マック」。
- 全員の札を回収して配り直すと、約 6 秒後に新しい札に差し替わる（前の手札が別の席に来ても移る）。
- 札を持ち上げて見る・数秒の読み落ちでは何も変わらない。

## Implemented Behavior

上記のとおり。設計の要点（ADR-0058）:

1. **手札はボードの札にならない**（デッキ整合。RFID だけで確実に分かる）。
2. **ボードは滞留で確定**（通過は一瞬）。席は最初に見えた瞬間に記録（持ち上げて見られる前に拾う）。
3. **差し替えの根拠は「消えた」ではなく「新しい札が載り続けた」**。ボードはストリートの札が全部消えたときだけ
   なので、flop の 1 枚の読み落ち中に turn が来ても turn は 4 枚目（テストで固定）。

## Test Results

- `pytest tests/test_rfid_table_flow.py` — 24 passed。
- `pytest tests/ -q --ignore=tests/test_vision.py`（pwsh あり）— **1207 passed**、skip 0。
- `ruff check .` — clean。
- 既存の RFID テスト（`test_rfid.py` / `test_misdeal_correction.py` / `test_timeline_fidelity.py` /
  `test_table_state.py`）は従来の解釈（コンストラクタ既定）で不変のまま通る。

## Mismatches Found During Testing

- 新テストの UID に 16 進でない文字（`04:T1` / `04:N1`）を使い、カードマスターの正規化で両方 `00:41` になって
  後から登録した札に化けた（turn が `Ah` と出た）。実機の UID は 16 進なので、テストの UID を 16 進に直した。

## Fixes Applied

上記 1 件（テストデータ）。

## Remaining Gaps / Out-of-Scope

- [ ] 店舗の実卓での確認（`update.cmd` で反映 → フォールドの通過 / 配り直し / 読み落ちの 3 つ）。
- [ ] `commit_sec` / `release_sec` の実測による調整（`tools/analyze_table_state.py` の不在時間分布）。
- [ ] 回収した札の山・バーンカードをボードのリーダーの上に置く運用だと、席で読めていない札は 2 秒でボードの札に
      なる（卓の運用で避ける。usage.md に記載）。
- [ ] マック時刻を合成 fold の時刻に使う（ADR-0055 の不在時刻より正確）。今回は表示と記録のみ。

## Related ADRs

- ADR-0058（本件）/ ADR-0053・ADR-0054（部分的に置換）/ ADR-0055 / ADR-0056 D4

## Related Issues

- ISSUE-0035 / ISSUE-0025 / ISSUE-0026

## Related Commits

- 本タスクのコミット（`feat(rfid): 卓の流れに合わせた解釈 …`）。
