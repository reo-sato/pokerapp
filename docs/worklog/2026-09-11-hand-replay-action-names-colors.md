# 2026-09-11 — リプレイ 4 列にプレーヤー名と種別色を入れる

## Goal

ADR-0051 の 1 画面リプレイについて、ユーザー要望:

> アクションの欄にプレーヤー名を書いてほしいです。また、各アクション名(fold, bet, raise, call, all-in)
> に対して色を与えて表示してほしいです。

4 列のアクション行は席番号のみだったため、テーブル図と突き合わせないと誰の手番か分からない。
また全種別が同色テキストで、列を流し読みしても攻防の強弱が読めない。

## Changed files

- `shared/hand_replay/handReplayModel.ts` — `ACTION_COLORS` / `actionColor(action)` を additive 追加
  （fold 灰 / check 鋼 / call 青 / bet 緑 / raise 橙 / all-in 赤 / blind 灰、未知はテキスト色）。
- `shared/hand_replay/handReplayModel.test.ts` — `actionColor` のテストを追記
  （6 種別が互いに異なる / `all_in` と `allin` が同色 / 未知は fallback）。
- `shared/hand_replay/HandReplay.tsx`
  - `ActionLine` を **2 段**に変更。1 段目 = `席番号 + 名前`、2 段目 = 色付きアクション名 + 金額 + `!`/`✎`。
  - `HandReplay` に `nameOf` を組み立て（`action.player_name` → `model.seats` の席名 → `席N`）、
    `StreetColumn` 経由で `ActionLine` に渡す。
  - 本人行は 1 段目の名前も金色（`c.selfEdge`）。金額は accent 青 → 通常テキスト色に変更
    （種別色と競合させない）。styles: `actionLine` を column 方向に、`actionMain` / `actionWho` を追加、
    `actionSeat` を廃止。
- `python scripts/sync_shared_ui.py` で mobile / staff へ再配布。
- docs: ADR-0051 に「追記（2026-09-11）: アクション行の『誰が』と種別色（D8）」、CHANGELOG、CLAUDE.md。

## Expected vs implemented

| 期待 | 実装 |
|------|------|
| 行にプレーヤー名 | 2 段化して 1 段目に `席N 名前`（列幅 88px に 1 行で収まらないため） |
| 種別ごとの色 | `actionColor` をアグレッション順の色相で定義、ラベルに適用 |
| 1 画面のまま | 400×860 で 3 アーキタイプとも `scrollHeight == clientHeight`（実測） |
| 書き出し不変 | `handReplayText.ts` は `actionLabel`（日本語）のまま、色も名前も描画のみ |

## Test results

- `cd mobile && npx tsc --noEmit && npm test` → 39 pass
- `cd staff && npx tsc --noEmit && npm test` → 47 pass
- `pytest tests/ --ignore=tests/test_vision.py` → 836 passed
- `pytest tests/test_shared_ui_sync.py` → 2 passed（drift 0）
- `ruff check .` → All checks passed
- staff Playwright E2E → 8 passed（`staff/dist` を先に `npm run export:web` で用意）
- 実画面: デモ fixtures で web export → 400×860 スクリーンショット目視
  （名前表示 / 種別色 / 本人の琥珀 / 1 画面維持）+ Artifact v4 を再 publish

## Mismatches / fixes

- なし（種別色を入れたことで金額の青が競合したため、金額を通常テキスト色に変更した = 意図的）。

## Remaining gaps

- 色は補助チャンネルで、テキストラベルを常に併記しているため色覚特性に依存しない。
  ただし実機（iPad / 各種スマホ）でのコントラスト確認は未実施（WS4 の実機 QA と同枠）。
