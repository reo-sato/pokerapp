# Contracts (contract-first development)

このディレクトリは **contract-first 並行開発** の単一 source。
core (WS1) / desktop (WS2) / mobile (WS3) は、ここで凍結された契約に対して **独立に** 実装する。

> 位置づけ: 本ディレクトリは ADR-0004（contract-first parallel development）と
> ADR-0005（contracts repository layout & freeze workflow）の具体物。
> CLAUDE.md の `# Parallel development plan` と併せて読むこと。
>
> **現時点で実装済なのは hand logger core と S1 player registry のみ。**
> session / ledger / point / settlement / mobile / sync の契約はすべて
> **planned / future scope** であり、ここに schema を置いても「実装済」を意味しない。

## このディレクトリの役割

- 並行開発の主リスクである **contract drift**（各 workstream が別々の前提で進み結合時に食い違う）を
  防ぐため、schema・共有 ID・validation・error 形・repository interface を **実装より先に固定** する。
- front-end（desktop / mobile）は、core 実装の完成を待たずに、ここの schema と fixtures だけを見て
  mock 実装・画面・状態管理・validation 表示を作れる。

## ディレクトリ構造

```
docs/contracts/
├── README.md                   ← このファイル（置き場と運用の入口）
├── shared-ids.md               ← player_id / session_id / hand_id の共有 ID 契約
├── session-seating.md          ← session / seat_assignment / hand_ref 契約（S2 draft, 未 freeze）
├── ledger-overview.md          ← ledger / point / settlement ドメイン + invariants 契約（S3 draft, 未 freeze）
├── ledger-schema.md            ← ledger / point / settlement の擬似 JSON Schema 草案（S3 draft）
├── versioning-and-freeze.md    ← freeze / versioning / additive vs breaking / freeze order
├── repository-interfaces.md    ← front-end が呼ぶ抽象 repository/service interface 契約（テンプレート）
├── error-shapes.md             ← validation / not-found 等の error 形契約
├── validation-rules.md         ← schema で表現しきれない業務 validation（core が source of truth）
├── schemas/                    ← 1 model = 1 JSON Schema
│   ├── shared-ids.schema.json      ← 共有 ID の文字列形式 $defs（参照用・documentary）
│   ├── player.schema.json          ← player schema（S1, freeze 候補, v1.0）
│   ├── session.schema.json         ← session schema（S2 draft, v0.1）
│   ├── seat_assignment.schema.json ← seat_assignment schema（S2 draft, v0.1）
│   └── hand_ref.schema.json        ← hand_ref schema（S2 draft, v0.1）
└── fixtures/                   ← schema に対するサンプル。contract test の oracle
    ├── player/                 ← canonical / valid-* / invalid-*
    ├── session/                ← canonical / valid-minimal / invalid-*（S2 draft）
    ├── seat_assignment/        ← canonical / valid-minimal / invalid-*（S2 draft）
    └── hand_ref/               ← canonical / valid-minimal / invalid-*（S2 draft）
```

## 運用ルール（要約）

1. **1 model = 1 schema**。`schemas/<model>.schema.json`、`fixtures/<model>/` に対応させる。
2. **schema を変えたら同じ PR で fixtures と docs も更新する**（CLAUDE.md § Documentation and
   Traceability Rules と同じ「コード・テスト・docs 一体」原則を contract にも適用）。
3. **freeze 前は変更自由、freeze 後は additive 原則**。breaking change は ADR か issue を要求する
   （`versioning-and-freeze.md` 参照）。
4. **fixtures は contract test の oracle**。`valid-*` / `canonical` は schema を通過し、`invalid-*` は
   必ず違反する。drift は `tests/test_contracts.py` で検知する（`versioning-and-freeze.md` §
   drift detection 参照）。
5. **採番はアプリ内で完結**。`player_id` / `session_id` / `hand_id` は外部システム前提を作らない
   （`shared-ids.md` 参照）。

## freeze order（依存順）

`versioning-and-freeze.md` に詳細。要点のみ:

1. shared IDs（最優先・全 phase 共通）
2. player（S1）
3. session / seat_assignment / hand_ref（S2）
4. ledger_entry / point_ledger_entry（S3, **draft** — `ledger-overview.md` / `ledger-schema.md` / ADR-0011）
5. session_settlement（S4, draft — ADR-0011, S3.3 で derived view 先行）
6. repository / service interface、sync（S5）

## 将来拡張余地

- 本 bootstrap は **JSON Schema (draft 2020-12)** を前提とする。将来 OpenAPI（API 化, S5）や
  mobile 側 typed models（TypeScript 等）へは、JSON Schema を single source として生成する方向で
  拡張する（contract-first を維持）。
- consumer-driven contract testing（front-end ごとの期待を契約に取り込む）は将来の選択肢。
  今回は schema↔fixture の最小整合検証に留める。
