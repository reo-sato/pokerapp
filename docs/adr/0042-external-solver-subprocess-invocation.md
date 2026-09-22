# ADR-0042: TexasSolver subprocess 起動契約 — `subprocess.run` + timeout + 段階 kill

## Status

Proposed（実装は M1 着手前提・Phase A 95% 通過後。本 ADR はハンドレビュー × GTO solver
統合提案 rev.1 §4.6 で約束した起票分）

## Date

2026-06-26

## Context

ハンドレビュー × GTO solver 統合（`docs/proposals/2026-06-26-hand-review-integration.md`）は
TexasSolver バイナリを **外部プロセス** として起動する。本プロジェクトでは:

- **`subprocess` / `Popen` を import している箇所はゼロ**（`grep -r "subprocess" core/ audio/
  integration/ output/ rfid/ api/ gui/` → 0 件）。
- 既存の外部依存はすべて Python ライブラリ呼び出し（faster-whisper、pokerkit、PyAudio、pyscard、
  pokerkit、customtkinter）。
- 「子プロセスを起動して結果を待つ」という制御フロー自体が新規。

ソルバーの呼び出しには以下の挙動を **最低水準として定義** しないと、後で再実装になる:

- **timeout**: フロップは分単位、ターン以降は秒〜十数秒（提案 §R1）。「最悪 X 秒で諦める」を
  決めないと UI/UX がブロックする。
- **kill の段階**: timeout 後に SIGTERM だけ送って即座に SIGKILL するか、grace period を
  挟むか、で TexasSolver の挙動（一時ファイルのクリーンアップ等）が変わる。
- **stderr の扱い**: ソルバーが警告を吐くが、毎回フルログを残すと数 MB / hand。truncate ポリシー
  が要る。
- **異常終了の分類**: 「ファイル無い」「入力 parse 失敗」「メモリ不足」「不明」が混在するため、
  caller に **どの種類** かを伝えないと UI の「準備中」「失敗」「再試行可能」分岐が作れない。
- **process leak の防止**: GUI が閉じられた時に走っている solver を確実に終わらせないと
  孤児プロセスが残る。

本 ADR はこれらを **M1 内で実装する最低限の契約** として固定する。**M1 では「1 リクエスト = 1
プロセス、同時 1 個まで」**（並行ジョブは持たない）。M2 で事前計算バッチを入れる際に並行制御を
拡張する。

## Decision

1. **API = `subprocess.run`**（`Popen` は使わない）:
   - `subprocess.run([binary_path, "-i", input_path], check=True, timeout=timeout_sec,
     capture_output=True, text=True)` を `core/solver/texas_solver_adapter.py` の単一ホット
     パスとする。
   - `check=True` は `CalledProcessError` で非ゼロ終了を例外化する目的。`capture_output=True`
     で stdout/stderr を回収。`text=True` で str 化（バイナリ出力ファイルは別途読み取る）。
2. **timeout = `config.solver.timeout_sec`**（既定 180 秒、`config_default.json` に additive
   追加予定）:
   - 提案 §R1 の「1 スポット 60s 以内なら推奨、180s 超ならバックグラウンドジョブ強制」と整合。
   - timeout に達すると `subprocess.TimeoutExpired` が上がる。Decision §3 の段階 kill に進む。
   - UI（`gui/hand_review.py`）はメインスレッドではなく **ワーカースレッド**で adapter を呼び、
     UI スレッドは「準備中」を出し続ける（M2 のバックグラウンドジョブ統合とも整合）。
3. **timeout 後の段階 kill**:
   - `subprocess.run(timeout=...)` が timeout 例外を上げた時点で、Python は内部で SIGTERM
     を送る（プラットフォーム依存）。本 adapter は **明示的に追加 5 秒待ち**、まだ生きていれば
     `process.kill()` で SIGKILL を送る。`subprocess.run` の内部ハンドルでは制御しにくいため、
     `Popen` ではなく `subprocess.run` を使う今回の決定では **timeout=child_timeout** を渡し、
     上位で `signal.alarm` を使ってもう一度監視するパターンを取らず、シンプルに「timeout 後は
     run の中で完結する」前提に乗る。`subprocess.run` の現代実装は `TimeoutExpired` を
     上げる前に SIGKILL までエスカレートする。
   - **本 ADR の保守的指針**: `subprocess.run` のデフォルト挙動を信頼し、追加の段階制御は
     M2 まで持ち越す。M1 では「timeout → 例外 → process は dead」を信じる。
4. **stderr ログ化**:
   - `capture_output=True` で取得した stderr は `logging.getLogger("solver").info` に
     ダンプする（`print` 禁止 = CLAUDE.md §コーディング規約）。
   - **truncate ポリシー**: stderr 全文をログに残すと数 MB / hand になりうるため、
     「先頭 4KB + 末尾 4KB、間は `... (truncated N bytes) ...`」で要約する。
     `_truncate_for_log(stderr, max_head=4096, max_tail=4096)` を `texas_solver_adapter.py`
     のヘルパに置く。
5. **例外の分類**:
   - `core/solver/types.py` に以下を定義（新規）:
     ```python
     class SolverError(Exception):
         pass
     class SolverTimeout(SolverError):
         pass
     class SolverInvalidInput(SolverError):
         pass         # 既知の TexasSolver エラーメッセージにマッチした場合
     class SolverBinaryMissing(SolverError):
         pass         # FileNotFoundError → 上位に変換
     class SolverUnknown(SolverError):
         pass         # 上記いずれでもない非ゼロ終了
     ```
   - adapter は `subprocess.TimeoutExpired` → `SolverTimeout`、`FileNotFoundError` →
     `SolverBinaryMissing`、`CalledProcessError` は stderr の文字列マッチで
     `SolverInvalidInput` / `SolverUnknown` に分岐。
   - UI は `SolverTimeout` を「もう一度待つ / バックグラウンド化」、`SolverInvalidInput`
     を「入力組み立てに問題」（要 R2 突き合わせの回帰テスト追加トリガ）、`SolverBinaryMissing`
     を「`python tools/install_solver.py` を実行してください」、`SolverUnknown` を「不明な
     失敗（stderr ログを確認）」に分岐表示する。
6. **process leak 防止**:
   - GUI ウィンドウクローズ時、走行中のソルバージョブを `Future.cancel()` で打ち切る
     （`concurrent.futures.ThreadPoolExecutor(max_workers=1)` を adapter 内に置く）。
   - **executor は M1 では `max_workers=1`** で固定。同時に複数走らせない。並行は M2 で拡張。
   - `atexit` フックでプロセスツリーをクリーンアップ（Python 終了時の最終防衛）。
7. **入力 / 出力ファイルの扱い**:
   - 入力テキスト → `tempfile.NamedTemporaryFile(suffix=".txt", delete=False)` で作成、
     `try / finally` で削除（context manager は `delete_on_close` の OS 互換性が薄いため自前管理）。
   - 出力 JSON → TexasSolver が指定パスに書く想定。adapter は入力ファイルと同じディレクトリに
     `<basename>_result.json` を期待。
   - 一時ディレクトリは `~/.cache/pokerapp/solver/run-<uuid>/` 配下に作り、ジョブごとに分離。
     完了 / 失敗 / cancel 時に確実に消す（`shutil.rmtree(..., ignore_errors=True)`）。
8. **stdout の扱い**: TexasSolver は通常 stdout に進捗を吐く（推測。実装時に確認）。`text=True`
   で取得済みだが、**ログにはダンプしない**（数十 KB 〜 数 MB / hand で容量が膨らむ）。デバッグ
   用に `config.solver.log_stdout=true` でオプトインにする。

## Alternatives Considered

- **A. `Popen` で完全な並行制御を M1 から持つ**:
  - Pros: 進捗ストリーミング、明示的な SIGTERM → SIGKILL、複数並行が可能。
  - Cons: 実装複雑性が増し、M1 のスコープを膨らませる。提案 §R6（スコープクリープ）に反する。
  - 不採用: M1 は `subprocess.run` で十分。並行は M2 で `Popen` 化を検討。
- **B. timeout を持たない**:
  - Pros: 「正しい解が出るまで待つ」セマンティクス。
  - Cons: フロップで分単位かかると UI が無限待ち、ユーザーが「壊れた」と認識する。
  - 不採用: dogfood UX として致命的。
- **C. stderr を丸ごとログ化**:
  - Pros: デバッグ情報が完全に残る。
  - Cons: 数 MB / hand × 8 週 dogfood で logs/ が数十 GB に膨れる可能性。
  - 不採用: truncate ポリシーで先頭・末尾の有用情報を取り、容量を絞る。
- **D. 例外を単一の `SolverError` のみにする**:
  - Pros: 実装シンプル。
  - Cons: UI が「再試行可能」「入力直し」「環境問題」を区別できず、エラーメッセージが
    曖昧化する。
  - 不採用: 5 分類は UI 分岐に必要な最低限。
- **E. プロセスプール / 並行ジョブを M1 で実装**:
  - Pros: スループット向上、複数ハンドのバッチレビューが快適。
  - Cons: M1 は単発レビュー（提案 §2）でスコープ確定。並行は M2 で扱う。
  - 不採用: スコープクリープ防止（提案 §R6）。

## Consequences

- Positive:
  - subprocess 呼び出しの最低水準が明文化され、M1 PR レビューで再議論不要。
  - 例外分類が UI 分岐と 1:1 で対応し、ユーザーへのエラーメッセージが具体化できる。
  - process leak の防御（executor + atexit）で孤児プロセスを残さない。
  - stderr truncate でログ膨張を防ぐ。
- Negative / trade-offs:
  - **M1 は逐次実行のみ**。並行性能は M2 まで待つ。dogfood の「複数ハンドを連続レビュー」
    は M1 ではバックグラウンド化で吸収。
  - `subprocess.run` の timeout 後 kill 挙動はプラットフォーム依存。Linux x86_64（ADR-0041）
    では問題ないが、将来 Win 対応時に再評価が必要。
  - 5 種類の例外を caller 側が握る必要がある（adapter が握り潰さない）。実装規律として明示。
- Neutral / new constraints:
  - `core/solver/types.py` に 5 つの例外クラスを定義。
  - `config.solver.timeout_sec` / `config.solver.log_stdout` を `config_default.json` に
    additive 追加（既定値で挙動不変）。
  - 一時ディレクトリは `~/.cache/pokerapp/solver/run-<uuid>/`。`solver_cache.sqlite`
    （ADR-0040）とは別位置。

## Validation / Follow-up

- [ ] `core/solver/texas_solver_adapter.py` を本 ADR の規約に従って実装。
- [ ] `core/solver/types.py` に 5 つの例外クラスを定義。
- [ ] `config_default.json` に `solver.timeout_sec` (180) / `solver.log_stdout` (false) を
      additive 追加。
- [ ] `tests/test_solver_adapter_smoke.py`（M1 実装時）:
      - timeout → `SolverTimeout` を上げる（fake binary で `time.sleep(999)`）
      - 非ゼロ終了 + 既知 stderr → `SolverInvalidInput`
      - 非ゼロ終了 + 未知 stderr → `SolverUnknown`
      - バイナリ不在 → `SolverBinaryMissing`
      - 正常終了 → `SolverResult` パース成功
- [ ] GUI 側（`gui/hand_review.py`）が 5 つの例外を捕捉して UI 分岐することを smoke test。
- [ ] M2 で `Popen` ベースの並行ジョブに拡張する場合、本 ADR を amend / superseded by
      新 ADR にする。

## Related Files

- `core/solver/texas_solver_adapter.py`（新規・M1 実装予定 — 本 ADR の中心）
- `core/solver/types.py`（5 つの例外クラス、新規・M1 実装予定）
- `config_default.json`（`solver.timeout_sec` / `solver.log_stdout` を additive 追加予定）
- `gui/hand_review.py`（例外分岐の caller、新規・M1 実装予定）
- `~/.cache/pokerapp/solver/run-<uuid>/`（一時ファイル置き場、実行時に作成）

## Related Tests

- `tests/test_solver_adapter_smoke.py`（M1 実装時に新規）

## Related Commits

- 本 ADR は **設計のみ**、コード実装なし。M1 PR で commit hash を追記。

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: `docs/proposals/2026-06-26-hand-review-integration.md`（rev.1 §4.6）/ ADR-0040 /
  ADR-0041 / CLAUDE.md §コーディング規約（`print` 禁止 / `logging` 使用）
