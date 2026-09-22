# ハンドレビュー機能の統合方針

**ステータス:** 提案 / レビュー反映済み（rev.1, 2026-06-26）
**作成日:** 2026-06-26
**改訂:** 2026-06-26（Must 6 + Should 4 反映, レビュー: `/root/.claude/plans/dapper-gathering-hammock.md`）
**関連文書:**
- `docs/reviews/2026-06-15-v1.0-launch-review.md`（背景）
- `docs/dogfood/measurement-plan.md`（別途作成予定）
- ADR-0036（hand correction overlay = 本提案 §4.1 の入力源）
- ADR-0011（deterministic replay = 本提案の入力源生成系）

## TL;DR

捕捉済みライブハンドに GTO ソルバー（TexasSolver）の解を重ねる単発レビュー機能を追加する。
**自店ドッグフードに必要な最小範囲のみ**を本 PR 系列で導入し、商用展開・有料化・SaaS 化は範囲外とする。
ソルバー本体は内製せず、TexasSolver バイナリを外部プロセスとして呼ぶ。
本 PR はフェーズ A の捕捉精度検証が 95% 以上で通過した後に有効化することを前提とする。

---

## 1. 背景

v1.0 ローンチレビュー（2026-06-15）で、本製品の三層構成（会計／会員／ハンド記録）のうち
**ハンド記録は支払い動機が二次的** と整理した。しかし、捕捉済みハンドを「自分の上達のための学習データ」
として使う経路を確立できれば、上達志向プレーヤーへの独自価値が立ち上がる ——
これはオンラインでは既に証明された需要（GTO Wizard 年 $360 規模）であり、ライブには欠けている入力データである。
本機能はこの仮説の検証手段である。

なお、特定の他プレーヤーを狙う解析（個別プロファイル）は業界規約および倫理上、
**永続的に範囲外** とする。本機能の解析対象は常に「自分の手」のみで、
母集団分析は集計レンズ（個人特定不能な k-匿名性レベル）に限定する。

### 1.1 自店ユーザー層との適合性（rev.1 追記）

「上達志向プレーヤー」と自店現状ユーザー層の重なりは未確認。dogfood 設計（§6）で
最低 N 人のうち何人が「GTO Wizard / Pio / 体系的学習リソースに既に触れた経験がある」かを
**ベースライン質問** として収集し、Phase B の Negative 結果を「機能の失敗」と
「ユーザー層のミスマッチ」のどちらに帰属させるかを切り分けられるようにする。

## 2. 目的（本 PR 系列の範囲）

- 既存ハンド記録の出力（**`api/read_models.py:get_hand()` 経由 = B4 訂正適用後の `HandSummary`**）を
  TexasSolver が読める入力に変換する
- TexasSolver バイナリを外部プロセスとして起動し、解を取得・キャッシュする
- 単発ハンドレビュー画面で、ヒーローの実アクションと GTO の推奨を並べて表示する
- 上記を **自店内ローカル実行のみ** で動かす（ネットワーク経由でのサービス提供はしない）

## 3. 非目的（本 PR 系列の範囲外）

以下はドッグフード期では実装しない。フェーズ B 通過後に別 PR 系列で扱う。

- 母集団メタの集計・可視化
- 集計レンズを単発レビューに重ねる融合表示
- 全履歴を横断するリーク自動検出
- 課金フロー・有料機能のゲーティング
- クラウド配置・SaaS 化（コマーシャルライセンス取得後）
- フルソルバーの内製
- マルチウェイ（3 人以上）対応
- **リアルタイム卓上助言**（業界規約・倫理上、永続的に対象外）
- **B4 ハンド訂正済みでないハンドへの GTO 重ね表示**（rev.1 追記 — silent failure 源を遮断）

非目的の明示は本 PR の最重要セクションである。スコープが膨らむと、
フェーズ A の捕捉精度検証より先にここに着地してしまう。

## 4. アーキテクチャ

### 4.1 既存との接続点（rev.1 で大幅改訂）

入力源は **`api/read_models.py:get_hand(session_id, hand_id, ...)` の戻り値**（dict 形 `HandSummary`）に固定する。
これは B4 ハンド訂正（ADR-0036）を **オーバーレイ適用済み** で返すため、訂正前の誤アクション列を
ソルバーに渡してしまう silent failure を排除できる。生 JSON（`output/json_writer.py` 出力）を直読みするのは禁止。

入力に使う実際のデータ構造（コードと 1:1 対応）:

| 項目 | データ源 | 備考 |
|---|---|---|
| ボード | `HandSummary["board"]` + `board_source` | RFID / OCR / manual / "" |
| アクション列 | `HandSummary["actions"][i]["action", "amount"]` | `ActionRecord` シリアライズ後 |
| 各アクション後の現在ポット | `actions[i]["pot_after"]` | 各意思決定点で有効 |
| 各アクション後の手元スタック | `actions[i]["stack_after"]` | 有効スタックは前 actor の `stack_after` から導出 |
| ブラインド | `HandSummary["blinds"]` | `{"sb": int, "bb": int}` |
| ヒーローのホール | `HandSummary["players"][i]["hole_cards"]` | **`None` あり**（プリフロップフォールド / RFID 取りこぼし） |
| メイン/サイドポット | `HandSummary["pots"]` | **pokerkit backend 必須**（legacy backend は `[]`） |
| ポジション（BTN/SB/BB/...） | **未保持** — `seat` 数値と `blinds`、`actions[0]` から **`spot_config_builder` 内で推定** | silent failure 源 → 専用テスト要 |

**入力前のフィルタ規約**（rev.1 で追加）:

1. `HandSummary["review_required"] is True` のハンド、または `actions[i]["needs_review"] is True` を
   含むハンドは **デフォルト除外**。UI には「訂正してから解析対象になります」と表示。
2. `players[hero].hole_cards is None` のハンドは M1 では **除外**（"hero unknown" の自動レンジ仮定は M1 範囲外、§9.5）。
3. `config.engine.backend != "pokerkit"` で生成されたハンドは **除外**（`pots` 無し → side-pot 解析不能）。
   CLAUDE.md 上は live 既定が `pokerkit` なので通常は素通り。

### 4.2 新規モジュール構成（rev.1 で既存規約に整合）

```
core/
  solver/                           # 新規
    texas_solver_adapter.py         # バイナリ起動・出力 JSON のパース
    spot_config_builder.py          # HandSummary(訂正後) → ソルバー入力テキストへの変換
                                    # ポジション推定もここ
    solver_cache.py                 # スポットキャッシュ（SQLite, node-local, sync/backup 非対象）
    types.py                        # SolverInput / SolverResult / SpotKey
    tests/
      test_spot_config_builder.py
      test_solver_adapter_smoke.py
gui/
  hand_review.py                    # 新規（既存 gui/ 規約に整合）
                                    # 起動口: main.py --review → run_hand_review()
                                    # 既存 --players / --sessions / --ledger と同パターン
vendor/
  texas_solver/                     # 新規・バイナリのみ同梱
    console_solver.exe              # または Linux/macOS 実行ファイル
    resources/                      # ソルバー付属のリソース
    LICENSE.md                      # AGPL v3 全文
    NOTICE.md                       # 上流クレジット・改変有無
```

**変更点（rev.1）**: 提案初稿の `ui/review/single_hand_review.py` は既存規約に反するため
`gui/hand_review.py` に揃えた。複数ファイルに分かれる場合のみ `gui/review/` サブディレクトリを許す。
`main.py` の起動は `--review` フラグを追加し、`--players` / `--sessions` / `--ledger` と
同じ「独立窓 + `run_X()` 関数」パターンに乗る。

### 4.3 依存方針（重要）

TexasSolver は **バイナリのみ** を `vendor/texas_solver/` 配下に同梱する。
**ソースコードは取り込まない**。理由はライセンス上の境界線維持（後述）。
バイナリは外部プロセスとして起動し、ファイル経由でやり取りする。

### 4.4 処理フロー（rev.1 で入力源を明示）

```
[UI] ハンド選択
  ↓
[api/read_models.get_hand(sid, hid)]  ← B4 訂正適用済み・schema 1.0 frozen
  ↓
[フィルタ §4.1] needs_review / hole_cards=None / legacy backend → スキップ
  ↓
[spot_config_builder] HandSummary → TexasSolver 入力テキスト + ポジション推定
  ↓
[solver_cache] スポットキー（ボード+スタック+ポット+履歴+ベットツリー設定のハッシュ）でルックアップ
  ↓                              ↓
ヒット                           ミス
  ↓                              ↓
キャッシュ返却               [texas_solver_adapter]
                                  console_solver -i <入力ファイル>
                                  ↓
                              output_result.json をパース
                                  ↓
                              [solver_cache] 結果を保存
  ↓                              ↓
[UI] ヒーローの実アクション + GTO 推奨頻度・EV を並べて表示
```

### 4.5 キャッシュ戦略

ソルバーは 1 スポットあたり数十秒〜数分かかるため、毎回解いていては実用にならない。

- スポットキーは「ボード × 有効スタック × ポット × ベット履歴 × ベットサイズツリー設定」のハッシュ
  （命名は `core/ledger_repository.py:grant_points` の `idempotency_key` 慣習を踏襲し、`spot_key`）
- SQLite で結果 JSON（圧縮）を保存。**node-local**。`core/sync.py:build_snapshot` の whitelist 対象外で
  自動的に sync 非対象。`core/backup.py:DEFAULT_BACKUP_SOURCES` にも追加しない（再生成可能）。
- 完全一致のみヒットとする（近似マッチは本 PR では扱わない）
- ミスした場合は非同期でジョブを投げ、完了したら UI に通知

頻出スポットを夜間に事前解析するバッチは別 PR（M2）で扱う。
本 PR では「使われたハンドをその場で解いてキャッシュに積む」遅延ロードのみで足りる規模を想定。

### 4.6 ADR commitments（rev.1 で追加）

本提案は **コードベースに前例のない選択** を 3 つ同時に持ち込む。M1 着手前に以下 3 件の ADR を
起票し、`docs/decision-log.md` の ADR Index に登録する。仮番号は連番（最新 ADR-0039 の次）。

| 仮 ID | 仮タイトル | 主要論点 |
|---|---|---|
| ADR-0040 | Solver cache persistence — SQLite, node-local, sync/backup 非対象 | SQLite 採用理由（速度・ハッシュ lookup）、ephemeral 性、sync・backup 非対象の明文化（`player_credentials.json` の "node-local, sync 非対象" と並ぶ位置付け） |
| ADR-0041 | Vendored solver binary shipping policy | git 直接同梱 vs release asset / install-time download。multi-MB を git に入れる場合の `.gitignore` 規約、Git LFS 検討、CI 影響、配布物のサイズ予算 |
| ADR-0042 | External solver subprocess invocation contract | `subprocess.run(check=True, timeout=...)` 最低水準、SIGKILL / SIGTERM 順序、stderr ログ化、プロセスリーク防止、`config.solver.timeout_sec` 等の hook |

ADR を先に書くことで M1 PR レビューで再議論にならない。

## 5. ライセンスポジション（最重要）

TexasSolver は AGPL v3 で、作者は次の境界を明示している:

- バイナリの統合 → OK
- ソースコードの統合、またはインターネット経由のサービス提供 → コマーシャルライセンスが必要

本 PR はこの境界を **バイナリ統合かつローカル実行のみ** に置く。

### 5.1 やらないこと

- TexasSolver のソースコードを取り込まない（vendored バイナリのみ）
- 自社サーバから解を返さない（自店 PC 上で実行）
- 解の結果を外部のユーザーに再配布しない
- TexasSolver のバイナリを再配布しない（ユーザー端末に配るような形にしない）

### 5.2 派生著作物の解釈

本 PR で追加する `core/solver/*` は、TexasSolver のバイナリを外部プロセスとして呼ぶラッパーである。
ソースコード結合ではないため、AGPL の伝播対象とは解釈しない方針。
**ただし、この解釈は法務確認が必要** で、商用展開前に確定させる。

### 5.3 ドッグフード期間中の安全運用

以下を満たす限り、コマーシャルライセンスなしで運用できる:

1. 金銭を取らない（フェーズ C で有料化する瞬間に崩れる）
2. インターネット経由でサービス提供しない（自店 PC 上のローカル実行に限る）
3. バイナリのみ統合（コード統合しない）
4. 改変したバイナリを配布しない

### 5.4 商用ライセンス交渉のタイミング

**フェーズ B 中盤（自店ドッグフード開始から 1.5 ヶ月目あたり）に作者へ問い合わせを開始する。**
連絡先: `icybee@yeah.net`。フェーズ C（有料化）に入る瞬間に必須となるため、
3 ヶ月目で慌てて交渉するのは遅い。問い合わせ項目:

- 価格モデル（年額／売上歩合／買い切り）
- ユーザー数・スポット数によるスケーリング
- SaaS 構成での具体的な許諾範囲
- 契約解除時の継続使用権（既存ユーザーへの提供猶予）

このタスクは本 PR 系列の範囲外だが、本 PR のレビュー時に担当を確定させる。

## 6. 検証計画との接続（rev.1 で KPI 閾値と dogfood 規模を本提案に内包）

本 PR は「フェーズ A 通過後にオンにする機能」という位置づけ。フェーズ B の計測 KPI を直接生む。

| フェーズ | 役割 | 本 PR との関係 |
|---|---|---|
| A: 捕捉精度の検証 | ハンドが正しく記録されるか | 本 PR の **前提条件**。A で 95% 未満ならオンにしない |
| B: 上達価値の検証 | ハンドレビューに価値があるか | 本 PR が **計測対象**。起動回数・滞在時間・書き出しクリック数 |
| C: 支払い意欲の検証 | 価値に金を払うか | 本 PR 系列の範囲外、別 PR |

### 6.1 dogfood 規模（rev.1 追加）

- **対象**: 自店内 N = 5〜10 名（最低 5 名を保証する。1〜2 名では Phase B 結果が体験談の域を出ない）
- **期間**: X = 8 週（CLAUDE.md ロードマップの「3 ヶ月」のうち、A 通過から B 評価会議までの正味）
- **観測単位**: 週次。週ごとにユーザー別の「起動回数」「平均滞在時間（秒）」「『次の手を表示』クリック数」を集計

### 6.2 KPI 閾値（rev.1 追加・暫定）

下記は **measurement-plan で精緻化** するが、提案単体で M1 着手判断ができるよう暫定閾値を本書に置く。
閾値はあくまで「Phase B 評価会議の議題を空欄にしない」目的であり、後から動かしてよい。

| 指標 | 暫定閾値（Phase B 通過の必要条件） | 観測手段 |
|---|---|---|
| 週次起動率 | N 人中の **過半数が 8 週中 4 週以上で週 3 回以上起動** | `gui/hand_review.py` 起動ログを `logs/hand_review_usage.jsonl` に append-only 記録 |
| 平均滞在時間 | 1 セッションあたり **中央値 ≥ 5 分** | 同上ログ（window open/close timestamp） |
| 能動操作率 | 1 セッションあたり「次の手表示」または「別ハンド選択」を **中央値 ≥ 3 回** | UI イベント計装 |
| ベースライン質問 | 参加者 N の **過半数が「他 GTO ツールを使ったことがある」と回答** | dogfood 開始時のオンボーディング 1 ページ |

**計装の置き場所**: `gui/hand_review.py` の `__init__` / `on_destroy` / 主要ボタンハンドラに
`logging` 経由でイベントを出し、`logs/hand_review_usage.jsonl` に追記。既存ログ（`logs/{session_id}.json`）
とは別ファイル。sync・backup 非対象（dogfood ローカル計測のため）。

詳細は `docs/dogfood/measurement-plan.md`（別途作成予定）で定義する。

## 7. マイルストーン

- **M1（本 PR）:** バイナリ統合、単発ハンドの GTO 重ね表示、キャッシュ、**Pio 差分 3 アーキタイプの手動 QA（rev.1 で前倒し, §R4 参照）**
- **M2:** 事前計算バッチ（頻出スポットを夜間に解く）／PioSolver Free との **本格回帰テストハーネス**
- **M3:** 母集団メタ集計（フェーズ B 後半、別系列）
- **M4:** ノードロック層（母集団傾向 → ソルバー設定の自動生成）

**M2 以降はフェーズ B 通過後に着手する。** フェーズ B で価値が確認できなければ M2 は不要になる可能性があり、
先回り実装は無駄になる。

## 8. リスクと緩和

### R1: ソルバーが遅すぎてレビュー体験が壊れる

ターン・リバーは秒〜十数秒で済むが、フロップは分単位かかることがある。
M1 段階では機能を絞ることで対処する。

- 当面は「過去 24 時間以内のハンド」「ターン・リバーから始まるスポット」を優先
- フロップから解析が必要なハンドはバックグラウンドジョブにして「準備中」表示
- M2 の事前計算バッチで根本対処
- 推奨スペック（§9.1）に **「1 スポット 60s 以内なら推奨、180s 超ならバックグラウンドジョブ強制」** を明記

### R2: ソルバー入力組み立てのバグ（rev.1 で 10 ハンド → 10 アーキタイプに精緻化）

ボード・アクション・スタックの変換に 1 箇所でもバグがあると、返ってくる解は
**「もっともらしいが間違い」** になる。これは silent failure であり、ユーザーには気づけない。

M1 完成時、以下 **10 アーキタイプ** について「同じハンドを手動で TexasSolver GUI に入力した結果」と
「`spot_config_builder` 自動入力の結果」を完全一致（戦略・EV が小数点以下まで一致）で突き合わせる。
一致しない限り dogfood に出さない。

1. heads-up SRP（preflop raise pot）BTN open / BB call → flop check-check
2. heads-up SRP BTN open / BB call → flop bet-call
3. heads-up 3-bet pot turn 決断
4. heads-up 4-bet pot river 決断（deep stack）
5. heads-up all-in flop（side-pot エッジ）
6. heads-up all-in turn
7. heads-up all-in river
8. BB defend vs open（ポジション逆転）
9. **B4 訂正適用後ハンド**（訂正があってもソルバー入力に訂正後の値が渡ることを確認）
10. ポジション推定が effective に効く境界ハンド（6-max と 9-max でテーブル形状違い）

突き合わせ結果は `tests/fixtures/solver_roundtrip_*.json` に固定し、回帰テスト化。
本パターンは ADR-0033（派生 confidence の property-based 較正）と並ぶ品質保証規約として位置付ける。

### R3: AGPL 解釈の事故

ローカル実行・バイナリ統合の解釈で問題が出る可能性。

- フェーズ B 中盤までに作者に直接確認
- フェーズ C 前にコマーシャルライセンスを締結
- 法務レビューを商用展開前に必ず実施

### R4: TexasSolver の精度が Pio と異なる（rev.1 で M1 に最小限を前倒し）

過去ベンチで「ピュア戦略の取りこぼし」（例: Pio では IP が 7% でレイズするスポットで、
TexasSolver はレイズが 0%）が報告されている。これは事業上致命的ではないが、
ユーザーへの説明責任に関わる。**Pio との方向性ズレを Phase B 開始後に発見するのはリスク露出が遅すぎる**ため、
最小限の事前確認を M1 内に含める。

- **M1 内に最小 3 アーキタイプ**（HU SRP flop / HU 3-bet pot turn / HU 4-bet pot river）について、
  PioSolver Free 手動入力結果と TexasSolver 自動入力結果を突き合わせる手動 QA タスクを含める
- 戦略の **方向性**（ベット主体／チェック主体）が一致しない場合は **dogfood に出さない判断** にする
- 頻度の微差（46% vs 72% 等）はアラートしない（実装上人間が区別できないため）
- M2 で **本格回帰テストハーネス** 化（CI 化、より広いアーキタイプ）

工数: M1 内 3 件で半日〜1 日。dogfood 出した後にズレを発見する最悪ケースを防ぐ。

### R5: 作者が商用ライセンスを拒否する／高額を提示する（rev.1 で自社ソルバー削除）

事業の根幹に関わる。

- フェーズ B 中盤までに問い合わせを開始
- フェーズ C 判断に交渉結果を織り込む
- **フォールバック: PioSolver の個別ライセンス購入**（バッチ事前計算のみ、結果数値のみ DB 格納する構成。
  グレーだが現実的）

> **rev.1 削除**: 初稿の「究極のフォールバック: 自社ソルバー実装」は **複数エンジニア年規模** の判断であり、
> 本提案のスケールでは現実的なフォールバックではない。読者の判断を歪めるため検討対象から除外する。
> 残す現実的フォールバックは Pio 個別ライセンスのみ。

### R6: スコープクリープ

「集計レンズもついでに」「リーク検出も入れちゃおう」が一番起こりやすい失敗。

- 本 PR で非目的（セクション 3）にあるものは、いかなる理由でも本 PR 系列に入れない
- PR レビュー時、非目的のリストを冒頭で確認する慣習にする

## 9. オープンな質問（rev.1 で多くが解決済み）

| # | 質問 | 回答（rev.1） |
|---|---|---|
| 9.1 | 自店 PC のスペックは推奨要件（メモリ 8GB+、現代的 CPU）を満たすか | **未確認**。推奨基準を **「1 スポット 60s 以内なら推奨、180s 超ならバックグラウンドジョブ強制」**（§R1）で定義。実機計測は M1 末に実施 |
| 9.2 | `hand_reconstruction` の現在の出力フォーマットが、本 PR の想定（PHH 相当）と完全一致しているか | **解決**: 入力源は `api/read_models.py:get_hand()`（schema 1.0 frozen、B4 訂正適用済）。`hand_reconstruction` という名前のモジュールは無く、実体は `core/hand_log.py:HandSummary` + `integration/replay.py` + `api/read_models.py`。詳細は §4.1 |
| 9.3 | UI フレームワーク（既存ハンドリプレイ表示の有無）の状況 | **解決**: customtkinter（CLAUDE.md §技術スタック）。**既存ハンドリプレイ UI はゼロ**。`gui/session_viewer.py` の list→detail パターンをハンド選択に再利用。新窓は `main.py --review` で `--players` / `--sessions` / `--ledger` と同パターン |
| 9.4 | キャッシュ SQLite のバックアップ方針（既存 B2: バックアップ未実装課題との関係） | **解決**: ソルバー結果はハンドログ + バイナリから再生成可能なので **sync・backup どちらも非対象**。ADR-0040 で明文化（§4.6） |
| 9.5 | ソルバーのレンジ設定（プリフロップアクションから推定する自動レンジ vs 手動指定） | **暫定**: M1 は **自動推定のみ**（プリフロップアクションから default range を引く）。手動指定は dogfood ユーザーから要望が出てから別 PR。M1 で手動指定を入れるとスコープクリープ（§R6） |

## 10. 関連文書

- `docs/reviews/2026-06-15-v1.0-launch-review.md` — v1.0 ローンチレビュー（本 PR の背景）
- `docs/dogfood/measurement-plan.md` — 3 ヶ月ドッグフード計測プラン（別途作成予定）
- ADR-0036 — Hand correction overlay（入力源で適用済み）
- ADR-0011 — Deterministic replay harness（入力源の生成系）
- ADR-0033 — Derived confidence weight calibration（R2 / R4 の品質保証パターン参照）
- ADR-0040〜0042 — 本 PR 起票予定（§4.6）
- TexasSolver 公式リポジトリ: https://github.com/bupticybee/TexasSolver
- AGPL v3 全文: https://www.gnu.org/licenses/agpl-3.0.html

## 11. 補足: 設計を保守的にする理由

本ドキュメントは意図的に保守的に書いてある。理由は 3 つ。

第一に、ソルバー統合は **silent failure の塊** である。動いているように見えるバグが最も怖く、
ユーザーは「自分の打ち方が悪い」と誤学習する。これはユーザーへの実害であり、機能の停止より重い問題である。
**rev.1 で訂正前ハンドへの GTO 重ね表示を非目的化したのも同じ理由** — 訂正前の誤アクション列に
正しい GTO を重ねると、判断の責任が完全に逆転する。

第二に、AGPL は網が細かい。「ローカル実行」「バイナリ統合」の境界はコードで実装できるが、
運用が境界を越えると一瞬で違反になる。本 PR は境界の内側で動く設計にしてあり、運用ルールも明示する。

第三に、自店ドッグフードの本当の目的は「ハンドレビューに価値があるか」の検証であり、機能の網羅性ではない。
M1 で必要十分な最小機能を出し、フェーズ B の行動データで価値を確認してから先に進む。
**先に作りすぎると、価値が出なかったときの撤退コストが膨らむ**。

## 12. 改訂履歴

- **2026-06-26 rev.1**: コードベースとの照合レビューを反映。Must 6 件 + Should 4 件を取り込み:
  - §2 / §4.1 / §4.4: 入力源を `api/read_models.py:get_hand()`（B4 訂正適用済）に固定
  - §4.1: データ実体ギャップ（ポジション未保持・hole_cards null・pots backend 依存）を明示
  - §4.2: `ui/review/` → `gui/hand_review.py`（既存規約整合）
  - §4.6: ADR-0040/0041/0042 を起票約束
  - §6.1 / §6.2: dogfood 規模（N=5〜10, X=8 週）と KPI 暫定閾値を本提案に内包
  - §7 / §R4: Pio 差分 3 アーキタイプの手動 QA を M2 → M1 へ前倒し
  - §R2: 10 ハンド → 10 アーキタイプに精緻化
  - §R5: 「自社ソルバー実装」フォールバックを削除（非現実的）
  - §1.1: 自店ユーザー層と「上達志向プレーヤー」の適合性チェックを追加
  - §3: B4 未訂正ハンドへの GTO 重ね表示を非目的化

以上。レビューでの指摘・反論を歓迎する。
