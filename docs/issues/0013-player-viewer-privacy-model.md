# Issue 0013: player viewer のプライバシーモデル（本人確認 / 閲覧範囲）が未確定

## Date

2026-06-10

## Status

Fixed（v1 = name-pick で確定。M5 / ADR-0015, 2026-06-11。詳細は § Fix）

## Severity / Priority

- Severity: Medium
- Priority: P2

## Area

viewer API / mobile (M1/M2, ADR-0013)

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

**v1 = name-pick で確定**（user 決定 2026-06-11, ADR-0015 §1）:

- 参照・注文とも認証なしの name-pick を維持。LAN 限定（`viewer_api.bind_host` 既定
  `127.0.0.1`、公開は明示 opt-in）という前提も維持。
- 注文（write, M5）は **スタッフ確定を挟む**（ADR-0015 §2 staff-in-the-loop）ため、
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

- `docs/worklog/2026-06-10-m1-viewer-api.md`

## Related ADRs

- `docs/adr/0013-player-facing-viewer-api-first-architecture.md`

## Related Commits

- —

## Notes

read-only の間（M1〜M4）は実害が「閲覧」に限られるため LAN 限定で許容する判断。
write（注文）導入時に再評価が必須。
