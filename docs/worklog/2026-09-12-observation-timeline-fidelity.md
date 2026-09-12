# 2026-09-12 — 観測の時刻精度（ADR-0044）+ 仕様との drift 記録（ISSUE-0031）

## Goal

オーナーの要件整理を受けて、**アクション履歴を音声録音の時系列と突き合わせて再生する**ために
必要な時刻を確保する。

> カード内容は事後に把握できればよくリアルタイム反映は不要。フロップ 3 枚の内部順序も重要ではない。
> リアルタイムの更新が必要なのは **fold のタイミング**だけで、これは後々音声の時系列と組み合わせて
> アクション履歴を再生するため。**ターン / リバーがディールされる時刻**もアクションのタイミングと
> 関連するため重要。

## 調査結果（着手前）

| 要求 | 現状 |
|------|------|
| fold の実時刻 | ❌ ディーラーが宣言しない fold は後続アクションで初めて合成され、`timestamp` に**次の人が行動した時刻**が入る（`_append_synth_fold` が `_now_iso()`, `integration/engine.py`） |
| ターン / リバーの配布時刻 | ❌ どこにも残らない。`HandSummary.board` はカード名の配列だけで `RFIDEvent.timestamp` は解釈後に破棄。生イベント sidecar は既定 off |

## Changed files

| ファイル | 変更 |
|---------|------|
| `rfid/reader_thread.py` | `clock` 注入（additive）/ `_seat_absent_since` + `_track_seat_presence` / `seat_cards_absent_since(seat)` / `reset_for_new_hand()`（board 位置 + マック観測をまとめて捨てる）/ `forget_seat_cards` で観測も捨てる |
| `integration/engine.py` | `seat_cards_absent_since` フック（additive）/ `_synth_fold_timestamp`（時刻のみ差し替え・ハンド範囲でクランプ・`source.rfid` で出所）/ `_hand_started_epoch` / `_board_dealt_at` + `_build_board_timeline` / `_iso` ヘルパ |
| `core/hand_log.py` | `HandSummary.board_timeline`（additive, 既定 `[]`）+ `to_dict` |
| `main.py` | `on_new_hand` を `reset_for_new_hand` に（無ければ従来の `reset_board_positions`）+ `seat_cards_absent_since` 結線（`run_cli` / `run_gui`） |
| `docs/contracts/schemas/hand.schema.json` | `board_timeline` を optional 追加、`1.0`→`1.1`（additive） |
| `tests/fixtures/reconstruction/*/expected_hand.json` | golden 5 件に `board_timeline: []` を追加（出力形の変化を golden に反映） |
| `tests/test_timeline_fidelity.py` | 新規 12 ケース |
| `docs/adr/0044-*.md` / `docs/issues/0031-*.md` / CLAUDE.md / CHANGELOG / decision-log | docs |

## Expected vs implemented

| 期待 | 実装 |
|------|------|
| 合成 fold が実時刻になる | ✅ マック観測があればそれを採用（`test_uses_muck_time_when_available`） |
| 観測が無ければ従来どおり | ✅ 処理時刻にフォールバック（フック未注入・観測 None・例外の 3 経路をテスト） |
| 不在で fold を判定しない | ✅ 判定経路は無改修。`_synth_fold_timestamp` は `timestamp` しか触らない |
| 前ハンドの観測が混ざらない | ✅ ハンド範囲外は棄却 + `reset_for_new_hand` で破棄（`test_observation_outside_the_hand_is_ignored` / `test_new_hand_clears_observations`） |
| ターン/リバーの配布時刻 | ✅ `board_timeline` の index 4 / 5 |
| 再検出で時刻がずれない | ✅ 最初の検出で固定（`test_redetection_does_not_move_the_deal_time`） |
| 訂正したら時刻も採り直す | ✅ `test_misdeal_correction_takes_a_fresh_deal_time` |
| confidence を動かさない | ✅ `SYNTH_FOLD_CONFIDENCE` 据え置き（ADR-0033 の較正を壊さない） |

## Mismatches found while implementing

- **golden fixtures 5 件が落ちた**。`HandSummary.to_dict()` にキーが 1 つ増えたため
  （`board_timeline: []`）。出力形の正しい変化なので golden 側を更新した（`board_source` の直後に挿入）。
- `from_rfid` を「戻り値の ISO 文字列が `_now_iso()` と違うか」で判定していたが、`_now_iso()` を
  再度呼ぶと時計が進んで常に True になり得る。`_synth_fold_timestamp` を `(iso, from_rfid)` の
  タプル返しに変更した。
- テストの時刻アサーションを `endswith("17:20.000")` のような UTC 依存の書き方にしていたので、
  `IntegrationThread._iso(epoch)` との比較に直した（タイムゾーン非依存）。

## 併せて記録した仕様 drift（ISSUE-0031）

オーナーの「**ベイズ推定を用いたアクション履歴推定は当初からの方針**」という指摘を受けて
要件定義書を確認したところ、**そのとおりだった**（`sprc_v4.docx` は拡張子に反して実体が
プレーン UTF-8 テキストなので直接読める）:

- 改訂履歴 v3.0「アクター推定を**尤度ベースに全面改訂**」
- FR-26（席ごとの推定確率）/ FR-27（次点との差 0.3 未満で needs_review）/ §5.4
  （`estimate_actor -> dict[int, float]`、ターン席 0.90 / アウトオブターン 0.05）
- FR-25・FR-35（**RFID フォールド検知**で `active_seats` を更新）/ FR-30（ストリート遷移は RFID 優先）

実装は `_resolve_actor` が固定優先順位で単一 actor を決め、`derive_confidence` は事後確率でない
ヒューリスティック、RFID フォールド検知は未実装、ポジション名言及も未対応。**ISSUE-0009 で尤度を
後回しにした暫定の決定的ポリシーが、そのまま既定として定着した**のが原因。→ **ISSUE-0031** に記録。

方針は **Fable 5.1 による設計監査**の結果を踏まえて別 ADR で決める（本タスクの範囲外）。本 ADR-0044 は
**どの推定器でも入力として必要な観測の時刻**に限定したので、方針がどちらに転んでも無駄にならない。

## Test results

```
python -m pytest tests/ -q --ignore=tests/test_vision.py
881 passed, 2 warnings      # 869 → 881（+12）
```

## Remaining gaps

- **推定方式の決定**（ISSUE-0031）— 監査待ち。最大の残件。
- マック観測は **ライブ経路のみ**。生イベント sidecar（既定 off）には現れないので、事後推定に
  使うなら sidecar への記録が別途要る。
- プレイヤーが長時間カードを手に持ってからマックすると fold 時刻が早めに出る（既知の限界）。
- ホールカードの**配布**時刻は未記録（要件に無いため。必要なら同じ仕組みで追加可）。
- 実機での確認が未了（`board_timeline` がターン/リバーの実時刻を拾うか）。

## Related

- ADR-0044 / ISSUE-0031 / ISSUE-0009
- ADR-0043（ミスディール訂正。訂正時に観測・配布時刻も捨てる）
- ADR-0033（confidence 較正。本タスクは confidence を動かさない）
