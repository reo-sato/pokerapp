# ADR-0013: S3 — point 残高の source of truth は point ledger の fold + ledger 永続化方式

## Status

Accepted

## Date

2026-06-10

## Context

S3（ledger entries + point ledger）の着手には ISSUE-0001（point 残高の計算と source of truth が
未確定）の決着が前提だった（CLAUDE.md § Phase 3 Blockers）。残高は buy-in / rebuy / add-on /
order への point 充当上限を決める値であり、UI 表示・entry validation・session 集計のすべてが
同じ値を参照する必要がある。ISSUE-0001 では次の 3 案が open だった:

- **A**: `point_ledger_entry` を全件 fold して残高を都度算出（source = ledger そのもの）。
- **B**: `player` に `current_point_balance` を物理化し、entry 追加時に同期更新。
- **C**: session 単位の集計テーブルに残高を持ち、session を跨ぐ場合は繰越 entry を生成。

また S3 実装には ledger の永続形・採番・冪等性・grant/spend の同一 session 同居可否
（ISSUE-0001 Reproduction 1〜4）の確定も必要だった。

前提となる本プロジェクトの永続化スタイル: S1 / S2 と同じく **単一 JSON ファイル +
アトミックリネーム + 起動時全件メモリロード**（`players.json` / `sessions.json`）。対象規模は
小規模クラブ・個人配信（1 session あたり高々数百 entry）。

## Decision

1. **残高の source of truth は A 案（fold）とする。**
   `point_balance(player_id)` = 当該 player の `point_ledger_entry.delta_points` の総和。
   cached 残高カラム（B 案）・session 別残高テーブル（C 案）は持たない。
   残高は **player に global**（session を跨いで持ち越す）であり、繰越 entry は不要。
2. **計算者は core（`LedgerRepository`）のみ**。front-end は `point_balance()` の結果を
   表示するだけで、残高計算・分割ロジックを再実装しない（Reproduction 論点 1 の決着）。
3. **spend 系 `point_ledger_entry` は core が同時生成する。**
   point 充当付き `ledger_entry`（`point_amount > 0`）の追加時に、core が
   `spend_on_*` entry（`delta_points = -point_amount`, `related_ledger_entry_id` back-link 付き）を
   atomic に併記する。front-end が spend entry を直接書く API は提供しない。
4. **point 不足は strict reject + `plan_payment` による cash 補完。**
   `add_entry` は `point_amount > 残高` を `insufficient_points` で拒否する（黙って減額しない）。
   業務ルール 3（不足分は cash 補完）の分割計算は `plan_payment(player_id, total_amount)` が
   core 側で行い、`(cash_amount, point_amount)` を返す。
5. **grant の冪等性は任意の `idempotency_key` の一意性で担保する**（Reproduction 論点 4）。
   同一キーの再 grant は `duplicate_grant` で拒否。キーなし grant は重複チェック対象外
   （手入力の同額 2 回は正当な別 entry）。
6. **同一 session 内の grant → spend 同居は可**（Reproduction 論点 3）。
   残高は entry 列の記録順 fold なので、`result_credit` 付与直後に同 session の buy-in へ
   充当できる。
7. **中間集計（`session_totals`）は途中スナップショットであり確定値ではない**
   （Reproduction 論点 2）。確定は S4 `session_settlement` のみが行う。
8. **永続化は専用ストア `ledger.json`**（`.gitignore`）。
   `{"ledger_entries": [...], "point_ledger_entries": [...]}` のフラット 2 配列 +
   アトミックリネーム。`entry_id` は UUID4 hex をアプリ内採番（shared-ids 原則と同じ）。
9. **`kind` enum に `entry_fee` を追加する**（future scope 草案の 5 種に additive）。
   業務ルール 1（entry fee は cash only）を ledger 上で enforce 可能にし、S4 settlement の
   `entry_fee` 集計の入力にする。`entry_fee` / `adjustment` への point 充当は拒否
   （point の補正は `adjust_points` を使う）。

## Alternatives Considered

- **B — `current_point_balance` の物理化**
  - Pros: 読み出し O(1)。
  - Cons: ledger と残高の **二重書き込み**になり、片方の書き込み失敗で不整合が出る。
    再計算ジョブ・修復手順の設計が必要。
  - Why rejected: 本プロジェクトは全件メモリロード方式なので fold は実質 O(n) の
    インメモリ走査で済み、O(1) 化の利益がない。整合リスクだけが残る。
    将来エントリ数が問題になれば **derived cache を additive に**追加できる
    （契約は `point_balance()` の結果のみで、算出方式は実装詳細）。
- **C — session 別残高 + 繰越 entry**
  - Pros: session 終了の確定処理と整合させやすい。
  - Cons: 繰越 entry の自動生成ルール・冪等性が必要になり、残高が「player の属性」でなく
    「session の属性」になって ledger の意味が複雑化する。
  - Why rejected: 残高は player に global という業務実態（次回来店時に使える）に合わない。
    session 単位の確定は S4 settlement の責務であり、残高に持ち込まない。
- **永続形の代替: `sessions.json` への入れ子**
  - Why rejected: `ledger_entry` は session 配下に置けるが、`point_ledger_entry` は
    player-scoped・session 横断なので置き場がない。2 model を 1 ストアに分離して持てる
    専用ファイルの方が単純（S1/S2 と同じ 1 ストア 1 関心事）。
- **auto-split API（`add_entry` が不足分を黙って cash に倒す）**
  - Why rejected: 記録される金額が呼び出し時の引数と変わるのは事故のもと（UI 表示と
    記録の乖離）。分割は `plan_payment` で事前計算し、`add_entry` は strict に保つ。

## Consequences

- Positive: 監査性が最大（残高は常に entry 列から再現可能）。二重書き込み不整合が
  構造的に発生しない。ISSUE-0001 の 4 論点がすべて決着し S3 contract draft を作成できた。
- Negative / trade-offs: 残高参照のたびに当該 player の entry 走査が走る（対象規模では
  無視できる。将来は derived cache を additive に追加可能）。
- Neutral / new constraints: `kind` に `entry_fee` が増えた（future scope 草案との差分は
  contract / CLAUDE.md に反映済み）。spend entry は core 生成のみで、`ledger_entry` と
  `point_ledger_entry` の整合は書き込み時に repository が保証する（hand_ref の denormalize と
  同種の drift 注意点）。

## Validation / Follow-up

- [x] `tests/test_point_ledger.py` — ISSUE-0001 予告の 4 回帰テスト + 同一 session 同居
- [x] `tests/test_ledger_repository.py` — CRUD / validation / persistence roundtrip / 中間集計
- [x] `tests/test_contracts.py` — schema↔fixture 整合 + code↔contract drift（2 model 追加）
- [ ] S4: settlement 確定時の `result_credit` 付与フロー（idempotency_key の規約化）
- [ ] schema `1.0` freeze（S2 session schema の freeze と依存順を揃える。現状は draft 0.1）
- [ ] desktop（WS2）/ mobile mock（WS3）の ledger 画面は後続

## Related Files

- `core/ledger.py` / `core/ledger_repository.py`
- `docs/contracts/ledger-points.md`
- `docs/contracts/schemas/{ledger_entry,point_ledger_entry}.schema.json`
- `docs/contracts/fixtures/{ledger_entry,point_ledger_entry}/`

## Related Tests

- `tests/test_point_ledger.py`
- `tests/test_ledger_repository.py`
- `tests/test_contracts.py::test_core_ledger_matches_contract`

## Related Commits

- S3 core 実装コミット（worklog `2026-06-10-s3-ledger-points-core.md` 参照）

## Supersedes / Superseded by

- Supersedes: —（ISSUE-0001 を Resolved にする決定）
- Superseded by: —
