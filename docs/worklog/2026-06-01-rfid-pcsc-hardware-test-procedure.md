# Worklog: PC/SC RFID 実機テスト手順書の作成

## Date

2026-06-01

## Scope / Task

本番 RFID センサー（PC/SC transport, 例: ACR122U）を使った実機テストを行いたいという
要望に対し、end-to-end の実機テスト手順書を作成する（docs-only タスク）。実機は
ローカル PC でのみ動作するため、コード変更ではなく手順・チェックリスト・回避策を整備する。

## Goal

- PC/SC リーダーを使った RFID 実機テストを、依存導入 → リーダー確認 → カード登録 →
  config 編集 → 単体スモーク → end-to-end → 検証 まで迷わず実施できる手順書を残す。
- 既存実装（`rfid/reader_thread.py` / `rfid/bridge.py` / `rfid/card_master.py` /
  `integration/engine.py` / `main.py`）の実挙動に即した「期待ログ・期待挙動」を記載する。
- 手順遂行を阻むギャップ（未実装の登録ツール）を issue として記録する。

## Changed Files

- `docs/testing/rfid-pcsc-hardware-test.md` — 新規。PC/SC 実機テスト手順書本体。
- `docs/issues/0007-rfid-register-tool-missing.md` — 新規。`python -m rfid.register` 未実装の記録。
- `docs/worklog/2026-06-01-rfid-pcsc-hardware-test-procedure.md` — 本ファイル。
- `docs/decision-log.md` — ISSUE-0007 を Major Issue Index に追記。
- `CHANGELOG.md` — Unreleased / Docs に手順書追加を追記。

## Expected Behavior

- 手順書だけ読めば、ローカル PC で PC/SC リーダーの実機テストを完走できる。
- 設定差（HTTP デフォルト vs PC/SC: `transport`・`readers` の dict/list 差・`name` 必須・
  `poll_interval_ms`）が明示され、よくある詰まり所が回避できる。

## Implemented Behavior

- 手順書を 9 セクション構成（事前準備 / 依存 / OS ミドルウェア / リーダー名確認 /
  カード登録 / 単体スモーク / end-to-end / トラブルシュート / 記録テンプレート）で作成。
- コード読解で確認した実挙動を反映:
  - `RFIDThread` 起動ログ・"No RFID readers connected" 早期終了（`rfid/reader_thread.py`）。
  - `RFIDEvent.reader_id` が config の `name` ではなく接続順 `reader_0/1/...` になる点。
  - デバウンス（同一 UID 1 回 / 離す→再タッチで再発火）。
  - 未登録タグは `card=""` → seat 経路で needs_review 警告（`integration/engine.py`）。
  - board は 3/4/5 枚で street 自動推移するが、これは `board_index` 前提。PC/SC の
    `RFIDThread` は `board_index` を付与しない（http_receiver のみ index 対応）ため、
    PC/SC board は枚数蓄積までで street 自動推移は行われない、と注記。
  - 詳細イベントログが DEBUG レベルである点（`main.py` は INFO 既定）。
- 未実装の `python -m rfid.register` を ISSUE-0007 として起票し、手順書 §4 に
  `CardMaster` + `PCSCBridge` を使う対話登録スニペットと手動編集の代替手順を記載。

## Test Results

- docs-only タスクのためコード変更なし。実機（PC/SC リーダー）はクラウド実行環境に
  存在しないため、本手順の end-to-end 実行は未実施（ローカルでの実施を前提とする）。
- 回帰防止のためのコードテストは追加なし（既存 `tests/test_rfid.py` /
  `tests/test_rfid_http.py` が RFID ロジックを既にカバー）。

## Mismatches Found During Testing

- `rfid_cards.json` の description が案内する `python -m rfid.register` が未実装。
  → `docs/issues/0007-rfid-register-tool-missing.md` に記録。実機テストは手順書の
  スニペット / 手動編集で代替可能なため blocker ではない。

## Fixes Applied

- 手順書側で回避策（登録スニペット・手動編集）を提供し、ギャップを issue 化。

## Remaining Gaps / Out-of-Scope

- [ ] `rfid/register.py`（対話登録 CLI）の実装（ISSUE-0007）。
- [ ] PC/SC board リーダーの `board_index` 付与（street 自動推移を PC/SC でも有効化）。
- [ ] ローカル実機での手順完走と、記録テンプレートに基づく実測ログの添付。

## Related ADRs

- —

## Related Issues

- `docs/issues/0007-rfid-register-tool-missing.md` — 未実装の登録ツール。

## Related Commits

- （本コミット）
