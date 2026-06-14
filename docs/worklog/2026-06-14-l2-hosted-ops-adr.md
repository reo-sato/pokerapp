# Worklog: L2 hosted モードの運用設計 ADR（ADR-0029）

## Date

2026-06-14

## Scope / Task

ADR-0028（L2 外部 IdP）§D7 が委譲した運用判断を確定する運用 ADR を起こす。L2 実装の前提を固める
**運用設計記録のみ**（コードなし）。ユーザーに 3 点を確認して方針を決定した。

## Goal

L2（LINE/Google サインアップ）を実装する前に、ホスティング形態・会場↔cloud のトポロジー・
シークレット管理・PII/法令・攻撃面の運用方針を「実装着手リストが書ける」粒度で確定する。

## User Decisions（AskUserQuestion, 2026-06-14）

- トポロジー = **会場 source-of-truth + cloud は player ミラー**。
- ホスティング = **マネージド PaaS**。
- IdP = **LINE と Google 両方**。

## Changed Files

- `docs/adr/0029-l2-hosted-operational-design.md`（新規, Proposed）: D1 トポロジー（会場真実 / cloud は
  signup・閲覧・注文+sync のみ、会計 originate しない）/ D2 マネージド PaaS（ephemeral FS → persistent
  volume or DB backed, interface frozen で差し替え）/ D3 LINE+Google 登録 / D4 secret は PaaS env
  （player_token_secret 固定）/ D5 PII 最小（APPI, sub のみ、IdP トークン非保存、退会削除）/ D6 cloud は
  会計 write 無効・CORS 絞り・レート制限・ログ PII マスキング / D7 sync は会場主導 pull/merge。
- `docs/adr/0028-l2-external-idp-oidc-auth-design.md`: §D7 と Superseded-by 注記を ADR-0029 参照に更新。
- `docs/decision-log.md`: ADR-0029 行追加 + ADR-0028 行の前提を 0029 参照に。
- `CLAUDE.md`: 実装状況 L2 行 + 残作業 #7 を ADR-0029 反映に更新。
- `CHANGELOG.md`: Docs 節追加。

## Key Decisions（要点）

- cloud は **会計を originate しない read-mostly ミラー** → 侵害時も会計の真実は会場側で保全、
  オフライン可を維持。
- player registry（players.json）は双方向 sync、`auth_identity` / PIN credential は cloud-local（sync 非対象）。
  cloud signup の新規 player_id は sync で会場に現れ、**staff merge** で name-pick player と統合（前提・残）。
- 運用負荷最小の PaaS を採用、secret は env、PII は sub のみ（APPI）。

## Expected / Implemented Behavior

- 運用設計タスクのため挙動変更なし（コードなし）。ADR は Proposed（L2 実装は別タスク）。

## Test Results

- コード不変。`ruff check .` — clean（docs のみ）。

## Mismatches Found During Testing

- なし（コード変更なし）。

## Remaining Gaps / Out-of-Scope（L2 実装着手リスト = ADR-0029 §Validation）

- cloud 永続方針 / env override / CORS 絞り / cloud 会計 write 無効モード / レート制限・ログマスキング /
  IdP 登録 / プライバシーポリシー + 退会フロー / **player merge（前提・scope 外）** / OIDC 本体（ADR-0028）。

## Related ADRs / Issues

- ADR-0029（本件 運用）/ ADR-0028（L2 本体）/ ADR-0027（principal トークン）/ ADR-0022（sync）/
  ADR-0020（interface frozen）/ ADR-0021（staff token）/ ADR-0025（方針）

## Related Commits

- 本 commit
