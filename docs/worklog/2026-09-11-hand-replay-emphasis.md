# 2026-09-11 — リプレイ UI の強調・省略規則（ADR-0051 追記 D7）

## Goal

ADR-0051 の 1 画面リプレイを見たうえでの読みやすさ改善 3 点:
プリフロップ fold 席のグレイアウト / 本人の席を黄色（琥珀）で強調 / プリフロップ列の fold 非表示。

ユーザー確定: 減光は**プリフロップ fold のみ**、黄色は**落ち着いた琥珀**、本人強調は**4 列にも広げる**。

## Changed files

- `shared/hand_replay/handReplayModel.ts` — additive 純関数 2 本:
  `preflopFoldedSeats(actions)`（preflop の fold 席を昇順で）/
  `visibleColumnActions(street, actions)`（preflop のみ fold を除く。**描画専用**）。
- `shared/hand_replay/handReplayModel.test.ts` — 3 テスト追記（ポストフロップ fold を含めない /
  preflop 以外は素通し / fold を隠しても potEnd が変わらない）。
- `shared/hand_replay/HandReplay.tsx` — optional prop `selfSeat`、色トークン
  `selfFill`/`selfEdge`/`selfRow`、`SeatChip` に `isSelf`/`dimmed`、`ActionLine` に `isSelf`、
  列は `visibleColumnActions` の結果を描く（空判定もフィルタ後）。
- `mobile/src/screens/HandDetailScreen.tsx` — 既存の `own`（`findOwnRow`）から
  `selfSeat={own?.seat}` を渡すだけ。staff は未指定のまま（「本人」が存在しない）。
- 配布: `python scripts/sync_shared_ui.py`。docs: ADR-0051 追記 / CHANGELOG / CLAUDE.md。

## Expected vs implemented

プランどおり。逸脱なし。設計上の要点は
**fold 非表示を `buildReplayModel` に入れない**こと — `handReplayText.ts` が同じモデルを使うため、
入れるとテキスト書き出しから preflop fold が消える。描画専用の別関数に分けて回避した。

## Test results

- `mobile`: 38 passed / tsc green（35 → 38）
- `staff`: 46 passed / tsc green（43 → 46）
- `pytest tests/test_shared_ui_sync.py`: 2 passed（drift 0）
- `pytest tests/ --ignore=tests/test_vision.py`: **836 passed** / `ruff check .`: pass
- staff Playwright E2E: **8 passed**（fold 行に依存したアサーションは無く、無改修で通過）
- **実画面**（400×860 / デモ fixtures）: 席3（プリフロップ fold）が減光、席1（本人 = 山田）が
  琥珀の地、プリフロップ列から Fold 行が消え**フロップの Fold は残存**、本人のアクション行に
  薄い琥珀。3 アーキタイプとも `scrollHeight == clientHeight == 860` で**1 画面維持**、pageerror 0。

## Mismatches / fixes

なし（初回の実装で意図どおり描画された）。

## Remaining gaps

- staff アプリ側には「本人」概念が無いため強調は出ない（仕様どおり）。
- ポストフロップ fold の減光は見送り（ユーザー選択）。要望が出たら 2 段階化は容易
  （`preflopFoldedSeats` と同形の純関数を足すだけ）。
