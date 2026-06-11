# ADR-0015: M5 — 注文リクエスト write path（staff-in-the-loop / in-process API / menu master / name-pick 確定）

## Status

Accepted

## Date

2026-06-11

## Context

M5（ADR-0013 ロードマップ）はプレイヤーのスマホからのドリンク注文 = **viewer API 初の write 系**。
前提として保留していた事項を確定する必要がある:

1. **ISSUE-0013（プライバシーモデル）**: 無認証 name-pick のまま write を許すか。
2. **注文と台帳の整合**: player の入力をそのまま `ledger_entry` にするか、スタッフ確定を挟むか
   （ADR-0013/0014 で staff-in-the-loop を既定案として記録済）。
3. **プロセス間の書き込み競合**: 注文リクエストは API プロセスが受け、確定はスタッフ desktop
   （`--ledger`）が行う。別プロセスが同一ファイルを書くと lost update が起きる。
4. **メニュー（品名・価格）**: 自由入力か、マスタ管理か。

## Decision

1. **本人確認は name-pick のまま**（user 決定, 2026-06-11。ISSUE-0013 を v1 として決着）。
   なりすまし・誤帰属はスタッフ確定とドリンク提供時の対面で発覚できる（小規模店内の信頼モデル）。
   PIN は将来必要になれば additive に導入可能（registry への属性追加 + write 時のみ要求）。
2. **staff-in-the-loop を採用**: player は `order_request`（pending）を POST するだけで、
   **ledger には書かれない**。スタッフが `--ledger` 画面で確定（confirm）したときに初めて
   `ledger_entry`（kind=order）が作られ、request に `ledger_entry_id` がリンクされる。却下も可。
   append-only の台帳整合・価格決定権をスタッフ側に残す。
3. **単一プロセス所有**: `order_requests.json` への書き込みは「viewer API を in-process で
   抱えた `--ledger` プロセス」に限定する。
   - `--ledger` は `config.viewer_api.enabled = true` のとき uvicorn を背景スレッドで起動し、
     **同一プロセスの repository インスタンスを共有**する（`OrderRequestRepository` は
     lock で thread-safe 化。API スレッド = リクエスト受付、GUI スレッド = 確定/却下）。
   - 単独 `python main.py --viewer-api` は従来どおり read-only（注文 POST は 503
     `orders_unavailable`。GET 系は mtime ベースの reload-on-read で他プロセスの書き込みを追従）。
   - 帰結: **注文を受け付けられるのはスタッフが会計画面を開いている間だけ**（運用的にも妥当）。
4. **menu master を導入**（user 決定）: `menu.json`（`{"items": [{item_name, unit_amount}]}`,
   rfid_cards.json と同じ「コミット済みサンプル + 店側で編集」運用）。player はメニューから
   選択（`unknown_item` で自由入力を拒否）、確定時の単価はメニューから prefill
   （スタッフが上書き可 = 価格の最終決定権はスタッフ）。
5. **`order_request` model**（schema 0.1 draft）: `request_id`（UUID4 hex）/ `session_id` /
   `player_id` / `item_name` / `quantity`（1..99）/ `note?` / `status`
   （`pending` | `confirmed` | `rejected`）/ `requested_at` / `resolved_at?` /
   `ledger_entry_id?`（confirmed のみ）。永続化は `order_requests.json`（`.gitignore`）。
   状態遷移は pending → confirmed | rejected のみ（resolved の再変更は `already_resolved`）。

## Alternatives Considered

- **player 直接 ledger 書き込み** — スタッフ不在でも台帳が増え、価格・重複・いたずらの検証点が
  ない。却下（ADR-0014 §6 とも整合）。
- **PIN 認証を今導入** — 小規模店の運用コスト（PIN 忘れ・登録手間）が利益を上回る。staff 確定が
  実質の検証点。将来 additive 導入可。→ 見送り（user 決定）。
- **order_requests を別プロセス間で共有書き込み（file lock / 都度 reload）** — クロスプラット
  フォームの file lock は壊れやすく、lost update の根本解決にならない。→ 単一プロセス所有。
- **確定もスマホ/HTTP 経由（GUI が API client 化）** — GUI→API→repo の循環依存が増えるだけで
  利点が薄い（同一マシン）。→ 同一プロセス共有。
- **メニュー自由入力** — 表記揺れで集計が汚れ、価格ミスの余地。→ master 化（user 決定）。

## Consequences

- Positive: viewer API に write が入っても台帳は append-only + スタッフ検証を維持。
  ISSUE-0013 が v1 として決着し M5 の gate が外れる。menu master で価格整合。
- Negative / trade-offs: 注文受付はスタッフが `--ledger` を開いている間に限定。
  `viewer_api.enabled` の意味が「`--ledger` への組み込み起動」に確定（placeholder 解消）。
  PIN なしのため LAN 内の越権参照は引き続き可能（read は従来どおり）。
- Neutral: 永続ファイルが 1 つ増える（`order_requests.json`）。mobile に注文画面が増える。

## Validation / Follow-up

- [x] `tests/test_order_request_repository.py`（状態遷移 / confirm→ledger リンク / thread-safe / reload）
- [x] `tests/test_viewer_api.py` 注文 endpoint（POST/GET / error shape / 503 read-only）
- [ ] 実機: `--ledger`（viewer_api.enabled=true）+ スマホからの注文 E2E
- [ ] 将来: PIN の再評価（問題が顕在化した場合）、order_request schema の freeze（ledger と同時, M6 後）

## Related Files

- `core/order_request.py` / `core/order_request_repository.py` / `core/menu.py` / `menu.json`
- `api/server.py` / `gui/ledger_entry.py` / `main.py` / `mobile/`
- `docs/contracts/{ledger.md,viewer-api.md,error-shapes.md,validation-rules.md}` /
  `docs/contracts/schemas/order_request.schema.json`

## Related Tests

- `tests/test_order_request_repository.py` / `tests/test_viewer_api.py` / `tests/test_contracts.py`

## Related Commits

- （M5 実装 commit を参照）

## Supersedes / Superseded by

- Supersedes: —（ADR-0013 §5 / ADR-0014 §6 の既定案を確定。ISSUE-0013 を v1 決着）
- Superseded by: —
