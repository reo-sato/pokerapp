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

## Related ADRs

- ADR-0058（追記）/ ADR-0053 / ADR-0054 / ADR-0055

## Related Issues

- ISSUE-0035（追加の報告）/ ISSUE-0026

## Related Commits

- 本タスクのコミット（`feat(rfid): ボードの 1 枚だけの差し直しを …`）。
