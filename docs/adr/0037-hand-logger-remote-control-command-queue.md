# ADR-0037: hand logger 遠隔制御 — append-only control-command queue

## Status

Accepted

## Date

2026-06-16

## Context

ADR-0035（staff iPad アプリ）の卓ビューは 会計 / 注文 / 座席 まで実装したが、**ハンドタブ
（hand logger の遠隔操作）** が残っている（ADR-0036 §C / ISSUE-0020 Q1）。難所は **プロセス境界**:

- hand logger（録音 = 音声認識 + RFID）は `python main.py`（GUI/CLI）の **常駐プロセス**で動く。
  マイク・USB CCID は物理的に PC 接続（ADR-0015）であり、**録音主体は PC のまま**にする（ADR-0035 §4）。
- staff API は `python main.py --ledger`（`viewer_api.enabled`）の **別プロセス**。両者は同一ファイル
  システム（`logs/`・`sessions.json` 等）を共有するが、メモリは共有しない。
- ゲーム状態の変更は **IntegrationThread に一元化**する規約（ISSUE-0012）。GUI/CLI の「新ハンド /
  ウィナー / リバイ」は既に **`AudioEvent` を `audio_queue` に積む**形で実装されている
  （`main.py` / `gui/dashboard.py` → `integration/engine.py:_handle_audio_event`）。

したがって iPad からの制御は「hand logger プロセスへ**コマンドを伝える**」だけにし、適用は従来どおり
hand logger プロセス内の IntegrationThread が行う（状態の二重書き込み = race を構造的に避ける）。

関連: ADR-0035 / ADR-0036（§C）/ ADR-0018（in-process API・単一書き手）/ ADR-0008（session 結線）/
ISSUE-0012（GameState 変更の一元化）/ ISSUE-0020（open question）。

## Decision

hand logger 遠隔制御を **append-only control-command queue（ファイル経由）** で実装する。

1. **キュー = セッション単位の JSONL ファイル** `{log_dir}/{session_id}.control.jsonl`。1 行 1 コマンド
   `{command_id(uuid hex), type, args, created_at}`。**append-only**（既存ストアと同じ不変条件）。
2. **書き手 = staff API**（`POST /api/staff/sessions/{sid}/control`、staff token / write 所有プロセス）。
   コマンドを 1 行 append するだけ（fire-and-forget）。type は `new_hand` / `winner`（args.seat）/
   `rebuy`（args.seat, args.amount）。
3. **読み手 = hand logger プロセス**の `ControlConsumerThread`。起動時に **ファイル末尾へシーク**
   （= 過去コマンドは再実行しない）し、以後 poll で**新規行のみ**を読み、`command_id` で重複排除して
   `AudioEvent` に翻訳し `audio_queue` に積む。適用は従来の `IntegrationThread` が行う（**新規の状態変更
   経路を作らない** = GUI ボタンと同一経路、ISSUE-0012 を遵守）。
4. **既定 off・additive**: config `hand_control.enabled`（既定 `false`）。有効化は **GUI モード +
   `session_layer.enabled`** が前提（session_id が `sessions.json` に存在し staff が卓を選べる、ADR-0008）。
   off では consumer を起動せず **挙動完全不変**。
5. **録音は PC**（ADR-0035 §4 を維持）。iPad は録音しない・状態を直接書かない。staff app の
   ハンドタブ v1 は **制御送信**（新ハンド/ウィナー/リバイ）に限定し、ハンド履歴の read は後続
   （staff 用 hands-list endpoint が要るため）。

## Alternatives Considered

- **hand logger が自前の control 受信（HTTP / socket）を立て、staff API が proxy** — プロセス間結合と
  故障モード（ポート競合・到達不能）が増える。ファイル append は OS が atomicity を担保しやすく、
  既存の「ファイル共有 + reload-on-read」運用（ADR-0020/0022）と同じ流儀。→ queue 採用。
- **staff API プロセスに hand logger を同居させる** — 録音スレッド・GameState を `--ledger` に移す大改修。
  ADR-0018 の所有境界・R 系の前提を崩す。録音は PC・別プロセス維持が原則。→ 不採用。
- **当面 read-only（PC オペレータが手動操作）** — ISSUE-0020 の選択肢 (c)。価値の大半（会計/注文/座席）は
  既に iPad 化済だが、ユーザ要望は §C 着手。→ queue で実装。
- **consumer 起動時にファイル先頭から再生** — hand logger 再起動で過去コマンドを再実行してしまう
  （新ハンド連打等の事故）。→ **起動時に末尾シーク**（新規のみ消費）+ `command_id` 重複排除。

## Consequences

- Positive: iPad から「新ハンド / ウィナー / リバイ」を送れる。状態変更は IntegrationThread 一元化を
  保ち race を作らない。ファイル経由でプロセス疎結合・既定 off で完全に後方互換。
- Negative / trade-offs: ファイル poll のため**反映に最大 poll 間隔の遅延**（既定 200ms）。GUI + 
  `session_layer.enabled` + `hand_control.enabled` の 3 条件が前提（CLI / 非 session レイヤは非対象）。
  iPad は配膳の ack のみで、適用結果（実際にハンドが進んだか）の確認はハンド履歴 read（後続）。
- Neutral: config に `hand_control` セクション追加（既定 off）。新 error code は `invalid_control`(400)
  のみ（control コマンドの形式不正）。`logs/*.control.jsonl` は append-only sidecar（ハンドログ不変）。

## Validation / Follow-up

- [x] `core/control_queue.py`（`ControlCommandLog` append/read_from/end_offset + `command_to_audio_event`）
      + `tests/test_control_queue.py`（append/offset/idempotent/translate）。
- [x] `integration/control_consumer.py`（`ControlConsumerThread`、末尾シーク + 重複排除）+ test。
- [x] `POST /api/staff/sessions/{sid}/control` + `ViewerApiClient.send_control` +
      `tests/test_viewer_api_staff_lifecycle.py`（append + authz + invalid_control）。
- [x] `main.py:run_gui` に consumer 結線（`hand_control.enabled` + `session_layer.enabled`、既定 off）。
- [x] staff app ハンドタブ（`staff/src/screens/HandTab.tsx`）+ `StaffRepository.sendControl`（mock/HTTP）。
- [ ] ハンド履歴 read（staff 用 hands-list endpoint）と E2E の実機確認は後続（ISSUE-0020）。

## Related Files

- `core/control_queue.py` / `integration/control_consumer.py` / `main.py`
- `api/server.py` / `api/client.py` / `config_default.json`
- `staff/src/screens/HandTab.tsx` / `staff/src/api/*`
- `docs/contracts/viewer-api.md` / `docs/contracts/error-shapes.md`

## Related Tests

- `tests/test_control_queue.py` / `tests/test_viewer_api_staff_lifecycle.py`
- `staff/src/api/mockRepository.test.ts`

## Related Commits

- 本 ADR と同じ commit（§C 実装）

## Supersedes / Superseded by

- Supersedes: —（ADR-0036 §C を具体化。ISSUE-0020 Q1 を解決）
- Superseded by: —
