# 2026-09-12 — ミスディールの載せ替えを訂正できるようにする（ADR-0043）

## Goal

実運用の要件に答える: **ゲームの性質上、一度読み込ませたカードを外して、新しいカードを正の
ハンド / ボードとして読み込ませることがある**。現状これに対応できるか調べ、できないなら直す。

## 調査結果（着手前の実挙動）

| ケース | 現状 |
|--------|------|
| ボードの誤った 1 枚を外して置き直す | ❌ 誤った札が位置を持ち続け（ハンド内 append-only）、置き直した札が**次の空き位置**を取る → ボードに 1 枚余分、**枚数でストリートが誤って進む** |
| 席のホールカードを差し替える | ❌ `_hole_cards[seat]` が **2 枚で打ち止め**なので差し替えが**黙って無視**される |
| ハンドごと配り直す | ✅ `n`（新ハンド）で全リセット。進行中のハンドは JSON に書かれず捨てられる |

つまり「ハンドごとのミスディール」は扱えていたが、「**1 枚だけ差し替える**」（ボードの露出カードの
張り替え等、ルール上ハンドを続行するケース）が壊れていた。

## なぜ素朴な修正がだめか

ISSUE-0026 で **「カードが見えなくなったら位置を解放する」を意図的に捨てている**。absence が
信用できないため（隣接リーダーの磁界の重なり = ISSUE-0025 / 結合の弱い台の間欠読み = 2026-09-12 の
実機で `Tc` が同じ位置で 10 回以上再検出）。解放に戻すと一瞬の読み落ちが載せ替えと解釈され、
**同じ札が 2 か所 / 枚数の水増し**が再発する。

→ **ミスディールはディーラーが宣言する明示イベント**として扱い、コマンドで解放する（ADR-0043）。

## Changed files

| ファイル | 変更 |
|---------|------|
| `rfid/reader_thread.py` | `forget_board_position(index)`（1 位置だけ解放 + board デバウンス落とし、解放した UID を返す）/ `forget_seat_cards(seat)`（席リーダーのデバウンス落とし）。`_seat_reader_ids` を poll 時に学習。`removed` の stale コメント（「presence 同期が解放する」= 廃止済み）を修正 |
| `integration/engine.py` | `correct_board` / `correct_seat` アクション + `_handle_correct_board` / `_handle_correct_seat` / `_notify_card_correction`。`on_card_correction` フックを additive 追加（既定 None） |
| `main.py` | CLI `cb <位置>` / `cs <席>` + ヘルプ行 + `_make_card_correction_hook`（`run_cli` / `run_gui` 両方に結線） |
| `core/control_queue.py` | `VALID_CONTROL_TYPES` に `correct_board` / `correct_seat`、`command_to_audio_event` に翻訳（index 1..5 / seat の型検査） |
| `api/server.py` | `_StaffControlBody.index` + control endpoint の args 検査（`invalid_control`） |
| `api/client.py` | `send_control(..., index=)` |
| `tests/test_misdeal_correction.py` | 新規 16 ケース（engine 8 / RFID 8） |
| `tests/test_control_queue.py` / `tests/test_viewer_api_staff_lifecycle.py` | 新コマンド種別の翻訳・API 検証 |
| `docs/adr/0043-*.md` / 契約 v1.4 / CLAUDE.md / CHANGELOG / decision-log | docs |

## Expected vs implemented

| 期待 | 実装 |
|------|------|
| ボードの 1 枚だけ取り消せる | ✅ `cb <位置>`。**他の位置は動かない**（`test_middle_position_correction_keeps_the_others`） |
| 取り消した位置に正しい札が入る | ✅ 空き最小を返す既存ロジックがそのまま効く（`test_freed_middle_position_is_reused`） |
| 席の札を読み直せる | ✅ `cs <席>`。`_hole_cards[seat]` を捨てるので 2 枚上限に引っかからない |
| 誤操作しても壊れない | ✅ 存在しない位置 / 席なしは no-op（RFID 側も触らない）。札を外す前に打っても同じ札が同じ位置に戻るだけ = **冪等**（`test_forget_is_idempotent_if_card_not_removed_yet`） |
| append-only は維持 | ✅ 解放は「新ハンド」と「明示訂正」のみ。absence からの推測は入れない |
| iPad から操作できる | ✅ control queue に種別を additive 追加（API 検査 + client 引数まで） |
| 監査痕が残る | ✅ 訂正したハンドは `needs_review` |

## 設計上の選択（詳細は ADR-0043）

- **absence + タイムアウトの自動解放は採らない**（実機の読み落ち間隔が数秒あり安全な閾値が引けない）。
- **ボード全体の読み直しではなく位置指定**（全体だと同じ台に載った複数枚の順序が入れ替わりうる）。
- **取り消した UID のブロックリストは持たない**（不可視状態が増える。冪等な再実行で足りる）。
- **音声語彙（`ACTION_KEYWORDS`）には追加しない**。「訂正」は卓上の一般語で、誤爆が**正しい記録を
  消す**方向に働く。注文確定を staff-in-the-loop にした ADR-0018 と同じ非対称性の判断。

## Test results

```
python -m pytest tests/ -q --ignore=tests/test_vision.py
869 passed, 2 warnings      # 852 → 869（+17）
```

## Remaining gaps

- **実機での確認が未了**（`cb` / `cs` を打ってボードの 1 枚を差し替える通し）。
- staff iPad アプリの **UI ボタン**は未実装（API は通っているので `staff/` 側の後続タスク）。
- 訂正の **undo は無い**（物理を直して打ち直す = 冪等なので収束する）。
- 席の訂正は **2 枚まとめて**読み直し（1 枚だけの指定はできない。席のカードは順不同で位置概念が無い）。
- 確定後のハンドの訂正は別系統（ADR-0036 のオーバーレイ）。本タスクは**ハンド進行中の物理訂正**のみ。

## Related

- ADR-0043 / 契約 `docs/contracts/rfid-usb-ccid.md` v1.4 §4
- ADR-0042 / ISSUE-0024・0025・0026（board 位置モデルの経緯 = 本 ADR の前提）
- ADR-0039（control queue）/ ADR-0036（確定後のハンド訂正）/ ISSUE-0012（状態変更の一元化）
