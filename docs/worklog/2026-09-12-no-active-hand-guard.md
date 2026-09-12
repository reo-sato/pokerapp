# 2026-09-12 — 実機通しで出た 2 件: ハンドが無いときのアクション（ISSUE-0028）/ ストリートのずれ（ISSUE-0029）

## Goal

実機 `--cli` 通しテストで見つかった traceback を潰す。`n`（新ハンド）を押す前にアクションを
打つと `RuntimeError: No actor (no active hand or hand over)` の traceback が出て、イベントが
黙って捨てられていた（backend=pokerkit）。CLAUDE.md のエラーハンドリング方針
「認識エラーでクラッシュしない」に合わせ、**落とすなら次の操作が分かる形で落とす**。

## Changed files

| ファイル | 変更 |
|---------|------|
| `core/poker_engine.py` | `PokerEngine` protocol に `is_hand_active()` を additive 追加。`PokerkitGameState` の `get_current_player` / `legal_context` / `is_legal_actor` が `_hand_active` を見るように（`apply_action` と前提を揃える） |
| `core/game_state.py` | `GameStateManager.is_hand_active()` → 常に `True`（legacy はハンドのライフサイクルを持たない = 挙動不変） |
| `integration/engine.py` | `_current_actor_or_none()` / `_warn_no_actor()` / `_handle_winner()` を追加。`_handle_legacy_action` は actor が無ければ案内して return |
| `integration/engine.py` | （ISSUE-0029）`ActionRecord.street` を **適用前**の値で記録（両経路） |
| `docs/contracts/schemas/action.schema.json` | （ISSUE-0029）`street` の意味を明文化 + `1.0`→`1.1`（description のみ = additive） |
| `docs/contracts/versioning-and-freeze.md` | action schema の版表記を更新 |
| `tests/test_no_active_hand_guard.py` | 新規（10 ケース） |
| `tests/test_action_street_label.py` | 新規（3 ケース, ISSUE-0029） |
| `docs/issues/0028-no-active-hand-action-crashes.md` / `0029-action-street-off-by-one.md` | 新規 |
| `CLAUDE.md` / `CHANGELOG.md` / `docs/decision-log.md` | エラーハンドリング方針 / Phase H 実機状況 / 索引 |

## Expected vs implemented

| 期待 | 実装 |
|------|------|
| `n` 前のアクションで traceback を出さない | ✅ `_current_actor_or_none()` が `RuntimeError` を握り、`_warn_no_actor` が「先に新ハンド（CLI の n）を」と WARN |
| 落としたことが操作者に分かる | ✅ WARN に action 名と `raw_text` を出す |
| legacy backend の挙動は不変 | ✅ `is_hand_active()` が legacy で常に `True`、`get_current_player()` も従来どおり seat を返すため分岐に入らない（回帰: `test_legacy_backend_is_unaffected`） |
| 確定済みハンドへの winner 再宣言で二重加算しない | ✅ `_handle_winner` が `is_hand_active()` を見る（回帰: `test_double_winner_does_not_pay_twice`） |

## Mismatches found while implementing

1. **`end_hand` 後も rules-aware 経路に入っていた**。最初は `integration` 側だけ直したが、
   `test_action_after_hand_end_does_not_raise` が落ちた。`pokerkit.State.actor_index` は
   `end_hand` 後も値を持つため `legal_context()` が合法手を返し、rules-aware 経路が
   `apply_action` → `ValueError("No active hand")` で**別の traceback**を出していた。
   → actor 系照会 3 つを `_hand_active` に揃えて根本解決（`apply_action` だけが見ていたのが
   非対称だった）。
2. **winner も同じ family だった**。ハンドが無い状態では `end_hand` が例外、確定済みハンドへの
   再宣言は `pot_total = sum(hand_start_stacks) - sum(st.stacks)` を再計算して**勝者に二重加算**
   していた（traceback より悪い = 黙ったデータ破損）。同タスクで塞いだ。

## needs_review の付け方

- **ハンド進行中に手番が無くて落とした**（全員オールイン後など）→ 記録が欠けるので
  `_hand_needs_review = True`。
- **そもそもハンドが無い**（`n` 前 / 確定後）→ 付け先が無いので立てない。次の `_start_new_hand`
  でどのみちリセットされる。

## 追加で見つかった: ストリートのずれ（ISSUE-0029）

実機ログの「フロップで 席1 が check → 席1 が bet と 2 回続く」が気になり、同じ 6 アクションを
スクリプトで再現したところ **`ActionRecord.street` が 1 つ先にずれていた**。

```
  [preflop] 席2 call 1      [preflop] 席2 call 1
  [flop]    席1 check   →   [preflop] 席1 check     ← BB のチェックは preflop
  [flop]    席1 bet 5       [flop]    席1 bet 5
  [turn]    席2 call 5  →   [flop]    席2 call 5    ← flop を閉じたコール
  [turn]    席1 bet 50      [turn]    席1 bet 50
  [showdown]席2 fold    →   [turn]    席2 fold      ← turn の fold
```

- 原因は `street=gs.street` を **`apply_action` の後**に読んでいたこと。legacy はここで
  ストリートが動かないので無害だったが、pokerkit は**ラウンドが閉じると内部で次ストリートへ
  自動進行**する。→ 適用前の値を控えて記録するように修正（`street_at_action`）。
- **警告も出ずハンド履歴が間違う**ので ISSUE-0028 より重い（Severity High）。Phase G で live 既定を
  pokerkit にした時点から埋まっていたが、golden fixtures がストリート境界を跨いでいなかったため
  テストでも検出できていなかった（= 回帰テストの穴も同時に塞いだ）。
- 契約: `action.schema.json` の `street` に意味を明文化した description を追加し `1.1`
  （validation 不変の additive, `versioning-and-freeze.md` §2）。
- 「同じ席が 2 回続く」自体は **pokerkit の正しい heads-up 手番順**だった（BB がプリフロップを
  チェックで閉じ、ポストフロップも BB が先）。表示がずれていただけ。

## Test results

```
python -m pytest tests/ -q --ignore=tests/test_vision.py
848 passed, 2 warnings      # 835 → 845（ISSUE-0028 +10）→ 848（ISSUE-0029 +3）
```

ISSUE-0029 の回帰 2 件は fix を戻すと落ちることを確認済み（`git stash` で検証）。

## Remaining gaps

- 実機でのマイク有り通し（音声→JSON/PHH）、`--export-phh`、`--ledger` + スマホ注文。
- ジョーカー 2 枚目（`JK`）の `rfid_cards.json` 登録。
- ISSUE-0023（reader 0 の `RST診断 during_rst=0`）は Open のまま。

## Related

- ISSUE-0028 / ADR-0009（rules-aware backend 境界）
- 同じ実機通しで出た ISSUE-0026（board 位置の同期点）/ ISSUE-0027（ひらがな入力）
