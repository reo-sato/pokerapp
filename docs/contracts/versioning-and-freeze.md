# Versioning, freeze, and drift detection

contract-first 並行開発で **contract drift**（ISSUE-0003）を防ぐための、freeze / versioning /
互換性 / drift detection の運用ルール。

## 1. freeze の定義

ある model の契約が **freeze 済** とは、以下が揃った状態を指す。

1. `schemas/<model>.schema.json` が commit され、`$id` と `version` を持つ。
2. `fixtures/<model>/` に canonical / valid-minimal / invalid-* が揃い、contract test が通る。
3. その freeze を承認する ADR が **Accepted** であり、`docs/decision-log.md` に登録済。

freeze 前（Proposed 段階）は schema・fixtures を **自由に変更してよい**。
freeze 後は下記の additive / breaking ルールに従う。

## 2. additive change vs breaking change

freeze 後の変更は互換性で分類する。

### additive（後方互換, ADR 不要・worklog で可）

- **optional** フィールドの追加（`required` に入れない）。
- enum 値の **追加**（consumer が unknown 値を無視できる前提）。
- 説明・description・examples の追加。
- 新しい invalid fixture の追加（より厳しい検査の明文化ではない範囲）。

### breaking（後方非互換, **新 ADR または issue 必須** + version bump）

- フィールドの **削除** / リネーム。
- optional → required への変更、型の変更。
- enum 値の **削除** / 意味変更。
- `additionalProperties` ポリシーの厳格化で既存 valid fixture が落ちる変更。
- 共有 ID（player_id / session_id / hand_id）の形式・採番・一意性スコープの変更。

breaking change は必ず ADR（または重い場合は issue → ADR）を起こし、旧契約を `Superseded` とする。

## 3. versioning

- 各 schema は `"version"` フィールド（`MAJOR.MINOR`）を持つ。
  - **MINOR**: additive change で +1。
  - **MAJOR**: breaking change で +1（MINOR を 0 に戻す）。
- `$id` は `https://pokerapp.local/contracts/<model>.schema.json` の論理 URI（解決はしない、識別用）。
- fixtures は schema の **現行 version に対して** 妥当であること。MAJOR 上げ時は旧 version 用の
  backward-compatibility fixture を残してよい（`fixtures/<model>/compat-vN-*.json`）。

## 4. null と absent / required と optional

- **required**: 永続・転送される正規形に必ず存在するフィールド。欠けたら invalid。
- **optional**: 省略（absent）可能。**absent と null は区別する**。
  - *absent* = 「この情報を持たない / 未設定」。
  - *null* = 「明示的に値なし」を表したい場合のみ schema で `"type": ["X", "null"]` を許可する。
  - 区別が不要なフィールドは null を許可せず absent のみとする（曖昧さを避ける）。

schema では表現しきれない業務 validation（重複・残高・cross-field 制約）は core が source of
truth で、`validation-rules.md` に明文化する。

## 5. schema 更新時の同時更新ルール

schema を変更する PR は、同じ PR で以下を更新する（漏れは contract test / レビューで弾く）。

- [ ] `schemas/<model>.schema.json`（`version` 更新含む）
- [ ] `fixtures/<model>/`（canonical / valid / invalid を新形に合わせる）
- [ ] 関連 docs（`shared-ids.md` / `validation-rules.md` / `error-shapes.md` / CLAUDE.md 該当箇所）
- [ ] front-end stub / mock への影響確認（desktop repository, mobile mock）
- [ ] breaking なら ADR + `decision-log.md`

## 6. freeze order（依存順）

契約は依存順に凍結する。これは **凍結の順序** であって、各 phase 内の実装並行性は妨げない
（CLAUDE.md § Parallel development plan 参照）。

| # | 契約 | phase | 状態 |
|---|------|-------|------|
| 1 | shared IDs（player_id / session_id / hand_id） | 全 phase 共通 | bootstrap 済（player_id 確定、hand_id は ADR-0006 で確定、session_id 採番は ISSUE-0005） |
| 2 | player schema | S1 | freeze 候補（本 bootstrap で schema/fixtures 追加） |
| 3 | session / seat_assignment / hand_ref | S2 | **draft（schema/fixtures/ADR-0006 追加, version 0.x）**。freeze は ISSUE-0005 決着 + S2 core 実装後 |
| 4 | ledger_entry / point_ledger_entry | S3 | **draft（schema/fixtures/ADR-0013 追加, version 0.x）**。ISSUE-0001 は ADR-0013 で決着、core 実装済。freeze は上流 session schema freeze（ISSUE-0005）後 |
| 5 | session_settlement | S4 | planned |
| 6 | repository / service interface, sync | S5 | planned |

## 7. drift detection（最小方針）

ISSUE-0003 対策の最小実装。

- **schema↔fixture 整合の自動検証**: `tests/test_contracts.py` が
  - すべての `fixtures/<model>/` を対応 schema に照合し、
  - `canonical` / `valid-*` は **通過**、`invalid-*` は **必ず違反** することを assert する。
  - schema 自身が valid JSON で `$id` / `version` を持つことを確認する。
- **CI での扱い**: contract test は通常の pytest スイートに含める。`jsonschema` 未導入環境では
  `pytest.importorskip` で skip し、導入環境では必ず実行する（CI には `jsonschema` を入れる）。
- **影響確認の要求**: schema 変更 PR では §5 のチェックリストを満たすこと。front-end mock が
  古い契約のまま緑にならないよう、mock も同じ fixtures を読む方針にする。

### 将来拡張余地（今回は scope 外）

- consumer-driven contract testing（desktop / mobile が期待する形を契約に取り込む）。
- JSON Schema を single source として OpenAPI / TypeScript 型を生成し、生成物の drift も検査する。
- fixtures に対する property-based / round-trip（serialize→deserialize→serialize）検査。

これらは S5（sync / cross-app hardening）前後で検討する。今回は schema↔fixture の最小整合に留める。
