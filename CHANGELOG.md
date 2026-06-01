# Changelog

This project's changelog follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project does not yet have versioned releases; entries are organized
per implementation phase under `[Unreleased]`.

For implementation-level history (per-task worklog, decision rationale,
issue / mismatch log) see `docs/worklog/`, `docs/adr/`, `docs/issues/`.

## [Unreleased]

### Docs

- **PC/SC RFID 実機テスト手順書を追加** (`docs/testing/rfid-pcsc-hardware-test.md`):
  PC/SC リーダー（ACR122U 等）を使った RFID 実機テストの end-to-end 手順
  （依存導入 → リーダー名確認 → カード登録 → `config.json` の PC/SC 設定 →
  単体スモーク → ハンドロガー起動検証 → トラブルシュート）。HTTP デフォルト設定との
  差分（`transport`・`readers` の dict/list 差・`name` 必須）と、未登録タグ /
  デバウンス / street 自動推移の期待挙動を明記。あわせて未実装の
  `python -m rfid.register`（`rfid_cards.json` の description が案内）を ISSUE-0007 として起票。

### Branch consolidation (2026-05-22)

並行開発で分岐していた複数ブランチを単一 `main` に統合した。hand logger コアは
`bayesian-action-estimation`（Phase 5-K まで: button rotation / Vosk / 音声正規化 /
ベイズ推論 M1-M3 / hand reconstruction / pot settlement / 手動入力エンジン経路）を
base とし、`player-registry-session-ledger-scope` 由来の player registry + contracts +
parallel-development plan、`new-session` 由来の web viewer をその上に再ベースラインした。
全体の設計計画の核は registry/contracts 系（contract-first / parallel development）を維持し、
hand logger 内部は「実装済み」として CLAUDE.md に統合した。統合経緯は
`docs/worklog/2026-05-22-branch-consolidation.md`。統合後テスト **711 passed**。

統合時に旧 main 由来の issue を採番し直した（ISSUE-0001→0003 / 0002→0004 / 0003→0005 /
0004→0006）。hand logger 由来の ADR-0001/0002・ISSUE-0001/0002 はそのまま。

### Added

- **トーナメントタイマー + 会場ディスプレイ** (`add-tournament-timer` から統合): `core/tournament_timer.py`
  （`BlindLevel` / `TournamentStructure` / `TournamentTimer`, deadline ベース level 進行 +
  `on_level_changed` callback）、`core/tournament_state.py`（entries/busts/addons + 派生量）、
  `gui/tournament_display.py`（会場掲示用 `CTkToplevel`）。dashboard に timer 操作 +
  entry/bust/addon 入力 + ディスプレイ起動を追加。起動時 `tournament_structure.json` があれば
  timer 構築（不在/不正なら warning で OFF）。level 変化は `IntegrationThread.update_blinds`
  （canonical, 次 hand から有効）。cherry-pick `4fb02da`（衝突は `gui/dashboard.py` 1 ファイルのみ、
  patch-details 系と timer 系の別メソッド併存で解決）。timer 関連 48 件追加、全体 **759 passed**。
- **Web ハンド履歴ビューア** (`new-session` から統合): `viewer/`（静的 HTML/CSS/JS,
  バックエンドなし）の 3 画面（session 一覧 / hand 一覧 / hand 詳細）。
  `output/json_writer.py` がハンド保存ごとに `logs/index.json` を再生成し、viewer が
  バックエンドなしで session を列挙できる。`cd viewer && python -m http.server 8765` で起動。
- **Player Registry (Phase S1)** (`player-registry-...` から統合): hand logger とは
  **別画面** の player 管理機能。`python main.py --players` で起動。player の新規作成 /
  一覧 / display_name リネーム、属性は `player_id`（UUID hex）+ `display_name` + `created_at`、
  `players.json` 永続化、validation（空文字 / 前後空白のみ / 完全一致重複を拒否）。
  実装: `core/player.py`, `core/player_repository.py`, `gui/player_registry.py`。
- `BettingState.acted_this_street: set[int]` + `is_round_closed()` +
  `IntegrationThread._advance_street_on_round_close` (Phase 5-K)。voluntary action のみ
  acted_this_street に追加（blind post は除外）。round close 判定で 1 街自動 advance。
- `ManualActionEvent(seat, action, amount, timestamp)` in `core/events.py` (Phase 5-I)。
- `ManualActionRejection(...)` in `core/events.py` (Phase 5-J)。
- `IntegrationThread.manual_queue` + `_drain_manual_queue` + `_handle_manual_action_event`
  (Phase 5-I)、`on_manual_rejected` callback + `GUIDashboard.on_manual_rejected` (Phase 5-J)。
- Docs-as-code 基盤: `CHANGELOG.md` + `docs/{worklog,adr,issues,decision-log,templates,contracts}`。
  "Documentation and Traceability Rules" を `CLAUDE.md` に明記。

### Changed

- **Phase 5-J** — manual action の actor mismatch を permissive warning から **strict reject** に
  変更。`IntegrationThread._handle_manual_action_event` が `event.seat != bs.actor_seat` 時に
  `gs.apply_action` / `bs.update_after_action` を呼ばず `ActionRecord` も積まず、`on_manual_rejected`
  (`ManualActionRejection`) を発火する。audio 経路の actor mismatch（needs_review=True で apply は
  通す permissive）には影響しない。詳細: `docs/issues/0002-...`, `docs/adr/0002-...`,
  `docs/worklog/2026-05-22-phase-5-J.md`。
- `gui/dashboard.py:_cmd_manual_action` を `manual_queue` push 経路に書き換え (Phase 5-I)。
  GUI スレッドから `gs.apply_action` の直叩きはしない。

### Fixed

- **Phase 5-K** — betting round が閉じてもストリートが自動推移せず、preflop 全 call 後の次
  アクションが「preflop 再 open」扱いになり `bet vs raise mismatch` の `⚠要確認` を出していた問題を
  修正。`BettingState.is_round_closed()` 導入により `_advance_street_on_round_close` が 1 街 advance。
  river close は showdown 待ちで auto-advance しない。詳細: `docs/worklog/2026-05-22-phase-5-K.md`。
- **Phase 5-I** — GUI 手動入力が `GameStateManager.apply_action` を直叩きし `BettingState` が更新
  されず 5 件のポーカールール違反（連続 raise / call 0 / bet vs raise 混同 / street 未更新 /
  SB-BB auto-post 不可視）が通っていた問題を、`ManualActionEvent` + `manual_queue` +
  `_handle_manual_action_event` で音声経路と同等品質に修正。詳細:
  `docs/issues/0001-...`, `docs/adr/0001-...`, `docs/worklog/2026-05-22-phase-5-I.md`。

### Docs / Planning

- **Contracts bootstrap (Phase 0a)**: `docs/contracts/` を新設し contract-first の単一 source を凍結。
  shared ID 契約（`player_id` UUID4 hex / `session_id` opaque string / `hand_id` int+複合キー）、
  `schemas/player.schema.json` (v1.0) + `shared-ids.schema.json`、`fixtures/player/`、
  freeze/versioning/drift detection ルール。`tests/test_contracts.py` 追加、
  `requirements.txt` に `jsonschema>=4.0.0`。
- **Parallel development plan**: `CLAUDE.md` に `# Parallel development plan` 節（WS0-WS3、
  parallelizable/blocker、freeze order、mobile mock、React Native scaffold 提案、phase 0-5）。
- **ADR-0003** (Accepted): hand logging から session ledger / store settlement / point ledger への
  ドメイン拡張。**ADR-0004** (Accepted): contract-first parallel development / shared IDs /
  separate front-ends。**ADR-0005** (Accepted): contracts repository layout & freeze workflow。
- **Issues**: ISSUE-0003 (point 残高 source of truth, Open) / ISSUE-0004 (display_name uniqueness,
  Open) / ISSUE-0005 (contract drift, Phase 0a で部分緩和) / ISSUE-0006 (hand_id int vs cross-app
  文字列, S2 で reconcile)。
- 提案: mobile は **React Native**（mock repository 先行、初期 skeleton は player registry のみ）。

### Notes

- 次フェーズ候補: S2 (session + hand-based seating) → S3 (ledger entries + point ledger) →
  S4 (session settlement + paid/unpaid) → S5 (cross-app contract)。
- hand logger 側の future scope は v6.0+ ベイズ推論の深化 / per-dealer 学習（CLAUDE.md 参照）。
