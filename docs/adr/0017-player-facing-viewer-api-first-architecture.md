# ADR-0017: Player 向け参照は API-first（viewer API 前倒し）+ Expo（web export 先行）

## Status

Accepted

## Date

2026-06-10

## Context

最終目標は「プレイヤーが自分のスマホからハンドログ・会計情報を参照し、将来はドリンク注文まで
一元管理できるモバイルアプリ」。この front-end をどこから着手するかが論点。

- プレイヤーは運営 PC に触れない。customtkinter による「player 向け desktop viewer」は、最終形に
  必要なアーキテクチャ（API 境界・player 別 read model・HTTP error shape）を何も検証しない
  使い捨てになる。
- 最終形で最もリスクが高い部品は、S5 に予定されていた **cross-process boundary（API 化）**。
- ADR-0004 は WS3（mobile）が WS0（契約）のみに依存し WS1/WS2 を待たないことを既に許容している。
  ただし WS3 の技術選定（React Native 案）は「Phase 1 着手時に確定」と保留されていた。
- 接続前提のデータ経路には gap がある: E3（seat 選択 GUI + main.py 結線, ISSUE-0006）が未了のため、
  実運用では hand log / sessions.json に player_id がまだ流れない。

## Decision

1. **player 向け desktop viewer は作らない**。desktop（WS2）は今後もスタッフ操作用
   （seat 選択 / ledger 入力 / settlement）に限定する。
2. **S5 の read-only サブセットを前倒し**し、運営 PC 上に読み取り専用の **viewer API**
   （`api/` パッケージ）を置く。契約は `docs/contracts/viewer-api.md`（M1, draft 0.x）。
3. **API 実装は FastAPI** を採用し、`pyproject.toml` の **optional extra `[api]`** に隔離する
   （core 依存は増やさない）。fastapi 未導入で API 起動を要求された場合は警告して落とさない
   （pokerkit fallback と同方針）。
4. **WS3 の技術選定を確定: Expo（React Native, TypeScript）**。ただし配布は当面
   **`expo export --platform web` の Web ビルドを LAN 配信（QR コード）** とし、App Store /
   Play Store 配布（ネイティブビルド）は同一コードベースの将来オプションとして残す。
5. **bind/security 既定は rfid receiver の前例に従う**: `viewer_api.bind_host` 既定 `127.0.0.1`、
   LAN 公開は明示変更（無認証のため信頼できるネットワークのみ）。M1 は GET のみ（read-only）。
6. **read model の規則**: 「player X のハンド」は `sessions.json` の seat_assignment
   （ADR-0008 の source of truth）から導出し、hand log（`logs/{session_id}.json`）を
   `(session_id, hand_id)` で join する。hand log 内の `players[].player_id` は best-effort 扱い。
   E3 着地前は gracefully-empty を返し、着地後に自動的に実データが流れる。

## Alternatives Considered

- **A: desktop 別画面（customtkinter viewer）から着手**
  - Pros: 既存スタックで最速・依存追加なし。
  - Cons: プレイヤーが触れない場所に viewer ができる。API 境界を何も検証せず、mobile 化で全捨て。
  - Why rejected: 最終形（mobile）への経路に乗らない。
- **B: React Native ネイティブ配布のみ（web export なし）**
  - Pros: ネイティブ機能（push 通知等）に最短。
  - Cons: 小規模クラブに App Store / TestFlight / APK 配布の運用コストが過大。更新も遅い。
  - Why rejected: v1 の参照ユースケースに push は不要。QR + ブラウザが配布摩擦最小。
- **C: 素の React PWA（Expo を使わない）**
  - Pros: web 専用なら最軽量。
  - Cons: CLAUDE.md の WS3 提案（RN/Expo）と乖離し、将来のネイティブ化で書き直しになる。
  - Why rejected: Expo の web export で「今は web 配信、後で同一コードをネイティブ化」が成立する。
- **D: API を stdlib http.server で実装（rfid receiver と同様）**
  - Pros: 依存ゼロ。
  - Cons: path param ルーティング / CORS / error shape 変換 / 将来の POST（注文）を手書きする
    ことになり、リポジトリ内で最もリスクの高いコードになる。
  - Why rejected: FastAPI は OpenAPI を自動生成し contract-first と整合。依存は `[api]` extra に隔離。

## Consequences

- Positive: 最終形に必要な API 境界が最初から検証される。mobile（M2）は契約 + fixtures だけで
  mock 先行でき、API client 差し替えで実データに接続できる。S5 での boundary 切り出しが前進する。
- Negative / trade-offs: optional とはいえ fastapi/uvicorn という新しい依存系列が増える。
  E3（ISSUE-0006）が終わるまで実運用データは空（viewer の製品価値は M3 で発現）。
- Neutral / new constraints: viewer API の応答は既存 schema（player 1.0 / hand 1.0 / session 0.x）を
  envelope するだけで新 domain model を作らない。無認証のため LAN 公開は明示 opt-in
  （プライバシーモデルは ISSUE-0019 で追跡）。

## Validation / Follow-up

- [x] `tests/test_viewer_read_models.py` / `tests/test_viewer_api.py`（error shape / schema 適合含む）
- [ ] M2: Expo scaffold（mock → API client 差し替え）で契約越しに同一画面が動くこと
- [ ] M3（= E3, ISSUE-0006）後に実データ E2E（session_layer.enabled=true で 1 session 流す）
- [ ] ledger（verify-v1 = cash+point/settlement, ADR-0016）→ 注文 write path（ADR-0018）→ ISSUE-0001（point gate）→ settlement

## Related Files

- `docs/contracts/viewer-api.md`
- `api/read_models.py` / `api/server.py`
- `config_default.json`（`viewer_api` セクション）/ `pyproject.toml`（`[api]` extra）
- `main.py`（`--viewer-api`）

## Related Tests

- `tests/test_viewer_read_models.py`
- `tests/test_viewer_api.py`

## Related Commits

- （M1 実装 commit を参照）

## Supersedes / Superseded by

- Supersedes: —（ADR-0004 を補完。WS3 技術選定の保留を確定し、S5 read-only サブセットを前倒し）
- Superseded by: —

> 統合メモ（2026-06-13）: 本 ADR は serene ブランチで ADR-0013 として起票されたが、
> verify-v1 統合時に ADR-0013（point 残高 fold, Superseded）と番号衝突するため **ADR-0017 に採番替え**。
> ledger 周りの参照は verify-v1 の cash+point/settlement 実装（ADR-0016）に読み替える。
> 詳細は `docs/worklog/2026-06-13-integrate-viewer-onto-verify-v1.md`。
