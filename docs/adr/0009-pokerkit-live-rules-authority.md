# ADR-0009: ルール認識型ハンド再構築 — pokerkit live 権威 ＋ 状態推定・融合

## Status

<!-- One of: Proposed / Accepted / Superseded / Rejected / Deprecated -->
Accepted（ISSUE-0008 spike 済。R2 pokerkit engine 実装済・default-off。R3 = actor 推定 / apply_corrections / 融合は planned）

## Date

2026-06-03

## Context

hand logger の **目的は「ノイジーな 2 ソース（マイク ASR ＋ RFID）から正確にハンドを再構築する」**こと
だが、現状の core はポーカーのルールを一切知らず、再構築が素通しパイプラインになっている。読み取り調査で
確認した事実:

- **アクターがルールから導出されていない。** `core/game_state.py:158` `get_current_player()` は
  `_active_seats` 上の単純ラウンドロビンで、BTN/SB/BB のポジション順を無視する（`:161` 自身が
  「Phase 3 で PokerKit の actor_index に差し替える」と明記）。`integration/engine.py:313` は
  `seat = gs.get_current_player()` をそのまま action に帰属させ、RFID/camera は `±2.0s` 窓で**信頼度算出
  のためだけ**に照合される（`_pop_matching_rfid_event`, `engine.py:276`）。`AudioEvent` は seat を持たない
  （`core/events.py`）。turn tracker がずれると全 action が誤帰属する。
- **合法手・ベッティング状態が無い。** ストリート毎コミット・min-raise・合法手集合・ラウンド完了判定が
  未実装。pot は `engine.py:413` の素朴な総和、`game_state.py:90` は「メインポットのみ・サイドポット
  扱わない」と明記。
- **pokerkit は宣言済みだが未使用。** `requirements.txt:4` に `pokerkit>=0.5.0` があるが、
  **コードのどこからも import されていない**（`output/phh_exporter.py` は手書き TOML）。
  `game_state.py:31/91/162` に「PokerKit ラッパーに差し替える／外部 I/F は変えない」TODO が既存。
- **ASR が文脈非依存。** `parse_action`（`recognizer.py:141`）はキーワード一致のみ、`parse_amount`（`:90`）は
  action 文脈を無視（"チェック 5000"→amount=5000）。CLAUDE.md(:54) が参照する `apply_corrections()` は
  **実在しない**。Whisper の per-segment 信頼度は破棄（`:218`）。call/check は PHH で両方 "cc"
  （`phh_exporter.py:174`）。
- **融合が静的。** `calc_confidence()`（`engine.py:59`）は `(rfid, audio, camera)` の有無に対する 8 行固定
  テーブルで、「3 ソースが**一致**」と「3 ソースが**矛盾**」を区別できず、合法性も Whisper 信頼度も無視する。

forces（制約）:

- **既存出力を壊さない。** `core/hand_log.py` の `HandSummary` / `ActionRecord`、`logs/*.json`、PHH は
  past data を read-only とし形を変えない（ADR-0008 の hand-logger immutability 原則を継承）。
- **ライブ処理を落とさない**（CLAUDE.md エラーハンドリング方針、`engine.py:315` の try/except + `needs_review`）。
- **rollback 可能**（新方式を無効化したら従来動作に戻れる）。

> **本 ADR のスコープ**: 「再構築の**権威**と、その上で動く**推定・訂正・融合アルゴリズム**」を 1 つの
> 結合した決定として確定する（engine とアルゴリズムは「合法手が読めて初めて推定できる」関係で不可分）。
> 直交する **contract 化・record/replay** は **ADR-0010** が扱う。

関連: ADR-0010（contract・replay）/ ADR-0008（additive・rollback・immutability 前例）/
ISSUE-0008（pokerkit online API 実現性, gate）/ ISSUE-0009（actor 競合 / silent-fold ポリシー）。
設計詳細・修復表は `docs/contracts/hand-reconstruction.md`。

## Decision

ハンド再構築を **ルール制約付き状態推定**として実装し、ゲーム状態の権威を `pokerkit.State` に置く。

### Part 1 — pokerkit を live ルール権威に（engine）

1. **安定境界の背後で実装を差し替える（additive）。** 現 `GameStateManager` の公開 I/F
   （`new_hand` / `advance_street` / `end_hand` / `apply_action` / `get_current_player` / `advance_turn` /
   `hand_id` / `street` / `pot` / `get_stack(s)` / `get_player_name` / `get_active_seats` / `update_stack` /
   `rebuy`、`engine.py:409` が読む `_sb`/`_bb`）を **`PokerEngine` Protocol** として明文化し、`pokerkit.State`
   実装 `PokerkitGameState` を提供する。呼び出し側（`integration/engine.py` / `main.py`）は変えない
   （`game_state.py:31` の「外部 I/F は変えない」前提に乗る）。`legal_context()` / `is_legal_actor()` /
   `fold_through()` / `pots()` / `committed()` を additive に足す（`hand-reconstruction.md` §2）。
2. **「境界での推定」。** raw ASR を `pokerkit.State` に直接流さない。各 action で `engine.legal_context()`
   （手番 prior・合法手集合・amount_to_call・min_raise）を読み、推定（Part 2）で合法手へ射影してから
   `State` に適用する。`State` が拒否したら crash せず `needs_review` ＋未検証 action としてフォールバック
   する。これで `State` は常に内部整合（side-pot / min-raise）を保ち、ノイズを境界に隔離する。
3. **backend 選択フラグ。** `config.engine.backend = "pokerkit" | "legacy"`（移行期間 default `legacy`）で
   実装を選択（ADR-0008 の `config.session_layer.enabled` rollback 思想に倣う）。旧ラウンドロビン実装は
   `LegacyGameState` として残す。
4. **出力は additive のみ。** `HandSummary` / `ActionRecord` と `logs/*.json` の現フィールドは不変。新情報
   （main/side pot、per-street commitment、legal context、hole cards）は **optional 追加フィールド**
   （schema は ADR-0010）。PHH は無改変だが、call/check の区別・side-pot は結果として**より正確**になる
   （その箇所だけ PHH/JSON が変わりうる点は明示する）。
5. **seat↔pokerkit index 写像。** pokerkit の player index（0..n-1）と疎な seat_no（1..9）の安定全単射を
   `new_hand()` で BTN 基準に固定する（`output/phh_exporter.py:119` の `seat_to_idx` を正式化・共有）。

### Part 2 — ルール制約付き状態推定（algorithm）

6. **アクター推定（prior × sensor、silent-fold 合成つき）。** prior = engine の `actor_index`（実ポジション
   順）。観測 = 同窓内の RFID seat read（強）/ audio の明示 seat（`engine.py:433` `_extract_seat_from_text`
   を全 action へ一般化, 中）/ camera（弱）。競合解決: **全ソース一致 → 高 confidence**。**不一致 → 物理/
   明示証拠を prior より優先**し、選ばれた actor が**合法アクターなら prior〜actor 間の席を "silent fold"
   （ディーラー未宣言 fold）として自動合成**して engine を同期、非合法なら prior 維持＋`needs_review=True`。
   `AudioEvent` に optional `seat`（default None, additive）を追加。
7. **`apply_corrections()`（合法手制約の純関数）。** signature `(parsed, legal_ctx, whisper_conf) →
   corrected` を `audio/recognizer.py` に新設（ゲーム状態を持たない純関数とし engine が `legal_ctx` を渡す）。
   **call/check を amount_to_call から決定的に一意化**（`>0→call`, `==0→check`）、非合法 "check" は call/fold へ
   尤度再解釈、既存ベット中の "bet" は raise へ、bet/raise の amount は合法レンジ／ラウンド数へスナップ、
   call は heard 数値を無視し engine の amount_to_call を採用、stack 超過は all-in。Whisper 信頼度を override
   重みに（高信頼 ASR が**非合法**なときが最強の `needs_review` トリガ）。
8. **派生 confidence（固定テーブル置換）。** `confidence = clamp(L × (w_A·A + w_Q·Q), 0, 1)`。L=合法性ゲート
   （pokerkit が受理したか）、A=**一致した**利用可能ソースの割合、Q=ソース品質（RFID>audio>camera）× Whisper
   信頼度。「全ソース一致・合法」のとき現 `calc_confidence` 値に近づくよう較正（円滑移行）。`needs_review=True`
   条件: pokerkit 拒否 / 高信頼 ASR と規則の矛盾 / prior が sensor を上書き / amount を許容超過スナップ /
   `confidence < review_threshold`。これにより `HandSummary.review_required` を初めて意味あるものにする。

本決定は `game_state.py` の既存「Phase 3 で PokerKit」TODO を **方針として確定**するもので（実装は ADR-0010 の
R2/R3）、過去の決定を反転しない。既存 `calc_confidence` / 照合窓 / `_extract_seat_from_text` / board-position
累積は捨てず、推定と派生 confidence の**下位入力**として再利用する。

## Alternatives Considered

- **pokerkit を live 権威にし、その合法手制約で推定する（採用）**
  - Pros: 既に支払い済みの依存を初めて活用。actor_index / 合法手 / side-pot / min-raise が「ただで」入り、
    ASR ノイズを訂正でき、silent fold という最頻の現実のズレを処理できる。confidence が「一致度＋合法性」
    から導出され監査可能。既存 TODO の素直な具体化。
  - Cons: pokerkit の incremental/online 駆動 API の実現性検証が必要（ISSUE-0008）。競合解決・silent-fold
    合成は運用判断を含む（ISSUE-0009）。seat↔index 写像・重み較正が要る。
- **engine と推定を別 ADR に分割（不採用 = 本 ADR で統合）**
  - 当初 3 分割案（engine / 推定 / contract）だったが、「合法手が読めて初めて推定できる」結合が強く、
    engine 決定と推定アルゴリズムは事実上一体。可読性・追跡性のため 1 ADR に統合した（contract/replay の
    直交決定は ADR-0010 に分離）。
- **現 `GameStateManager` を手作りで拡張 / 独自ベッティングエンジン（pokerkit 不使用, 不採用）**
  - Cons: ポーカールールの再発明。pokerkit が既に解いた問題を二重実装し、バグ表面積が大きい。宣言済み依存を
    使わないのは不合理。
- **prior（手番）を常に正とする（現状, 不採用）** / **sensor を常に正とする（不採用）**
  - Why rejected: 前者は未宣言 fold / out-of-turn を誤帰属、後者は RFID 取りこぼし／二重読みで actor が飛ぶ。
- **確率モデル（HMM / particle filter）で系列推定（不採用・将来候補）**
  - Pros: 原理的に最も頑健。Cons: 現段階 overkill。golden fixtures（ADR-0010）が揃えば再評価する。

## Consequences

- Positive
  - actor がポジション順＋物理証拠で確定し、silent fold を合成して以降の手番がずれない。
  - 合法手集合という制約で ASR を訂正でき、call/check が状態から一意に決まる（"コール 500" 誤り解消）。
  - side-pot / min-raise / ラウンド完了が正しくなり、`pot_total` の素朴総和バグ（`engine.py:413`）が解消。
  - confidence / `needs_review` が合法性と一致度を反映し、レビュー対象が監査可能になる。
- Negative / trade-offs
  - `core` に pokerkit への実依存が入る（テスト時は薄い wrapper / mock）。online API の実現性が未検証
    （ISSUE-0008 が gate）。silent-fold 自動合成は誤 fold を作る危険があり適用条件の確定が要る（ISSUE-0009）。
  - 移行期間は `pokerkit` / `legacy` の 2 backend が併存する。重み較正は ground truth（ADR-0010 の golden
    fixtures）に依存。
- Neutral / new constraints
  - `PokerEngine` Protocol を境界契約として維持（`hand-reconstruction.md`）。`apply_corrections` は純関数として
    recognizer に置き、engine が legal_ctx を注入して呼ぶ責務境界を守る。seat↔index 写像は `new_hand()` で固定。

## Validation / Follow-up

- [x] **ISSUE-0008**（2026-06-03 spike 済, Fixed）: pokerkit 0.7.4 で actor / legal-actions / min-raise /
      amount-to-call / side-pot の incremental 露出、不正額の `ValueError`、`HOLE_DEALING` でのカード不要駆動を
      実機確認。shadow tracker 不要。
- [ ] **ISSUE-0009**: actor 競合解決と silent-fold 合成の適用条件・件数上限・`needs_review` 閾値・派生
      confidence の重み較正を確定。
- [x] R2（実装済, 2026-06-03）: `core/poker_engine.py`（`PokerEngine` Protocol ＋ `PokerkitGameState` ＋
      `create_game_state` factory）＋ `config.engine.backend`（既定 legacy）。pokerkit は遅延 import で
      default-off。announced winner を手動 push、side-pot 取得、seat↔index 固定。
      `tests/test_poker_engine.py`（actor 順 / legal_context / street 自動進行 / 不正・非手番拒否 / side-pot /
      winner award / rebuy）緑。全 197 緑。**R2 単体では live 既定動作は不変**（live 接続は R3 で projection）。
- [ ] R3（実装）: actor 推定 ＋ `apply_corrections` ＋ 派生 confidence。golden-fixture（ADR-0010）で固定:
      「check facing a bet → call/fold」「コール 500 → engine 額」「silent fold → 次 actor 正」
      「unequal all-in → side-pot」「out-of-turn confirmed by RFID」。`apply_corrections` 純関数表テスト、
      派生 confidence 較正テスト。

## Related Files

- `core/game_state.py`（安定 façade。pokerkit 実装に差し替える対象。外部 I/F 据え置き）
- `integration/engine.py`（`legal_context()` を読み合法手へ射影する境界 / actor 推定 / 派生 confidence /
  `_extract_seat_from_text` 一般化。**本 ADR では未変更**）
- `audio/recognizer.py`（`apply_corrections()` 新設と Whisper 信頼度露出。**未変更**）
- `core/events.py`（`AudioEvent` の additive `seat?` / `confidence?`）
- `output/phh_exporter.py`（seat→index 写像の既存例、call/check・side-pot の下流）
- `requirements.txt`（`pokerkit>=0.5.0` は宣言済・未 import）
- `docs/contracts/hand-reconstruction.md`（`PokerEngine` interface・推定・修復・融合の詳細）

## Related Tests

- `tests/test_game_state.py`（legacy backend の regression baseline）
- 将来追加: `tests/test_reconstruction.py`（pokerkit backend の golden replay, ADR-0010）、`apply_corrections`
  単体表テスト、派生 confidence 較正テスト

## Related Commits

- 本 ADR と同じコミット（reconstruction engine design planning, code 未変更）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0010（contract・record/replay）/ ADR-0008（additive・rollback・immutability 前例）/
  ISSUE-0008（pokerkit API 実現性, gate）/ ISSUE-0009（actor / silent-fold ポリシー）
