# Worklog: ハンドレビュー × GTO solver 統合 — ADR 3 件起票（0040 / 0041 / 0042）

## Date

2026-06-26

## Scope / Task

ハンドレビュー × GTO solver 統合提案 rev.1（`docs/proposals/2026-06-26-hand-review-integration.md`
§4.6）で約束した 3 件の ADR を起票し、`docs/decision-log.md` の ADR Index に登録する。
**設計のみ、コード実装なし**。M1 実装着手は Phase A（捕捉精度 95%）通過後。

## Goal

- ADR-0040 / 0041 / 0042 を **Proposed** ステータスで起票
- 各 ADR は (a) 既存コードベース・既存 ADR との接続を明示し、(b) Decision を具体化し
  （実装者が迷わない水準）、(c) Alternatives Considered と Consequences で「なぜ他案では
  ないか」を将来読者に残す
- `docs/decision-log.md` の ADR Index に 3 行追加
- M1 PR レビュー時に **subprocess / vendoring / cache persistence の三本柱を再議論にしない**
  だけの仕様水準を確保

## Changed Files

- `docs/adr/0040-solver-cache-persistence.md` — 新規
  - SQLite 採用、`solver_cache.sqlite` node-local、`spot_key`（SHA256 hex 32）、`solver_version`
    で世代分離、drop-and-rebuild マイグレーション、sync・backup 非対象を明文化
- `docs/adr/0041-vendored-solver-binary-shipping.md` — 新規
  - git 同梱しない、`tools/install_solver.py` で install-time download + SHA256 verify、
    `~/.local/share/pokerapp/solver/<version>/` 配置、M1 は Linux x86_64 のみ、
    `config.solver.binary_path` 上書き可（オフラインフォールバック）、AGPL §Convey 境界を
    踏まない
- `docs/adr/0042-external-solver-subprocess-invocation.md` — 新規
  - `subprocess.run(check=True, timeout=180, capture_output=True, text=True)` を単一ホット
    パス、5 種類の例外分類（`SolverError` / `SolverTimeout` / `SolverInvalidInput` /
    `SolverBinaryMissing` / `SolverUnknown`）、stderr truncate（先頭 4KB + 末尾 4KB）、
    `ThreadPoolExecutor(max_workers=1)` + `atexit` で process leak 防止、M1 は逐次 1 個まで
- `docs/decision-log.md` — ADR Index に 3 行追加

## Expected Behavior

- 3 件の ADR がすべて Proposed ステータスで起票され、`docs/adr/` のファイル命名規約
  （`NNNN-<short-title>.md`）に準拠
- 各 ADR が template（`docs/templates/adr-template.md`）の節構造を踏襲
- `docs/decision-log.md` の ADR Index が ID 昇順を保ち、ADR-0039 の直後に 0040〜0042 が並ぶ
- 各 ADR に「関連 ADR」「関連 issue」「関連 commits（後追記）」のクロスリンクが入る

## Implemented Behavior

期待通り。差分なし。特記事項:

- **すべて Proposed ステータス**: 実装着手は Phase A（捕捉精度 95%）通過後の M1 で、
  本 PR ではコード実装なし。M1 PR で commit hash を関連 commits 節に追記する想定。
- **ADR-0040 が本プロジェクト初の SQLite 採用**: 既存はすべて JSON + atomic_io。
  Alternatives Considered で「JSON ファイル単一 / ファイル分割 / LevelDB / インメモリ」を
  並べた上で、ハッシュ lookup 性能と件数スケーリングが理由で SQLite を選択。
- **ADR-0041 で AGPL 境界を明確化**: 提案初稿の「`vendor/texas_solver/` git 同梱」を否定し、
  install-time download + SHA256 verify に切替。我々は **再配布せず**、ユーザーが上流から
  直接取得することで AGPL §5 の `Convey` 境界を踏まない。`config.solver.binary_path` 上書き
  でオフライン環境にも対応。
- **ADR-0042 で例外分類を 5 種類に固定**: `SolverError` 一発で済ませず分類することで、UI
  分岐（「再試行可能」「入力直し」「環境問題」「不明な失敗」）が明確になる。R2 突き合わせ
  失敗時に `SolverInvalidInput` が立つことで回帰テスト追加トリガにできる。
- **`subprocess.run` を選択（`Popen` ではなく）**: M1 は並行ジョブを持たないので
  `subprocess.run` の同期 API で十分。並行は M2 で `Popen` 化を検討（その時点で本 ADR を
  amend / 新 ADR で superseded）。

## Test Results

ドキュメントのみの変更のため自動テストは無し。

- `git status` — 4 ファイルの追加 / 編集のみ
- 表整合: `docs/decision-log.md` の ADR Index 表で ID 昇順が維持されていることを目視確認
- ADR テンプレート準拠: 各 ADR が Status / Date / Context / Decision / Alternatives /
  Consequences / Validation / Related の節を持つことを目視確認

## Mismatches Found

なし。

## Remaining Gaps

本 PR の範囲外で、後続タスクとして残るもの:

1. **M1 実装**: 3 ADR の Decision を実装するコード（`core/solver/*.py`、`gui/hand_review.py`、
   `tools/install_solver.py`、`config_default.json` の additive 追加、`.gitignore` 更新）。
   Phase A 通過確認後に着手。
2. **ADR-0041 §3 の SHA256 / バージョン確定**: M1 着手時に最新安定版を選び、SHA256 を ADR に追記。
3. **`docs/dogfood/measurement-plan.md`**: 提案 §6 が参照する別文書。dogfood 開始前に作成。
4. **`docs/installation.md` 更新**: ADR-0041 の install 手順 1 行追加は M1 実装時。
5. **テスト**: `tests/test_solver_cache.py` / `tests/test_install_solver.py` /
   `tests/test_solver_adapter_smoke.py` は M1 実装時に新規。
6. **Phase A 通過確認**: 全体の前提条件。本 PR では未着手。

## Related Commits

このタスク完了時の commit に記載。

## Related ADRs / Issues / Reviews

- 起票元提案: `docs/proposals/2026-06-26-hand-review-integration.md`（rev.1 §4.6）
- レビュー本体: `/root/.claude/plans/dapper-gathering-hammock.md`
- 先行 node-local + sync 非対象パターン: ADR-0027（player PIN）/ ADR-0031（auth_identity）
- 関連 v1.0 ローンチレビュー: `docs/reviews/2026-06-15-v1.0-launch-review.md`
