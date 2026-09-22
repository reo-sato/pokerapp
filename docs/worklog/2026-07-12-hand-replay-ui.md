# 2026-07-12 — ハンドリプレイ UI（mobile/staff 共有）+ staff ハンド履歴 read（ADR-0044）

## Goal

player 向け mobile / staff 向け iPad の両アプリに、GGPoker のハンドヒストリー画面を参考にした
**ストリート単位のハンドリプレイ UI** を追加する。あわせて WS4 残作業だった
**staff hands-list endpoint**（staff アプリがセッションの全ハンドを読む read）を実装する。

事前確認（AskUserQuestion）で確定した要件:
- 対象 = mobile + staff の**共有コンポーネント**
- 表示 = **ストリート単位**（GG 風。アクション単位ステップ再生ではない）
- ホールカード = **記録がある席は全員分表示**
- 周辺 = staff hands-list endpoint を今回スコープに含める（BTN/ポジション推定は scope 外）

## Changed files

- 設計: `docs/adr/0044-hand-replay-ui-shared-component-and-staff-hands-read.md`（新規, Accepted）、
  `docs/decision-log.md`、`docs/contracts/viewer-api.md`（staff read 節にハンド履歴 read 追加）
- 共有 UI 正本: `shared/hand_replay/{handReplayModel.ts, HandReplay.tsx, handReplayModel.test.ts}`（新規）
- sync 機構: `scripts/sync_shared_ui.py`（新規）+ `tests/test_shared_ui_sync.py`（drift 検知, 新規）
- コピー先（生成物）: `mobile/src/shared/hand_replay/`, `staff/src/shared/hand_replay/`
- API: `api/read_models.py`（`list_session_hands` 追加）、`api/server.py`
  （`GET /api/staff/sessions/{sid}/hands`）、`api/client.py`（`list_session_hands`）、
  `tests/test_viewer_api_hands_staff.py`（新規 4 件）
- mobile: `mobile/src/screens/HandDetailScreen.tsx`（テキスト行 → リプレイ表示）、
  `mobile/package.json`（test スクリプトに replay tests 追加）
- staff: `staff/src/api/{types,repository,httpRepository,mockRepository}.ts`（HandSummary 型 +
  `listSessionHands`）、`staff/src/mocks/fixtures.ts`（3 ハンドの `sessionHands`）、
  `staff/src/screens/HandTab.tsx`（ハンド履歴一覧 + リプレイ drill-in）、
  `staff/src/api/mockRepository.test.ts`（+2 件）、`staff/package.json` / `staff/package-lock.json`、
  `staff/e2e/staff.spec.ts`（locator 修正 + リプレイ E2E 追加）
- docs: `CLAUDE.md`（tree / 実装状況 / WS4）、`CHANGELOG.md`

## Expected vs implemented

期待どおり実装。設計上の要点:

- **共有方式 = copy-sync + drift test**（ADR-0044 D1）。両アプリは workspace の無い独立 npm
  プロジェクトで、monorepo 化は tsc / metro / web export / tsx tests / Playwright の 5 系統に
  波及するため却下。正本 `shared/hand_replay/` → `scripts/sync_shared_ui.py` でバイト同一コピー、
  `tests/test_shared_ui_sync.py` が CI で drift を落とす。
- 共有コンポーネントは**両アプリの types.ts / common.tsx に依存しない**（構造的型 + 自己完結
  スタイル）。hand schema 1.0 のサブセット `ReplayHand` を受け、mobile `HandSummary` /
  staff hands read 応答をそのまま渡せる。
- モデル層 `buildReplayModel` は純関数: street 分割（preflop→river）、board スライス（3/4/5）、
  potStart = 前 street 最終 `pot_after`（all-in ランアウトはアクション無し street も board が
  開いていれば表示・ポット引き継ぎ）。
- staff hands read は `_staff_guard(need_write=False)`（token 必須・read-only プロセス可）、
  訂正オーバーレイ適用済み・lenient（log 不在は空 list）。

## Mismatches / fixes（既存バグ 2 件を発見・修正）

1. **staff/package-lock.json の react-dom 19.2.7 ↔ react 19.2.3 不整合**。`npm ci` が
   ERESOLVE で失敗し、web export は React error #527 で**起動時に白画面クラッシュ**
   （HEAD 時点で staff web 配布が壊れていた）。react-dom を 19.2.3 に揃えて解消
   （mobile の lockfile は 19.2.3/19.2.3 で整合済み）。
2. **staff/e2e/staff.spec.ts の locator 不備**。Playwright strict mode で
   `getByText("ログイン")` 等が説明文にも部分一致して 6 件全滅、
   `/に割り当てました/` は実メッセージ「hand #N に M 席を割り当てました。」に不一致
   （この assert はそのままでは通り得ない）。exact 指定 / first・last / 正規表現修正で解消。

## Tests

- Python: `pytest tests/ --ignore=tests/test_vision.py` → **717 passed / 0 failed**（+6:
  staff hands API 4 + shared drift 2）。`ruff check .` clean。
- mobile: `tsc --noEmit` green、`npm test` **21 passed**（mock 13 + replay model 8）、
  `expo export --platform web` 成功。
- staff: `tsc --noEmit` green、`npm test` **30 passed**（mock 20 + 新規 2 + replay model 8）、
  `expo export --platform web` 成功、Playwright E2E **7 passed**（既存 6 修正 + リプレイ 1 追加。
  preinstalled Chromium を executablePath 指定した一時 config で実行、config はコミットしない）。
- 目視: staff（iPad 縦相当）/ mobile（スマホ幅）の web export でリプレイ画面をスクリーンショット
  確認（street セクション・4 色スート・ポット推移・全員分ホールカード・裏向き「?」・結果）。

## Remaining gaps

- 実機（iPad Safari / Expo Go）でのタッチ・レイアウト手動 QA（WS4 既存の残作業と同枠）。
- アクション単位のステップ再生・アニメーションは今回 scope 外（要望が出たら additive に拡張）。
- BTN/ポジション表示は scope 外（ソルバー M1 の `spot_config_builder` と同時に検討, 提案 §4.1）。
- staff E2E は `@playwright/test` 1.61（lockfile 解決）が要求する Chromium build 1228 を
  ダウンロードできない環境では executablePath 上書きが必要（環境依存・コード変更不要）。
