# ADR-0044: ハンドリプレイ UI — mobile/staff 共有コンポーネント（copy-sync 方式）+ staff hands read

- **Status**: Accepted
- **Date**: 2026-07-12
- **Related**: ADR-0017（viewer API / mobile）/ ADR-0021（staff shared token）/ ADR-0036（ハンド訂正
  オーバーレイ）/ ADR-0037（staff iPad app）/ ADR-0038（staff API 拡張）/
  提案 `docs/proposals/2026-06-26-hand-review-integration.md` §9.3（既存ハンドリプレイ UI ゼロ）

## Context

player 向け mobile（`mobile/`）のハンド詳細は「アクションのテキスト行を時系列に並べただけ」で、
ビジュアルなハンドリプレイ UI は存在しない（提案 rev.1 §9.3 でも確認済み）。staff iPad app
（`staff/`）にはハンド履歴 read 自体がない（CLAUDE.md WS4 の残作業 = staff hands-list endpoint）。

ハンドリプレイ UI（GGPoker のハンドヒストリー画面を参考にした **ストリート単位表示**）を
player 用 mobile と staff 用 iPad の **両方** に載せたい。両アプリは Expo/RN だが
**完全に独立した npm プロジェクト**（workspace なし・metro.config.js なし）であり、
`useAsync` / `common.tsx` を既に重複実装している。

将来のソルバーレビュー（提案 M1, Phase A 通過後）でも「ハンドの構造化表示」は入力 UI の
土台になるため、リプレイ表示ロジック（ストリート分割・ボードスライス・ポット境界）は
1 箇所に置いて回帰テストで固定したい。

## Decision

### D1: 共有方式 = canonical source + copy-sync + drift test（monorepo 化しない）

- 正本はプロジェクト直下 **`shared/hand_replay/`**（TS ソース: `handReplayModel.ts` /
  `HandReplay.tsx` / `handReplayModel.test.ts`）。
- `python scripts/sync_shared_ui.py` が正本を `mobile/src/shared/hand_replay/` と
  `staff/src/shared/hand_replay/` に **バイト同一コピー** する（生成ヘッダ等の加工なし）。
- `tests/test_shared_ui_sync.py`（pytest = CI）が正本とコピーの **バイト同一性を検証** し、
  drift を CI で落とす。コピー側を直接編集した場合もテストが落ちる（正本を編集 → sync が正規手順）。

**理由**: 両アプリを monorepo 化（npm workspaces + metro `watchFolders` +
`resolver.nodeModulesPaths` + tsconfig `paths`）すると、tsc / metro native / web export /
tsx tests / Playwright E2E の 5 系統すべてに設定変更が波及し、実機・実 export で検証できない
環境では回帰リスクが大きい。copy-sync は各アプリが完全に self-contained のまま
（`npm run typecheck` / `test` / `export:web` は無改変で動く）、単一ソース性は CI の drift test で
担保できる。これは本リポジトリの contract-first / drift-test 文化（`tests/test_contracts.py`,
ADR-0005）と同型のパターンである。

**却下した代替案**:
- *monorepo 化*: 上記のとおり 5 系統のビルド設定に波及。得られるのは「コピー不要」だけで釣り合わない。
- *手動重複（現状の useAsync 方式）*: リプレイロジックは分岐が多く、無検証の重複は必ず drift する。

### D2: 共有コンポーネントは両アプリの型・スタイルに依存しない

- 入力型は `shared/hand_replay/` 内で **構造的に自前定義**（`ReplayHand` / `ReplayAction` /
  `ReplayPlayer`）。hand schema `1.0`（`docs/contracts/schemas/hand.schema.json`）の
  サブセットで、mobile の `HandSummary` / staff の hands read 応答をそのまま渡せる。
- スタイルも自前（両アプリ共通のダークトーンに合わせた自己完結 StyleSheet）。
  `common.tsx` への依存を持たない（コピー先で import 解決が変わらないための必須条件）。

### D3: 表示はストリート単位（GGPoker ハンドヒストリー風）

- モデル層 `buildReplayModel(hand)` は純関数: アクション列を street（preflop/flop/turn/river）で
  分割し、各 street に「その時点のボード（最終 board の先頭 3/4/5 枚スライス）」
  「開始時ポット（前 street 最終 action の `pot_after`、preflop は 0）」「終了時ポット」を付ける。
  showdown/結果セクション（winner / pots main-side / 各 player の net result）を末尾に付ける。
- ホールカードは **記録がある席は全員分表示**（ライブで卓上に公開された情報の記録という前提。
  `hole_cards=null` は「不明」表示）。
- カードは `As` → スート記号 + 色（♠♣=白 / ♥=赤 / ♦=青系）のチップ描画。
- `needs_review` / `corrected`（ADR-0036 オーバーレイ痕）はアクション行にバッジ表示する。
- BTN/ポジション推定は **scope 外**（席番号のみ。推定はソルバー M1 の
  `spot_config_builder` で扱う。silent failure 源をリプレイ UI に持ち込まない）。

### D4: staff hands read = `GET /api/staff/sessions/{session_id}/hands`

- staff read（`_staff_guard(need_write=False)` = staff token 必須、read-only プロセスでも可）。
- 応答は `{"hands": [HandSummary...]}`（hand_id 昇順、**訂正オーバーレイ適用済み** = ADR-0036。
  viewer の player read と同じ「ユーザー可視の最終状態」を返す）。
- read model は `api/read_models.py:list_session_hands(session_id, log_dir, correction_repo)`
  （player 縛りなしの全ハンド版。seat assignment との join はしない — staff は卓の全ハンドを見る）。
- log 不在 / session 不在は空 list（`list_measurement_rows` と同じ gracefully-empty）。
- Python client は `ViewerApiClient.list_session_hands(session_id)`。
- schema / error code の新設なし（hand schema `1.0` の read 再利用のみ）。

## Consequences

- mobile の HandDetailScreen はテキスト行表示を `HandReplay` に置き換える（訂正導線・自分の
  収支表示は維持）。staff のハンドタブに履歴一覧（hand_id / 勝者 / pot）→ タップでリプレイ表示を追加。
  CLAUDE.md WS4 残作業の「ハンド履歴 read（staff hands-list endpoint）」が解消する。
- 共有ソースの編集手順が 1 段増える（正本編集 → `scripts/sync_shared_ui.py` 実行）。
  drift test が CI で強制するため、手順漏れは merge 前に検出される。
- 将来ソルバーレビュー UI（M1）が RN 側に必要になった場合、`buildReplayModel` を土台に
  GTO 重ね表示を additive に足せる。desktop `gui/hand_review.py`（customtkinter）を選ぶ場合も
  モデル層のセマンティクス（street 分割・ボードスライス）を移植参照できる。
