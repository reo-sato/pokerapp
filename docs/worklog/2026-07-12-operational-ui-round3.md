# 2026-07-12 — 実運用 UI 補強 第 3 弾（棚卸し ○ 残り 4 点）

## Goal

UI 機能棚卸しの残り ○ 4 点: menu 編集（ADR-0046）/ mobile PIN 自己設定 / 座席の明示解除 /
desktop 録音死活表示。

## Changed files

- menu 編集: `docs/adr/0046-menu-master-staff-edit.md`（新規）、`core/menu.py`
  （thread-safe read-write + reload-on-read + `set_items` + `sold_out` + `MenuValidationError`）、
  `api/server.py`（`PUT /api/staff/menu` + 注文 POST の `item_sold_out`）、`api/client.py`
  （`update_menu`）、`docs/contracts/{viewer-api,error-shapes}.md`、`tests/test_menu_edit.py`
  （新規 14 件）、staff `MenuScreen.tsx`（新規）+ repository 3 実装 + App 結線 + mock test、
  mobile `OrderScreen`（品切れ表示・注文不可）+ types/fixtures/mock + mock test
- PIN 自己設定: mobile `src/api/{repository,httpRepository,mockRepository}.ts`（`setPin`。mock は
  per-player pin 保存 + login 参照）、`AuthScreen.tsx`（設定/変更フォーム + 設定後自動ログイン）、
  mock test
- 座席明示解除: staff `SeatingTab.tsx`（「現在の座席をコピー」→ staged 展開 → 行単位取消 →
  次 hand 割り当て）
- 録音死活: `audio/recorder.py` / `rfid/reader_thread.py`（`health` dict = 差し替え atomic。
  audio はデバイス open 失敗もクラッシュせず error 状態に）、`gui/dashboard.py`
  （ヘッダー死活行 + `_refresh_health`）、`tests/test_thread_health.py`（新規 3 件）
- docs: `CLAUDE.md` / `CHANGELOG.md` / `docs/decision-log.md` / 本 worklog

## Expected vs implemented

期待どおり。設計上の要点:

- menu は **全量 PUT（last-write-wins）+ sync 非対象**（ADR-0046 D3。店設定であり会計レコードでは
  ない）。`sold_out=false` は永続形に書かず既存 `menu.json` と後方互換。
- 品切れ reject は API 境界（`unknown_item` と同層）。既に pending の注文には影響しない。
- PIN 設定はサーバ既存 endpoint（ADR-0027 D6）をそのまま使い、mobile は UI 導線のみ追加。
- 死活表示は **監視のみ**（health dict を GUI が読むだけ。business logic なし・既存スレッド挙動
  不変。追加の例外安全: audio デバイス open 失敗で return + 表示）。

## Tests

- Python: pytest **745 passed**（+17: menu 14 + health 3）、ruff clean。
- mobile: typecheck green、**30 passed**（+2: sold_out / setPin）、web export 成功。
- staff: typecheck green、**38 passed**（+1: updateMenu）、web export 成功、E2E **8 passed**。

## Remaining gaps

- 棚卸し △ 群（プッシュ通知 / 通算成績 / player 検索 / オフラインキュー / 多言語 /
  計測ランダム drill-in / staff token ローテ運用）— ネイティブ配布・外販期に再評価。
- 死活表示の実機確認は Phase H（実マイク・実 PN5180）で行う。
