# ADR-0004: Contract-first parallel development with shared IDs and separate front-ends

## Status

Accepted

## Date

2026-05-22

## Context

本プロジェクトは今後、以下を **並行的** に育てる方針が確定した。

- hand logger core（既存）
- desktop の別画面 registry / ledger / settlement UI
- iOS / Android 用の別アプリ（将来の別 front-end）

複数 front-end を同時に動かす際、各 front-end が core 実装の完成を待ってから着手すると
逐次化してしまい、並行性が失われる。また各 front-end が validation や残高計算などの業務ルールを
独自実装すると、source of truth が分散して整合が崩壊する。

制約として以下が確定している（CLAUDE.md § Future Scope / § Parallel development plan）。

- `player_id` / `session_id` / `hand_id` は全 front-end / core で共有する安定キー。
- hand logger の既存 UI (`gui/dashboard.py`) は汚さない。registry / ledger は別画面。
- mobile は hand logger の置き換えではなく、将来の別 front-end。最初は mock で進めてよい。
- 物理配置（同一プロセス → 別プロセス → 別アプリ + API/sync）は段階移行する。

関連:

- ADR-0003（domain をハンドログから session ledger / settlement へ拡張）
- `docs/issues/0003-parallel-dev-contract-drift.md`
- `docs/worklog/2026-05-22-parallel-dev-plan.md`

## Decision

**Contract-first parallel development** を採用する。

1. **契約を先に凍結する（WS0 が常に上流）**。各 domain model の schema・共有 ID・validation・
   error 形・repository/service interface を、実装より先に凍結する。契約は `docs/contracts/`
   （planned）にサンプル fixtures とともに置き、凍結・更新手順を WS0 が管理する。
2. **4 workstream に分離する**: WS0 (contract) / WS1 (core) / WS2 (desktop) / WS3 (mobile)。
   ある model の契約が凍結したら、WS1/WS2/WS3 はその model について **並行実装** できる。
3. **core (WS1) を業務ルールの唯一の source of truth とする**。validation・残高計算・
   settlement 確定は core の repository/service にのみ置き、front-end に複製しない。
4. **front-end は repository interface にのみ依存する**。具象（local 実装 / mock / API client）は
   注入で差し替える。UI は「データがどこにあるか」を知らない。
5. **共有 ID は不変・アプリ内採番**。`player_id` / `session_id` / `hand_id` は文字列の安定キーで、
   sync / API 導入後も backend を跨いで不変。外部システム前提の採番を作らない。
6. **front-end は対等な別画面 / 別アプリ**。desktop registry/ledger は hand logger とは別画面、
   mobile は別アプリ。hand logger 既存 UI は改変しない。
7. **mobile は mock で先行する**。WS0 の契約と fixtures だけを使い、WS1 完成を待たずに
   screen skeleton と状態管理・validation 表示を作る。後で実 repository に差し替える。

契約凍結順序（依存順）は S1(player) → S2(session/seat) → S3(ledger/point) → S4(settlement) →
S5(sync)。ただしこれは「契約凍結の順序」であって、各 phase 内の実装並行性は妨げない。

## Alternatives Considered

- **Alternative A — core を完成させてから front-end に着手（core-first / 逐次）**
  - Pros: front-end が安定した実 API に対して書ける。手戻りが少ない。
  - Cons: mobile / desktop が core の各 phase 完成を待つため、完全に逐次化し並行性が消える。
    mobile たたき台の検証（UX / 画面遷移）が大きく遅れる。
  - Why rejected: 並行的に育てるという本タスクの目的に真っ向から反する。

- **Alternative B — front-end ごとに業務ルールを実装（front-end-owned logic）**
  - Pros: 各 front-end が自己完結し、core への依存が薄い。短期的には速い。
  - Cons: validation / 残高計算 / settlement が desktop と mobile で二重実装され、必ず乖離する。
    business rule の source of truth が分散し、整合性バグの温床になる。
  - Why rejected: ADR-0003 の business rules（cash-only entry fee / point 不足 cash 補完 等）を
    一箇所で enforce できなくなる。

- **Alternative C — schema を後から固める（implementation-first / 暗黙契約）**
  - Pros: 初期の自由度が高い。とりあえず動かせる。
  - Cons: 各 WS が暗黙の前提で実装を進め、結合時に schema / ID / error 形が食い違う
    （contract drift）。mobile が mock で先行する前提が成立しない。
  - Why rejected: 並行開発では結合時の手戻りが致命的。contract-first の利点を全て失う。

- **Alternative D — mobile を hand logger の移植（mobile = hand logger 再実装）**
  - Pros: 単一プロダクトとして一貫する。
  - Cons: ユーザー要件（mobile は hand logger の置き換えではなく将来の別 front-end）に反する。
    リアルタイム音声/RFID 統合を mobile に載せる重い前提を作ってしまう。
  - Why rejected: スコープと責務分離の方針に反する。

## Consequences

- Positive
  - WS1/WS2/WS3 が同一 model について並行進行でき、mobile が WS1 完成を待たずに進む。
  - business rule の source of truth が core に一本化される。
  - repository interface 境界により、将来の API/sync 移行で front-end が壊れにくい。

- Negative / trade-offs
  - 契約凍結（WS0）が常に先行する分、初動の段取りコストが増える。
  - 契約変更時は全 WS に波及するため、変更管理（drift 検出）が必要（`docs/issues/0003`）。
  - mock と実 repository の二重メンテが mobile に一時的に発生する。

- Neutral / new constraints
  - `docs/contracts/` を新設し、schema/fixtures をそこで管理する（planned）。
  - front-end は repository interface 以外から core 具象を直接 import しない、という規律が要る。

## Validation / Follow-up

- [ ] Phase 0 で `docs/contracts/` の置き場所・凍結プロセス・共有 ID 契約 ADR を確定する。
- [ ] repository / service interface のテンプレート契約を定義する。
- [ ] contract drift 検出の仕組み（fixtures に対する schema テスト等）を `docs/issues/0003` で追う。
- [ ] mobile 技術選定（React Native 案）を Phase 1 着手時に別 worklog で確定する。
- [ ] S3 着手前に ISSUE-0001（point 残高 source of truth）を解決し残高計算 API 契約を凍結する。

## Related Files

現時点では計画のみ（spec/docs）。実装着手時の想定:

- `docs/contracts/`（planned, WS0）
- `core/*`（WS1）
- `gui/*`（WS2、別画面）
- mobile プロジェクト（WS3、別リポジトリ or サブディレクトリは Phase 1 で決定）

## Related Tests

現時点ではテストなし（計画）。契約テスト（fixtures に対する schema 検証）を Phase 0 で導入予定。

## Related Commits

- 本 ADR と同じコミット（parallel development planning）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
- 関連: ADR-0003（本 ADR は ADR-0003 のドメイン分割を「並行開発の実行計画」として具体化する）
