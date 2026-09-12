# ADR-0010: hand core の contract 化と決定的 record/replay

## Status

<!-- One of: Proposed / Accepted / Superseded / Rejected / Deprecated -->
Accepted（R1 record-only 実装済。R4/R5 = hand/action contract freeze・replayer は planned）

## Date

2026-06-03

## Context

本リポジトリの思想は **contract-first / fixtures-as-oracle / drift をテストで検知**（ADR-0004 / ADR-0005、
`docs/contracts/`）。player / session / seat_assignment / hand_ref はこの規律下にあり、
`tests/test_contracts.py` が schema↔fixture↔core の整合を固定している。

ところが **製品の心臓である hand logger core はこの規律の外**にある:

- `HandSummary` / `ActionRecord`（`core/hand_log.py`）に対応する schema / fixtures が無い。
- 入力は **ノイジーな realtime センサー列**（マイク ASR ＋ RFID）で、`IntegrationThread` の挙動を
  **オフラインで再現・テストする手段が無い**。実センサー列に対する golden fixture が皆無。
- 結果として ADR-0009 が導入する再構築アルゴリズムを、**ground truth に対して反復・回帰固定できない**。
  融合の重み（ADR-0009 §8）も手置きになる。

つまり「最新層（session/ledger）は厳密、最重要層（hand core）は organic」という逆転が起きている。
本 ADR はこの非対称を解消する **アーキテクチャ決定**で、ADR-0009（再構築エンジン）とは分離可能
（現エンジンのままでも record/replay は導入できる）。

関連: ADR-0004 / ADR-0005（contract-first）/ ADR-0008（hand logger immutability・sidecar 前例）/
ADR-0009（再構築エンジン）/ ISSUE-0010（replay 決定性の記録境界）/ ISSUE-0011（hand/action freeze）。
詳細は `docs/contracts/hand-reconstruction.md`（モデル）/ `docs/contracts/event-replay.md`（harness）。

## Decision

hand core を他層と同じ contract-first 規律に入れ、決定的に replay 可能にする。4 つの決定:

1. **hand core を contract 化する。** `hand`（= 永続化される `HandSummary`）/ `action`（= `ActionRecord`）/
   `reconstruction_event`（= 記録される生センサーイベントの envelope）を `docs/contracts/` の
   1-model-1-schema＋fixtures 規約で定義する。`hand` / `action` は既存 on-disk JSON に対し **additive**
   （ADR-0008 §8.1 の HandSummary draft sketch を出発点にし、別案を作らない）。新 optional フィールドは
   `pots`（main/side）/ per-player `committed`（ADR-0009）、`legal_actions` / `amount_to_call` /
   `corrected_from` / `actor_source` / `asr_confidence`（ADR-0009 の推定が出力する監査情報）。
   `reconstruction_event` は我々が新設する surface なので **より厳格**（`additionalProperties:false`）に寄せる。
2. **append-only event sidecar を記録する。** `IntegrationThread` の入口で、**解釈する前**に各
   `AudioEvent`（raw_text / confidence 含む）/ `RFIDEvent` / `CameraEvent`（`frame` は除外）を
   `reconstruction_event` の JSON Lines として `logs/{session_id}.events.jsonl` に追記する。記録は解釈ゼロ
   ＝挙動不変で、`logs/*.json` / PHH に触れない sidecar（ADR-0008 の legacy sidecar と同じ非破壊方針）。
   単独で先行 ship できる。
3. **決定的 replayer を提供する。** `tools/replay_hand.py`（planned）が `*.events.jsonl` を読み、live と
   **同一の** 再構築ロジックをイベント注入で再実行して `HandSummary` を生成する。決定性の条件:
   - **timestamp は event 由来**にする。engine に **clock source を注入**（live = 実時計、replay =
     観測済み最大 event timestamp）し、`engine.py:429` の `_now_iso()` / `:261` の `time.time()` 窓を
     event time で動かす。
   - **スレッド/キュー競合に依存しない**。replay は event を **timestamp 順**で同一 drain ロジックへ通す。
   - **RNG / 隠れグローバルに依存しない**（カードは RFID 由来か未知 `????`、乱数で配らない）。
4. **golden fixtures を core の oracle にする。** `tests/fixtures/reconstruction/<case>/{events.jsonl,
   expected_hand.json}` を置き、`tests/test_reconstruction.py`（`test_contracts.py` 流）が replayer の
   出力 `HandSummary.to_dict()` を `expected_hand.json` と突き合わせ、`hand` schema で検証する。既知バグを
   そのまま回帰ケースに採る（ADR-0009 の 5 ケース）。さらに `HandSummary(...).to_dict()` /
   `ActionRecord(...).to_dict()` が `hand` / `action` schema を通る **code↔contract** テストを
   `test_core_player_matches_contract`（`test_contracts.py:65`）の前例に倣って追加する。
   `tests/test_contracts.py` の `_MODELS` に `hand` / `action` / `reconstruction_event` を追加する。
5. **session 層（ADR-0008）と整合する。** ADR-0008 の write-through（`player_id` additive、
   `(session_id, hand_id)` 複合キー）と本決定は直交し合成する。`hand` schema が ADR-0008 の
   `players[i].player_id` と ADR-0009 の `pots`/`committed` の **共通の additive 受け皿**になる。event sidecar /
   replay 出力は `(session_id, hand_id)` でアドレスでき、`logs/*.json` / PHH は immutable のまま（ADR-0008
   §8.4）。

## Alternatives Considered

- **record/replay ＋ golden fixtures で contract 化（採用）**
  - Pros: 心臓部に初めて oracle が付き、推定アルゴリズム（ADR-0009）を ground truth で反復・回帰固定できる。
    記録は非破壊・先行 ship 可能。他層と同じ規律に揃う。
  - Cons: 決定性（注入クロック / timestamp 順 replay）の作り込みが要る（ISSUE-0010）。fixtures 整備コスト。
- **snapshot テストのみ（実出力をそのまま固定, 不採用）**
  - Cons: 入力（センサー列）が契約化されず、何を変えると何が壊れるか追えない。脆い。
- **full event-sourcing（hand logger も session も同一 event log, 不採用）**
  - = ADR-0008 の Pattern C。S2 規模に対し overkill。`logs/*.json` / PHH と二重化。S5 で再評価。
- **現状維持（hand core は契約外のまま, 不採用）**
  - Why rejected: 目的（正確な再構築）の改善を**検証する手段が無い**まま据え置く選択であり、思想にも反する。

## Consequences

- Positive
  - hand core が fixtures-as-oracle 規律に入り、ADR-0009 の融合重みを ground truth で較正可能になる。
  - 実セッションの生センサー列を **再現可能なデータ資産**として蓄積できる（不具合の永続再現）。
  - 記録だけ先行導入でき、挙動を変えずに移行を開始できる。
- Negative / trade-offs
  - 決定性のために engine へ clock 注入等の設計制約が入る（ISSUE-0010）。
  - sidecar ファイルが増える（`logs/{session_id}.events.jsonl`）。`.gitignore` 方針は logs と同様。
- Neutral / new constraints
  - `hand` / `action` は当面 draft（v0.x、`additionalProperties:true`）。`1.0` freeze は ISSUE-0011。
  - `reconstruction_event` は `additionalProperties:false` で早めに締める（replay 決定性の要）。

## Validation / Follow-up

- [ ] **ISSUE-0010**: 何を記録すれば決定的 replay になるか（ASR decode 後 text+conf vs raw audio）と、
      live スレッド順序 ≈ timestamp 順の許容度を確定。
- [ ] **ISSUE-0011**: `hand` / `action` の `additionalProperties:false` 昇格と必須/optional の確定。
- [x] R1（実装済, 2026-06-03）: record-only sidecar（`output/event_recorder.py`）＋ `reconstruction_event`
      schema/fixtures（`tests/test_contracts.py` の `_MODELS` 登録）。`config.recording.enabled` で opt-in、
      recorder 未指定で挙動不変。tests: `test_event_recorder` / `test_integration_recording` / 全 187 緑。
- [ ] R4: `hand` / `action` schema 実ファイル化 ＋ `tests/test_contracts.py` の `_MODELS` 登録 ＋
      code↔contract テスト。
- [ ] `tests/test_reconstruction.py` の golden replay が緑（既知バグ 5 ケース）。

## Related Files

- `core/hand_log.py`（`HandSummary` / `ActionRecord` = `hand` / `action` contract の基）
- `integration/engine.py`（event 記録境界 / clock 注入 / drain 順序の対象。**未変更**）
- `core/events.py`（`reconstruction_event` envelope の素。`CameraEvent.frame` は除外）
- `output/json_writer.py`（`logs/*.json` は immutable のまま）
- `docs/contracts/hand-reconstruction.md`（`hand`/`action` sketch）/ `docs/contracts/event-replay.md`
  （sidecar・replayer・golden fixture レイアウト・`reconstruction_event` sketch）
- `docs/contracts/hand-integration.md`（ADR-0008、HandSummary draft sketch の出発点）

## Related Tests

- `tests/test_contracts.py`（`_MODELS` を `hand`/`action`/`reconstruction_event` に拡張する対象）
- 将来追加: `tests/test_reconstruction.py`（golden replay + code↔contract）

## Related Commits

- 本 ADR と同じコミット（design planning, code 未変更）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0009（再構築エンジン）/ ADR-0008（immutability・sidecar 前例）/
  ISSUE-0010（replay 決定性）/ ISSUE-0011（hand/action freeze）
