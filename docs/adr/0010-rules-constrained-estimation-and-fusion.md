# ADR-0010: ルール制約付き状態推定と多ソース融合モデル

## Status

<!-- One of: Proposed / Accepted / Superseded / Rejected / Deprecated -->
Proposed

## Date

2026-06-03

## Context

ADR-0009 で再構築の権威を `pokerkit.State` に置くと、各 action 時点で**合法手集合・手番 prior・
amount_to_call・min_raise** が読めるようになる。本 ADR は、その合法手という制約を使って
**ノイジーなセンサー入力をどう"正しい action"へ確定するか**（推定・訂正・融合のアルゴリズム）を決める。
ADR-0009 が engine/dependency の決定なのに対し、本 ADR は **アルゴリズムの決定**で、両者は分離可能
（ルール権威を入れても推定方針は複数あり得る）。

現状の欠陥（読み取り調査）:

- **actor 推定が無い。** `engine.py:313` は `gs.get_current_player()`（ラウンドロビン）の戻りをそのまま
  使い、RFID/camera は `±2.0s` 窓で**信頼度算出のためだけ**に照合される（`_pop_matching_rfid_event`,
  `engine.py:276`）。センサーで actor を確認・訂正しない。`AudioEvent` は seat を持たない（`core/events.py`）。
- **ASR が文脈非依存。** `parse_action`（`recognizer.py:141`）はキーワード一致のみ、`parse_amount`
  （`:90`）は action 文脈を無視（"チェック 5000"→amount=5000）。CLAUDE.md(:54) が参照する
  `apply_corrections()` は**実在しない**。Whisper の per-segment 信頼度は破棄（`:218`）。
- **融合が静的。** `calc_confidence()`（`engine.py:59`）は `(rfid, audio, camera)` の有無に対する 8 行
  固定テーブルで、「3 ソースが**一致**」と「3 ソースが**矛盾**」を区別できず、合法性も Whisper 信頼度も
  無視する。call/check は両方 PHH "cc"（`phh_exporter.py:174`）。

関連: ADR-0009（engine, 前提）/ ADR-0011（replay・contract）/ ISSUE-0009（actor 競合 / silent-fold
ポリシー）。アルゴリズム詳細・修復表は `docs/contracts/hand-reconstruction.md`。

## Decision

再構築を **ルール制約付き状態推定**として実装する。3 つの決定:

1. **アクター推定（prior × sensor、silent-fold 合成つき）。**
   - prior = engine の `actor_index`（実ポジション順）。観測 = 同窓内の RFID seat read（強）/ audio の
     明示 seat（`engine.py:433` `_extract_seat_from_text` を全 action へ一般化, 中）/ camera（弱）。
   - 競合解決規則: **全ソース一致 → 高 confidence で確定**。**不一致 → 物理/明示証拠（RFID seat / 明示
     発話 seat）を prior より優先**する。選ばれた actor が engine 上で**合法アクターなら、prior から
     その actor までの間の席を "silent fold"（ディーラー未宣言の fold）として自動合成**して engine を
     同期する。actor が非合法なら prior を維持し `needs_review=True`。
   - `AudioEvent` に optional `seat`（default None, additive）を追加し、recognizer が明示 seat を見つけた
     ときに埋める。確定 actor は従来どおり `ActionRecord.seat` に載る（下流不変）。
2. **`apply_corrections()`（合法手制約の純関数）。** signature `(parsed, legal_ctx, whisper_conf) →
   corrected` を `audio/recognizer.py` に新設するが、ゲーム状態を持たない純関数とし、engine が
   `legal_ctx` を渡して呼ぶ（recognizer をゲーム状態から疎結合に保ち単体テスト可能にする）。規則:
   - **call/check を amount_to_call から決定的に一意化**: `amount_to_call > 0 → call`, `== 0 → check`
     （JA キーワードの曖昧さに依存しない）。
   - 非合法 "check"（ベットに直面）は call/fold へ尤度再解釈、既存ベット中の "bet" は raise へ再マップ。
   - bet/raise の amount は合法レンジ／ラウンド数へスナップ、**call は heard 数値を無視**し engine の
     amount_to_call を採用、stack 超過は all-in（現 `game_state.py:130` の clamp 意図を正しい all-in 化）。
   - Whisper 信頼度を override 重みに使う（低信頼ほど state からの上書きを許す。高信頼 ASR が**非合法**な
     ときが最も強い `needs_review` トリガ）。
3. **派生 confidence（固定テーブル置換）。** `confidence = clamp(合法性ゲート L × (ソース合意 A,
   ソース品質/ASR信頼 Q の重み和), 0, 1)`。L は「採用 action が合法か（pokerkit が受理したか）」、A は
   「**一致した**利用可能ソースの割合」、Q は「RFID>audio>camera の事前重み × Whisper 信頼度」。
   重みは「全ソース一致・合法」のとき現 `calc_confidence` 値に近づくよう較正する（円滑移行）。
   `needs_review=True` の条件を明文化: pokerkit 拒否 / 高信頼 ASR と規則の矛盾 / prior が sensor を上書き /
   amount を許容超過スナップ / `confidence < review_threshold`。これにより `HandSummary.review_required`
   を初めて意味あるものにする。

## Alternatives Considered

- **ルール制約付き状態推定（採用）**
  - Pros: 合法手という強い制約で ASR ノイズを訂正でき、silent fold という最頻の現実のズレを処理できる。
    confidence が「一致度＋合法性」から導出され監査可能。
  - Cons: 競合解決・silent-fold 合成は運用判断を含む（ISSUE-0009）。重み較正が要る。
- **prior（手番）を常に正とする（現状, 不採用）**
  - Why rejected: ディーラー未宣言の fold / out-of-turn で actor を誤帰属する核心バグが残る。
- **sensor を常に正とし prior を無視（不採用）**
  - Cons: RFID 取りこぼし／二重読みで actor が飛ぶ。prior（ルール）の安定性を捨てるのは過剰。
- **確率モデル（HMM / particle filter）でベッティング系列を潜在状態推定（不採用・将来候補）**
  - Pros: 原理的に最も頑健。Cons: 現段階 overkill、説明可能性・実装コストが見合わない。
    将来 golden fixtures（ADR-0011）が揃えば再評価する。

## Consequences

- Positive
  - actor がルール＋物理証拠で確定し、silent fold を合成して以降の手番がずれない。
  - call/check が状態から一意に決まり、call 額が engine の正確値になる（"コール 500" 誤り解消）。
  - confidence / `needs_review` が合法性と一致度を反映し、レビュー対象が監査可能になる。
- Negative / trade-offs
  - silent-fold 自動合成は誤って fold を作る危険があるため、適用条件と `needs_review` 閾値の確定が要る
    （ISSUE-0009）。
  - 重み較正のための ground truth（golden fixtures, ADR-0011）に依存する。
- Neutral / new constraints
  - `apply_corrections` は純関数として recognizer に置き、engine が legal_ctx を注入して呼ぶ責務境界を守る。
  - 既存 `calc_confidence` / 照合窓 / `_extract_seat_from_text` は捨てず、派生 confidence と actor 推定の
    **下位入力**として再利用する。

## Validation / Follow-up

- [ ] **ISSUE-0009**: actor 競合解決と silent-fold 合成の適用条件・重み・`needs_review` 閾値を確定。
- [ ] R3（実装フェーズ, ADR-0009 R2 後）: actor 推定 ＋ `apply_corrections` ＋ 派生 confidence を追加。
      各々 golden-fixture ケース（ADR-0011）で固定:「check facing a bet → call/fold」「コール 500 →
      engine 額」「silent fold → 次 actor 正」「unequal all-in → side-pot」「out-of-turn confirmed by RFID」。
- [ ] `apply_corrections` の純関数テーブル（`(raw_text, legal_ctx) → corrected`）単体テスト。
- [ ] 派生 confidence が「全一致・合法」で現 `calc_confidence` 値に近いことの較正テスト。

## Related Files

- `integration/engine.py`（actor 推定 / 派生 confidence / `_extract_seat_from_text` 一般化の対象。**未変更**）
- `audio/recognizer.py`（`apply_corrections()` 新設と Whisper 信頼度露出の対象。**未変更**）
- `core/events.py`（`AudioEvent` の additive `seat?` / `confidence?`）
- `core/game_state.py`（`legal_context()` を供給する engine, ADR-0009）
- `docs/contracts/hand-reconstruction.md`（推定・修復・融合の詳細表）

## Related Tests

- 将来追加: `tests/test_reconstruction.py`（golden replay）、`apply_corrections` 単体表テスト、
  派生 confidence 較正テスト

## Related Commits

- 本 ADR と同じコミット（design planning, code 未変更）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0009（engine, 前提）/ ADR-0011（replay・contract）/ ISSUE-0009（actor / silent-fold ポリシー）
