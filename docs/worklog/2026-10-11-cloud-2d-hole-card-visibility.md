# Worklog: ホールカードの公開範囲（フェーズ 2d）

## Date

2026-10-11

## Scope / Task

ADR-0067 D10 の 2d（D6）。`all` / `own_and_shown` を店の設定 + セッションごとに。店内とクラウドの両方。

## Changed Files

- `core/session.py`（`hole_card_visibility`）/ `core/session_repository.py`（`set_hole_card_visibility`・
  `InvalidHoleCardVisibilityError`）/ `core/sync.py`（店内の同期でも項目を残す）/ `docs/contracts/schemas/session.schema.json`（1.1）
- `api/read_models.py`（`HOLE_CARD_VISIBILITY`・`hole_card_mode`・`viewer_seats`・`hide_hole_cards`）/ `api/server.py`
  （お客さんの読み取りに適用・`?player_id=`・スタッフ API 2 つ・`auth_kwargs_from_config` が `viewer_api.hole_cards` を渡す）
- `cloud/app.py`（読み取りに適用、店舗の設定は `store_settings`）/ `cloud/records.py`（セッションは送り手の番号）/
  `core/cloud_sync.py`（`viewer_api.hole_cards` を送る）/ `config_default.json`
- `staff/`（`HoleCardVisibility` 型・`getHoleCardDefault` / `setHoleCardVisibility`・卓の上の選択）/ `mobile/`（`getHand` に
  `playerId`）→ 3 つのビルドを作り直し
- テスト: `tests/test_hole_card_visibility.py`（新規 11）/ `test_cloud_viewer.py`（+1×2）/ `test_cloud_storage.py`（セッションの
  merge を書き換え）/ `tests/cloud_helpers.py`（Postgres につながらなければすぐ失敗）/ staff mock +1
- 文書: ADR-0067 追記 4 / `docs/usage.md`（手札の公開範囲）/ `docs/contracts/viewer-api.md` / CHANGELOG / CLAUDE.md

## Expected Behavior

- `own_and_shown`: 自分の席の手札とショーダウンで見せた手札だけ。ほかは `hole_cards: null` + `hole_cards_hidden: true`。
- 決まり方: セッションの設定 > 店の設定 > all。スタッフの読み取りは常に全部。

## Implemented Behavior

- 期待どおり。本物のエンジンのハンド（見せずにマック / 全員が見せたショーダウン）で、隠したあとに札が残っているのが
  ボード・見せた手札・自分の席だけであることを確かめた。

## Test Results

- 全体（Postgres あり, Python 3.13）: 2963 passed / 5 skipped / 1 failed（`postflop-street-transition` = 手元の環境だけの既知の失敗、CI の 3.11 では通る）。ruff OK。
- staff: typecheck + mock 48 / mobile: typecheck + 42。

## Mismatches / Fixes

- ADR の `viewer.hole_cards` → `viewer_api.hole_cards`（お客さんの画面の設定の節に揃えた）。
- クラウドのセッションの merge を解決関数のままにすると「店の設定に戻す」（項目が無い）が写しに伝わらない → 送り手の番号へ。
- 店内の 1 ハンドの読み取り（`/api/sessions/.../hands/{hid}`）は見ている人を知らない → 画面が `?player_id=` を付ける。
- 手元の試験用 Postgres が止まっていてテストが待ち続けた → つながらなければ 3 秒で失敗するようにした。

## Remaining Gaps

- 2e 置く（Dockerfile・Cloud Build・Secret Manager）と公開前の確認 10 項目。
- 店内の名前選びでは公開範囲は目安（誰でも誰を選べる）。本人確認はフェーズ 3（LINE / Google）。

## Related Commits

- 本 worklog と同じ commit
