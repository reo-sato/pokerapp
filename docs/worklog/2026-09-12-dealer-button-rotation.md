# Worklog: ディーラーボタンの回転とポジション名（P0b / ISSUE-0032）

## Date

2026-09-12

## Scope / Task

ADR-0045 の **P0b**。ハンドごとにディーラーボタンを回し、`button_seat` / `position_map` /
`ActionRecord.position` を記録し、ディーラーの**ポジション名の読み上げ**を actor 推定の
明示証拠として使えるようにする（ISSUE-0032 の解消）。

## Goal

仕様 `sprc_v4.docx` FR-05b（ボタン回転）/ §6.1・§6.2（`position_map` / `position` の記録）/
FR-26（ターン順がアクター推定の最優先証拠）/ §7（「BTN、コール」の読み上げ）を満たす。

「done」の形:

- 6-handed を 6 ハンド回すとボタン・ブラインド・初手 actor が 1 席ずつ進む。
- ポジション名が **実際にブラインドを出す席**と一致する（名前だけの飾りでない）。
- 「BTN、コール」が「シート1、コール」と同じ強さの明示証拠として actor を決める。
- 既存 golden fixtures（1 ハンド物）の**値が 1 つも変わらない**（回転の導入が過去の記録を
  遡って書き換えないことの確認）。

## Changed Files

- `core/positions.py` — **新規**。ボタンとポジション名の純粋ロジック。
  `seat_order_from_button` / `next_button` / `position_names` / `position_map` /
  `seat_for_position` / `parse_position` + `POSITION_ALIASES`。I/O もゲーム状態も持たない。
- `core/poker_engine.py` — `PokerkitGameState` にボタンを導入。`new_hand` で 1 つ進め、
  ボタンの次の席を先頭にした並びで `create_state` を呼ぶ。`button_seat` プロパティと
  `position_map()` を `PokerEngine` Protocol に additive 追加。`create_game_state` に
  `button_seat` 引数。
- `core/game_state.py` — legacy の stub（`button_seat` は None、`position_map()` は `{}`）。
- `core/hand_log.py` — `HandSummary.button_seat` / `position_map`、`ActionRecord.position`
  を additive 追加（`to_dict` も）。
- `core/events.py` — `AudioEvent.position`（読み上げられたポジション名）。
- `audio/recognizer.py` — `parse_action` が `parse_position` でポジション名を拾う。
- `integration/engine.py` — `_sensed_seat`（席番号 → ポジション名の順で明示証拠を解決）を
  追加し `_resolve_actor` / `audio_agree` から使う。`_position_of` / `_safe_position_map` を
  ActionRecord / HandSummary / 卓状態に結線。
- `integration/replay.py` / `output/event_recorder.py` — `position` の記録・復元。
- `core/table_state.py` / `tools/table_monitor.py` — 卓状態にボタンとポジション名を表示。
- `main.py` — セッション設定で「1 ハンド目のボタン席」を訊き、`_make_game_state` に渡す。
- `docs/contracts/schemas/hand.schema.json` — `1.1` → `1.2`（`button_seat` / `position_map`）。
- `docs/contracts/schemas/action.schema.json` — `1.1` → `1.2`（`position`）。
- `docs/contracts/schemas/reconstruction_event.schema.json` — `0.1` → `0.2`（audio の `position`）。
- `tests/test_positions.py`（新規, 54 件）/ `tests/test_position_evidence.py`（新規）/
  `tests/test_table_state.py` / `tests/test_event_recorder.py` に回帰を追加。
- `tests/test_poker_engine.py` / `tests/test_phase_d0_engine.py` — 「ボタン固定」前提の 2 件を修正。
- `tests/fixtures/reconstruction/*/expected_hand.json` — 追加キーのみ再生成（5 件）。

## Expected Behavior

- ボタンは**ハンドごとに 1 つ**、席番号の昇順で回る。1 周で全席を訪れる。
- `position_map` は `button_seat` から決定的に導出され、SB/BB と名付けた席が実際に
  ブラインドを出している。
- ディーラーが読み上げたポジション名は、席番号と同じ経路（`_resolve_actor` の sensed）で
  actor を決める。両方あれば**席番号が優先**。
- 卓に無いポジション名（6-handed の "UTG+2" 等）は**無視**され、手番は動かない。
- legacy backend は**一切変わらない**（ボタンを持たない = rollback path の挙動不変契約）。
- 既存 golden fixtures は additive キーが増えるだけで、既存の値は変わらない。

## Implemented Behavior

上記のとおり。設計上の要点 2 つ:

1. **初期ボタンは「最大の席番号」**（`next_button(seats, None) == max(seats)`）。ボタンの次の席が
   先頭になるので、1 ハンド目の並びは `sorted(seats)` と一致する。結果としてボタン導入前と
   1 ハンド目が同一になり、既存 golden fixtures が「値の変化なし」で通る。回転はハンド 2 から。
   CLI の入力（「1 ハンド目のボタン席」）は内部表現に合わせて **1 つ手前**に変換して渡す。
2. **ポジション名の席への解決は engine 側**（`IntegrationThread._sensed_seat`）。recognizer は
   ゲーム状態を持たない規約なので、正準名を `AudioEvent.position` で持ち回るだけにした。

heads-up の並びは実測で確定した: pokerkit の `create_state` は index 0 に BB・index 1（= 末尾 =
ボタン）に SB を post する。したがって `position_names(2) == ["BB", "BTN"]`（ヘッズアップは
**ボタンが SB を出す**）。`tests/test_positions.py::test_position_names_match_who_posts_blinds` が
2/3/6/9 人でこの対応を固定する。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **976 passed**（skip 0）。
  実装前は 911 passed（うち本件着手直後に 7 failed）。
- golden fixtures の差分検査: 削除行はすべて `"confidence": X` → `"confidence": X,`
  （後続キー追加によるカンマのみ）。**値の変化ゼロ**を確認。
- 実測（`PokerkitGameState`, 6-handed, 6 ハンド）: `button = [6,1,2,3,4,5]`、初手 actor は
  6 席すべてを 1 周。

## Mismatches Found During Testing

1. **heads-up のポジション名が逆だった**。最初 `["SB","BB"]` と書いたが、pokerkit の実挙動は
   index 0 = BB / 末尾（ボタン）= SB。ポーカーの規則どおり「HU はボタンが SB」が正しく、
   実装の方を直した。
2. **「ボタン固定」を前提にしたテストが 2 件**あった（本体の欠陥ではなく、旧前提の固定）。
   - `test_rebuy_between_hands`: 次ハンドで seat 3 がブラインドを出すようになり
     `get_stacks()[3]` が 14800 になった。
   - `test_synthesizes_intermediate_folds`: コメントに「button 固定なので新ハンドでも同順」と
     書かれていた前提が成立しなくなった。
3. `event_to_envelope` に `position` を足すと `test_audio_envelope` の完全一致が落ちた
   （記録契約の変更なので期待値側を更新するのが正しい）。

## Fixes Applied

- `position_names(2)` を `["BB", "BTN"]` に変更し、2/3/6/9 人でブラインド額と名前の対応を
  固定する回帰テストを追加した（名前が飾りに退化しないようにするため）。
- `test_rebuy_between_hands` を「持ち込み総額 = `stack + committed`」で見る形に変更。
  ブラインドを出しても持ち込みは変わらない、という本来の意図を保つ。
- `test_synthesizes_intermediate_folds` を、当該ハンドの state から非破壊に手番順を取る
  `_action_order` を使う形に変更（2 ハンド目で観測する必要がなくなった）。
- `event_to_envelope` / `event_from_envelope` の往復テストを追加。記録しないと replay が
  別の actor を選びうる（R4 の決定性が壊れる）ため、schema にも optional で足した。

## Remaining Gaps / Out-of-Scope

- [ ] **SB/BB の自動 post は pokerkit backend のみ**。legacy backend はボタンも post も持たない
      （CLAUDE.md の「ディーラーボタン自動回転 / SB・BB 自動 post ❌ 未実装」は legacy の話として残る）。
- [ ] **ミッドセッションの着席/離席でボタンが飛ぶ**ケースは扱っていない。`_seats` は
      `PokerkitGameState` の生成時に固定されるため、席の増減にはセッションの作り直しが要る。
- [ ] GUI（`gui/dashboard.py`）にボタン表示・ボタン席の手動指定は入れていない。CLI の設定と
      卓状態モニタで足りる範囲に留めた。
- [ ] `turn_order` の明示記録（FR-05b の 3 つ目）は `position_map` から導けるため持っていない。
- [ ] ポジション名の別名は日本語/英語の代表形のみ。実運用の読み上げログが貯まってから拡張する。

## Related ADRs

- `docs/adr/0045-post-hoc-probabilistic-action-history.md` — P0b の出典。
- `docs/adr/0009-rules-aware-hand-reconstruction.md` §5 — 「BTN 基準の安定全単射」の未実施分。

## Related Issues

- `docs/issues/0032-dealer-button-never-rotates.md` — **Fixed**。
- `docs/issues/0031-live-deterministic-actor-inference-drift.md` — 上流の方針（事後・確率的推定）。
- `docs/issues/0033-rfid-appear-is-not-an-action.md` — 直前の P0a。`_sensed_seat` は
  P0a の判断（RFID の検出を actor 証拠にしない）をそのまま引き継いでいる。

## Related Commits

- 本タスクのコミット（`feat(engine): ディーラーボタンを回す …`）。
