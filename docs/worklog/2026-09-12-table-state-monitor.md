# 2026-09-12 — 卓状態モニタ（RFID だけでカード / 有効席 / ストリートを見る, ADR-0056 D5）

## Goal

オーナーの「実プレイ環境でのテスト」の真意が判明したので、それに合わせて作る。

> アクション推定はダミーで構いませんので、**カード読み取りと、有効席、ストリート遷移の判定**を
> 実際のプレイスピードで行えるか、また **UI に数分程度のラグで反映**できるかのテストを行いたい。
> ／ 音声入力はまだ行いません。

つまり検証対象は **RFID 由来の 3 つの観測**と **UI 反映経路**であって、アクション履歴ではない。
P0a/P0b（アクター帰属の是正）はこのテストには効かないので、本タスクを先に出す。

## 設計

**アクション推定から完全に独立した経路**にした（推定がダミーでも、壊れていても動く）。

```
RFIDThread.presence_snapshot()  ─┐
engine の _hole_cards / board    ─┼→ core/table_state.build_table_state()（純粋）
engine の street                 ─┘        │
                                            ↓
              output/table_state_writer.TableStateWriter
                logs/{session}.table_state.json   （最新・atomic 上書き）
                logs/{session}.table_state.jsonl  （実質変化時だけ append + observed_at）
                                            ↓ 別プロセス・reload-on-read（ADR-0020 の型）
                             tools/table_monitor.py（HTTP + 自動更新ページ）
```

### 有効席を 1 つの真偽値に潰さない（ADR-0056 D4）

プレイヤーは札を持ち上げて見るので「載っている = ゲームに残っている」ではない。4 つを分けて出す:

| 項目 | 意味 |
|------|------|
| `dealt_in` | このハンドで札を受け取ったか（カードが読めた or 一度でも検出された） |
| `present` | いま札がリーダー上にあるか |
| `away_sec` | 離れている秒数 |
| `likely_folded` | `away_sec > 20` の**表示上の推測**。判定ではない |

カード名が出なくても検出されていれば `dealt_in` にする（= `rfid_cards.json` 未登録のサインとして
「配られたのにカード名が出ない」が画面で分かる）。

### ストリートは 2 つ並べる

`rfid_street`（ボード枚数から導出）と `engine_street` を両方出す。pokerkit backend では
`advance_street` が no-op なので両者は食い違い得るが、**その食い違いこそ見たい情報**。

### カードが外れても event は出ない → 定期 publish

RFID のデバウンスは**増えた UID にしか反応しない**ので、札を引いても `RFIDEvent` は出ない。
`TABLE_STATE_INTERVAL = 1.0` 秒で engine の run ループから定期 publish する。これが無いと
fold が有効席に反映されない。履歴 `.jsonl` は**実質的に変化したとき**だけ append する
（経過秒数の差分で膨らませない）。

## Changed files

| ファイル | 変更 |
|---------|------|
| `core/table_state.py` | 新規。`SeatState` / `TableState` / `derive_street` / `build_table_state`（純粋） |
| `output/table_state_writer.py` | 新規。snapshot atomic 上書き + 実質変化時だけ履歴 append。書き込み失敗でハンドを止めない |
| `rfid/reader_thread.py` | `presence_snapshot()` 追加。役割の登録を**接続時**に移した（まだ札が載っていない席も「未配布」として出すため） |
| `integration/engine.py` | `seat_presence` / `table_state_writer` フック（additive）+ `_publish_table_state` / `_publish_table_state_if_due` + RFID イベント・新ハンド・確定・1 秒ごとに publish |
| `main.py` | `_make_table_state_writer` + `run_cli` / `run_gui` に結線 |
| `config_default.json` | `table_state.{enabled,history}`（既定 true） |
| `tools/table_monitor.py` | 新規。HTTP サーバ + 自動更新ページ（反映遅延を画面表示）+ `--once` の端末表示 |
| `tools/analyze_table_state.py` | 新規。履歴から反映遅延 / 不在時間（戻った・戻らない別）/ ストリート遷移を集計し、しきい値の妥当性を判定 |
| `tests/test_table_state.py` | 新規 25 ケース |

## Expected vs implemented

| 期待 | 実装 |
|------|------|
| カード読み取りが見える | ✅ 席ごとのカードとボードを表示 |
| 有効席が見える | ✅ 配布 / 在否 / 離席秒数 / fold らしさを分けて表示 |
| ストリート遷移が見える | ✅ RFID 由来 + engine の両方 |
| 札を引いた（fold）が反映される | ✅ 定期 publish（`test_fold_shows_up_without_any_new_event`） |
| UI 反映のラグ | ✅ publish 1 秒 + ページ更新 1 秒 = **実測 2 秒程度**。ページに遅延を表示するので実機で確認できる |
| 推定に依存しない | ✅ `build_table_state` は engine の状態を読むだけ。writer 未注入なら no-op |
| 壊れても記録を止めない | ✅ 在否フックの例外 / 書き込み失敗をログに留めて続行 |

## Test results

```
python -m pytest tests/ -q --ignore=tests/test_vision.py
902 passed, 2 warnings      # 881 → 902（+21）
```

モニタは smoke テストで HTTP 応答（ページ + `/state.json` + `age_sec`）を確認済み。

## Remaining gaps

- **実機での確認が未了**（本タスクの目的そのもの）。
- `likely_folded` の 20 秒は**暫定**。`.table_state.jsonl` の不在時間分布を実測して決める
  （ADR-0056 D7 計測 #3）。
- staff iPad アプリ（`staff/`）へのタブ追加は未着手（当面はモニタページで足りる）。
- モニタは**無認証**（`--host 0.0.0.0` は信頼できる LAN のみ）。viewer API と同じ前提。
- P0a（ISSUE-0033）/ P0b（ISSUE-0032）は本タスクの後。

## Related

- ADR-0056 D4/D5（有効席の扱い・卓状態）/ ADR-0055（ボード配布時刻 = `board_timeline` を同梱）
- ADR-0020（単一書き手 + reload-on-read）/ ISSUE-0031
