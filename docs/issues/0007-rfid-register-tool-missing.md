# Issue 0007: `python -m rfid.register` が参照されているが未実装

## Date

2026-06-01

## Status

Open

## Severity / Priority

- Severity: Low
- Priority: P2

## Area

rfid / docs / tooling

## Expected Behavior

`rfid_cards.json` の `description` フィールドに次の記述がある:

> "... Use 'python -m rfid.register' to register cards interactively."

この文面どおり、`python -m rfid.register` を実行するとタグ UID を読み取り、
card_code を対話登録できる CLI が存在することが期待される。RFID 実機テストでは
`rfid_cards.json` への事前登録が必須（未登録タグは `RFIDEvent.card` が空になり
ゲーム状態へ反映されず needs_review 警告のみ）なので、この登録手段は実機投入の
前提作業に当たる。

## Actual Behavior

`rfid/` パッケージに `register.py` は存在せず、`python -m rfid.register` は
`No module named rfid.register` で失敗する。`CardMaster.register()` という API は
あるが、それを叩く CLI / 対話ツールは未提供。結果として `rfid_cards.json` は空の
ままで、利用者は登録手段を自分で書く必要がある。

```
$ python -m rfid.register
... No module named rfid.register
```

## Reproduction

1. クリーンな作業ツリーで `python -m rfid.register` を実行する。
2. モジュールが見つからずエラー終了する。
3. `rfid_cards.json` は `"cards": {}` のまま。

## Root Cause

`CardMaster` 実装時に対話登録 CLI（`rfid/register.py` の `__main__` エントリ）が
未実装のまま、`rfid_cards.json` の説明文だけ先行して当該コマンドを案内している。
ドキュメント（データファイルの description）と実装の乖離。

## Fix

未着手（このタスクのスコープ外）。当面の回避策として、PC/SC 実機テスト手順書
（`docs/testing/rfid-pcsc-hardware-test.md` §4）に `CardMaster` + `PCSCBridge` を
使う対話登録スニペットと手動編集手順を記載した。

恒久対応案（将来）:
- `rfid/register.py` に `__main__` を実装し、PC/SC（および HTTP 受信）両 transport で
  UID を取得して card_code を対話登録できる CLI を提供する。
- 実装後、`rfid_cards.json` の description の案内が事実と一致する。

## Regression Test

- 未作成（ツール実装時に `tests/test_rfid_register.py` を追加し、登録フローを pin する）。

## Affected Files

- `rfid_cards.json`（description が未実装コマンドを案内）
- `rfid/`（`register.py` が不在）

## Related Worklog

- `docs/worklog/2026-06-01-rfid-pcsc-hardware-test-procedure.md`

## Related ADRs

- —

## Related Commits

- （本コミットで issue 起票）

## Notes

実機テストの観点では blocker ではない（手順書のスニペット / 手動編集で代替可能）。
ただしオンボーディング摩擦と「docs が実在しないツールを案内している」整合性の
問題として残す。
