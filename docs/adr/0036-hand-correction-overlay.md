# ADR-0036: ハンド訂正（append-only オーバーレイ / iPad アプリから staff 訂正）

## Status

Accepted（実装済 2026-06-15。v1.0 ローンチレビュー B4。音声は自動記録、訂正は iPad アプリで行う方針）

## Date

2026-06-15

## Context

音声認識は自動記録（rules-aware 再構築, ADR-0009）で、誤認識には `needs_review=True` が付くが、
**記録後に訂正する手段が無かった**（hand log `logs/{session_id}.json` は write-once、個別アクション編集不可）。
v1.0 ローンチレビュー B4。ユーザー決定: **音声は自動記録のまま、訂正は iPad などのアプリ（スタッフ操作）で
行う**。

制約:

- hand log は **live hand-logger プロセス**が per-hand で書く（単一書き手）。iPad 訂正は **別プロセス（API）**
  経由 → 同一ファイルを 2 プロセスが書くと競合する。
- 本プロジェクトは **append-only + 監査**を重んじる（ledger は mutate せず reversal, ADR-0016）。誤認識の
  「元の記録」は再学習・監査のため保持したい。

## Decision

1. **訂正は元 hand log を mutate せず、append-only な「訂正オーバーレイ」**として別ストアに記録する
   （ledger の append-only/reversal と同じ思想）。
   - `core/hand_correction.py`: `HandCorrection`（correction_id / session_id / hand_id /
     action_index（None=hand レベル）/ field / new_value / corrected_by / corrected_at / note）。
   - `core/hand_correction_repository.py`: **append-only** ストア（`hand_corrections.json`,
     atomic+fsync, node-local）。`add_correction` / `list_for_hand` / `list_for_session`。
2. **対象キー** = `(session_id, hand_id, action_index)`（アクションは安定 ID を持たないため、hand 内の
   位置 index）。hand レベル訂正は `action_index=None`。
3. **訂正可能フィールド（v1.0）**: アクションの `action`（種別）/ `amount`、hand レベルの `winner_seat`。
   それ以外（board/hole/seat 帰属）は scope 外（将来 additive）。
4. **read-time オーバーレイ**: `apply_hand_corrections(hand, corrections)` が、元 hand に訂正を時系列順
   （後勝ち）で適用した**訂正済みビュー**を返す。元値は `_original` に保持、`corrected=true` を立て、
   `needs_review` を解除。hand レベルは `_corrections` に監査痕を残す。**元ファイルは不変**。
   - viewer API（`get_hand` / `list_player_hands`）は返却前にオーバーレイ適用。schema は
     `additionalProperties:true`（ISSUE-0011）なので追加フィールドは契約内。
5. **認可 = staff**（ハンド履歴の権威的編集はスタッフ/オペレータ操作）。
   `POST /api/staff/sessions/{sid}/hands/{hid}/corrections`（staff token, write 所有プロセスのみ）。
   player read は訂正できない（閲覧のみ）。
6. **単一書き手の整合**: 訂正は `hand_corrections.json`（別ファイル）に書くため、hand-logger プロセスの
   hand log write と競合しない。訂正ストアの write 所有は API プロセス（`--ledger`, viewer_api.enabled）。

## Alternatives Considered

- **元 hand log を直接 mutate** → write-once/append-only に反し、live hand-logger と別プロセス API が
  同一ファイルを書く競合。元の ASR 記録（誤認識の証跡）が消える。→ overlay（D1）。
- **訂正をハンドロガー GUI（desktop）で** → ユーザーは iPad アプリ希望。desktop 訂正 UI は作らない（B4 決定）。
- **アクションに安定 ID を付与** → schema/記録系の変更が大きい。位置 index で十分（append-only で安定）。→ index。
- **player が自分のハンドを訂正** → 権威性・なりすまし問題。→ staff 認可（D5）。

## Consequences

- Positive: 「音声は自動記録 + iPad で訂正」が実現。元記録を保持したまま訂正でき（監査・再学習）、
  プロセス競合なし。viewer/mobile は訂正済みビューを表示。1 方向の append-only 思想で一貫。
- Negative / trade-offs: 読み取り側がオーバーレイ適用を呼ぶ規律が要る（viewer に集約）。**PHH export への
  オーバーレイ適用は本 ADR では未対応**（follow-up。export は元記録のまま）。iPad の訂正 UI 画面自体は
  別途実装（本 ADR は data path = core+API+mobile repo まで）。
- Neutral: `hand_corrections.json`（node-local, .gitignore）+ `core/hand_correction*.py` + staff endpoint 1 本。

## Validation / Follow-up

- [x] `core/hand_correction.py` / `core/hand_correction_repository.py`（append-only）。
- [x] `apply_hand_corrections` オーバーレイ（元値保持・needs_review 解除・監査痕）。
- [x] staff API `POST /api/staff/sessions/{sid}/hands/{hid}/corrections` + viewer get_hand/list_player_hands に適用 + `ViewerApiClient.add_hand_correction`。
- [x] mobile `ViewerRepository.addHandCorrection`（mock/HTTP）+ 型 + mock test。
- [ ] iPad 訂正 UI 画面（mobile, 反復実装）。PHH export へのオーバーレイ適用。board/hole/seat 訂正。
  訂正の取り消し（unwind）。

## Related Files

- `core/hand_correction.py` / `core/hand_correction_repository.py` / `hand_corrections.json`
- `api/read_models.py`（overlay 適用）/ `api/server.py`（staff endpoint）/ `api/client.py`
- `mobile/src/api/*`（repo / types）/ 将来 `mobile/src/screens/CorrectionScreen.tsx`

## Related Tests

- `tests/test_hand_correction.py` / `tests/test_viewer_api_correction.py` /
  `mobile/src/api/mockRepository.test.ts`

## Related Commits

- 本 ADR の実装 commit（2026-06-15）

## Supersedes / Superseded by

- Supersedes: —（関連: ADR-0009（再構築/needs_review）/ ADR-0016（append-only 思想）/ ADR-0021（staff write）/
  ADR-0017（viewer/mobile）/ ISSUE-0011（hand schema additionalProperties））
- Superseded by: —
