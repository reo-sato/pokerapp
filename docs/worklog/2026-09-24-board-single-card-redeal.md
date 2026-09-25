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

## 追補 2: 置いてから読めるまでの遅れ（2026-09-24 夜, ADR-0058 追記 3）

### 報告とログ

「新しいターンカードの 3s、新しい 2 枚目フロップの Ac の反映がだいぶ遅い。Ac はターンを置いた後、3s は揺らしてから
反映された」。店舗のログ（`枚目にしました|確かめています|配り直しを検出`）を読むと:

- どの札も「最初に読めてから数えるまで」は 2 秒、差し替えは即時（7c → 2c, Ac → 4s）= ホストの判定は想定どおり。
- 7c を外してから新しい札が初めて読めるまで約 10 秒、3s は揺らすまで読めていない = **置いてから読めるまで**が遅い。
- 3s は読めたとき 4s がまだ読めていた（見えていない札なし）ので 5 枚目。Ac は右端（ターンの位置）、2c は左
  （フロップの位置）で読まれており、報告と札の名前が逆に見える（取り違え / 登録の入れ替わりの確認を依頼）。
- firmware: 既に札がある台で新しい札を探す完全確認は 6 周に 1 回（≈ 1.8 秒）。揺らすまで読めないのは、位置
  （リーダーの境目）か重ね置き（capture effect で強い方の札だけが応答し、Stay Quiet が効かないと 2 ラウンドで
  打ち切る）が主因と考えられる。firmware は変えていない。

### 変更

- `rfid/reader_thread.py`: 確定前の board の札は `_PENDING_GAP_SEC`=3 秒までの途切れを許す（確定後・席は
  `gap_sec`）。見え始め「ボードに札 X が載りました（左から K 台目）」と読み直し（1 枚 3 回まで）を INFO。
  `_Run.waiting`（差し直し確認中）。`board_presence()` は `{"absent", "pending"}`。
- `core/table_state.py`: `TableState.board_pending`（`build_table_state(board_pending=...)`）。
- `integration/engine.py`: 新しい形を卓状態に渡す。`tools/table_monitor.py`: 「確認中 N s」「差し直し確認中」
  （点線・青）+ 端末表示。headless Chromium で描画を確認。
- tests: 5 件追加・2 件更新（52 件）。許容を 1.5 秒に戻すと「途切れながら読める札」のテストが落ちる（mutation）。
- docs: ADR-0058 追記 3 / ISSUE-0035 追加の報告 3 / 契約 v1.8 / CLAUDE.md / usage.md / CHANGELOG / decision-log。

### テスト

- `pytest tests/test_rfid_table_flow.py tests/test_table_state.py` — 79 passed。
- `pytest tests/ -q --ignore=tests/test_vision.py`（pwsh あり）— **1237 passed**、skip 0。`ruff check .` — clean。

### 残

- [ ] 店舗で再確認: 札を置いた直後に卓モニタへ「確認中」が出るか（出なければ読めていない = 位置・重ね置き）。
      ログの「載りました」の時刻と置いた時刻の差。
- [ ] Ac / 2c の名前の確認（1 枚ずつ空いた席のリーダーに置いて卓モニタの表示を見る）。
- [ ] 読めにくい位置が特定できたら、リーダーの配置 / firmware の完全確認の周期を検討（別 issue）。
      → 追補 3: turn の位置（中と右のリーダーの境目）と特定。配置の推奨は ISSUE-0035 追加の報告 4。

## 追補 3: turn と river が逆になる（2026-09-24 夜, ADR-0058 追記 4）

### 報告とログ

「フロップはよく読めています。ターンの読み取りが難しいようで、ボードの真ん中に向けて動かしたら読めました。
５枚のボードに対して３つのリーダーが配置されているのですが、これらとフロップ、ターン、リバーの配分を考え直す
べきだと思います。また、ターンを T に変更したあと、リバー J を配置したのですが、逆に反映されてしまっている
ようです。」ログ（ISSUE-0035 追加の報告 4 に全文）: 9s（4 枚目, 左から 2・3 台目）→ Ts に差し直し。Ts は 12.7 秒
読めずに読み直してから確定、その後も載ったまま 15.1 秒読めず、その間に置いた Js が 4 枚目を差し替え、戻った Ts
が 5 枚目に入った。flop 3 枚は読み直し無し。

### 期待と実装

- 期待: Ts = 4 枚目・Js = 5 枚目。読みにくい位置の札の読み落ちを差し直しと取り違えない。取り違えても、札が戻れば
  直る。
- 実装（`rfid/reader_thread.py`）:
  - `_board_runs`: ボードの札ごとの「載り続けている期間」の数（このハンド）。`_sight` で新しい期間ごとに +1。
    `_redeal_candidates` は 2 以上の札を候補にしない（旧: 確定後に途切れた札だけを厳しい側 = 6 秒・同じリーダー）。
    候補は位置のリストだけになり、確認は常に `redeal_confirm_sec`。
  - `_board_retired`: 差し替えた前の札 → (位置, 配った時刻, 差し替えた札, 読んでいたリーダー)。
    `_restore_board_card` を `_decide_board` の滞留の後・flop 全体の判定の前に呼ぶ。差し替えた札が同じ位置にあり・
    いまも見えていて（`gap_sec` 以内）・最後に配った札（`_board_first_seen` が最大）で、戻った札が前のリーダーか
    その隣で読め、元の位置より後ろに空きがあれば、戻った札を元の位置（`replaces` = 差し替えた札, 元の配布時刻）→
    差し替えた札を次の空き位置、の順に送る。条件を満たさないときは記録を残したまま（次の poll で再判定）、位置を
    得たら `_place_board_card` が記録を消す。
  - `_slot_label` に「読み直し N 回」。`reset_board_positions` で `_board_runs` / `_board_retired` を捨てる。
  - 使わなかった案: 取り消しで「後に配った札」も並べ直す（読み落ちが 2 枚にまたがっても直るが、回収した札を
    ボードに置いたとき正しい turn まで崩す）→ 最後に配った札のときだけ。flop 全体の配り直しの差し替えも取り消しの
    対象に含めた（flop が全部読めない間に turn が置かれた場合、戻った最初の札で直る。違反運用では取り消さなくても
    誤りなので悪化しない）。

### テスト

- `tests/test_rfid_table_flow.py` 58 件（6 件追加・2 件置換）: 店舗のログの再現（Js は 5 枚目、ログに「読み直し
  1 回」、engine のボード `5d Tc 2h Ah 6h`）/ 途切れた札の本当の差し直しは次の位置 → 6 枚目で詰め直す / flop の
  読み落ちの上の turn を戻った札で取り消す（engine のボード・配布時刻・重複 WARN なし）/ turn の読み落ちの上の
  river を取り消す（同）/ 差し直しを戻した = 通常の差し直し / 離れたリーダーに現れた札・後に別の札を配ったあとに
  戻った札は取り消さない / 新ハンドで読み直しの記録を捨てる。
- 修正前の `reader_thread.py` で新テストのうち 4 件が落ちる（店舗のログの再現を含む）ことを確認。新ハンドのリセット
  を外すと新ハンドのテストが落ちる（mutation）。
- `pytest tests/ -q --ignore=tests/test_vision.py`（pwsh あり）— **1243 passed**、skip 0。`ruff check .` — clean。

### 残

- [ ] 店舗で再確認: turn を差し直したあと river を置く / turn の位置で札を 30 秒置いたまま「外れた」が出ないか。
- [ ] リーダーの配置（ISSUE-0035 追加の報告 4 の推奨）: 当面は「左 = flop 1・2 / 中 = flop 3 + turn / 右 = river」で
      置く位置をモニタで確かめて印。根本は board 5 台（予備コネクタ #4 / #11 の修理 + `PN5180_READER_COUNT` 13）。
- [ ] 読みにくい位置の turn を本当に差し直し、turn でハンドが終わると 5 枚のまま（`needs_review` にならない）。
      配置で読みにくい位置を無くすのが本筋。

## 追補 4: 置き方の決定と、river のあとの turn の差し直し（2026-09-25, ADR-0058 追記 5）

### 報告とログ

オーナーが置き直しを繰り返して確かめ、「flop と turn の間を空けて turn を 3 枚目のリーダーに読ませるのがよさそう」と
決定。ログ（ISSUE-0035 追加の報告 5）を追うと、flop 全体・flop の 1 枚・river の差し直しと、読み落ちたことのある turn
の差し直し → river で詰め直しは想定どおり。13:46:54 だけが誤り: river まで配ったあとで turn（6s → Ks）を差し直すと
`Qs, Ks`（river が 4 枚目・新しい turn が 5 枚目）。

### 再現と原因

同じ手順（turn は読み落ちたことがある → river → turn を取って新しい札）を `Table` ドライバで再現:
取ってから 5 秒後に置くと `[('Ah', 5, '6h'), ('6h', 4, '7c')]`（逆）、1 秒後に置くと「5 枚を超えました」で
`('Ah', None, None)` のまま確定（二度と位置を与えない = engine のボードが 6 枚）。5 枚埋まったボードの規則
（`_rebuild_board`）が「新しい札 = 次のストリート」と決め打ちし、消えた札が `release_sec` に達する前に確定させていた。

### 変更（`rfid/reader_thread.py`）

- `_settle_full_board`: 見えていない札（`gap_sec` 超）がちょうど 1 枚で `release_sec` 以上 → `_replace_on_full_board`。
  見えていない札がまだ `release_sec` 未満なら None（待つ。`run.waiting` = 卓モニタの「差し直し確認中」、ログ 1 回）。
  無い / 2 枚以上消えて決められない → 従来どおり WARN・位置なし。`release_sec=None` は従来どおり即 WARN。
- `_replace_on_full_board`（`_rebuild_board` を置換）: 消えた札のあとに空き位置へ入れた札（`_board_swapped_in` に
  無く、`_board_first_seen` が消えた札の `last_seen` より後）があれば、最初のものを消えた札の位置へ移し、その後ろを
  1 つ前へ詰めて新しい札を 5 枚目に。無ければ新しい札を消えた札の位置へ。engine へは後ろの位置から送る。
- `_board_swapped_in`: 差し替えで位置を得た札（1 枚の差し直し・flop 全体・5 枚埋まったボードの置き換え）。取り消しで
  次の位置へ移った札・空き位置に入れた札は外す。新ハンドで捨てる。
- `_decide_board`: `run.fired` を実際に決めたときだけ立てる（待つ場合は立てない）。
- docs: ADR-0058 追記 5（Alternatives 10・11、並び順の修正）/ ISSUE-0035 追加の報告 5 / 契約 v1.10 / CLAUDE.md /
  usage.md（置き方・river のあとの差し直し）/ CHANGELOG / decision-log。

### テスト

- `tests/test_rfid_table_flow.py` 63 件（5 件追加・1 件更新）: river のあとの turn の差し直し（店舗の再現・engine）/
  取ってすぐ置くと待つ / 差し替えで入った river は turn の代わりにしない / 離れたリーダーで差し直した flop の札を river
  で移す / 読めていない札が戻れば 6 枚目の WARN / 時間窓の外の flop の差し直し（移すのは Ah だけ = Tc・2h は動かない）。
- 修正前のコードで 6 件が落ちる。`_board_swapped_in` の除外を外すと 1 件が落ちる（mutation）。
- `pytest tests/ -q --ignore=tests/test_vision.py`（pwsh あり）— **1248 passed**、skip 0。`ruff check .` — clean。

### 残

- [x] 店舗で再確認: 新しい置き方で turn / river の差し直し（river のあとで turn も）。→ 14:12〜14:13 のログで、
      5 枚とも途切れなしで 2 秒で確定、turn の差し直し 2 回（river のあとを含む）が 4 枚目で反映（ISSUE-0035）。
- [ ] 13:47:03 の 2s（3 台目で一度読めて 148 秒読めず）が何だったか（置いた位置 / すぐ外した / 重ねた）。
- [x] 14:02:44 に席 1 へ置かれた未登録の札（`E0:04:01:53:1C:2A:B5:D6`）が何か確認し、デッキの札なら登録する。
      → ジョーカー（登録不要。卓のデッキから抜く）。未登録の札をアプリで無視する案はオーナーの判断待ち。
- [ ] 新しいハンドは `n` を押してから配る運用の徹底（押さないと前の turn / river の位置が残る）。

## Related ADRs

- ADR-0058（追記 / 追記 2 / 追記 3 / 追記 4 / 追記 5）/ ADR-0053 / ADR-0054 / ADR-0055

## Related Issues

- ISSUE-0035（追加の報告）/ ISSUE-0026

## Related Commits

- 本タスクのコミット（`feat(rfid): ボードの 1 枚だけの差し直しを …`）。
