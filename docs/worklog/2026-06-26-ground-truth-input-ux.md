# Worklog: Phase A ground truth 入力 UX（staff iPad app の計測タブ, ADR-0043）

## Date

2026-06-26

## Scope / Task

Phase A 捕捉精度の計測（`docs/dogfood/measurement-plan.md`）で人手の ground truth を作る
UX を staff iPad app に追加する。M1 着手の前提条件である Phase A の Go/No-Go 判定に必要な
データを生む基盤。ADR-0043 を起票したうえで core / API / staff app / docs を一気通貫で実装。

ユーザー選択（2026-06-26）:

- 役割 = **B**（観戦・録画担当スタッフ）
- タイミング = **T2**（ハンド直後、録画機材依存なし）
- 入力モード = **M2**（捕捉ログを横に並べて訂正のみ）
- 配置 = **P1**（既存 staff iPad app に追加）
- バイアス対策 = **C-2 のみ**（needs_review 入りは「✓ 流す」を無効化、強制 drill-in）。
  C-1（annotator passthrough 率の計測ログ）はユーザー判断で除外

## Goal

- 計測タブの UX 確定（一覧 triage + per-row 「✓ 流す」/「✏ 修正」+ 一括 + needs_review フィルタ）
- 永続化は **LWW**（per-session ファイル `logs/{sid}.ground_truth.json`、計測ツールが既に期待する形式）
- 訂正適用後の captured を「✓ 流す」の入力源に固定（ADR-0036 と一貫）
- `tools/measure_capture_accuracy.py` の入力ファイル形式と互換（変更不要）
- 全ファイル + テストが緑、typecheck OK

## Changed Files

- `docs/adr/0043-ground-truth-input-ux.md` — 新規。Decision §1〜§8、Alternatives 7 件、
  Consequences、Follow-up checkbox
- `core/ground_truth.py` — 新規。`GroundTruthHand` dataclass、`validate_source`、
  `hand_has_needs_review`（C-2 ガード判定）
- `core/ground_truth_repository.py` — 新規。LWW per-session ファイル
  （`logs/{sid}.ground_truth.json`、atomic+fsync）
- `api/server.py` — 3 endpoints 追加（`GET /measurement-rows`, `PUT/GET /ground-truth/{hid}`、
  staff token + write 所有プロセスのみ）+ `_GroundTruthBody` モデル + ground_truth_repo を
  `create_app` に DI（既定構築あり）
- `api/read_models.py` — `list_measurement_rows()` 追加（訂正適用済 hand を訓練して一覧 row を出力）
- `api/client.py` — `list_measurement_rows` / `pass_through_ground_truth` /
  `submit_ground_truth_edit` / `get_ground_truth` メソッド追加
- `tests/test_ground_truth_repository.py` — 新規（13 件、LWW / per-session / malformed skip /
  reload / C-2 ガード判定）
- `tests/test_viewer_api_ground_truth.py` — 新規（12 件、E2E / C-2 ガード / 訂正適用後 passthrough /
  LWW 上書き / 401/403/404/503 経路）
- `staff/src/api/types.ts` — `MeasurementRow` / `MeasurementGroundTruth` /
  `GroundTruthHand` / `GroundTruthEditPayload` を追加
- `staff/src/api/repository.ts` — 4 メソッドを interface に追加
- `staff/src/api/httpRepository.ts` — 4 メソッド実装
- `staff/src/api/mockRepository.ts` — 4 メソッド実装（C-2 ガード含む in-memory 動作）
- `staff/src/mocks/fixtures.ts` — `measurementRows`（3 行、うち 1 行 needs_review=true）追加
- `staff/src/screens/MeasurementTab.tsx` — 新規（一覧 + per-row 「✓ 流す」/「✏ 修正」+
  一括 + needs_review フィルタチップ + 5 秒 polling + 編集モーダル: winner_seat / board /
  notes 上書き）
- `staff/src/screens/TableViewScreen.tsx` — 「計測」タブを既存 4 タブの後ろに追加
- `staff/src/api/mockRepository.test.ts` — 7 件追加（needs_review 反映 / passthrough /
  C-2 ガード / manual-edit + LWW / 404 / 認可 / unknown hand）
- `docs/dogfood/measurement-plan.md` — §2.2（per-hand metadata の additive 拡張）、§2.3
  （計測タブ運用フロー）、§2.4（C-2 ガードと LWW のバイアス対策）を実装に揃えて更新
- `docs/decision-log.md` — ADR-0043 を ADR Index に登録
- `CHANGELOG.md` — Unreleased に新節
- `CLAUDE.md` — 実装状況テーブルに新行

## Expected Behavior

- staff iPad app の卓ビュー → 「計測」タブで session 内の captured hands を triage できる
- 完璧捕捉ハンドは 1 タップ（「✓ 流す」）で GT 化、`logs/{sid}.ground_truth.json` に書かれる
- needs_review 入りハンドは「✓ 流す」が無効化され、強制 drill-in（C-2 ガード、server + mock 両方）
- 訂正画面（mobile/ の CorrectionScreen, ADR-0036）で訂正後、戻ると review が解除されて流せる
- 「✏ 修正」モーダルで winner_seat / board / notes を override すると manual-edit GT が記録される
- 同じ hand_id に再 PUT すると LWW で上書きされる
- 既存 4 タブ（会計 / 注文 / 座席 / ハンド）の挙動は不変
- 既存テストは無影響、新規テスト 32 件（core 13 + API 12 + mock 7）すべて緑
- typecheck（`npx tsc --noEmit`）が clean

## Implemented Behavior

期待通り。確認した挙動:

- **Python tests**: `pytest tests/test_ground_truth_repository.py
  tests/test_viewer_api_ground_truth.py -v` → **25 passed in 1.11s**
- **無影響テスト**: `pytest tests/test_viewer_api.py tests/test_viewer_api_correction.py
  tests/test_viewer_api_staff.py tests/test_measure_capture_accuracy.py
  tests/test_hand_correction.py -q` → **48 passed, 1 skipped**（既存への副作用なし）
- **staff app typecheck**: `npx tsc --noEmit` → clean
- **staff app tests**: `npm test` → **20 passed, 0 failed**（13 既存 + 7 新規 measurement）

実装上の判断:

- per-hand metadata は **`annotator` / `annotated_at` / `source`** の 3 フィールドを additive 追加
  （measurement-plan §2.2 の `additionalProperties:true` 方針）。session-level の `annotator` は
  「ファイル最終 writer」の足跡として LWW で更新
- C-2 ガードは **server + mock** 両方で実装。server 側は `core.ground_truth.hand_has_needs_review`
  を共有関数として使い、mock 側は `row.has_needs_review` フラグを参照
- 訂正適用後の captured を passthrough 元に使うため、server endpoint は `get_hand()`
  （correction_repo 付き）を呼んでから C-2 判定する。これで「訂正で needs_review 解除 →
  passthrough 可能」が自然に成り立つ（テストで固定）
- 「✏ 修正」モーダルは winner_seat / board / notes に限定。action 単位の訂正は ADR-0036 の
  CorrectionScreen に経路を集約（コード重複なし、責任分界明確）
- mock の fixture には 1 行 needs_review=true を入れて UX 動作確認と C-2 ガード回帰を兼ねる

## Test Results

- `pytest tests/test_ground_truth_repository.py tests/test_viewer_api_ground_truth.py -v`
  → 25 passed in 1.11s
- `pytest tests/test_viewer_api.py tests/test_viewer_api_correction.py
  tests/test_viewer_api_staff.py tests/test_measure_capture_accuracy.py
  tests/test_hand_correction.py -q` → 48 passed, 1 skipped
- `cd staff && npx tsc --noEmit` → clean
- `cd staff && npm test` → 20 passed, 0 failed

注: 環境に numpy 未導入のため `pytest tests/` 全体は collection error で止まる（既存問題、
本 PR と無関係）。

## Mismatches Found

- 1 件のみ: 初実装で `to_dict` のキーを `annotated_by` にしていたが、measurement-plan §2.2
  session-level の `annotator` と揃えるべきだったため `annotator` に統一（テスト 3 件失敗 →
  rename + 修正で全緑）。

## Remaining Gaps

本 PR の範囲外で残るもの:

1. **「流す」率の運用観測**: C-1（annotator passthrough 率の計測ログ）はユーザー判断で除外した。
   dogfood 1〜2 週で偏りが大きく出るようなら後続 ADR で mitigation 検討
2. **sync の whitelist 拡張**: `*.ground_truth.json` を `core/sync.py` の whitelist に
   加えるかは別 PR で議論（ADR-0043 §8）。本 PR は file-level union が自然に効く範囲のみ
3. **action 単位の編集 UX 統合**: 訂正は ADR-0036 の CorrectionScreen に経路集約してあるが、
   計測タブから CorrectionScreen への直接ナビゲーションは未実装（staff app と player mobile は
   別アプリのため）。dogfood で導線不便が顕在化したら追加
4. **PR を分けるか**: 本 PR は前回までの累積（提案 rev.1 + ADR + 計測ハーネス + 今回 UX）と
   合わせて PR #33 のブランチに乗っている。スコープ膨張すれば PR を分割する判断

## Related Commits

このタスク完了時の commit に記載。

## Related ADRs / Issues / Reviews

- 起票 ADR: ADR-0043（本 PR で新規）
- 入力源 = 訂正適用後 captured: ADR-0036（hand correction overlay）
- 配置先: ADR-0037（staff iPad app）
- 認可: ADR-0021（staff shared token）
- read model: ADR-0017（viewer API）
- 計測プラン: `docs/dogfood/measurement-plan.md`（rev.1 で §2.3/§2.4 を実装に揃えて更新）
- 提案: `docs/proposals/2026-06-26-hand-review-integration.md`（rev.1 §6.1）
