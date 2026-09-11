# ADR-0032: sync 後続（rename 伝播 / hand log file-level union / 定期 auto-trigger）

## Status

Accepted（**実装済 2026-06-14**。ADR-0022 の双方向 sync を additive に拡張）

## Date

2026-06-14

## Context

ADR-0022（state-based merge）の残課題として CLAUDE.md に 3 点が挙がっていた:

1. **player rename 伝播**: 既存 `merge_players` は create-only union + local 優先で、rename が peer に
   伝播しなかった。
2. **hand log の file-level union**: sync は players/sessions/ledger/orders の 4 ストアのみで、
   `logs/{session_id}.json`（hand log）は同期対象外だった。
3. **定期 auto-trigger**: sync は on-demand pull（`ViewerApiClient.sync_bidirectional` の手動呼び出し）
   のみで、定期実行が無かった。

いずれも ADR-0022 の収束マージ（可換・結合・冪等）と整合する形で additive 実装できる。

## Decision

### D1. player rename 伝播 = `updated_at` の last-writer-wins（LWW）

- `Player` に optional `updated_at`（schema `1.1`→`1.2` additive）を追加。create で `created_at` と同値、
  **rename で現在時刻に更新**する（merge/unmerge では触らない = display 変更のみを追跡）。
- sync の `_resolve_player` を、**display 系（display_name / created_at）は `updated_at` LWW**
  （同値は内容の安定 tiebreak）、**`merged_into` は monotonic**（ADR-0030 D5: merged が勝つ、両 merged は
  survivor 最小）、**`updated_at` は max** で合成する。display LWW と merge monotonic は直交フィールドなので
  可換・冪等で収束する。
- **制約**: unmerge（ADR-0030 D4）は monotonic 規則により sync では伝播しない（局所操作）。これは
  ADR-0022 の単調収束方針の既知の帰結として許容する。

### D2. hand log file-level union

- `core/sync.py:merge_hand_logs(local, remote)`: `session_id → {session_id, hands:[...]}` を union。
  hands は **`hand_id` で append-only union**（同一 id は同一内容 ⇒ 衝突なし）。決定的順序は hand_id 昇順。
- `build_snapshot` / `merge_snapshot_into` に optional `log_dir` を additive。snapshot に `hand_logs` を含め、
  merge は `logs/{session_id}.json` をアトミックに書き戻す（変更時のみ）。summary に `hand_logs_added`。
- viewer API の `_sync_paths()` が `log_dir` を渡す（`/api/staff/sync/{snapshot,merge}` で hand log も同期）。
- `log_dir` 未指定（既存呼び出し / 純関数テスト）は従来どおり hand log を扱わない（後方互換）。

### D3. 定期 auto-trigger = `SyncScheduler`（純粋ロジック + clock 注入）

- `core/sync_scheduler.py:SyncScheduler(interval_sec, sync_fn, now_fn=time.time)`: `interval_sec<=0` で
  無効（既定 off）。`tick(now)` が due（初回は即時、以後 interval 経過）なら `sync_fn` を 1 回実行して
  `last_run` を更新する。callback の例外は握りつぶしてログのみ（sync 失敗で停止しない）。
- 「いつ sync するか」の純粋ロジックだけを持ち、実 sync（`ViewerApiClient.sync_bidirectional` 等）は
  callback として注入 → clock 注入で決定的にテスト可能。config `viewer_api.sync_auto_interval_sec`
  （既定 0 = off）。
- **実プロセスへの常駐スレッド組み込み**（一定間隔に `tick` を呼ぶ薄いラッパ）と「どのプロセスが回すか」
  は ADR-0029 の **会場主導**に従う運用判断とし、本 ADR ではスケジューラ本体までを実装する。

## Alternatives Considered

- **rename を local 優先のまま（伝播しない）** → 別端末で改名しても反映されない。→ `updated_at` LWW（D1）。
- **rename にバージョンカウンタ** → 別 metadata が増える。timestamp LWW で十分。
- **hand log を session ストアに内包して同期** → hand log は大きく file-per-session で append-only。
  別 union（D2）が自然で sessions の入れ子 merge を汚さない。
- **auto-trigger を常駐スレッドとして即実装** → 「誰が回すか」は運用判断（ADR-0029）。純粋スケジューラ +
  注入で本質をテスト可能にし、常駐化は薄いラッパに留める（D3）。

## Consequences

- Positive: rename が両ノードで一致、hand log も収束、定期同期の土台。すべて ADR-0022 の可換・冪等・収束を
  維持し additive（既定挙動不変: rename は新フィールドで後方互換、hand log は `log_dir` opt-in、scheduler は
  interval 0 で off）。
- Negative / trade-offs: unmerge は sync 伝播しない（D1 既知制約）。auto-trigger の常駐スレッド化と
  「回す主体」の最終結線は運用タスクに残る。
- Neutral: `player` schema `1.1`→`1.2`（`updated_at` optional additive）。`core/sync_scheduler.py` 新規 +
  config 1 個。

## Validation / Follow-up

- [x] `updated_at`（player schema 1.2 + create/rename 設定）+ `_resolve_player` の LWW×monotonic 合成。
- [x] `merge_hand_logs` + `build_snapshot`/`merge_snapshot_into` の `log_dir` + `_sync_paths` 結線。
- [x] `SyncScheduler`（clock 注入）+ config `sync_auto_interval_sec`。
- [x] tests: rename LWW（伝播 / 古いほうが負ける / 可換）/ hand log union（純関数 + snapshot + API E2E）/
  scheduler（off / 初回即時 / interval 待ち / 例外耐性）。607 passed。
- [ ] auto-trigger の常駐スレッド結線（どのプロセスが `tick` を回すか）= 運用タスク（ADR-0029 会場主導）。

## Related Files

- `core/player.py` / `core/player_repository.py` / `docs/contracts/schemas/player.schema.json`（`1.2`）
- `core/sync.py`（`_resolve_player` / `merge_hand_logs` / snapshot に `log_dir`）/ `api/server.py`（`_sync_paths`）
- `core/sync_scheduler.py` / `config_default.json`（`viewer_api.sync_auto_interval_sec`）

## Related Tests

- `tests/test_sync.py`（rename / hand log）/ `tests/test_sync_scheduler.py` /
  `tests/test_viewer_api_sync.py`（rename / hand log の API E2E）

## Related Commits

- 本 ADR の実装 commit（2026-06-14）

## Supersedes / Superseded by

- Supersedes: —（ADR-0022 を additive 拡張。関連: ADR-0022/0024（sync）/ ADR-0030（merge monotonic）/
  ADR-0029（会場主導の運用）/ ADR-0019（schema additive 規則））
- Superseded by: —
