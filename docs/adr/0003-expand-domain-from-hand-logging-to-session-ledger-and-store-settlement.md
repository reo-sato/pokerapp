# ADR-0003: Expand domain from hand logging to session ledger and store settlement

## Status

Accepted

## Date

2026-05-22

## Context

現状のプロジェクトは **hand logger 単機能** であり、`HandSummary` / `ActionRecord` で
ハンド単位のアクション履歴のみを記録している。一方、運営側の実需要として以下が浮上した。

- アプリ内で player を新規作成し、display_name で識別したい。
- 1 session 内で発生する **buy-in / rebuy / add-on / order / adjustment** を ledger として残したい。
- **prize point** を導入し、buy-in 等に充当できるようにしたい（不足分は cash 補完、entry fee は cash only）。
- session 終了時に **player ごとの「店への net 支払額」** を確定し、paid/unpaid を管理したい。
- player 間の精算（友人同士の割り勘等）は **扱わない**。常に「player → 店」のみ。
- session 中に **seat change** が実際に起きる。
- hand logger と ledger app は **将来別画面・別アプリ** になる前提で設計したい。

既存の hand summary 構造を拡張して全部詰め込む方向で進めると、hand-level の不変参照と
session-level の金銭イベントが同居して責務が崩壊する。本 ADR では、**ドメインを
複数モデルに分割し、cross-app 前提の共通 ID 契約を先に定義** する方針を採用する。
実装は spec 確定後に Phase S1〜S5 で段階導入する（CLAUDE.md の Phase candidates 参照）。

関連:

- `CLAUDE.md` § Future Scope（product scope / domain model / business rules / cross-app boundary / phase candidates）
- `docs/issues/0003-point-balance-source-of-truth.md`
- `docs/worklog/2026-05-22-spec-expand-session-ledger-scope.md`

## Decision

ドメインを以下のモデル群に分離し、hand logger（既存）と session/accounting レイヤ（新規）が
共通 ID で参照しあう構造を採用する。

1. **player** — `player_id` + `display_name` のみ。属性は最小限から始める。
2. **session** — 1 卓 1 回の運営単位。`started_at` / `ended_at` / `status` を持つ。
3. **seat_assignment** — **hand-based** にスナップショットする。session 中の seat change は
   hand 単位のスナップショット差分として表現する。
4. **hand_ref** — ledger 側から hand logger 側を参照する軽量参照（`hand_id` / `session_id`
   / `started_at` / `seat_assignments`）。hand logger と ledger app が別アプリ化されたあとも
   安定する。
5. **ledger_entry** — `buy_in` / `rebuy` / `add_on` / `order` / `adjustment` を共通 schema で記録。
   `order` のみ `item_name + amount` の明細を持つ。`cash_amount + point_amount` 両方を持ち得る。
6. **point_ledger_entry** — point の grant / spend。増加理由は `manual_grant` / `result_credit`
   / `campaign_grant` の 3 種類。
7. **session_settlement** — session 終了時に player ごと 1 行確定。**net due to store** と
   **paid/unpaid** のみを表現し、player 間精算は持たない。

Business rules:

- entry fee は cash only。
- buy-in / rebuy / add-on / order は cash + point 併用可。
- point 不足分は cash 補完。
- paid/unpaid は店への支払い完了のみを表す状態。
- player-to-player settlement は扱わない。

Cross-app boundary:

- `player_id` / `session_id` / `hand_id` を共通 ID として **先に契約定義** する。
- hand logger と ledger app は相互参照前提（双方向）。
- 物理配置（同一プロセス → 別プロセス → 別アプリ）は Phase ごとに段階移行する。

## Alternatives Considered

- **Alternative A — `HandSummary` に全部埋め込む**
  - Pros: ファイル数が増えない。既存 JSON ログをそのまま拡張できる。
  - Cons: hand 単位の不変参照（PHH エクスポート対象）と session 単位の可変金銭イベントが
    同居し、責務が崩壊する。再集計・精算のたびに hand JSON を書き換える必要が生じ、
    hand_id を不変参照として使えなくなる。
  - Why rejected: hand logger の安定性が ledger 機能の追加で巻き込まれる。cross-app
    boundary を後から切り出す難度が一気に上がる。

- **Alternative B — session ledger を後付けで別 JSON にする（設計を先送り）**
  - Pros: 初期コストが低い。とりあえず ad-hoc に JSON を増やせる。
  - Cons: cross-app 前提の共通 ID（`player_id` / `session_id` / `hand_id`）と
    seat_assignment が hand-based であるべきという制約を、後から強制するのが極めて困難。
    後付け JSON は schema が分散し、後で正規化する際に大きな移行コストが発生する。
  - Why rejected: 本 ADR は「Phase 着手前に共通 ID 契約を確定する」ことが核心で、後付けは
    その目的に真っ向から反する。

- **Alternative C — player-to-player settlement まで先に入れる**
  - Pros: 友人同士のセッションなどで便利。一般的な「割り勘」機能が付く。
  - Cons: 現場の実運用は **常に「player → 店」** であり、player 間の債権債務を扱うと
    別途 KYC / 出納・トラブル対応が必要になる。スコープが膨張し、本来必要な
    store settlement の確定が遅れる。
  - Why rejected: 現時点の運用要件に存在しない。必要になった段階で別 ADR を起こす。

- **Alternative D — seat_assignment を session 単位（hand-based ではなく）で持つ**
  - Pros: モデルが 1 階層浅くなる。
  - Cons: 実際に session 中で seat change が起きるため、session 単位だと「いつ移動したか」を
    表現できず、hand との照合ができなくなる。hand logger 側で seat → action を確定するためには
    hand-based の seat snapshot が不可欠。
  - Why rejected: 業務実態に反する。

## Consequences

- Positive
  - hand logger の安定性（hand_id を不変参照として使える）を維持しつつ、session/accounting を
    別レイヤとして増築できる。
  - 共通 ID 契約を先に定義することで、別アプリ化（S5）への移行コストが小さくなる。
  - business rules（point 不足は cash 補完 / entry fee は cash only / paid-unpaid 等）が
    ledger_entry / point_ledger_entry / session_settlement の責務として明確に分かれる。

- Negative / trade-offs
  - モデル数が増え、初期実装の認知負荷は高まる。
  - hand-based seat snapshot は冗長度が高い（多くの hand で seat_assignment は変わらない）。
    実装時にスナップショット差分のみ持つ最適化を検討する余地がある（S2 で issue を起こす）。

- Neutral / new constraints
  - ledger_entry に **cash_amount + point_amount を併存** させる schema を採用する。
  - point ledger の source of truth と残高計算の責任所在は確定していない（`docs/issues/0001` を参照）。
  - hand logger と ledger app の参照同期方式（pull / push / イベントバス）は別 ADR が必要。

## Validation / Follow-up

- [ ] S1 着手時に `player` データモデルと CRUD の最小実装を別 ADR/worklog で起こす。
- [ ] S2 着手時に `seat_assignment` を hand-based に持つ実装方針と、差分最適化の方針を ADR 化する。
- [ ] S3 着手時に point ledger の source of truth 問題（`docs/issues/0001`）を解決する ADR を追加する。
- [ ] S4 着手時に paid/unpaid の状態遷移と partial paid 対応の要否を確定する。
- [ ] S5 着手前に cross-app 参照同期方式（pull/push/event）の ADR を追加する。

## Related Files

現時点では実装ファイルなし（spec only）。実装着手時に以下が想定パス。

- `core/player.py`（planned, S1）
- `core/session.py`（planned, S2）
- `core/ledger.py`（planned, S3）
- `core/point_ledger.py`（planned, S3）
- `core/settlement.py`（planned, S4）

## Related Tests

現時点ではテストなし（spec only）。

## Related Commits

- 本 ADR と同じコミット（spec expansion phase）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
