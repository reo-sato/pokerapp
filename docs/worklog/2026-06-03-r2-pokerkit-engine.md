# Worklog: R2 — pokerkit game-state backend (preview / default-off)

## Date

2026-06-03

## Scope / Task

ADR-0009 の段階導入 R2。`pokerkit.State` を live ルール権威にした game-state backend を、legacy
`GameStateManager` と差し替え可能な形（`config.engine.backend`, 既定 legacy）で追加する。前段の
ISSUE-0008（feasibility spike）を実施して gate を解除する。

## Goal

- actor 順（ポジション順 BTN/SB/BB）/ 合法手集合 / amount_to_call / min-raise / **side-pot** を pokerkit に
  委ねる backend を、既存の安定 I/F の背後に additive 追加する。
- **既定 legacy で挙動完全不変**。pokerkit は遅延 import で default-off（未導入でも legacy は動く）。
- pokerkit のユニットテストで actor 順・合法手・side-pot・announced-winner を pin。
- "done" = code + tests + docs（ADR/decision-log/CHANGELOG/CLAUDE/worklog）が揃い全テスト緑。

## Changed Files

- `core/poker_engine.py`（新規）— `PokerEngine` Protocol（legacy/pokerkit 共通 I/F）/ `PokerkitGameState`
  （pokerkit 実装）/ `create_game_state` factory。
- `main.py` — `_make_game_state(cfg, players, sb, bb)` で backend 選択。CLI/GUI の `GameStateManager(...)`
  構築を factory 呼び出しに置換。未使用になった `GameStateManager` import を整理。
- `config_default.json` — `engine.backend: "legacy"` を追加。
- `tests/test_poker_engine.py`（新規, 10 ケース）。
- `docs/issues/0008-...md` — spike 結果で Fixed（pokerkit 0.7.4 で API 実機確認）。
- docs: `ADR-0009` を Accepted（R2 実装済）に、`decision-log` / `hand-reconstruction.md` §2 /
  `CHANGELOG.md` / `CLAUDE.md`（実装状況表＋R0–R5 行）を更新。

## Expected Behavior

- `engine.backend=legacy`（既定）→ 従来 `GameStateManager`、挙動不変。
- `engine.backend=pokerkit` → `PokerkitGameState`。actor がポジション順、合法手のみ受理（不正は ValueError）、
  betting 完了で street 自動進行、`end_hand(winner)` がアナウンス勝者へ pot を push、`pots()` が side-pot を返す。
- pokerkit 未導入でも legacy 経路は動く（pokerkit は backend=pokerkit 選択時のみ import）。

## Implemented Behavior

- 上記のとおり。`PokerkitGameState` は showdown/push automation を外して `end_hand` で手動 push
  （pokerkit の auto-showdown はダミーカードで誤った勝者を出すため）。`pot_total = sum(starting) - sum(stacks)`、
  side-pot は `state.pots` を end_hand 時にスナップショット。seat↔pokerkit index は `new_hand` で固定。
- ISSUE-0008 spike（`pokerkit==0.7.4`）で `actor_index` / `can_*` / `checking_or_calling_amount` /
  `min|max_completion_betting_or_raising_to_amount` / `state.pots` / `statuses` / 不正額 `ValueError` /
  `HOLE_DEALING` のカード不要駆動を実機確認。

## Test Results

- `python -m pytest tests/ -q --ignore=tests/test_vision.py` → **197 passed**（185 + R1 2 + R2 10）。
- `tests/test_poker_engine.py`: factory 既定 legacy / actor 順・legal_context / street 自動進行 /
  不正 raise・非手番の ValueError / side-pot（unequal all-in, 900+1400=2300）/ announced-winner award /
  rebuy 反映 — 全緑。
- `python -m py_compile main.py core/poker_engine.py` → OK。legacy 既定が pokerkit 無しで構築可も確認。

## Mismatches Found During Testing

- None（design どおり）。fold-to-one 局面で `state.pots` は途中集計を返すことがあるため、pot 総額は
  `sum(starting)-sum(stacks)` で頑健に算出する設計にした（all-in 局面では `state.pots` が side-pot を正しく返す）。

## Fixes Applied

- なし（新規実装）。announced-winner override / 手動 import 遅延 / pot 総額の頑健算出は最初から設計に織り込み。

## Remaining Gaps / Out-of-Scope

- [ ] **R3**: live 接続。`apply_corrections`（raw ASR → 合法手・amount 射影）/ actor 推定（prior×sensor＋
      silent-fold）/ 派生 confidence。これが入るまで pokerkit backend は live 既定にしない（preview のまま）。
- [ ] pokerkit backend の既知差（ブラインド自動 post による stack_start のタイミング差、mid-hand stack 編集は
      次ハンド反映）は preview として許容。R5 で session 統合時に再点検。
- [ ] R1 capture（`events.jsonl`）を pokerkit backend で replay 差分検証する harness は R4（replayer）で。

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`（R2 = 本 worklog で実装）

## Related Issues

- `docs/issues/0008-pokerkit-online-feeding-feasibility.md`（Fixed, spike）
- `docs/issues/0009-actor-conflict-silent-fold-policy.md`（R3 で確定）

## Related Commits

- 本 worklog と同じコミット（R2 pokerkit engine 実装）。
