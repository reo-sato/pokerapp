# 2026-09-12 — P0a: RFID を actor 証拠から外す（ISSUE-0033）+ CLI のログ割り込み修正（ISSUE-0034）

## Goal

ADR-0045 の **P0a** を実施し、併せて実機テスト中に判明した CLI の操作不能を直す。

## P0a — RFID の「カード検出」を actor 証拠から外す（ISSUE-0033）

### 変更

- `_resolve_actor`: sensed を **明示発話席（`event.seat`）のみ**にした。戻り値を
  `(actor, conflict, folded)` に縮小（RFID イベントを返す必要が無くなった）。
- `_pop_nearest_rfid_seat`（席を問わず ±2 秒で最近傍を取り出す）を**削除**。
- corroboration は `_pop_matching_rfid_event`（**同席限定**）に切替。`rfid_present` /
  `rfid_agree` も同席一致に統一した。
- golden `out-of-turn-rfid` → **`rfid-appear-is-not-an-action` へ改名 + 期待値を作り直し**。

### なぜ改名したか

旧 fixture は「新ハンド 0.8 秒後に seat 1 のカードが検出 → seat 3 を fold 合成 + seat 1 に call」を
**正解として固定**していた。これは**配布と区別できない**挙動なので、そのまま残すと不具合を守る
テストになる。イベント列は有用（配布 → 席の言及が無い「コール」）なので、名前と期待値を
「**RFID の検出は actor を動かさない**」に付け替えた。新しい期待値:

```
seat=3 call 200 conf=0.49 review=False source={'audio': True, 'rfid': False}
```

（fold 合成なし。actor は prior のまま。seat 1 のカードは記録されるがアクションには効かない）

### 回帰

`tests/test_phase_d2_wiring.py::TestRfidIsNotActorEvidence` 3 件:
他席への配布で actor が動かない / 同席の読みは裏付けとして残る / 明示発話席は従来どおり勝つ。

## ISSUE-0034 — 実機で `w 1` が通らなかった

実機ログ（8 席 + board）で `w 1` を 3 回打って全部弾かれ、**ハンドが確定せず JSON に残らなかった**。
原因は 3 つ重なっている。

1. **同じ位置・同じ札の board 再検出を INFO で出していた**（結合の弱いリーダーの間欠読み）。
   45 秒で 20 行以上流れ、入力行に割り込む。→ **変化が無い再検出は DEBUG**。
2. **ストリート自動遷移が毎回「自動遷移した」と INFO を出していた**。pokerkit では
   `advance_street` が **no-op** なので `gs.street` が変わらず、「既にそのストリート」の
   早期 return に**一度も入らない**。嘘のログでもある。→ **実際に動いたときだけ INFO**、
   動かなければ DEBUG で `RFID street hint … — backend keeps …`。
3. **`w1`（空白なし）を受け付けていなかった**。→ `_normalize_cli_command` が `w`/`r` + 数字を
   分割する（`q`/`n` と読み上げ文は対象外）。

さらに **`--log-file`**（既定 `logs/pokerapp.log`）を追加した。卓の状態は `tools/table_monitor.py`
で見られるので、実機テストではログを端末に出す必要がない。これが根本的な回避策。

## Changed files

| ファイル | 変更 |
|---------|------|
| `integration/engine.py` | `_resolve_actor` から RFID を除去 / `_pop_nearest_rfid_seat` 削除 / 同席 corroboration へ切替 / board 再検出とストリート hint のログ水準 |
| `main.py` | `_normalize_cli_command` が `w1` を分割 / `_route_logs_to_file` + `--log-file` |
| `tests/fixtures/reconstruction/rfid-appear-is-not-an-action/` | `out-of-turn-rfid` から改名 + 期待値作り直し |
| `tests/test_phase_d2_wiring.py` | `TestRfidIsNotActorEvidence` 3 件 |
| `tests/test_main_audio_optional.py` | glued command 2 件 |
| `tests/test_reconstruction.py` / `tests/test_confidence_calibration.py` | fixture 名・コメント |
| `docs/issues/0033-*.md`（Fixed）/ `0034-*.md`（新規）/ CLAUDE.md / CHANGELOG / decision-log | docs |

## Test results

```
python -m pytest tests/ -q --ignore=tests/test_vision.py
911 passed, 2 warnings      # 906 → 909（P0a）→ 911（ISSUE-0034）
```

## 実機テストで確認できたこと（2026-09-12 16:00, 8 席）

**カード読み取りは要件を満たしている**:

- 8 席 × 2 枚 = 16 枚すべて正しく検出（各席の 2 枚目は 1 枚目の **約 70 ms 後**）。
- フロップ 3 枚を **120 ms** で検出し、位置 1/2/3 に正しく割り当て。
- ターン（pos 4）/ リバー（pos 5）も正しい位置。
- 席 8 人ぶんの配布〜リバーまで、実プレイ速度で追随。

**残った確認事項**: 卓状態モニタの表示と反映遅延（本タスクの修正後に再測定）。

## Remaining gaps

- **P0b（ISSUE-0032）ディーラーボタンが回らない** — 次のタスク。既存 golden の作り直しを伴う。
- `likely_folded` の 20 秒しきい値は未検証（`tools/analyze_table_state.py` の実測待ち）。
- 卓状態モニタの実機確認が未了。

## Related

- ADR-0045（P0a）/ ISSUE-0033 / ISSUE-0034 / ISSUE-0032（次）
- ADR-0009 §4（置き換えた優先順位の出所）/ ISSUE-0009
