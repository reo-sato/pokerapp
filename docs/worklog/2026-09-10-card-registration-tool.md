# Worklog: 実機カード UID のタップ駆動登録ツール（tools/register_cards.py）

## Date

2026-09-10

## Scope / Task

RFID 実機 bring-up（`docs/worklog/2026-09-10-rfid-ccid-end-to-end-bringup.md`）の次段階として、
ICODE SLIX トランプ **2 デッキ（52 + ジョーカー 2）× 2** の UID を `rfid_cards.json`（tag_id → card_code）に
登録する作業を、`watch` で UID を控えて手で JSON を書く（104+ 回）方式から **タップ駆動の CLI** に置き換える。

## Goal

- 「次に置くカード」を表示 → 置くと登録 → 離すと次、で 1 デッキを一気に登録できる。
- 2 デッキ目も同じ JSON 形式（tag_id → code、同じ code に別 UID）で登録でき、途中中断/再開できる。
- 置き間違い（別 code で登録済みの UID）を拒否し、修正手段（`unregister`）を持つ。
- 実機なしでテストできる（`MockPCSCBridge` 注入）。

## Changed Files

- `tools/register_cards.py`（新規）— `run` / `list` / `unregister`。純粋ロジック `deck_order`（suit-rank /
  rank-suit + ジョーカー）、`pending_codes`（`--deck N` = code の UID 数 < N を未登録と判定）、`parse_codes`、
  `format_status`、登録ループ `run_registration`（離すまで次を受け付けない / 別 code 登録済みは拒否 /
  期待 code 登録済みは冪等スキップ / `max_polls` で上限）。reader は config `pcsc_readers` 先頭で接続中のもの。
- `tests/test_tools_register_cards.py`（新規, 17 件）— 順序プリセット / 未登録判定 / 表示 / 登録ループ
  （置きっぱなし・拒否・冪等・2 デッキ目・上限）/ parser / list・unregister / pyscard ゲート /
  `_cmd_run` に MockPCSCBridge 注入。
- `rfid/card_master.py` — `_save` の description に登録ツールを明記（存在しない `python -m rfid.register` を
  参照していた `rfid_cards.json` の説明も差し替え）。
- `rfid_cards.json` — description のみ（`cards` は空のまま。実機登録は各環境で）。
- docs — `CLAUDE.md` よく使うコマンド、`docs/hardware-qa-checklist.md` §3、
  `docs/rfid-ccid-firmware-checklist.md` 完了後手順 3、`CHANGELOG.md`。

## Expected Behavior

- `python tools/register_cards.py run --deck 1` → `→ 次: [ 1/54] As（deck 1）` → 置く → `✓ [ 1/54] As ← E0:04:…`
  → `カードを離してください` → 離す → `→ 次: [ 2/54] 2s` … → 54 枚で終了、`deck 1: 54/54 済 ✅ 完了`。
- `--deck 2` で同じ順序をもう一周（各 code に 2 つ目の UID）。中断後は同じコマンドで残りから再開。
- 別 code で登録済みの UID を置くと `⚠ … 既に 'Qs' として登録済み` で登録せず、同じ code を待ち続ける。

## Implemented Behavior

上記どおり。加えて `--order rank-suit` / `--only Ah,Kd` / `--start-at Kd` で順序を実デッキに合わせられる。
`list --deck N` で deck ごとの不足 code、deck 数より多い UID が付いた code、順序外の code を表示。
`unregister <UID>` は正規化（区切り/大小）してから解除。

## Test Results

- `pytest tests/test_tools_register_cards.py tests/test_rfid.py tests/test_tools_probe_pcsc.py -q` → **94 passed**
  （register_cards 17 / rfid 36 / probe_pcsc 41）。
- CLI smoke: `register_cards.py --help`、`list --deck 2 --jokers 2`（空ファイルで 0/54 × 2 と不足一覧）。
- 実機: 未実施（本 worklog 時点）。実機 1 slot は `watch` で UID 到達済みなので同じ `PCSCBridge.read_uid` で動く見込み。

## Mismatches Found During Testing

None observed.

## Fixes Applied

- `rfid_cards.json` / `CardMaster._save` の説明が存在しないコマンド（`python -m rfid.register`）を案内していたのを修正。

## Remaining Gaps / Out-of-Scope

- [ ] 実機で deck 1 / deck 2 を登録し、`watch` で `card=Ah` 等が解決することを確認。
- [ ] 登録済みカードの**検証モード**（置いたカードの code を読み上げる）は `probe_pcsc watch` の `card=` 表示で代替。
- [ ] 13 slot 化後は `--reader` で任意 slot を指定できる（既定は config 先頭）。

## Related ADRs

- `docs/adr/0040-ccid-virtual-card-always-present.md`（カード有無は Get UID の SW → `read_uid` の None/UID で動く前提）
- `docs/adr/0034-…`（契約 §7 UID 正規化 = `normalize_tag_id`）

## Related Issues

- ISSUE-0015（USB CCID firmware 契約）— host 側「完了後にやること」3 の実装

## Related Commits

- （本 worklog と同コミット）
