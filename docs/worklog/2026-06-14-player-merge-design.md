# Worklog: player merge の設計（ADR-0030）

## Date

2026-06-14

## Scope / Task

L2（ADR-0028/0029）の唯一残った前提ブロッカー = **player merge** を設計する。設計記録のみ（コードなし）。
同一人物が複数 player_id を持つケース（cloud signup ↔ 会場 name-pick、または LAN の二重作成）を統合する。

## Goal

既存不変条件（ledger append-only / 収束 sync / player_id 不変）を壊さずに merge を成立させる方式を
「実装着手リストが書ける」粒度で確定する。

## Investigation（merge の波及 = player_id をキーにする read 箇所）

- settlement 集計: `core/ledger_repository.py:compute_settlement`（player_id で group, 行 487–496）/
  point 残高（292）/ list フィルタ（336, 481）。
- viewer per-player: `api/read_models.py:list_player_sessions`/`list_player_hands`/`get_player_session_ledger`。
- seating: `core/session_repository.py`（current_seating / resolve_seat_map_for_hand）。
- order フィルタ: `core/order_request_repository.py:list_requests`（230）。
- login principal（L1 verify_pin 後 / L2 auth_identity 解決後）。
- node-local キー: `player_credentials.json`（L1）/ `auth_identity`（L2）。

## Decision（ADR-0030）

- **alias/tombstone**: absorbed player に `merged_into` を付け、**履歴を rewrite しない**。理由: append-only
  と整合 / monotonic additive で収束 sync に乗る / 可逆。rewrite・物理削除は不採用（append-only 違反・
  非収束・dangling 参照）。
- **read-time canonicalization**: `PlayerRepository.resolve_canonical`（チェーン/サイクルガード）で上記
  全 read を survivor に解決。core が source of truth、front-end に複製しない。
- **survivor はスタッフ明示**（既定提案 = 会計履歴を持つ古い方 = 通常会場 player）。staff API
  `POST /api/staff/players/merge` + registry GUI。
- **可逆**（unmerge = `merged_into` 除去）。**sync conflict** は決定的 tiebreak（安定キー最小等）で収束。
- schema `player` `1.0`→`1.1`（optional `merged_into`/`merged_at` additive, settlement 1.1 先例）。

## Changed Files

- `docs/adr/0030-player-merge-alias-design.md`（新規, Proposed）。
- `docs/decision-log.md`: ADR-0030 行追加。
- `CLAUDE.md`: 実装状況に player merge 行 + L2 行・残作業 #7・S1 out-of-scope を ADR-0030 反映に更新。
- `CHANGELOG.md`: Docs 節追加。

## Expected / Implemented Behavior

- 設計タスクのため挙動変更なし（コードなし）。ADR は Proposed（実装は別タスク）。

## Test Results

- コード不変。`ruff check .` — clean（docs のみ）。

## Mismatches Found During Testing

- なし（コード変更なし）。

## Remaining Gaps / Out-of-Scope（実装着手リスト = ADR-0030 §Validation）

- schema additive bump / core merge + resolve_canonical / 各 read 境界の canonicalize / sync tiebreak /
  staff API + GUI / tests（merge・unmerge・チェーン・settlement 合算・login-through-merge・sync 収束）。
- これが済めば L2 本体（ADR-0028）に進める。

## Related ADRs / Issues

- ADR-0030（本件）/ ADR-0028・0029（L2 前提）/ ADR-0004（player_id 不変）/ ADR-0016（append-only）/
  ADR-0022（収束 sync）/ ADR-0019（schema freeze の additive 規則）

## Related Commits

- 本 commit
