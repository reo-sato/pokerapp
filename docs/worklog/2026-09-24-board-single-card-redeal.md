# Worklog: ボードの 1 枚だけの差し直しを入力なしで反映する（ADR-0058 追記）

## Date

2026-09-24

## Scope / Task

ADR-0058（卓の流れに合わせた解釈）を店舗 PC に入れた直後、オーナーから補足（ISSUE-0035 追加の報告）:

> ボードの配り直しは一枚だけ差し直すこともあります。フロップ全体をわざわざ外したりはしません

ADR-0058 D3 のボードの規則は「ストリートの札が**全部** `release_sec` 以上見えないときだけ差し替え」。flop の
1 枚だけを差し直すと他の 2 枚が載っているので差し替え対象にならず、新しい札が 4 枚目 = turn になる。

## Goal

- flop / turn / river の 1 枚だけを差し直したら、その位置の札が替わる（ストリートが進まない）。
- 読み落ち（札は載っているが読めない）の間に次のストリートの札が来ても、次の位置のまま（ISSUE-0026 の
  壊れ方を再発させない = D3 がストリート単位にした理由を守る）。
- ディーラーに入力を求めない。

## Changed Files

- `rfid/reader_thread.py` — ボードの位置決めを 4 段に:
  1. `_flop_redeal_slot`: flop が全部消えたら時間によらず flop を差し替え（`_board_pending` で残りの位置も）。
     旧 `_redeal_streets`（ストリート単位の「差し替え中」）を置き換え。turn / river の 1 枚は対象外にした。
  2. `_redeal_candidates`: 1 枚だけの差し直しの候補 = いま見えない・新しい札が最初に見えた時刻の
     `redeal_window_sec` 前より後まで見えていた・**新しい札を読んだリーダーで読まれていた**。候補が
     `release_sec` 以上見えなくなるまで新しい札の位置を決めずに待つ（戻れば次の位置）。待機はログ 1 回。
  3. 次の空き位置（従来）。
  4. `_rebuild_board`: 5 枚埋まったあとの 6 枚目で、消えた札がちょうど 1 枚なら抜いて詰める（後ろの位置から
     送る。時刻は各札の配布時刻 = `_board_first_seen`）。
  `_Run.readers`（その期間に札を読んだ reader）を追加。`_decide_board` は event のリストを返す。
  コンストラクタに `redeal_window_sec`（既定 `None` = 従来）。
- `main.py` — `_rfid_tracking_kwargs` に `redeal_window_sec`（既定 30.0、`null` で無効）。
- `config_default.json` — `rfid.redeal_window_sec: 30.0` と説明。
- `tests/test_rfid_table_flow.py` — 12 件追加（36 件）。テスト用の卓に `window` 引数。
- docs: ADR-0058 追記（D3 に改訂の印 + Alternatives 5）/ ISSUE-0035 追加の報告 / 契約 v1.6（§4・§10・版）/
  decision-log / CLAUDE.md（RFID の行・エラーハンドリング方針）/ usage.md（卓での扱い・設定表）/ CHANGELOG。

## Expected Behavior

- flop の 2 枚目を取って同じ場所に別の札を置く → 約 6 秒後（前の札が消えてから）に 2 枚目が差し替わる。
  続く turn は 4 枚目。
- turn を取ってすぐ別の札を置く → 4 枚目が差し替わる（旧実装は新しい札の確定時に前の札がまだ 6 秒消えて
  おらず、5 枚目 = river になっていた）。
- 左のリーダーの札が読めない間に、右のリーダーへ turn → 待たずに 4 枚目。同じリーダーでも、読めない札が
  戻れば 4 枚目（待つだけ）、30 秒より前から読めない札なら待たずに 4 枚目。
- flop を全部外して 40 秒後に配り直し → 1〜3 枚目を差し替え。
- 早すぎた turn を外して 40 秒後に本当の turn → いったん 5 枚目（読み落ちと区別できない）→ river が来た時点で
  早すぎた turn を抜いて詰め、turn = 4 枚目・river = 5 枚目（engine のボード・配布時刻とも正しく、重複 WARN なし）。

## Implemented Behavior

上記のとおり（テストで固定）。

## Test Results

- `pytest tests/test_rfid_table_flow.py` — 36 passed。
- `pytest tests/ -q --ignore=tests/test_vision.py`（pwsh あり）— **1221 passed**、skip 0。
- `ruff check .` — clean。
- 既存の 24 件はテスト用の卓に時間窓を渡しても不変のまま通る（`flop の 1 枚の読み落ち中に turn` は別のリーダー
  なので次の位置のまま）。

## Mismatches Found During Testing

- なし（設計段階で次を判断した）:
  - 「差し替えた札が戻ったら差し替えを取り消す」は入れない。読み落ちの誤判定は直るが、取り除いた札をボードの
    上で見せてから片付けると正しい差し直しを取り消し、以降の判断が連鎖して崩れる。戻った札は新しい札として
    次の位置に入れ、最後は 6 枚目の詰め直しに任せる（ADR-0058 Alternatives 5）。
  - turn / river の「全部消えた」（= 1 枚消えた）は時間によらない差し替えから外した。turn を揃えたときに
    リーダーから外れて読めなくなった札の上に river が来ると、river が turn の差し替えになってしまうため。
    早すぎた turn は 6 枚目の詰め直しで直す。

## Fixes Applied

なし（設計どおり）。

## Remaining Gaps / Out-of-Scope

- [ ] 店舗の実卓で確認（flop の 1 枚の差し直し / turn の差し直し / 読み落ち中の turn）。
- [ ] 既知の制約: 札が読めなくなった直後に**同じリーダーの上へ**次のストリートの札が置かれ、前の札が 6 秒以上
      読めないままだと差し直しとして記録する（`needs_review`。並びだけが入れ替わる）。実卓で頻度を見て判断。
- [ ] 新しい札を先に置き、確定（約 2 秒）より後に前の札を取ると次の位置に入る（river まで進めば詰め直しで直る）。
      運用は「前の札を先に取る」（usage.md に記載）。
- [ ] `redeal_window_sec` / `release_sec` の実測による調整。

## 追補: 店舗での確認と修正（2026-09-24 夜, ADR-0058 追記 2）

### 報告とログ

「フロップの変更はうまく行っていますが、ターンは外したカードが消えず、リバーへ遷移してしまいます。また反映に
かなり時間がかかっているようです」。店舗 PC の `logs/pokerapp.log`:

```
22:23:26 ボードの新しい札 5c: 同じリーダーから 6c が消えた直後です — 差し直しか確かめています（前の札が 6 秒見えなければ差し替え）
22:23:38 配り直しを検出: ボード 2 枚目を差し替えました（6c → 3c）
```

- flop: 5c を置いたが 6 秒の確認中（モニタが変わらない）に 5c を外し、3c を置き直していた = 「遅い」。
- turn: 「確かめています」が出ていない = 新しい札は差し直しの候補なしで 5 枚目。候補から外れる条件は
  「別のリーダーで読まれた」「30 秒超」「前の札がまだ読めていた」。3 台で 5 枚を受けるので turn の位置は
  リーダーの境目にあり、隣の台が読んだのが最有力（30 秒超もあり得る）。当時のログでは確定できないので、
  切り分け用のログと卓モニタの表示を足した。

### 変更

- `rfid/reader_thread.py`:
  - `_redeal_candidates` が `(位置, 確かめる秒数)` を返す。最後に配った turn / river（最大の位置 ≥ 4）は
    `_near`（同じか隣の board reader, config の左からの並び = `_board_order`）+ 時間窓なし。それ以外は同じ
    リーダー + 時間窓。**一度読めなくなって戻った札**（最新の期間の開始 > 確定時の開始 = `_board_first_seen`）は
    同じリーダー + 時間窓 + `release_sec`。
  - `redeal_confirm_sec`（既定 `None` = `release_sec`、本番 3.0）。
  - `_flop_redeal_slot` は flop が全部見えなくなった時点で新しい札を待たせ、全部が確認秒数以上見えないのを
    確かめてから差し替える（`(位置, 待つか)` を返す）。pending は「いま見えていない」で保つ。
  - `_log_board_commit`: 空き位置に入れるたびに INFO「ボードの札 X を N 枚目にしました（左から K 台目）—
    見えていない札: …（位置・リーダー・何秒前から）」。差し替え・待機のログにもリーダーを出す。
  - `board_presence()`: 記録したボードの札のうち、いま読めていない札（`gap_sec` 超）→ 見えなくなった時刻。
- `core/table_state.py`: `TableState.board_away_sec`（`build_table_state(board_absent_since=...)`、ボードに無い札は無視）。
- `integration/engine.py`: `board_presence` 提供関数（失敗しても卓状態は出す）。`main.py`: 2 か所で結線 +
  `_rfid_tracking_kwargs` に `redeal_confirm_sec`。`config_default.json`: `redeal_confirm_sec: 3.0` と説明。
- `tools/table_monitor.py`: 外れた札を破線・薄く + 「外れた N s」（端末表示も）。headless Chromium で描画を確認。
- `tests/test_rfid_table_flow.py`: テスト用の卓を board reader 3 台（左・中・右）に。確認 3 秒に合わせて時刻を
  直し、新規 11 件（47 件）。各規則（隣の台 / 時間窓なし / 読めなくなった札 / 確認 3 秒）を 1 つずつ外すと対応する
  テストが落ちることを確認（mutation）。
- docs: ADR-0058 追記 2（Alternatives 6・7）/ ISSUE-0035 追加の報告 2 / 契約 v1.7 / CLAUDE.md / usage.md /
  CHANGELOG / decision-log。

### テスト

- `pytest tests/test_rfid_table_flow.py` — 47 passed。
- `pytest tests/ -q --ignore=tests/test_vision.py`（pwsh あり）— **1232 passed**、skip 0。`ruff check .` — clean。

### 残

- [ ] 店舗で再確認（turn の差し直し / 取ってすぐ置く flop の差し直し / モニタの「外れた」）。turn がまだ 5 枚目に
      なるなら、「枚目にしました」のログ行で原因（見えていない札なし = 前の札がまだ読めていた / 別の台）を確認。
- [ ] 既知の制約: turn / river の読み落ち（同じか隣の台）が 3 秒以上続く間に次の札が置かれると差し直しとして
      記録する（`needs_review`, 並びだけが入れ替わる）。一度読めなくなった札は対象外。

## Related ADRs

- ADR-0058（追記 / 追記 2）/ ADR-0053 / ADR-0054 / ADR-0055

## Related Issues

- ISSUE-0035（追加の報告）/ ISSUE-0026

## Related Commits

- 本タスクのコミット（`feat(rfid): ボードの 1 枚だけの差し直しを …`）。
