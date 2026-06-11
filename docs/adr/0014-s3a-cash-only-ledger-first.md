# ADR-0014: S3a — ledger を cash-only で先行導入（point ledger は M6 に分離）

## Status

Accepted

## Date

2026-06-11

## Context

プレイヤー向け参照アプリ（ADR-0013 ロードマップ）の M4 は「会計参照」を要求するが、
S3（ledger + point ledger）全体は **ISSUE-0001（point 残高の source of truth）** がブロッカーで、
残高計算のアルゴリズムと API 契約が凍結できない。一方で、小規模クラブの当面の会計実務
（buy-in / rebuy / add-on / ドリンク注文 / 調整の記録と中間集計）は **point を使わなければ
残高概念なしで成立**する。

CLAUDE.md の業務ルールでは ledger_entry は `cash_amount + point_amount` 併用可だが、
point 増減は point_ledger_entry の責務であり、point を「使わない」限り ledger_entry 側に
残高計算は発生しない。

## Decision

1. **S3 を S3a（cash-only ledger, 本 phase = M4）と S3b（point 連携, M6）に分割**する。
   S3a は ISSUE-0001 の決着を**必要としない**（point を使えない間、残高計算が存在しないため）。
2. **`ledger_entry` model を導入**: `entry_id`（UUID4 hex, アプリ内採番）/ `session_id` /
   `player_id` / `kind`（buy_in | rebuy | add_on | order | adjustment）/ `occurred_at` /
   `cash_amount` / `point_amount` / `note?` / `order?`（kind=order のみ:
   item_name / unit_amount / quantity）。
3. **schema は draft 0.1**（freeze しない）。`point_amount` はフィールドとして存在するが、
   S3a の**業務 validation で 0 以外を reject**（error code `points_not_supported`）。
   `1.0` freeze は M6（point 連携）で point 意味論が決着してから（session 0.x → ISSUE-0005 と
   同じ運用）。schema レベルで `point_amount: 0` を強制しないのは、M6 での解放を
   breaking change にしないため。
4. **永続化はプロジェクト直下 `ledger.json`**（`{"entries": [...]}` フラット配列、
   アトミックリネーム、`.gitignore`）。players.json / sessions.json と同じ流儀。
5. **業務 validation は `core/ledger_repository.py` が source of truth**:
   - session は実在しかつ **open**（closed への追記は `session_closed`。close 後の訂正は
     S4 settlement の責務）。player は registry に実在（`unknown_player`）。
   - kind 別の金額規則: buy_in / rebuy / add_on は `cash_amount > 0`、order は
     `cash_amount == unit_amount × quantity`（unit_amount ≥ 0, quantity ≥ 1）、
     adjustment は `cash_amount != 0`（負も可 = 返金/値引き）。違反は `invalid_amount`。
   - 中間集計（CLAUDE.md 業務ルール 7）は repository の `session_player_summary` が提供:
     `buy_in_total`（buy_in+rebuy+add_on）/ `order_total` / `adjustment_total` / `total_due`
     （= 3 つの和）。**確定値ではない**（確定は S4 settlement）。
6. **入力（write）はスタッフの desktop 別画面のみ**（`python main.py --ledger`,
   `gui/ledger_entry.py`）。player のスマホからの注文 write は M5（ISSUE-0013 決着後）。
7. **viewer API に read-only の会計参照を additive 追加**:
   `GET /api/players/{id}/sessions/{sid}/ledger` → `{entries, summary}`（viewer-api.md 更新）。
   mobile（M2 viewer）に「会計」画面を追加（read-only）。

## Alternatives Considered

- **A: S3 全体（point 含む）を一括実装** — ISSUE-0001 の決着（残高 source of truth の ADR）が
  先に必要で、M4 の「会計参照を早く出す」目的に対し過大。→ 分割。
- **B: point_amount フィールド自体を S3a schema から外す** — M6 で required/optional の追加が
  入ると fixtures / core / front-end の二度手間。フィールドは置き validation で塞ぐ方が
  additive 移行になる。→ 不採用。
- **C: ledger entry を sessions.json 配下に入れ子で持つ** — session 跨ぎの player 集計
  （将来の S4/S5）でフラットな方が扱いやすく、sessions.json の責務（seating）と混ざる。
  → 別ファイル `ledger.json`。
- **D: dashboard（hand logger 画面）に入力 UI を組み込む** — WS2 原則「registry / ledger は
  常に別画面」に反する。記録卓と会計係が別人の運用も自然。→ 別 window `--ledger`。

## Consequences

- Positive: ISSUE-0001 を持ち越したまま、buy-in / 注文の記録と「player 別中間集計」が
  desktop / mobile / API の 3 面で動く。M5（注文 write path）の土台になる。
- Negative / trade-offs: point 払いは S3a では一切不可（cash で記録するしかない）。
  closed session への訂正手段が S4 まで無い。
- Neutral / new constraints: `ledger.json` という第 3 の永続ファイルが増える。
  error code `points_not_supported` / `invalid_amount` / `invalid_kind` を additive 追加。

## Validation / Follow-up

- [x] `tests/test_ledger_repository.py`（validation / 永続化 / 集計）+ contract fixtures
- [x] `tests/test_viewer_api.py` ledger endpoint（error shape / summary）
- [ ] M5: player からの注文 write path（ISSUE-0013 決着が前提）
- [ ] M6: ISSUE-0001 決着 ADR → point_ledger_entry → `points_not_supported` の解除 →
  ledger schema `1.0` freeze

## Related Files

- `docs/contracts/ledger.md` / `docs/contracts/schemas/ledger_entry.schema.json`
- `core/ledger.py` / `core/ledger_repository.py` / `gui/ledger_entry.py`
- `api/read_models.py` / `api/server.py` / `mobile/`

## Related Tests

- `tests/test_ledger_repository.py` / `tests/test_viewer_api.py` / `tests/test_contracts.py`

## Related Commits

- （M4 実装 commit を参照）

## Supersedes / Superseded by

- Supersedes: —（ADR-0003 の S3 計画を分割実行。関連: ADR-0013 / ISSUE-0001 / ISSUE-0013）
- Superseded by: —
