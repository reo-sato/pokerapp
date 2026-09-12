# Worklog: RFID 1 reader 複数枚（席 2 枚 / flop 3 枚）— host 側対応

## Date

2026-09-10

## Scope / Task

canonical PC/SC 経路（`rfid/`）と診断/登録ツール・config・契約を、**1 つの reader に複数カードが
重なって置かれる**運用に対応させる（host = Python 側のみ。firmware 側の anti-collision と Get UID
連結は別 agent が同時に実装 = ISSUE-0021）。

## Goal

- 席 reader（8 台）に hole card **2 枚を重ねたまま**置いて 2 枚とも記録される。
- board reader **3 台**で 5 枚を扱う（board1 = flop 3 枚重ね / board2 = turn / board3 = river）。
  board の位置（`RFIDEvent.board_index` 1..5）が重ね置きでも一意に決まり、street 自動遷移が壊れない。
- 合計 **11 slot**（席 8 + board 3）を config のサンプル・lint・QA 手順の既定にする。
- `integration/engine.py` は **変更しない**（board は `board_index` 指定、席は `_hole_cards[seat]` に
  2 枚まで蓄積 = 既に複数 event 前提の実装）。

## Changed Files

- `rfid/bridge.py` — `split_uid_response()`（応答長 16/24/32 のときだけ 8B ずつ分割）+
  `PCSCBridge.read_uids() -> list[str]`（`read_uid()` は先頭 or None の薄いラッパに）+
  `bridge_read_uids(bridge)`（旧 bridge 互換シム）+ `MockPCSCBridge` が `str`/`None`/`list[str]` を受理。
- `rfid/reader_thread.py` — デバウンスを **UID 集合の差分**に（`_last_uids: dict[reader_id, set[str]]`）。
  増えた UID ごとに 1 event、消えた UID は状態更新のみ。board は `cards`（既定 1）ぶんの offset を
  管理し `board_index = index + offset` を付与（外して戻すと同じ位置 / 超過は WARN + `board_index=None`）。
- `tools/probe_pcsc.py` — lint に `cards` 検査（1..5 / `cards>1` は index 必須 / 範囲超え / board 位置の
  重なり）、`reader_label` が `board 1-3` を表示、`raw` が `UID×k = A, B, C` を表示、`watch` ヘッダに 1 文。
- `tools/register_cards.py` — `read_uid()` → `bridge_read_uids()`。**2 枚以上検出中は登録せず**
  `⚠ N 枚検出 — 1 枚だけ置いてください`（状態が変わったときだけ表示）。
- `config_default.json` — `pcsc_readers` サンプルを本番 11 件（`PokerRFID PN5180-CCID 0..10`）に。
  `_pcsc_readers_comment` を 11 slot 構成 + 他 OS は `probe_pcsc list` で置換に更新、`cards` の意味を
  `_pcsc_cards_comment` に追加。
- `tests/test_rfid.py` — `split_uid_response` / `PCSCBridge.read_uids`（SW≠9000・例外・未接続）/
  互換シム / 集合デバウンス / board offset 割り当ての各テスト（`_Poller` = `_poll_reader` 決定的ドライバ）。
- `tests/test_tools_probe_pcsc.py` — `cards` lint（正常 / index 無し / 範囲超え / 重なり / 11 slot 構成）、
  `reader_label` の `board 1-3`、`format_uid_payload`。
- `tests/test_tools_register_cards.py` — 重ね置き中は登録しない → 1 枚で登録（ループ + CLI 経路）。
- `docs/contracts/rfid-usb-ccid.md` — **v1.1（additive over 1.0 frozen）**: §3 本番 11 slot、§4 `cards` と
  位置割り当て規則、§6 Get UID の複数 UID 連結（8B × k, k≤4, 昇順, host は 16/24/32 で分割）、
  §7 UID は MSB-first（firmware が ISO15693 の LSB-first を反転）、§8 UID 単位デバウンス、§10 v1.1 の内容。
- `docs/hardware-qa-checklist.md` — 手順 2 の config 例を 11 件版に、lint の説明に `cards`、手順 4 の期待に
  重ね置き（席 2 行 / flop `board 1`〜`board 3`）、受け入れ基準表を v1.1 に。
- `docs/installation.md` — `pcsc_readers` の説明に 11 台構成と `cards` を追記。
- `CHANGELOG.md` / `CLAUDE.md` / `docs/decision-log.md` — 変更点の反映と ISSUE-0021 の索引行。

## Expected Behavior

- **bridge**: Get UID 応答のデータ長が 16/24/32 なら 8B ずつ分割して複数 UID、それ以外（4/7/8 等）は
  単一 UID。SW≠`90 00` / 例外 / 未接続は空リスト。`read_uid()` は先頭 1 件（後方互換）。
- **RFIDThread**: reader ごとに UID 集合を持ち、増えた UID ごとに `RFIDEvent` を 1 件。置きっぱなしは
  再発火しない。1 枚だけ外して戻すとその UID だけ再発火。board は `index + offset`（検出順に最小の
  空き、外して戻せば同じ offset、超過は WARN + `board_index=None`）。`index` の無い board は従来どおり None。
- **engine 無改修で**: flop 3 枚重ね → `board_index` 1/2/3 → `_board_positions` 3 件 → street=flop に自動遷移。
  席 2 枚重ね → 同一 seat の event 2 件 → `_hole_cards[seat]` に 2 枚。
- **tools**: `check` が `cards` の設定ミス（index 無し / 範囲超え / 位置の重なり）を検出。`raw` が複数 UID を
  分割表示。`register_cards` は複数枚が載っている間は登録しない。

## Implemented Behavior

Expected と一致。補足（実装上の判断）:

- **分割の判定は「長さのみ」**（契約 v1.1 §6）。4/7B の ISO14443A と衝突しないよう 16/24/32 のみを
  複数枚とみなす。12B（4B × 3 枚など）は分割しない = ISO15693 8B 以外の重ね置きは非対応。
- **offset の記憶**は「現在の割り当て（`_board_offsets`）」と「過去の割り当て（`_board_offset_memory`）」を
  分けて保持。外れた UID は現在割り当てから外すが記憶には残すので、戻すと同じ位置に復帰する。
  記憶は reader ごとに単調増加（ハンドを跨いでクリアしない）。1 デッキ 54 枚規模なので実害はない。
- **`cards` の不正値**（0 / 非 int）は WARN の上 1 として扱う（起動を落とさない = エラーハンドリング方針）。
  設定ミスの検出は `probe_pcsc check` の lint に寄せた。
- **`bridge_read_uids`** を bridge 側に置いて `RFIDThread` と `register_cards` で共用（旧 bridge 互換の
  一元化）。`rfid/bridge.py` は `rfid.card_master.bytes_to_tag_id` を module-level import に変更した
  （従来は関数内 import。循環は無い）。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **774 passed, 0 skipped**（`requirements-dev.txt`
  導入後。未導入だと pokerkit/fastapi/jsonschema 由来で 44 skip）。
- 追加テスト: `tests/test_rfid.py`（66 件に増加）/ `tests/test_tools_probe_pcsc.py` /
  `tests/test_tools_register_cards.py`。
- 実機確認は **未実施**（下の Remaining Gaps）。

## Mismatches Found During Testing

None observed（既存テストは無改修で通った = 1 枚運用の挙動は不変）。

## Fixes Applied

- なし（新規実装。既存の `read_uid()` 依存箇所は `read_uids()` へ移行しつつ後方互換シムを残した）。

## Remaining Gaps / Out-of-Scope

- [ ] **実機確認**: 席に 2 枚重ね → 2 行、board1 に flop 3 枚重ね → `board 1`〜`board 3`、1 枚だけ外して
      戻すと同じ位置。`probe_pcsc watch` / `raw` と hand logger 通し（`docs/hardware-qa-checklist.md` 手順 4/5）。
- [ ] **`cards` 超過時の運用挙動**: 現状は WARN + `board_index=None`（engine は末尾に追記）。
      実機で「4 枚目が載る」ケースがどのくらい起きるか、needs_review にすべきかは未決。
- [ ] **firmware 側**（別 agent / 別 worklog）: mask DFS anti-collision + Get UID 連結 + 高速 inventory
      （ISSUE-0021）。実機未検証。
- [ ] 8B（ISO15693）以外の重ね置きは非対応（判定が長さベースのため）。現行カードは ICODE のみなので可。
- [ ] `_board_offset_memory` はプロセス寿命で単調増加（クリア API なし）。

## Related ADRs

- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 本契約の freeze（v1.1 は additive）。
- `docs/adr/0040-ccid-virtual-card-always-present.md` — slot 常時 present / カード有無は Get UID の SW
  （v1.1 でも前提として不変）。

## Related Issues

- `docs/issues/0021-pn5180-poll-cycle-latency.md` — PN5180 の poll 1 周の遅延と複数枚 anti-collision
  （firmware 側。本 worklog は host 側の対応）。
- `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md` — 契約の出所。

## Related Commits

- （未 commit。親 agent がレビュー後に commit する）
