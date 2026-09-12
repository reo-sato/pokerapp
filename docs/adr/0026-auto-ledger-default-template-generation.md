# ADR-0026: buy-in 金額のプリセット選択（メニュー方式 / chip↔円換算なし）

## Status

Accepted

## Date

2026-06-14

## Context

ledger entry の buy_in を記帳するとき、毎回 cash 金額を手入力するのは煩雑。ユーザー要望
（2026-06-14）は **「buy-in 時にメニューから店員が金額を選択する形」** — ドリンクの `menu.json`
（品名・単価のマスタ, ADR-0018）と同じ「店が編集したプリセットから選ぶ」UX を buy-in 金額に適用する。

本アプリの不変条件 **「金額は整数円・chips とは別単位・自動換算なし」**（ADR-0016）は維持する
（チップ→円換算は行わない。当初検討した「着席者への一括自動生成」「hand 結果→換算」は不採用）。

## Decision

1. **buy-in 金額のプリセット（メニュー）** を設定として持ち、スタッフが ledger 記帳時に選択する。
   ledger entry の作り方・schema は不変（既存の buy_in / rebuy / add_on kind に、選んだ固定円を
   `cash_amount` として渡すだけ）。chips とは無関係。
2. **config**: `ledger.buyin_presets` = 円金額の整数リスト（例 `[10000, 20000, 30000]`、店が編集）。
   `config_default.json` にサンプルを置く（rfid readers と同じ config 駆動）。
3. **GUI**（`gui/ledger_view.py`）: プリセット金額のボタン列を表示。クリックで kind を `buy_in` に、
   cash 入力欄に当該金額をセットする（スタッフは player を選び「エントリ追加」で確定）。プリセットは
   あくまで金額の入力補助で、kind / player / 最終確定はスタッフが行う（手入力も従来どおり可）。
4. **staff API**: `GET /api/staff/buyin-presets` → `{"presets": [int, ...]}`（別端末のスタッフ UI が同じ
   メニューを出すため。staff token gate, read）。記帳自体は既存の
   `POST /api/staff/sessions/{sid}/ledger-entries`（選んだ金額を送る）。`ViewerApiClient.get_buyin_presets`。
5. core / schema は変更しない（プリセットは入力値の供給源で、ledger の業務ルールに影響しない）。

## Alternatives Considered

- **着席者への既定額一括生成 / hand 開始トリガ自動生成** — ユーザー意図は「記帳時に金額を選ぶ」で、
  全員一括や自動記帳ではない。→ 不採用。
- **hand 結果（chips）→ 円換算** — 「自動換算なし」不変条件に反する。→ 不採用（別 ADR が前提）。
- **buyin_menu.json（drink menu と同形の別ファイル）** — buy-in は金額のみで品名を持たないため、
  config の整数リストで十分。drink の `menu.json` とは別物として config に置く。→ config。

## Consequences

- Positive: 定型 buy-in 額をワンタップで入力でき、打ち間違いが減る。chip↔cash 分離・ledger schema・
  業務ルールは不変。desktop / 別端末スタッフ（API）で同じプリセットを共有。
- Negative / trade-offs: プリセットは config 編集（GUI からの編集機能は持たない）。非定型額は
  従来どおり手入力。
- Neutral: config に `ledger.buyin_presets` 追加、staff API に read endpoint 1 本。schema 変更なし。

## Validation / Follow-up

- [x] config + GUI プリセットボタン + staff API GET + client。
- [x] tests: GUI（プリセットで kind/cash がセットされる）/ staff API（presets 取得 + auth）。
- [ ] 必要ならプリセットにラベル（"スタート" 等）を additive 付与、rebuy/add_on 用の別プリセット。

## Related Files

- `config_default.json` / `gui/ledger_view.py` / `api/server.py` / `api/client.py` / `main.py`
- `docs/contracts/viewer-api.md`

## Related Tests

- `tests/test_ledger_view_gui.py` / `tests/test_viewer_api_staff.py`

## Related Commits

- 本 ADR と同じ commit

## Supersedes / Superseded by

- Supersedes: —（関連: ADR-0016（ledger, chip↔cash 分離）/ ADR-0018（menu 方式）/ ADR-0021（staff API））
- Superseded by: —
