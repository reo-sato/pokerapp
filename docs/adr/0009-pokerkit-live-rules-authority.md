# ADR-0009: pokerkit を live ルール権威として採用する（hand reconstruction engine）

## Status

<!-- One of: Proposed / Accepted / Superseded / Rejected / Deprecated -->
Proposed

## Date

2026-06-03

## Context

hand logger の **目的は「ノイジーな 2 ソース（マイク ASR ＋ RFID）から正確にハンドを再構築する」**こと
だが、現状の core はポーカーのルールを一切知らない。読み取り調査で確認した事実:

- **アクターがルールから導出されていない。** `core/game_state.py:158` `get_current_player()` は
  `_active_seats` 上の単純ラウンドロビンで、BTN/SB/BB のポジション順を無視する（`:161` 自身が
  「Phase 3 で PokerKit の actor_index に差し替える」と明記）。`integration/engine.py:313` は
  `seat = gs.get_current_player()` をそのまま action に帰属させる。turn tracker がずれると全 action が
  誤帰属する。
- **合法手・ベッティング状態が無い。** ストリート毎コミット・min-raise・合法手集合・ラウンド完了判定が
  未実装。pot は `engine.py:413` の素朴な総和、`game_state.py:90` は「メインポットのみ・サイドポット
  扱わない」と明記。
- **pokerkit は宣言済みだが未使用。** `requirements.txt:4` に `pokerkit>=0.5.0` があり CLAUDE.md は
  「PHH 出力」用と記すが、`output/phh_exporter.py` は手書き TOML シリアライザ（`_to_toml`）で、
  **pokerkit はコードのどこからも import されていない**。`game_state.py:31/91/162` に「PokerKit
  ラッパーに差し替える／外部 I/F は変えない」TODO が既に存在する。

forces（制約）:

- **既存出力を壊さない。** `core/hand_log.py` の `HandSummary` / `ActionRecord`、`logs/*.json`、PHH は
  past data を read-only として扱い、形を変えない（ADR-0008 の hand-logger immutability 原則を継承）。
- **ライブ処理を落とさない。** 認識エラーで crash しない（CLAUDE.md エラーハンドリング方針、
  `engine.py:315` の try/except + `needs_review` 付与）。
- **rollback 可能。** 新エンジンを無効化したら従来動作に戻れること。
- **本 ADR は接続される推定アルゴリズム（actor 推定 / ASR 訂正 / 融合）そのものは決めない** — それは
  ADR-0010。本 ADR は「再構築の**権威をどこに置くか**」という engine/dependency の 1 決定に限定する。

関連: ADR-0010（推定・融合アルゴリズム）/ ADR-0011（contract 化・replay）/ ADR-0008（session 接続の
additive/rollback 前例）/ ISSUE-0008（pokerkit online API 実現性）。設計詳細は
`docs/contracts/hand-reconstruction.md`。

## Decision

再構築の **ゲーム状態の権威（source of truth for legal state）を `pokerkit.State` に置く**。要点:

1. **安定境界の背後で実装を差し替える（additive）。** 現 `GameStateManager` の公開 I/F
   （`new_hand` / `advance_street` / `end_hand` / `apply_action` / `get_current_player` / `advance_turn` /
   `hand_id` / `street` / `pot` / `get_stack(s)` / `get_player_name` / `get_active_seats` /
   `update_stack` / `rebuy`、および `engine.py:409` が読む `_sb`/`_bb`）を **`PokerEngine` Protocol** として
   明文化し、`pokerkit.State` 実装 `PokerkitGameState` を提供する。`integration/engine.py` / `main.py` の
   呼び出し側は変えない（`game_state.py:31` の「外部 I/F は変えない」前提に乗る）。
2. **「境界での推定」パターン。** raw ASR を `pokerkit.State` に直接流さない。各 action で
   `engine.legal_context()`（手番 prior・合法手集合・amount_to_call・min_raise）を読み、推定（ADR-0010）で
   合法手へ射影してから `State` に適用する。`State` が拒否したら crash せず `needs_review` ＋未検証 action
   としてフォールバックする。これで `State` は常に内部整合（side-pot / min-raise が正しいまま）を保ち、
   ノイズを境界に隔離する。
3. **backend 選択フラグ。** `config.engine.backend = "pokerkit" | "legacy"`（default は移行期間 `legacy`）で
   実装を選択する（ADR-0008 の `config.session_layer.enabled` rollback 思想に倣う）。旧ラウンドロビン実装は
   `LegacyGameState` として残す。
4. **出力は additive のみ。** `HandSummary` / `ActionRecord` と `logs/*.json` の現フィールドは不変。
   新情報（main/side pot、per-street commitment、legal context、hole cards）は **optional 追加フィールド**
   として乗せる（schema は ADR-0011）。PHH は無改変（external interchange）。ただし call/check の区別・
   side-pot は結果として **より正確**になる（その箇所だけ PHH/JSON が変わりうる点は明示する）。
5. **seat↔pokerkit index 写像。** pokerkit の player index（0..n-1）と疎な seat_no（1..9）の安定全単射を
   `new_hand()` で BTN 基準に固定する（`output/phh_exporter.py:119` の `seat_to_idx` を正式化・共有）。

本決定は `game_state.py` の既存「Phase 3 で PokerKit」TODO を **方針として確定**するものであり（実装は別
Phase, ADR-0011 の R2）、過去の決定を反転しない。

## Alternatives Considered

- **pokerkit を live 権威にする（採用）**
  - Pros: 既に支払い済みの依存を初めて活用。actor_index / 合法手 / side-pot / min-raise が「ただで」手に
    入る。境界での推定で `State` 整合を保証。既存 TODO の素直な具体化。
  - Cons: pokerkit の incremental/online 駆動 API の実現性検証が必要（ISSUE-0008）。seat↔index 写像と
    blinds/antes セットアップを正しく扱う必要。
  - Why chosen: 目的（正確な再構築）に対して最小コストで最大のルール正しさを得られる。
- **現 `GameStateManager` を手作りで拡張（不採用）**
  - ベッティング状態・min-raise・side-pot・ポジション順を自前で実装する。
  - Cons: ポーカールールの再発明。pokerkit が既に解いた問題を二重実装し、バグ表面積が大きい。依存が
    宣言済みなのに使わないのは不合理。
- **独自ベッティングエンジンを新規作成（pokerkit 不使用, 不採用）**
  - Cons: 同上に加え、PHH 互換のための pokerkit セマンティクス（cbr/cc 等）と二重管理になる。
- **現状維持（export 時のみ pokerkit を想定し実際は未使用, 不採用）**
  - Why rejected: アクター誤帰属・pot 誤計算という目的の核心欠陥が残る。

## Consequences

- Positive
  - actor がポジション順から導出され、合法手集合という ASR 訂正・融合（ADR-0010）の土台が得られる。
  - side-pot / min-raise / ラウンド完了が正しくなり、`pot_total` の素朴総和バグ（`engine.py:413`）が解消。
  - ストリート遷移を「ベッティング完了」から導出でき、RFID ボード枚数（`engine.py` の board-count）が
    **唯一のトリガ**ではなく cross-check に格下げできる。
- Negative / trade-offs
  - `core` に pokerkit への実依存が入る（テスト時は薄い wrapper を介す）。online 駆動 API の実現性が
    未検証（ISSUE-0008 が gate）。
  - 移行期間は `pokerkit` / `legacy` の 2 backend が併存する。
- Neutral / new constraints
  - `PokerEngine` Protocol を境界契約として維持する（`hand-reconstruction.md`）。
  - seat↔index 写像は `new_hand()` で固定し hand 内で不変。

## Validation / Follow-up

- [ ] **ISSUE-0008**: pokerkit `>=0.5.0` の online-feeding API（actor / legal-actions / min-raise /
      amount-to-call / side-pot の incremental 露出、fold/check-call/raise-to・カード dealing）を spike で検証。
      不可なら shadow tracker で補完する方針を確定。**本 ADR Accepted の前提**。
- [ ] R2（実装フェーズ）: `PokerEngine` Protocol ＋ `PokerkitGameState` を `engine.backend` フラグ下で追加。
      legacy backend と R1 capture の replay 差分で同値検証。
- [ ] 既存 `tests/test_game_state.py` が `legacy` backend で緑のまま回ること（regression 非発生）。
- [ ] seat↔pokerkit index 写像のユニットテストを追加。

## Related Files

- `core/game_state.py`（安定 façade。pokerkit 実装に差し替える対象。外部 I/F 据え置き）
- `integration/engine.py`（`legal_context()` を読み合法手へ射影する境界。**本 ADR では未変更**）
- `output/phh_exporter.py`（seat→index 写像の既存例、call/check・side-pot の下流）
- `requirements.txt`（`pokerkit>=0.5.0` は宣言済・未 import）
- `docs/contracts/hand-reconstruction.md`（`PokerEngine` interface とエンジン設計の詳細）

## Related Tests

- `tests/test_game_state.py`（legacy backend の regression baseline）
- 将来追加: `tests/test_reconstruction.py`（pokerkit backend の golden replay, ADR-0011）

## Related Commits

- 本 ADR と同じコミット（reconstruction engine design planning, code 未変更）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0010（推定・融合）/ ADR-0011（contract・replay）/ ADR-0008（additive・rollback 前例）/
  ISSUE-0008（pokerkit API 実現性, gate）
