# ADR-0008: Hand logger × session/seating integration strategy (Phase 2.x)

## Status

Accepted

## Date

2026-06-03

## Context

S2 core（`core/session.py` / `core/session_repository.py`, ADR-0006/0007）が既存 hand logger と
**未接続**のまま導入された。Phase 2.x で両世界を段階的に接続する必要があるが、接続戦略を選ばないと
（1）どこに player_id / session_id / hand_ref を流すか、（2）どのファイル形式が真の source of truth か、
（3）既存 logs/PHH の互換性、が宙に浮く。本 ADR はこの **接続戦略の選択を確定**する（実装は別 phase）。

制約（forces）:

- **既存 hand logger 出力（logs/*.json）と PHH を壊さない**。past log は read-only。
- **PHH は external interchange の安定性が最優先**（registry の player_id を載せない）。
- **S2 core を作り直さない**。`SessionRepository` のメソッド境界はそのまま使う。
- **player_id は S1 registry が source of truth**（hand 側で別採番しない）。
- **hand_id は session 内 int 据え置き**（ADR-0006）。
- **rollback 可能**（session layer を無効化したら従来動作に戻る）。
- 設計詳細・図・migration outline は `docs/contracts/hand-integration.md` を参照。

関連: ADR-0006 / ADR-0007 / ISSUE-0005 / ISSUE-0006 / ISSUE-0007。

## Decision

Phase 2.x の接続戦略として **Pattern A（write-through, additive）** を採用する。要点:

1. **hand logger 側を session-aware にする**（SessionRepository への DI 依存を入れる）。
   双方向同期や read-side reconciler ではなく、hand logger が書き込み時に SessionRepository へ
   write-through する片方向の依存を持つ。
2. **session_id は session レイヤの UUID4 hex（ADR-0007）を canonical** とする。Phase 2.2 で
   `main.py` の採番を切り替える。legacy timestamp 形式の session_id も schema 上は許容（opaque 非空）。
3. **hand 開始時に `SessionRepository.assign_seat` をバッチ呼び出し**して、
   `(session_id, hand_id, seat_no, player_id)` の seat snapshot を session レイヤに記録する。
   これにより `resolve_hand_ref` が以降読める。
4. **HandSummary に `players[i].player_id` を additive に追加**する（optional, legacy では null）。
   schema は `additionalProperties: true` を許す draft sketch（`hand-integration.md` §8.1）に留め、
   `1.0` freeze は Phase 2.x 完了後。
5. **PHH は無改変**。player_id は external interchange に載せない。
6. **`hand_ref` は HandSummary に埋め込まない**。session レイヤ側に住み、cross-app reader は
   `SessionRepository.resolve_hand_ref` で読む（denormalized 重複を作らない＝ADR-0006 の drift 注意点回避）。
7. **rollback path**: `config.session_layer.enabled = false` で SessionRepository への依存を切り、
   従来の timestamp session_id + name-only HandSummary 動作に戻せる。
8. **legacy log は破壊しない**。reconciliation が必要になったら、別 utility と sidecar ファイルで
   後付けする（ISSUE-0007）。元 logs/*.json は read-only。

## Alternatives Considered

- **Pattern A — write-through（採用）**
  - Pros: 既存 JSON が additive で済む。書き込み起点が 1 つで race なし。S2 core の contract を
    そのまま使える。rollback も DI 切替で容易。
  - Cons: hand logger に SessionRepository への依存が入る。seat 選択 UI が必要（ISSUE-0006）。
  - Why chosen: 「既存出力不変」「rollback 容易」「contract 不変」を同時に満たす最短路。

- **Pattern B — read-side reconciler（不採用）**
  - SessionRepository が logs/*.json を後追いで読む。
  - Pros: hand logger 完全無改修。
  - Cons: 元データに player_id が無いため fuzzy name match に依存し人間の confirm が必須。
    ファイル監視・race・順序・削除に弱い。リアルタイム性が無い。
  - Why rejected: 本筋の接続戦略には弱い。**ただし legacy log の遡及取り込み手段としては併用可**
    （ISSUE-0007）。

- **Pattern C — 共通 third store（event-sourced ledger）（不採用）**
  - Pros: 将来的に最も拡張性が高い。
  - Cons: S2 の規模に対して overkill。実装量が S5 並み。既存 logs/PHH との二重化。
  - Why rejected: 過剰設計。S5 で API/sync 化を検討する際に再評価する。

- **Pattern A 派生 — PHH にも player_id を載せる（不採用）**
  - Pros: 1 ファイルで完結。
  - Cons: PHH は external interchange のため registry ID を漏らす副作用。互換性破壊リスク。
  - Why rejected: PHH の安定性が player_id 統合の利点を上回る。

## Consequences

- Positive
  - 既存 logs/*.json / PHH を破壊せずに player_id / session レイヤを通せる。
  - SessionRepository の現 contract に変更が要らない（assign_seat / resolve_hand_ref のみで足りる）。
  - rollback path（config flag）で hand logger 単独運用に戻せる。
- Negative / trade-offs
  - hand logger に SessionRepository 依存が入り、テスト時に mock / in-memory repo が必要になる。
  - seat 選択 UI（ISSUE-0006）が Phase 2.3 までは未確定。
  - session_id が 2 系統並存する移行期間がある（Phase 2.x の間）。
- Neutral / new constraints
  - HandSummary.players の `additionalProperties: true` を当面維持（freeze は Phase 2.x 完了後）。
  - past logs を SessionRepository に取り込むのは別 utility（ISSUE-0007）扱い。

## Validation / Follow-up

- [x] 接続パターンを doc 化（`docs/contracts/hand-integration.md`）。
- [x] HandSummary draft schema sketch を doc に置く（schema ファイル化はしない）。
- [x] ISSUE-0006（seat 選択 UX）/ ISSUE-0007（legacy log 取り込み）を起票。
- [x] Phase 2.1: `config.session_layer.enabled` フラグの追加（E1+E2-core, #10）。
- [x] Phase 2.2: `main.py` の session_id 切替 + `IntegrationThread._start_new_hand` の assign_seat 連携
       + `HandSummary.players[i].player_id` additive（E1+E2-core で engine 側、M3 = E3 で main.py 結線）。
- [x] Phase 2.3: seat 選択 GUI（registry 連動）（M3 = E3, ISSUE-0006 Fixed, 2026-06-11）。
- [ ] Phase 2.4: legacy log reconciler tool（必要に応じて, ISSUE-0007）。
- [ ] schema `1.0` freeze（ISSUE-0005 残項目決着 + Phase 2.2 動作後）。

## Related Files

- `docs/contracts/hand-integration.md`（本 ADR の設計詳細）
- `docs/contracts/session-seating.md`（S2 contract）
- `docs/contracts/shared-ids.md`（session_id 2 系統の併存期）
- `core/hand_log.py`, `output/json_writer.py`, `output/phh_exporter.py`,
  `integration/engine.py`, `main.py`（現 hand logger world, **本 ADR では変更しない**）
- `core/session.py`, `core/session_repository.py`（S2 core, 不変）

## Related Tests

- `tests/test_session_repository.py`（S2 core, 接続後も regression を出さない目標）
- 将来追加: hand logger × SessionRepository 連携 integration test（Phase 2.2 以降）

## Related Commits

- 本 ADR と同じコミット（S2.x integration planning, code 未変更）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0006（複合キー）、ADR-0007（S2 core 永続形）、ISSUE-0005 / 0006 / 0007
