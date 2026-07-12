# ADR-0046: menu master の staff 編集（価格改定・品切れ）+ `sold_out` フラグ

- **Status**: Accepted
- **Date**: 2026-07-12
- **Related**: ADR-0018（M5 menu master / staff-in-the-loop）/ ADR-0021（staff shared token）/
  ADR-0037（staff iPad app）/ ADR-0045（注文まわりの直近拡張）

## Context

menu master（`menu.json`）は「コミット済みサンプルを店側で手編集」する read-only 運用
（ADR-0018）だった。実運用では**価格改定**と**品切れ**が営業中に発生し、JSON 手編集 +
プロセス再起動は現実的でない（UI 棚卸しで dogfood 中に欲しくなる項目と整理）。

## Decision

### D1: `MenuMaster` を thread-safe な read-write + reload-on-read に

- `set_items(items)` で全量置換し **atomic write**（`core/atomic_io.py`）で `menu.json` に永続化。
- `--ledger` プロセスでは GUI スレッド × API スレッドが同居するため **RLock** で保護
  （`OrderRequestRepository` と同型）。
- 読み込みは **reload-on-read（mtime 検知）**: 別プロセス（単独 `--viewer-api`）も staff 編集に
  追従する。ファイル不在・破損は従来どおり空メニュー継続。
- validation: `item_name` 1〜100 文字（trim 後）・重複不可、`unit_amount` 0 以上の整数、
  `sold_out` bool。違反は `MenuValidationError`（error code: `invalid_menu`, 400）。

### D2: `sold_out` フラグ（additive）

- item は `{item_name, unit_amount, sold_out?}`。`sold_out` は省略時 false（JSON には
  true のときだけ書き、既存ファイルと後方互換）。
- `GET /api/menu` は sold_out を含めて返す（additive。旧クライアントは無視するだけ）。
- **注文 POST は sold_out の品を 400 `item_sold_out` で reject**（API 境界。unknown_item と
  同じ層）。既に pending の注文には影響しない（確定/却下はスタッフ判断のまま）。
- menu は schema freeze 対象外（`_MODELS` 外・ADR-0018 の位置付けを維持）。

### D3: 編集 API は staff write の全量 PUT

- `PUT /api/staff/menu` body `{"items": [...]}` → 保存後の items。staff token +
  **write 所有プロセスのみ**（read-only は 503）。
- 部分更新（item 単位 PATCH）は作らない: メニューは高々数十件で、iPad の編集画面が
  全量を持って保存する方が単純・atomic。同時編集の衝突は last-write-wins
  （1 卓 + iPad 1 台の dogfood では実害なし。多端末はサイズ的にも将来課題）。
- menu は **sync（ADR-0022）対象外のまま**（店設定であり会計レコードではない。
  buyin_presets = config と同じ扱い）。

## Consequences

- staff アプリに「メニュー管理」画面（価格編集 / 品切れトグル / 追加・削除 / 保存）。
  mobile 注文画面は品切れを表示し注文ボタンを無効化する。
- 確定時単価の prefill は従来どおり menu 由来 + スタッフ上書き可（価格の最終決定権は
  確定時、ADR-0018 不変）。
- 新 error code: `invalid_menu`(400) / `item_sold_out`(400) を error-shapes.md に追加。
