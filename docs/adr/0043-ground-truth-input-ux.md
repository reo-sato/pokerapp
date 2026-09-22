# ADR-0043: Ground truth 入力 UX — staff iPad app の一覧 triage（needs_review ガード付き）

## Status

Proposed（実装は本 ADR と同 PR 系列で進行中）

## Date

2026-06-26

## Context

Phase A 捕捉精度の計測（`docs/dogfood/measurement-plan.md`）には人手の ground truth が要る。
ハーネス（`tools/measure_capture_accuracy.py`）は既にあるが、**ground truth を作る UX が
無い**ため dogfood を回しても入力データが集まらない。

ground truth UX には正確さ × 運用負荷の強いトレードオフがある:

- **正確さ重視（M1: ゼロから独立アノテーション）**: 高品質だが annotator あたり 5〜10 分/ハンド。
  dogfood は週 50〜200 hand 規模を想定（5 卓 × N セッション）なので 1 人で回らない
- **運用重視（M2: 捕捉値を見ながら訂正だけ）**: 軽量だが annotator が捕捉値にアンカリングされて
  systematic な誤認識を見落とすバイアスがある
- **タイミング（T2: ハンド直後）**: 録画機材依存ゼロで動くが、annotator はハンドあたり 15〜30 秒
  しか猶予がない（次のハンドに追い越される）

加えて、ground truth 入力を独立プロセスにすると:

- staff の workflow が分散して破綻する
- annotator が現場スタッフ以外だと dogfood セットアップが重い

## Decision

ground truth 入力 UX を以下に確定する（ユーザー合意 2026-06-26）。

### 1. 役割 / タイミング / モード / 配置

| 軸 | 確定 |
|---|---|
| 誰が | **観戦・録画担当スタッフ**（卓を回さない観戦専任） |
| いつ | **ハンド直後**（T2、録画機材依存なし） |
| 入力モード | **M2 — 捕捉ログを横に並べて訂正のみ** |
| どの app | **staff iPad app（既存 `staff/`）に「計測」タブを追加** |

### 2. UX = 一覧 triage（per-hand 詳細レビューではなく）

T2 制約（15〜30 秒/ハンド）を満たすため **per-hand 詳細レビューはデフォルトではない**:

- メイン画面 = **当該セッションのハンド一覧**。各行に `hand_id` / `winner_seat` /
  `winner の chip won（result）` / `needs_review` バッジを表示
- 各行に 2 つのアクション:
  - **「✓ 流す（レビュー不要）」**: GT = 捕捉ハンド verbatim。annotator は winner + chip won を
    目視照合し「妥当」と判断したことを記録する
  - **「✏ 修正」**: 詳細画面（side-by-side 編集）に drill-in
- フッタに **「表示中の全件を流す」一括ボタン**（perfect 連続時の高速 path）
- フィルタチップ「⚠ needs_review のみ表示」あり

### 3. C-2 ガード（強制 drill-in）

**`HandSummary.review_required=True` または任意 action に `needs_review=True` を含むハンドは
「✓ 流す」を無効化**。「✏ 修正」のみ可能。一括「表示中の全件を流す」も needs_review 入りの
ハンドはスキップする。

理由: capture 自身が「自信がない」と言っているのに annotator が verbatim で通すのは
測定の趣旨に反する（silent failure が確定する）。

### 4. データ flow / 永続化

- **書き手**: staff iPad app → POST → viewer API（write 所有プロセス、`--ledger` /
  `viewer_api.enabled` のみ）→ `logs/{session_id}.ground_truth.json` に書き込む。
  `--viewer-api` 単独は read-only（503）
- **読み手**: `tools/measure_capture_accuracy.py` がファイルを直読み（既存挙動）。
  viewer API の read も補助的に提供
- **永続化モード**: **LWW（上書き）**。同一 `(session_id, hand_id)` の再送信は最新で上書き。
  append-only ではない（dogfood 初期は単一 annotator 前提で KISS、計測ツール側の overlay 適用を不要に保つ）
- **粒度**: `logs/{session_id}.ground_truth.json` 1 ファイル。schema は
  `docs/dogfood/measurement-plan.md` §2.2 と同じ（`{session_id, annotator, annotated_at, source, hands: [...]}`）

### 5. 「流す」時の captured 値の出処

「✓ 流す」/ 「表示中の全件を流す」で GT として記録する captured 値は、**`api/read_models.py:get_hand()` の戻り値**（B4 訂正適用後、ADR-0036）から作る。生 log から作らない。これで「訂正後のユーザー可視値」を測ることになる（measurement-plan §1 と整合）。

### 6. API

| Endpoint | 用途 | 認可 |
|---|---|---|
| `PUT /api/staff/sessions/{sid}/ground-truth/{hid}` | 単一 hand の GT を upsert | staff token |
| `GET /api/staff/sessions/{sid}/ground-truth` | session の GT 全体取得（一覧表示用） | staff token |

write は write 所有プロセスのみ（503 `orders_unavailable` を ground-truth 用に流用）。
新規 error code は導入しない（既存 `not_found` / `invalid_amount` / `unauthorized` を再利用）。

### 7. リアルタイム性

- staff app の「計測」タブを開いている間 **5 秒間隔で polling**（WebSocket 等は dogfood 規模で over-engineering）
- 「未処理 N 件 / 進行中ハンド: #M」をキューバッジとして上部に常時表示

### 8. sync / backup の扱い

- `logs/{session_id}.ground_truth.json` は `core/sync.py:build_snapshot` の whitelist に
  **加える**（hand log と同じ `log_dir` 配下なので自然に乗る。実装で確認）
- `core/backup.py:DEFAULT_BACKUP_SOURCES` には **加えない**（再生成可能ではないが、
  prototype 段階では運用負荷とのトレードオフ。実運用で価値が確認されたら ADR amend）

## Alternatives Considered

- **A. M1（ゼロから独立アノテーション）**: 高品質だが annotator あたり 5〜10 分/ハンド。
  dogfood 規模で運用破綻。アンカリング・バイアスは初期にはむしろ **「どこで誤認識しているか」**
  のシグナルになり、M2 で十分。→ 不採用
- **B. T3（セッション後 + 録画）**: 高品質だが録画機材依存。自店に機材があるか未確認の段階で
  前提を増やしたくない。→ 不採用
- **C. per-hand 詳細レビューを必須化**: 私の初期案。T2 制約で運用破綻するため、一覧 triage に転換
- **D. annotator passthrough 率の計測ログ**（私が C-1 として提案）: 「流す」率が高すぎる annotator
  を検出する surveillance ガード。ユーザー判断で除外。dogfood 単一 annotator 前提で過剰
- **E. append-only ground truth**: 履歴は残るが計測ツール側で overlay 適用が必要。LWW で十分
- **F. ground truth を mobile player app に置く**: player 自己申告は本質的にバイアスがある
  （勝った負けたの記憶が偏る）。staff annotator のほうが信頼性高い
- **G. ground truth を独立画面（`gui/` desktop）に置く**: staff workflow が分散する。
  iPad app への統合がワークフロー上自然

## Consequences

- Positive:
  - T2 制約下で運用が回る（perfect 捕捉が連続する時は **1 タップ/ハンド** で消化可能）
  - C-2 ガードで「capture が自分で不安だと言ってる action」を素通りさせない設計に
  - 既存 staff app への additive 拡張（4 タブ → 5 タブ）。新規アプリ / プロセスを増やさない
  - 計測ツール側のロジックを変えず、ground truth ファイルが生まれるだけ
  - `api/read_models.py:get_hand()` 経由で訂正適用済み値を GT 化するため、ADR-0036 との一貫性が保てる
- Negative / trade-offs:
  - **「流す」のバイアス**: GT = 捕捉値なので、当該ハンドの `action_accuracy` は trivially 100% に
    なる。**実測値が overestimate されるリスク** がある。dogfood 早期で実際に偏りが大きく出る
    ようなら、ランダム M% を強制 drill-in する mitigation を後続 ADR で追加
  - LWW のため annotator A → B の上書きで A の判断が消える（dogfood 単一 annotator 前提では問題ない）
  - polling 5s なので最大 5 秒の遅延。dogfood 規模で許容
- Neutral / new constraints:
  - `logs/{session_id}.ground_truth.json` を `.gitignore` に追加（実装で対応）
  - staff app に 5 番目のタブ "計測" を追加。既存 4 タブの導線は変えない
  - 新規 error code 無し（既存再利用）

## Validation / Follow-up

- [x] core: `GroundTruthHand` / `GroundTruthRepository`（LWW、`get_for_session(sid)` で
      measurement-plan §2.2 schema を返す）
- [x] API: `PUT/GET /api/staff/sessions/{sid}/ground-truth/{hid?}`（staff token、write 所有のみ）
- [x] `api/client.py`: `submit_ground_truth(sid, hid, gt)` / `get_ground_truth(sid)`
- [x] staff app: `MeasurementScreen.tsx`（一覧 + バッジ + 「✓ 流す」+ 一括 + C-2 ガード） +
      `MeasurementDetailScreen.tsx`（drill-in 編集）+ `StaffRepository` 拡張（mock/HTTP）+ tab wiring
- [x] tests: core / API / staff mock（C-2 ガードが needs_review ハンドを「流す」させないことを fix）
- [x] docs: `docs/dogfood/measurement-plan.md` §2.2 を実装に合わせて更新 +
      `CLAUDE.md` / `CHANGELOG.md` / `decision-log.md`
- [ ] dogfood 実運用での「流す」率を 1〜2 週後に観測。偏りが大きければ Mitigation ADR
- [ ] sync の whitelist に `*.ground_truth.json` を加えるかは別 PR で議論（本 PR は file-level union が
      自然に効くか試す）

## Related Files

- `core/ground_truth.py` / `core/ground_truth_repository.py`（新規）
- `api/server.py`（PUT/GET endpoint 追加）/ `api/client.py`（client メソッド追加）
- `staff/src/screens/MeasurementScreen.tsx` / `MeasurementDetailScreen.tsx`（新規）
- `staff/src/api/repository.ts` / `mockRepository.ts` / `httpRepository.ts`（追加）
- `tools/measure_capture_accuracy.py`（**変更しない** — ファイル形式は既に matched）
- `docs/dogfood/measurement-plan.md`（§2.2 を実装に合わせて update）
- `.gitignore`（`*.ground_truth.json` を追加）

## Related Tests

- `tests/test_ground_truth_repository.py`（新規 — LWW upsert / get_for_session / 永続化）
- `tests/test_viewer_api_ground_truth.py`（新規 — PUT/GET / staff token / write 所有）
- `staff/src/api/mockRepository.test.ts`（追記 — C-2 ガードが needs_review を流させない）

## Related Commits

- 本 ADR の実装 commit（同 PR 系列。M1 着手前 = solver 統合前）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: `docs/dogfood/measurement-plan.md`（rev.1, 2026-06-26）/ `docs/proposals/2026-06-26-hand-review-integration.md`
  （rev.1）/ ADR-0036（hand correction overlay = 「流す」時の入力経路）/ ADR-0037（staff iPad app）/
  ADR-0021（staff write token）/ ADR-0017（viewer API）
