# Worklog: verify-v1 のマージ（RFID 非証拠方針を優先）

## Date

2026-09-22

## Scope / Task

`claude/confident-hawking-5e4hff`（RFID 実機 bring-up / ミスディール訂正 / 観測時刻 / 卓状態モニタ /
P0a / P0b = 73 コミット）に `origin/verify-v1`（solver 基盤 / リプレイ UI / 注文キャンセル /
menu 編集 / **復元の正当性修正バッチ ADR-0047〜0050** = 17 コミット、共通祖先 `45f1af0` から 3 か月
分岐）を取り込む。オーナー判断: **「RFID の検出は actor の証拠にしない」（P0a / ISSUE-0033 /
ADR-0056）を優先し、verify-v1 の ADR-0049 G3 の該当部分を supersede する**。

## Goal

- 18 ファイル・51 か所のコンフリクトを解消し、両側の機能をすべて残す（落とすのは G3 の actor 証拠部分だけ）。
- 番号衝突（ADR / schema 版）を解く。
- `pytest`（vision 除外）と `ruff check .`（CI ゲート）が緑。
- golden fixtures は verify-v1 のルール「pin は手計算検証必須」（event-replay.md §6.5）に従い、
  変わったものを手計算で確認する。

## Changed Files

**マージ前の準備（`ed8be5b`）**

- `docs/adr/0040〜0045-*.md` → `0051〜0056-*.md` に git mv。参照 1001 行を書き換え（`ADR-0034/0040/0041`
  のような連結表記と `docs/adr/0040-…` のパス表記を含む。0040〜0045 の残存参照ゼロ）。
- `main.py` — ruff F841（未使用 `audio_cfg` ×2）。

**マージ本体（コンフリクト解消）**

- `integration/engine.py`（17 か所）— verify-v1 の B1（fold 合成後の legal_ctx 再取得）/ B2・B4
  （unresolved レコード, `_handle_audio_event` で例外捕捉）/ B5（winner fallback 連鎖 + 空 summary 抑止）/
  S5（`stack_start` = ブラインド post 前）/ S6（`pot_total` = engine の pots）/ G1（制御語ガード）/
  G2（監査フィールド）/ T1〜T3（発話区間窓, `event.timestamp` 由来の時刻）/ S7（split pot）を取り込み、
  こちらの ISSUE-0028/0029, ADR-0054/0055, 卓状態 publish, P0a, P0b を残す。
  **`_pop_nearest_rfid_seat` は採らない**（P0a）。`_resolve_actor` は `(actor, conflict, folded,
  conflict_reason)` を返し、G3 の監査 reason を引き継ぐ。`actor_source` は `spoken_seat` /
  `spoken_position`（新設）/ `engine_prior` / `unresolved`（`"rfid"` は emit しない）。
  `_synth_fold_timestamp(seat, event)` の既定はイベント時刻（T3）、RFID 不在観測があればそれ（ADR-0055）。
  winner は `is_hand_active()`（ISSUE-0028）→ B5 の順で判定。`_emit_unresolved` は
  `reason="no_active_hand"` で「先に新ハンド」を案内し `position` も載せる。
- `audio/recognizer.py`（4）— NFKC + ひらがな→カタカナ（ISSUE-0027）を重ね、キーワード側にも同じ
  正規化を掛ける（「降ります」がカタカナ化した入力と食い違わないように）。`parse_amount_ex` /
  `_extract_all_seat_nos` / `parse_flags` / `utterance_start_ts`（verify-v1）+ `position`（P0b）。
- `core/events.py` / `core/hand_log.py` / `integration/replay.py`（各 1〜2）— フィールドの union。
- `main.py`（2）— IntegrationThread 引数の union + `control_conf_threshold`。**追加**: キーボード入力は
  `parse_action(line, confidence=1.0)`（None は「Whisper 欠測」= 保守既定 0.5 で要レビューに倒れる,
  ADR-0047 B3。操作者の意図的な入力なので満点）。CLI の `on_action` は unresolved を
  `[未適用] <action> (<reason>)` と表示。
- `rfid/reader_thread.py`（1）— こちらの集合デバウンス等に verify-v1 の `health` を追加。
- `docs/contracts/schemas/{action,hand,reconstruction_event}.schema.json` — 版の衝突（両側が `1.1`/`1.2`/`0.2` を
  別の意味で使用）を union で `action 1.3` / `hand 1.3` / `reconstruction_event 0.3` に統合。
- `tests/fixtures/reconstruction/*` — 13 件を再生成（下記）。`rfid-vs-spoken-seat-conflict/events.jsonl` と
  `multi-hand-session/events.jsonl` を作り直し。
- `tests/test_reconstruction_hardening.py` / `test_phase_d2_wiring.py` / `test_position_evidence.py` /
  `test_no_active_hand_guard.py` — 期待値を新セマンティクスへ。
- docs: `CLAUDE.md`（3）/ `CHANGELOG.md`（1 = 両側の Unreleased を連結）/ `docs/decision-log.md`（1）/
  ADR-0049 Status update / ADR-0056 Status / ISSUE-0028・0033 追記 / `versioning-and-freeze.md`。

## Expected Behavior

- verify-v1 の全機能 + こちらの全機能が共存し、RFID の seat 読みだけは actor を動かさない。
- 記録の版: `action 1.3` / `hand 1.3` / `reconstruction_event 0.3`。旧記録はそのまま読める（すべて optional）。
- CI ゲート（ruff → pytest）が緑。

## Implemented Behavior

上記のとおり。判断を要した点:

1. **RFID 証拠** — ADR-0049 G3（active 席のみ）は配布直後（全席 active）を防げないので P0a を優先
   （オーナー承認）。G3 の監査 reason は残した。`TestTimingT::test_t2_...` の `actor_source == "rfid"` は
   `"engine_prior"`（`source.rfid` は True のまま = 裏付けは効く）に変更。
2. **ハンド外イベント** — こちらの「落として案内」（ISSUE-0028）を verify-v1 の B2「unresolved レコード」に
   寄せた（黙って消えない）。ただし確定済みハンドへの winner 再宣言は `is_hand_active()` で止める
   （B5 の「end_hand 失敗でも書き出す」に落とすと空 summary を量産する）。
3. **whisper 欠測 = 0.5（B3）** — CLI/`tools/play_hand_text.py` のキーボード入力は confidence=1.0 を
   明示（ASR ではない）。実プレイ環境テストのダミーアクションが全部 needs_review になるのを避ける。
4. **時刻** — T3（`event.timestamp` 由来）を採り、合成 fold だけ RFID 不在観測を優先（ADR-0055）。
5. **street** — ISSUE-0029（適用**前**のストリート）を維持。verify-v1 の golden は適用後（ラウンドを
   閉じたアクションが `flop`/`showdown` になる）を pin していたので再生成で直る。
6. **ボタン回転（P0b）と verify-v1 の 2 ハンド fixture** — `multi-hand-session` の hand 2 はボタンが
   回るので raise するのが seat 1（BTN）になる。宣言勝者を `シート1` に直した（残った唯一の席）。

## Test Results

- `ruff check .` — All checks passed（マージ前の 3 件は verify-v1 側で修正済み、2 件はこちらで修正）。
- `pytest tests/ -q --ignore=tests/test_vision.py` — **1148 passed**（マージ前: こちら 978 / verify-v1 の
  CI は 160d96b で緑）。
- golden 13 件の手計算検証（変わったもの）:
  - `rfid-appear-is-not-an-action`: BTN(3) call 200 → pot 500、seat 1 が勝って 10400（−100 +500）。
    RFID seat 1 は actor を動かさず、裏付けにもならない（別席）。
  - `rfid-vs-spoken-seat-conflict`（作り直し）: RFID seat 3 の読み → 「シート1 コール」で seat 3 を
    fold 合成（conf 0.3, review）、SB(1) call 100 → pot 400；BB check → flop；SB bet 400 → 800；
    BB fold → 「シート1 ウィナー」で seat 1 = 9400+800 = 10200（+200 = BB の 200）。
  - `multi-hand-session` hand 2: ボタン 1 → SB=2 / BB=3 / BTN=1。BTN raise 600（9900→9300）、
    SB fold（10000）、BB fold（9800）、seat 1 が 900 を取って 10200（+300）。`stack_start` は
    post 前（9900 / 10100 / 10000 = S5）。
  - `postflop-street-transition` / `full-ring-6max` / `split-pot-chop`: street が「適用前」に
    なるだけ（金額・席は不変）。
  - 他 7 件: 追加キー（position / button_seat / position_map / board_timeline）以外の差分なし。

## Mismatches Found During Testing

- `test_reconstruction_hardening.py::TestVocabularyV1::test_synonyms[降ります]` — こちらのカタカナ化で
  ひらがなの語彙が一致しなくなった → キーワード側にも同じ正規化を掛けて解消。
- confidence を渡さない AudioEvent を使うこちらのテスト 4 件が、B3（欠測 = 0.5）で needs_review に
  なった → テストで `confidence=0.9` を明示（verify-v1 が自テストで行ったのと同じ）。
- `test_no_active_hand_guard.py` 4 件 — 「記録を作らない」から「unresolved レコードを流す」への
  期待値変更。`test_winner_without_seat_uses_current_actor` は engine 経由で new_hand する形に
  （B5 の junk 判定は `_hand_open` を見る）。
- `rfid-vs-spoken-seat-conflict` の元 events は「RFID が勝つ」前提で組まれており、P0a では
  fold 不能な席が fold する不整合な列になる → 作り直し。

## Fixes Applied

上記「Implemented Behavior」「Mismatches」のとおり。

## Remaining Gaps / Out-of-Scope

- [ ] verify-v1 に**この branch を取り込む**操作（fast-forward または PR #32 のマージ）はオーナーの承認待ち。
- [ ] `actor_source="rfid"` の schema enum は旧記録互換のため残置。計測ハーネス
      （`tools/measure_capture_accuracy.py`）の切り分け表で "rfid" が空になることを次の Phase A 計測で確認。
- [ ] ADR-0047 B5 で `_fallback_winner_seat()` が None（全員オールインの runout 中に new_hand を
      重ねた等）のとき `_finalize_hand(None, …)` が review 付き summary を書く経路は verify-v1 由来のまま。
- [ ] ADR-0056 P1〜P8 は未着手（変わらず）。

## Related ADRs

- `docs/adr/0056-post-hoc-probabilistic-action-history.md`（P0a/P0b）/ `docs/adr/0049-…md`（Status update）
- `docs/adr/0047-…md` / `0048-…md` / `0050-…md`（取り込み）

## Related Issues

- `docs/issues/0033-rfid-appear-misused-as-actor-evidence.md`（追記）/ `0028-…md`（追記）/ `0032-…md`

## Related Commits

- `ed8be5b` ADR 振り直し + ruff / 本マージコミット（`Merge origin/verify-v1 …`）
