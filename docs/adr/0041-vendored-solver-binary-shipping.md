# ADR-0041: TexasSolver バイナリの配布方針 — install-time download + SHA256 verify

## Status

Proposed（実装は M1 着手前提・Phase A 95% 通過後。本 ADR はハンドレビュー × GTO solver
統合提案 rev.1 §4.6 で約束した起票分）

## Date

2026-06-26

## Context

ハンドレビュー × GTO solver 統合（`docs/proposals/2026-06-26-hand-review-integration.md`）は
TexasSolver（AGPL v3、multi-MB の C++ コンソールバイナリ）を外部プロセスとして起動する。
バイナリ本体は内製せず、上流リリースを使う。

本プロジェクトにおいて:

- **`vendor/` ディレクトリは存在しない**。ベンダー規約はゼロ。
- **`.gitignore` にバイナリ関連の除外/含有ルールはない**。
- **依存はすべて Python パッケージ**（`pyproject.toml` / `requirements.txt`）。前例ゼロ。
- 配布形態は「自店 PC に `git clone` + `pip install`」が想定（M1 範囲では SaaS 配布は非目的）。

提案初稿は `vendor/texas_solver/console_solver.exe` を git に同梱する案だったが、レビューで
3 点の問題が浮上した:

1. **repo bloat**: TexasSolver の単一実行ファイルで数十 MB、付属リソース込みで 100MB 超になる
   可能性。CI clone・mobile/staff dev 環境への push/pull コストが線形に悪化。
2. **OS 別バイナリの肥大化**: Win / Linux / macOS の 3 種を同梱すると 300MB 級になり、
   git のオブジェクト管理が現実的でなくなる。
3. **ライセンス再配布リスク**: AGPL 自体は再配布を許すが、改変なし表明 / NOTICE の整備が
   必要で、運用 mistake のリスクがある。

加えて、AGPL §5.4 で「ドッグフード期間中の安全運用」として「バイナリのみ統合・改変版は配布
しない」を約束している。バイナリを **配布物として扱う**（git に入れて clone 経由で配る）と、
この境界に近づく。`pip install` の外で **個別取得** する設計のほうがライセンス境界が明確になる。

## Decision

1. **バイナリは git に同梱しない**。`vendor/texas_solver/` ディレクトリは作らない。
2. **配布 = install-time download via setup script**:
   - `tools/install_solver.py` を新規追加（Python 標準ライブラリのみ）。
   - 上流 GitHub Releases から指定バージョンのバイナリを HTTPS で取得。
   - **SHA256 ハッシュを ADR に固定**（最初の採用バージョン）。マッチしなければ install 失敗。
   - 保存先 = `~/.local/share/pokerapp/solver/<version>/`（XDG_DATA_HOME を尊重）。
     プロジェクト直下の `vendor/` には置かない。
3. **採用バージョン（M1 初期）**: TexasSolver v0.2.0（暫定 — 実装着手時に最新安定版を確定し、
   その時点で本 ADR を amend）。SHA256 は **実装 PR で本 ADR に追記**。
4. **README / docs/installation.md に手順を 1 行追記**:
   ```
   python tools/install_solver.py  # GTO レビュー機能を使う場合のみ。AGPL v3、上流から取得。
   ```
   `pip install` 後の手動ステップとし、ハンドレビュー機能を使わないユーザーには走らせない。
5. **`config.solver.binary_path`** で明示パス上書きを許可（既定: 上記キャッシュ位置を自動探索）。
   開発者が手元の別バージョンを試す用。
6. **`.gitignore`** に念のため `vendor/texas_solver/` を追加（誤コミット防止のセーフティネット）。
   ディレクトリ自体は使わないが、開発者がローカルで作ってしまった場合の防御。
7. **AGPL 配布表明**: `tools/install_solver.py` がバイナリを取得した直後、ユーザーに
   AGPL v3 ライセンス全文の URL と上流リポジトリの URL をターミナルに表示する（ライセンス
   通知の追加）。`~/.local/share/pokerapp/solver/<version>/LICENSE.md`（AGPL 全文）と
   `NOTICE.md`（上流クレジット）を同 download 時に配置。
8. **OS 別配布物**: M1 は **Linux x86_64 のみ**サポート（自店 PC が Linux 想定 — 実機未確認
   なら別途確認）。Win / macOS は M2 以降。`tools/install_solver.py` は実行 OS を検出し、
   未対応 OS なら明示的に失敗する（暗黙のフォールバックを避ける）。

## Alternatives Considered

- **A. git に同梱（`vendor/texas_solver/` 直 commit）**:
  - Pros: clone した瞬間に動く、ネットワーク不要、再現性が完全。
  - Cons: 数十〜100MB の repo bloat、CI clone コスト、OS 別バイナリ追加で破綻、AGPL
    境界に近づく（自分が配布元になる）。
  - 不採用: bloat と境界リスクが上回る。
- **B. Git LFS**:
  - Pros: git ワークフローに乗ったまま大容量を分離。
  - Cons: LFS サーバの料金 / 設定 / 認証が追加運用コスト。clone 時の LFS pull が必須に
    なる（オフライン clone 不能）。AGPL 境界は A と同じ。
  - 不採用: 運用コストが見合わない。
- **C. GitHub Releases に self-host（自分が再配布）**:
  - Pros: upstream URL の安定性に依存しない、SHA256 を自分が管理できる。
  - Cons: 自分が **配布元** になる（AGPL の `Convey` に該当する可能性）。改変なし表明や
    NOTICE 整備が必須に。フェーズ B/C で SaaS にしないと約束しているので、境界を踏まない
    ほうが安全。
  - 不採用: ライセンス境界リスクが上回る。Decision の install-time download を選択。
- **D. 完全に手動配置**（ユーザーが自分でダウンロードして config で path 指定）:
  - Pros: 我々のコードは一切バイナリに触らない。境界が最も clear。
  - Cons: 自店ドッグフードユーザーへの導入摩擦が高い（手順ミスでサポート負荷）。
  - 不採用: dogfood UX として弱い。**ただし C のフォールバックとして config 上書きは
    残す**（Decision §5）。
- **E. PyPI 経由（pip でバイナリを依存として宣言）**:
  - Pros: `pip install` 一発で済む。
  - Cons: TexasSolver は Python パッケージとして公開されていない。AGPL バイナリを
    自分で wheel 化すると C と同じ境界問題。
  - 不採用: 上流が公開していない以上、選択肢にならない。

## Consequences

- Positive:
  - repo がスリムなまま保たれる（CI / mobile dev 環境への波及なし）。
  - AGPL 境界が明確（我々は再配布せず、ユーザーが上流から直接取得）。
  - バージョン更新が config + SHA256 の amend で済み、git history を汚さない。
  - ハンドレビュー機能を使わないユーザーは install script を走らせないだけで影響ゼロ。
- Negative / trade-offs:
  - **install 時にネットワーク必須**。オフライン PC への配布が困難（自店内ネットワークが
    通常はある前提）。手動配置（Alt D）のフォールバックパスを残してオフライン要件に対応。
  - 上流 GitHub Releases の URL 変更 / リポジトリ削除リスク。SHA256 で改竄検知はできても、
    取得不能になったら新しいミラーへの切替が必要（その時は本 ADR を amend）。
  - **本プロジェクト初の subprocess 用バイナリ配布規約**。将来別の外部バイナリ依存が
    増えた場合、本 ADR の規約を流用するか、別 ADR を起こすかは open。
- Neutral / new constraints:
  - `tools/install_solver.py` は Python 標準ライブラリのみで実装（`urllib.request`、`hashlib`、
    `zipfile`/`tarfile`、`os`）。追加 pip 依存を入れない。
  - M1 は Linux x86_64 のみ。Win / macOS 対応は M2 で別途扱う。

## Validation / Follow-up

- [ ] `tools/install_solver.py` を実装し、SHA256 検証 + LICENSE/NOTICE 配置までを含む。
- [ ] M1 PR で採用バージョンと SHA256 を本 ADR §3 に追記。
- [ ] `.gitignore` に `vendor/texas_solver/` を追加（誤コミット防止）。
- [ ] `docs/installation.md` にハンドレビュー機能のオプトイン install 手順を 1 セクション追加。
- [ ] `config.solver.binary_path` の上書きパスを実装し、テスト（mock binary パスで smoke）。
- [ ] OS 検出と未対応 OS の明示失敗を実装。
- [ ] M2 で Win / macOS バイナリの SHA256 追記。

## Related Files

- `tools/install_solver.py`（新規・M1 実装予定）
- `~/.local/share/pokerapp/solver/<version>/` 配下の `console_solver` / `LICENSE.md` /
  `NOTICE.md`（install 時に配置）
- `core/solver/texas_solver_adapter.py`（バイナリ path 解決はここに集約・M1 実装予定）
- `core/config.py` / `config_default.json`（`solver.binary_path` を additive 追加予定）
- `.gitignore`（`vendor/texas_solver/` を追加予定）
- `docs/installation.md`（オプトイン install 節を追加予定）

## Related Tests

- `tests/test_install_solver.py`（M1 実装時に新規。SHA256 mismatch / OS 不一致 / 上書き
  パス指定の smoke）

## Related Commits

- 本 ADR は **設計のみ**、コード実装なし。M1 PR で commit hash + 採用バージョン + SHA256 を追記。

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: `docs/proposals/2026-06-26-hand-review-integration.md`（rev.1 §4.6 / §5）/
  ADR-0040 / ADR-0042
