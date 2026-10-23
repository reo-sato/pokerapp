# Issue 0031: 仕様は「尤度ベースのアクター推定」だが実装は決定的な単一解に drift している

## Date

2026-09-12

## Status

Open（方針 ADR で決着予定。実装の是正はその後）

## Severity / Priority

- Severity: High（製品の中核方針。個々のバグではなく**設計が仕様と違う**）
- Priority: P1

## Area

reconstruct / integration / contracts

## Expected Behavior（`sprc_v4.docx` の要求）

要件定義書 `sprc_v4.docx` は **アクション履歴の推定を確率的に行う**ことを明記している。

- **改訂履歴 v3.0（2026-04-09）**: 「カメラ廃止。音声をストリーム処理化。ポーカールールエンジン導入。
  **アクター推定を尤度ベースに全面改訂**」
- **FR-26**: アクション種別が確定した時点で、以下に基づき **席ごとの推定確率**を算出する —
  ターン順（`current_turn_seat`、legal_actions にも含まれれば確率 0.95 以上）/ `active_seats`
  （**RFID フォールド検知で随時更新**、フォールド済みは確率 0.0）/ 音声中の席番号言及（最大値に boost）/
  音声中の**ポジション名**言及（`position_map` で席特定、席番号言及と同等）/ `all_in_seats`（除外）。
- **FR-27**: 推定確率の最大値が単一席に集中しない場合（**次点との差が 0.3 未満**）→ `needs_review: true`。
- **§5.4 アクター推定の尤度算出**: 参照実装 `estimate_actor(audio_action, state) -> dict[int, float]`
  （フォールド/オールイン済みと非合法アクションは 0.0、ターン席 **0.90**、アウトオブターン **0.05**、
  席・ポジション言及で boost）。
- **FR-25**: 「**RFID によるフォールド検知**と音声のフォールド発話が矛盾する場合」→ needs_review。
- **FR-35**: ゲーム状態は `active_seats: set[int]  # RFID でカードを持つ席` / `folded_seats` /
  `all_in_seats` を保持する。
- **FR-30**: ストリート遷移トリガーは **RFID の board 枚数変化を優先**、音声宣言を補助とする。

オーナーの明示確認（2026-09-12）: **「ベイズ推定を用いたアクション履歴推定は当初からの方針」**。
加えて運用上の要件として、**カード内容は事後に正しければよい**（リアルタイム精度は不要）/
**フロップ 3 枚の内部順序は無意味**（どの 3 枚かだけが重要）/ **実時刻が要るのは fold の時刻と
ターン・リバーの配布時刻**（音声の時系列と突き合わせて履歴を再生するため）。

## Actual Behavior（実装）

| 仕様 | 実装 |
|------|------|
| FR-26 席ごとの確率分布 | `integration/engine.py:_resolve_actor` が **固定優先順位（RFID > 明示発話席）で単一 actor を決定**。分布を返さない |
| FR-27 集中度 0.3 で needs_review | 存在しない。代わりに `derive_confidence`（ADR-0033）の 3 因子ヒューリスティック + `REVIEW_THRESHOLD=0.40`。**事後確率ではない** |
| §5.4 `estimate_actor` | 未実装 |
| FR-25 / FR-35 RFID フォールド検知 | **未実装**。席からカードが消えても DEBUG ログのみ（`rfid/reader_thread.py`）。`folded_seats` は pokerkit 内部状態で、RFID は寄与しない |
| ポジション名言及（UTG/BTN 等） | 未実装（`_extract_seat_from_text` は「シート N」のみ） |
| 確率的な履歴推定 | **決定的なライブ状態機械**。pokerkit を live 権威とし（ADR-0012）、1 つの解釈を即座に確定して書き出す |

`docs/issues/0009-actor-conflict-silent-fold-policy.md` は尤度導入を **後続の open question** とし、
暫定で決定的ポリシー（silent-fold 合成 cap=2 / 固定 `confidence=0.3` / 「曖昧なら call + review」）を
採用したまま Phase D を完了している。**その暫定が既定として定着した**のが本 issue の実態。

## Root Cause

ADR-0009（pokerkit を live ルール権威に）以降、設計の重心が「**リアルタイムに 1 つの正解を確定する**」
方向に寄った。ISSUE-0009 で尤度を後回しにした判断自体は当時妥当（ground truth となる golden fixtures が
無く重みを決められなかった）だが、その後 golden fixtures / 較正ハーネス（ADR-0033）が揃った時点でも
確率化に戻らず、暫定の決定的ポリシーが正規の設計として CLAUDE.md に昇格した。

構造的な帰結として、**早期確定が情報を捨てている**（本 issue の核心）:

- ASR の代替候補・単語別タイムスタンプ・信頼度を保持せず、単一の decode 結果だけを使う。
- 合法手射影（`apply_corrections`）が **その場で** 1 つのアクションに丸め、元の曖昧性を残さない。
- silent-fold を **その場で合成**し（cap=2）、合成でない可能性を残さない。
- RFID の「カードが消えた/戻った」遷移を **記録すらしていない**。
- 生イベント sidecar（ADR-0010 R1）は **既定 off** で、事後推定の入力が通常は存在しない。

## Fix

**未着手**。方針は Fable 5.1 による設計監査の結果を踏まえて ADR で決定する（本 issue の上位）。
論点は少なくとも:

1. FR-26/§5.4 の **アクション単位の局所的分布**でよいか、**系列全体の推定**（合法手列に対する
   sequence-level 推定）へ格上げすべきか。後者なら FR-26 は改訂対象。
2. FR-27 の `0.3` / §5.4 の `0.90` / `0.05` という仕様固定値を踏襲するか、較正し直すか。
   既に較正済みの `derive_confidence` 重み（ADR-0033）と `REVIEW_THRESHOLD` との関係。
3. pokerkit の役割（live 権威のままか、事後推定の制約オラクルか、両方か）。
4. 出力の不確実性表現（MAP 系列 + 事後確率 / N-best / 明示的な unknown）と、凍結済み
   `hand` / `action` schema との整合（additive で足りるか）。
5. FR-25/35 の RFID フォールド検知は、**プレイヤーがカードを持ち上げる**運用と両立するか
   （不在 ≠ fold）。

**先行して着手済み（ADR-0055）**: 推定方式に依存しない **観測の時刻精度**（fold の実時刻 =
マック観測 / ターン・リバーの配布時刻）。どの推定器でも入力として必要なので先に確保した。

## Regression Test

方針確定後に追加。現時点では `tests/test_timeline_fidelity.py`（ADR-0055 の観測時刻）のみ。

## Affected Files

- `sprc_v4.docx`（FR-25/26/27/30/35, §5.4）
- `integration/engine.py`（`_resolve_actor` / `derive_confidence` / `_append_synth_fold`）
- `rfid/reader_thread.py`（カード消失の扱い）
- `output/event_recorder.py`（既定 off）
- `docs/adr/0009-*.md` / `0012-*.md` / `0033-*.md`、`docs/issues/0009-*.md`

## Related

- ISSUE-0009（尤度を後回しにした当時の判断 = 本 drift の起点）
- ADR-0009 / ADR-0012（live 決定的設計）/ ADR-0033（confidence 較正）
- ADR-0010 / ADR-0011（record/replay = 事後推定の土台）
- ADR-0055（観測の時刻精度。本 issue の前提として先行実装）
