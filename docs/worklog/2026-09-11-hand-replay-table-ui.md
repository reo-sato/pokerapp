# 2026-09-11 — ハンドリプレイ UI の刷新（テーブル図 + 4 ストリート列, ADR-0051）

## Goal

GGPoker のハンドログを参考に、**スマホ縦 1 画面・スクロールなし**で
「上半分 = テーブル + ハンド表示 / 下半分 = プリフロップ・フロップ・ターン・リバーの 4 列」
という新しいリプレイ UI を作る。ユーザー確定: テーブルは**最終状態で固定**（連動・再生なし）、
従来の縦スクロールリプレイは**完全に置き換え**、**mobile + staff 両方**に載せる。

## Changed files

- `shared/hand_replay/HandReplay.tsx` — **全面書き換え**。テーブル図（felt + 席リング + 中央の
  ボード/ポット/勝者/ブラインド）+ 4 列（列見出し = 新規ボード + 街終了ポット、行 = 席 / 短縮
  アクション / 短縮金額 / 状態ストライプ）。`CardChip` に `xs` サイズを追加（export 名は不変）。
- `shared/hand_replay/handReplayModel.ts` — additive: `ALL_STREETS`（4 列固定順の公開）/
  `seatRingLayout(count)`（席中心の % 座標。先頭を下中央 → 時計回り）/ `compactActionLabel` +
  `COMPACT_ACTION_LABELS`（Fold/Check/Call/Bet/Raise/All-in）/ `formatChipsCompact`（1.5k / 12.2k）/
  `formatSignedCompact`。`buildReplayModel` と `actionLabel` は不変。
- `shared/hand_replay/handReplayModel.test.ts` — 5 テスト追記（リング配置の先頭位置と時計回り /
  2..9 席で枠内に収まる / `ALL_STREETS` の順序 / 短縮金額 / 短縮アクション）。
  **新規テストファイルは作らない**（両アプリの package.json `test` スクリプト追記を避けるため）。
- `mobile/src/screens/HandDetailScreen.tsx` — ScrollView を撤去。ヘッダ（Hand #N + 自分の収支）を
  1 行に畳み、リプレイを `flex: 1`、共有 / 訂正はフッタの小ボタン 2 つに。
- `staff/src/screens/HandTab.tsx` — drill-in のリプレイに固定高 560 を与える（ScrollView 内では
  flex:1 の高さが決まらないため）。訂正パネルはその下にスクロールで到達（staff 専用ツール）。
- `staff/e2e/staff.spec.ts` — 旧 UI 文言（「結果」「訂正済」「ベット」）を新 UI（`/^ポット /`・
  `✎`・`Bet`）に追随。
- `mobile/src/shared/hand_replay/` / `staff/src/shared/hand_replay/` — `scripts/sync_shared_ui.py` で再配布。
- docs: ADR-0051 新規 / ADR-0044 Status に「D3 のみ supersede」注記 / CHANGELOG / CLAUDE.md /
  `docs/ui-feature-inventory.md`。

## Expected vs implemented

プラン（承認済み）どおり。逸脱 1 点:

- プランでは「日本語の短縮アクション表記」を想定していたが、**実測で見切れた**
  （「1 オールイン 4k」が 88px 列で `オール… 4…`）。日本語は 1 文字が広いため、列の中だけ
  ポーカーの原語表記（Fold / Check / Call / Bet / Raise / All-in）に変更し幅を半減させた
  （ADR-0051 D4）。共有テキストの日本語ラベルは不変。

## Test results

- `mobile`: 35 passed / `tsc --noEmit` green（30 → 35、新モデルテスト 5 本）
- `staff`: 43 passed / `tsc --noEmit` green（38 → 43）
- `python scripts/sync_shared_ui.py` → `pytest tests/test_shared_ui_sync.py` 2 passed（drift 0）
- `pytest tests/ --ignore=tests/test_vision.py`: **836 passed**（Python 側は不変）
- `ruff check .`: pass
- staff Playwright E2E: **8 passed**（新 UI 文言に追随後）
- **実画面実測**（400×860 / デモ fixtures の 3 アーキタイプ）: 4 ストリート・要確認+訂正済・
  サイドポットのいずれも `scrollHeight == clientHeight == 860` = **1 画面に収まりスクロールなし**、
  pageerror 0。

## Mismatches / fixes

1. 初回レンダで「オールイン + 金額」が列幅を超えて見切れた → 上記の原語表記化で解消（実測で確認）。
2. 初回レンダで勝者席チップだけ席番号が 🏆 に置き換わり、他の席と表記が揃わなかった →
   🏆 は前置のみにして**席番号は常に表示**、勝者は名前を緑 + 枠を緑に。
3. フェルトの楕円が席リングより内側すぎて席が卓から浮いて見えた → felt を
   `left/right 15% · top/bottom 12%` にし、席リング（rx 35% / ry 38%）の上に席が乗るようにした。

## Remaining gaps

- BTN / ポジション表示（ADR-0044 D3 の判断を維持。ソルバー M1 の `spot_config_builder` で扱う）。
- ストリート連動・自動再生（ADR-0051 D2 で見送り。要望が出たら additive）。
- 実機（iPhone / iPad 実寸）でのタッチ確認は Phase H の実機 QA と同枠。
