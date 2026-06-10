# Issue 0013: player viewer のプライバシーモデル（本人確認 / 閲覧範囲）が未確定

## Date

2026-06-10

## Status

Open

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

未定。M5（注文 write path）着手前に決着させ、必要なら ADR 化する。

## Regression Test

—（決着後に追加）

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
