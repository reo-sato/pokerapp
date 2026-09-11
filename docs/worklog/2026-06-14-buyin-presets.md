# Worklog: buy-in 金額プリセット（staff メニュー選択, ADR-0026）

## Date

2026-06-14

## Scope / Task

「auto ledger 生成」要望の実装。ユーザー明確化により、対象は **着席時の自動生成ではなく、
buy-in 記帳時にスタッフがメニュー（プリセット）から金額を選ぶ** 形（drink menu.json と同じ発想を
buy-in 金額に適用）。chip→円換算を伴う hand 結果からの自動 ledger 生成は明示的に作らない。

## Goal

スタッフが buy-in を記帳する際、店設定の整数円プリセットから金額を 1 クリックで選べるようにする。
schema / 業務ルール変更なし・additive。chips↔円の自動換算は導入しない（ADR-0016 の不変条件を維持）。

## Changed Files

- `docs/adr/0026-auto-ledger-default-template-generation.md`（新規）: 「auto ledger」= buy-in 金額
  プリセットと定義。config `ledger.buyin_presets`、GUI ボタン、staff API、schema 不変、chip↔cash 分離。
- `config_default.json`: `ledger.buyin_presets`（既定 `[10000, 20000, 30000]`）+ 説明コメント追加。
- `gui/ledger_view.py`: `LedgerViewWindow.__init__` に `buyin_presets` param 追加（正規化）。
  `_build_ui` に preset ボタン行（grid row=3, grant_row を row=4 に移動）。
  `_cmd_pick_buyin_preset(amount)`: kind=buy_in をセット + cash entry に金額を prefill（確定は
  従来どおり「エントリ追加」）。
- `api/server.py`: `create_app(..., buyin_presets=None)`。`GET /api/staff/buyin-presets`
  （`_staff_guard(need_write=False)`、`{"presets": [...]}` を返す）。
- `api/client.py`: `ViewerApiClient.get_buyin_presets() -> list[int]`（staff headers, `["presets"]`）。
- `main.py` `run_ledger_view()`: `cfg["ledger"]["buyin_presets"]` を読み、`create_app` と
  `LedgerViewWindow` の両方に渡す。
- `tests/test_ledger_view_gui.py`: `_make_window` が buyin_presets を受け渡し。`TestBuyinPresets`
  （正規化 / pick で kind+cash セット / pick→追加で buy_in entry 生成）。
- `tests/test_viewer_api_staff.py`: `_build` の create_app に `buyin_presets=[10000, 20000]`。
  `test_staff_buyin_presets`（token あり=取得 / token 無し=401）。
- docs: `docs/decision-log.md`（ADR-0026 行）、`docs/contracts/viewer-api.md`（staff endpoint 表に
  `GET /api/staff/buyin-presets` 行）、`CLAUDE.md`（ledger ツリー行 / Out-of-scope / Phase 4 / 残作業）、
  `CHANGELOG.md`。

## Expected / Implemented Behavior

- `--ledger` 画面に buy-in プリセットボタンが並び、押すと種別=buy_in + 金額が入力欄に prefill される。
  確定は既存の「エントリ追加」ボタン（誤クリックで即記帳されない）。
- staff token を持つ別端末は `GET /api/staff/buyin-presets` で同じプリセットを取得できる。
- プリセットは正の整数円のみ（0/負は除外）。空 list なら何も表示しない（既定挙動不変）。

## Test Results

- `pytest tests/ --ignore=tests/test_vision.py -q` — **530 passed, 0 skipped**。
- `ruff check .` — clean。
- live smoke: staff_token 未設定の config_default で `GET /api/staff/buyin-presets` →
  `{"code":"staff_writes_disabled",...}`（期待どおり、token 必須）。

## Mismatches Found During Testing

- なし（実装後に full suite + ruff green を確認）。

## Remaining Gaps / Out-of-Scope

- hand 結果からの自動 ledger 生成（chip→円換算）は **作らない**（ADR-0016/0026）。
- プリセットの per-session オーバーライドや point プリセットは scope 外（需要が固まってから）。

## Related ADRs / Issues

- ADR-0026（本件）/ ADR-0016（金額 = 整数円・chip 別単位）/ ADR-0018（menu.json 先例）/
  ADR-0021（staff write API）

## Related Commits

- 本 commit
