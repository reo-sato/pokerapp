# 2026-07-12 — 実運用 UI 補強 第 2 弾（棚卸し ○ 4 点）

## Goal

UI 機能棚卸しの「○ = dogfood 中に欲しくなる」から、価値×工数で選んだ 4 点を実装する。

1. **注文キャンセル**（唯一の backend 変更, ADR-0045）
2. staff の **営業日サマリ**（本日の集計）+ **プレイヤー管理画面**（リネーム/merge）
3. staff の **ライブ polling 拡大**（ハンド履歴・座席）
4. mobile の **ハンド共有/書き出し**（Phase B KPI「書き出しクリック数」の観測対象になる導線）

## Changed files

- 注文キャンセル: `docs/adr/0045-order-request-player-cancel.md`（新規）、
  `core/order_request.py` / `core/order_request_repository.py`（`cancel_request`）、
  `core/sync.py`（status rank に cancelled）、`api/server.py`（cancel endpoint）、
  `api/client.py`（`cancel_order_request`）、
  `docs/contracts/schemas/order_request.schema.json`（1.0→1.1）+ `fixtures/order_request/valid-cancelled.json`、
  `docs/contracts/{viewer-api,error-shapes}.md`、
  `tests/test_order_request_repository.py`（+7）/ `tests/test_viewer_api.py`（+3）/ `tests/test_sync.py`（+1）、
  mobile `src/api/{types,repository,httpRepository,mockRepository}.ts` + `OrderScreen`（キャンセル導線）+
  mock test、staff `src/api/types.ts`（status union）
- staff 運用機能: `src/screens/SessionListScreen.tsx`（本日の集計 + プレイヤー管理導線）、
  `src/screens/PlayersScreen.tsx`（新規: 作成/リネーム/merge）、`App.tsx`（route 追加）、
  `src/api/{repository,httpRepository,mockRepository}.ts`（`mergePlayers`。mock listPlayers は
  canonical = tombstone 除外に変更）、`HandTab.tsx` / `SeatingTab.tsx`（5 秒 polling）、mock test +1
- ハンド共有: `shared/hand_replay/handReplayText.ts` + `handReplayText.test.ts`（正本 + sync 配布）、
  mobile `HandDetailScreen.tsx`（共有ボタン: Share → clipboard fallback）、両 package.json の test script
- docs: `CLAUDE.md` / `CHANGELOG.md` / `docs/decision-log.md` / 本 worklog

## Expected vs implemented

期待どおり。設計上の要点:

- キャンセルの本人判定は merge 考慮の equivalence class。**他人の request は not_found**
  （存在を漏らさない）。sync 衝突（キャンセル×確定）は **confirmed 勝ち**（ledger 既存を優先）。
- 営業日サマリは **API 追加なし**（既存 settlement read のクライアント合算）。「確定値ではない」
  旨を UI に明示（確定は各卓の精算 commit）。
- `buildHandText` はリプレイモデルと同じ純関数系に置き、view と drift しない
  （street 分割・board スライス・pot 境界を共有。名前解決も view と同じ席 fallback）。

## Tests

- Python: pytest **728 passed**（+11）、ruff clean。
- mobile: typecheck green、**28 passed**（+2: cancel mock / 共有 text は shared 側）、web export 成功。
- staff: typecheck green、**37 passed**（+3: merge / text 2）、web export 成功、Playwright E2E **8 passed**。

## Mismatches / fixes

- `buildHandText` 初版が action の `player_name` 欠落時に名前を落としていた → view（HandReplay）と
  同じ「席→名前」fallback を追加（テストが検出）。

## Remaining gaps（棚卸しの残り）

- ○: menu 編集 UI（価格改定・品切れ）、mobile PIN 自己設定導線、座席の明示解除、
  desktop の録音死活表示（マイクレベル / RFID 接続状態）。
- △: プッシュ通知、通算成績、player 検索、オフラインキュー、多言語、計測ランダム drill-in 等。
