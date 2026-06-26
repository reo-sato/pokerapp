# ADR-0040: Solver cache persistence — SQLite, node-local, sync / backup 非対象

## Status

Proposed（実装は M1 着手前提・Phase A 95% 通過後。本 ADR はハンドレビュー × GTO solver
統合提案 rev.1 §4.6 で約束した起票分）

## Date

2026-06-26

## Context

ハンドレビュー × GTO solver 統合（`docs/proposals/2026-06-26-hand-review-integration.md`）で
TexasSolver を外部プロセスとして起動する設計を取る。1 スポットの解析は数十秒〜数分かかり、
リクエストのたびに解いていてはレビュー体験が成立しない（提案 §R1）。同一スポットへの再要求は
キャッシュで即返ししたい。

本プロジェクトの永続化は **すべて JSON + `core/atomic_io.py`** で統一されている
（players.json / sessions.json / ledger.json / order_requests.json / hand_corrections.json /
player_credentials.json / auth_identity.json / logs/{session_id}.json）。
**SQLite を使った前例はゼロ**（`grep -r "sqlite" core/` は 0 件）。

ソルバー結果のキャッシュは:

- **スポットキー検索が支配的**（ハッシュ lookup を O(1) で打ちたい）
- **件数が増えるとファイル数が爆発する**（自店ドッグフード 8 週で数百〜数千スポット）
- **再生成可能**（ハンドログ + バイナリ + 同一入力ビルダーから決定的に再現できる）
- **node-local**（複数店舗を跨いで共有する意味がなく、`(session_id, hand_id)` のような共有キーで引かない）

という性質を持ち、既存 JSON ストアと性格が違う。

加えて、本キャッシュは:

- `core/sync.py:build_snapshot` の **whitelist 対象外**（追加しない限り自動的に sync 非対象）
- `core/backup.py:DEFAULT_BACKUP_SOURCES` の **enumeration 対象外**（追加しない限り backup 非対象）

なので、**何もしなければ** sync・backup どちらも触られない。本 ADR は「触らない選択を明文化する」
ことで運用者の判断を確定させる。

## Decision

1. **永続化 = SQLite**。プロジェクト直下 `solver_cache.sqlite`（`.gitignore`、node-local）。
   標準ライブラリ `sqlite3` のみ使用、ORM は導入しない（依存追加なし）。
2. **スキーマ（M1 最小）**:

   ```sql
   CREATE TABLE IF NOT EXISTS solver_results (
     spot_key TEXT PRIMARY KEY,
     result_blob BLOB NOT NULL,       -- zlib 圧縮した結果 JSON
     created_at TEXT NOT NULL,        -- ISO 8601 UTC
     solver_version TEXT NOT NULL,    -- TexasSolver バイナリのバージョン文字列
     input_summary TEXT               -- デバッグ用の人間可読要約（ボード+ストリート等）
   );
   CREATE TABLE IF NOT EXISTS schema_version (
     version INTEGER PRIMARY KEY
   );
   ```

   `schema_version` を別表に分けることで将来のマイグレーションを楽にする。
3. **spot_key 生成 = `core/solver/types.py:make_spot_key()`** の純関数で生成。SHA256 hex の
   先頭 32 文字。入力は **正規化済み** の `(board_sorted, stacks_tuple, pot, action_history_canonical,
   bet_sizing_tree_id)` を JSON 直列化したもの。命名は既存の `idempotency_key` 慣習
   （`core/ledger_repository.py:grant_points`）を踏襲。
4. **`solver_version` カラムは TexasSolver バイナリのバージョン文字列を必ず含める**。
   バイナリのバージョンが変わったら **古いキャッシュは未ヒット扱い**にする（=
   ルックアップ時 `WHERE spot_key=? AND solver_version=?`）。明示的な無効化は不要。
5. **sync 非対象**:
   - `core/sync.py:build_snapshot` に `solver_cache.sqlite` を **追加しない**。
   - 本 ADR で「sync 対象外であること」を契約として明記。`player_credentials.json` /
     `auth_identity.json` の "node-local, sync 非対象" と並ぶ位置付け（ADR-0027 / ADR-0031）。
6. **backup 非対象**:
   - `core/backup.py:DEFAULT_BACKUP_SOURCES` に **追加しない**。
   - 再生成可能（ハンドログ + バイナリ + 入力ビルダー）であり、失われても運用は止まらない。
   - 自店内で複数台運用する場合も、各ノードで独立に re-warm すれば足りる。
7. **マイグレーション方針 = "drop and rebuild"**。`schema_version` 変更時はテーブル DROP →
   CREATE。`solver_cache.sqlite` を `solver_cache.sqlite.corrupt-<ts>` にリネームしてリビルド
   （`core/atomic_io.py:read_json_file` の破損退避と同じ思想）。本キャッシュは ephemeral なので
   保守的マイグレーションは不要。
8. **lock / 並行**: `sqlite3` 既定の write-locking（プロセス内シリアライズ + ファイルロック）に乗る。
   `--review` 画面のみが書き込み、他プロセスは触らない。M1 では並列ソルバージョブを持たない
   （ADR-0042）ため、複雑な並行制御は不要。
9. **`config.solver.cache_path`** で位置上書き可能にする（既定: プロジェクト直下）。テスト時の
   一時パス注入を容易にするため。

## Alternatives Considered

- **JSON ファイル単一（`solver_cache.json`）+ atomic_io** — 既存規約と整合する最短手だが、
  数百〜数千スポットで毎回 read all + 書き込み atomic rename は線形にコストが伸びる。
  ハッシュ lookup 性能が破綻する。→ 不採用。
- **ファイル分割（`solver_cache/<spot_key>.json`）** — lookup は早いが file system に
  数千ファイルが堆積、`ls` / バックアップ系コマンドが詰まる。OS によっては inode 枯渇懸念。
  → 不採用。
- **LevelDB / RocksDB** — KV ストアとしては理想だが、追加バイナリ依存（C++ ライブラリ）が
  入る。自店 PC への配布難度が上がる。→ 不採用。
- **インメモリのみ（`dict` キャッシュ）** — 再起動でキャッシュ全消失。ソルバーの実行コストを
  考えると毎日 cold start するのは致命的。→ 不採用。
- **既存 JSON ストアの 1 つに同居（例: ledger.json の sub-section）** — append-only / sync /
  backup の前提が全く違うのに同居させると、運用の不変条件が崩れる。→ 不採用。
- **SQLite を採用しつつ sync 対象にする** — node-local の意味が薄れ、無駄に容量を消費する。
  再生成可能なものを同期する必要がない。→ 不採用。

## Consequences

- Positive:
  - O(1) ハッシュ lookup でレビュー体験が成立する。
  - 既存 JSON ストアの append-only / atomic_io の不変条件を汚染しない（独立の永続層）。
  - sync・backup どちらも触らない明示の契約があり、運用者の判断を空欄にしない。
  - 再生成可能なため、ディスク事故・ノード破棄に対する耐性をわざわざ作らなくてよい。
- Negative / trade-offs:
  - **本プロジェクト初の SQLite 永続化**。将来別の機能が同じパターンを採用するかは
    open（M3 母集団メタ集計等で再利用されうるが、本 ADR は solver cache に限る）。
  - マイグレーションが "drop and rebuild" のため、ユーザーは初回再ヒットまで遅い体験を
    一度だけ受け入れる。
  - バイナリ更新時に古いキャッシュが事実上死蔵される（disk 上は残るが lookup に使われない）。
    クリーンアップは別 PR で `tools/solver_cache_gc.py` 等に切り出す。
- Neutral / new constraints:
  - `solver_cache.sqlite` を `.gitignore` に追加する必要がある（実装 PR で対応）。
  - `core/atomic_io.py` の atomic write 規約は SQLite には適用されない（SQLite 自身の
    journaling に依存）。これは「ファイル単位 atomic」と「行単位 transaction」のレイヤが
    違うため意図的。

## Validation / Follow-up

- [ ] `core/solver/solver_cache.py` 実装時に本 ADR を参照（`make_spot_key` 正規化規約、
      `solver_version` 必須、drop-and-rebuild マイグレーション）。
- [ ] `.gitignore` に `solver_cache.sqlite*` を追加。
- [ ] `core/sync.py` / `core/backup.py` のレビュー時に「solver_cache.sqlite が含まれていない」
      ことを確認（含めない、が決定事項）。
- [ ] `tests/test_solver_cache.py`（M1 実装時）で put/get/hit-miss/solver_version 切り替えを検証。
- [ ] `config.solver.cache_path` 上書きの差し込みテスト。
- [ ] 将来: 容量上限 / TTL / `tools/solver_cache_gc.py` は本 ADR ではなく後続で扱う。

## Related Files

- `core/solver/solver_cache.py`（新規・M1 実装予定）
- `core/solver/types.py`（`SpotKey` / `make_spot_key()`, 新規・M1 実装予定）
- `core/sync.py:build_snapshot`（**変更しない** = 本 ADR の契約上の確認点）
- `core/backup.py:DEFAULT_BACKUP_SOURCES`（**変更しない** = 同上）
- `core/atomic_io.py:read_json_file`（破損退避の思想を参考）
- `core/ledger_repository.py:grant_points`（`idempotency_key` 命名慣習の参考）
- `.gitignore`（`solver_cache.sqlite*` 追加予定）

## Related Tests

- `tests/test_solver_cache.py`（M1 実装時に新規）

## Related Commits

- 本 ADR は **設計のみ**、コード実装なし。M1 PR で commit hash を追記。

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: `docs/proposals/2026-06-26-hand-review-integration.md`（rev.1 §4.6）/ ADR-0041 /
  ADR-0042 / ADR-0027（node-local, sync 非対象の先行例）/ ADR-0031（同上）
