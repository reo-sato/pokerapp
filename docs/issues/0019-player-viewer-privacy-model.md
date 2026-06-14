# Issue 0019: player viewer のプライバシーモデル（本人確認 / 閲覧範囲）が未確定

## Date

2026-06-10

## Status

Fixed（v1 = name-pick で確定。M5 / ADR-0018, 2026-06-11。詳細は § Fix）

## Severity / Priority

- Severity: Medium
- Priority: P2

## Area

viewer API / mobile (M1/M2, ADR-0017)

## Expected Behavior

プレイヤー向け参照アプリでは「誰がどの player のデータを見られるか」が定義されているべき。
候補:

1. **name-pick（現状の M1 採用案）**: player 一覧から自分を選ぶだけ。認証なし。
   LAN 限定（`viewer_api.bind_host` 既定 `127.0.0.1`、公開は明示 opt-in）。
2. **PIN / トークン**: player ごとに PIN や QR トークンを発行し、本人のみ自分のデータを閲覧。
3. **open**: 全 player のデータを誰でも横断閲覧（店内の信頼前提）。

ドリンク注文（M5, write path）が入る時点では、なりすまし注文の防止策が必要になる可能性が高い。

## Actual Behavior

M1 は name-pick・無認証・LAN 限定で出荷する。API には認可機構がなく、player 一覧と任意
player のハンド履歴は LAN 内の誰でも取得できる。

## Reproduction

1. `viewer_api.bind_host` を LAN IP に変更して `python main.py --viewer-api` を起動。
2. LAN 内の任意端末から `GET /api/players` → 全 player が見える。

## Root Cause

未決定の設計事項（小規模クラブの信頼モデルでは name-pick で十分か、注文機能でどう変わるかが
ユーザー判断待ち）。

## Fix

**v1 = name-pick で確定**（user 決定 2026-06-11, ADR-0018 §1）:

- 参照・注文とも認証なしの name-pick を維持。LAN 限定（`viewer_api.bind_host` 既定
  `127.0.0.1`、公開は明示 opt-in）という前提も維持。
- 注文（write, M5）は **スタッフ確定を挟む**（ADR-0018 §2 staff-in-the-loop）ため、
  なりすまし・誤帰属の注文はスタッフ確定時とドリンク提供時の対面で発覚・却下できる。
  ledger に直接書かれることはない。
- PIN（注文時のみ / 全面）は**導入しない**。問題が顕在化した場合に additive に再評価する
  （registry への PIN 属性追加 + write 時のみ要求、で後付け可能な設計を維持）。

## Regression Test

- `tests/test_viewer_api.py`: 注文 POST が pending request を作るだけで ledger に書かれない
  こと、read-only モード（単独 `--viewer-api`）では write が 503 になることを固定。

## Affected Files

- `api/server.py` / `docs/contracts/viewer-api.md`

## Related Worklog

- `docs/worklog/2026-06-13-integrate-viewer-onto-verify-v1.md`

## Related ADRs

- `docs/adr/0017-player-facing-viewer-api-first-architecture.md` / `docs/adr/0018-m5-order-request-write-path.md`

## Related Commits

- —

## Notes

read-only の間（M1〜M4）は実害が「閲覧」に限られるため LAN 限定で許容する判断。
write（注文）導入時に再評価が必須。

## Update（2026-06-13, ADR-0021）

S5 write 拡張で **スタッフ会計 write を HTTP に出す**にあたり、認可を **staff shared token** で解決した
（`Authorization: Bearer <viewer_api.staff_token>`, ADR-0021）。これは「会計をするのは店側スタッフだけ」
という信頼境界に合致し、per-player の認証は不要。**player read / 注文 POST は本 issue のとおり
name-pick / 無認証のまま**（staff token は player 体験に影響しない）。

残: **player ごとの本人確認**は将来課題。**進化方針は ADR-0025 で確定**（player_id を内部不変キーに
保ち、認証を additive レイヤで重ねる: L0 name-pick → L1 per-player PIN → L2 外部 IdP（LINE / Google
OIDC、`auth_identity` バインディング）。外部 IdP は LAN-only 前提を変える hosted モードとして別 ADR）。
name-pick で問題が顕在化したら L1 PIN を additive 導入する。staff write の認可は本件で解決済み。

## Update（2026-06-14, ADR-0027 / ADR-0028）

ADR-0025 の方針（L0→L1→L2）のうち **L1（per-player PIN）と L2（外部 IdP）を実装可能な詳細設計まで
具体化**した（いずれも **設計のみ・コードなし**）:

- **ADR-0027（L1 PIN）**: PIN を `players.json` ではなく node-local `player_credentials.json`
  （PBKDF2 + lockout、read API / sync 非対象）に分離（ADR-0025 の「players.json に pin_hash」素描を
  漏洩・伝播・schema リスクの観点で精緻化）。検証後に stateless 署名トークンを発行し、**player principal
  解決レイヤ**で self-write を認可。config `viewer_api.player_auth` 既定 `off` で**後方互換**（name-pick 維持）。
- **ADR-0028（L2 外部 IdP）**: `(provider, subject) → player_id` の `auth_identity`（多対一、player_id は
  外部 sub から導出しない）。OIDC Authorization Code フロー（サーバ側 JWKS 検証）→ L1 と同形の player
  トークン発行。**hosted モード**で LAN 会場モードと player_id + sync 共存。PII 最小化（sub のみ保存）。
  **前提**: 運用面 ADR（hosting/secret/PII/法令）と player merge フロー（scope 外）。

着手順・実装は引き続き別タスク。name-pick で問題が顕在化したら L1 から additive 導入する。

## Renumber note（2026-06-13）

serene ブランチでは ISSUE-0013 として起票されたが、verify-v1 には別内容の ISSUE-0013
（Session/Seating Viewer の data source）が既に存在するため **ISSUE-0019 に採番替え**した。
関連 ADR も ADR-0013→0017 / ADR-0015→0018 に採番替え。
