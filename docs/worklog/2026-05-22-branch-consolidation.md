# Worklog: Branch consolidation into main (逐次統合)

## Date

2026-05-22

## Scope / Task

並行開発で生えた複数ブランチの進捗を、新設 `main`（registry/contracts 系を trunk）へ
**逐次的にテスト付きで統合**する。本 worklog は統合タスク全体の親ログとし、各ブランチの統合を
ステップとして追記する。全体の設計計画は main（contract-first / parallel development /
registry-ledger future scope）を核とし、各ブランチの固有価値を additive に取り込む。

## 偵察結果（branch map）

系統A（Bayesian/reconstruction, trunk `7674d4d`=button rotation を共有）の系図:

```
7674d4d (trunk: button rotation + SB/BB auto-post)
 → Phase2-C → new-session            : + viewer/（独立・低衝突）
   → Phase5-F → add-tournament-timer  : + タイマー/会場ディスプレイ（モジュール独立寄り）
     → Phase5-H → docs-traceability    : プレースホルダ docs のみ（main の実 docs に劣後 → 破棄）
                → poker-manual-action-pad : 5-Ia GUI 手動アクションパッド
                → bayesian-action-estimation : 5-I〜5-K（系統A で最も推論機能が完成）
```

- bayesian-action-estimation が系統Aの hand logger 最完成（〜Phase 5-K）。他はその切詰め + 固有機能。
- manual-action-pad と bayesian は手動入力の**設計分岐**（GUI パッド vs エンジン経由）— 競合。
- enhance-poker-voice-commands: 固有 `betting_state.py` + GUI 修正だが trunk の button rotation に概ね先取られ実質劣後。
- poker-hand-logger-Dlhng: Flask/GameState/PokerRuleEngine の別アーキ全面再実装。main 路線と別物で obsolete。
- 命名衝突注意: 系統A "settlement"=ハンド内ポット分配 / main 計画 "session_settlement"=店への精算（別概念）。

詳細な各ブランチ偵察は本タスクのリサーチ（recon）で取得済。

## 統合ステップ

### Step 1 — Web ハンド履歴ビューア（from `new-session`） ✅ 完了

- **方法**: `git cherry-pick -x 8314e45`（`viewer/*` 新規 + `output/json_writer.py` +49 行のみ）。
- **衝突**: なし（main の json_writer がコミットのちょうどベース、追加のみ）。
- **内容**: 静的 HTML/CSS/JS の 3 画面。`JsonWriter._refresh_index()` が `logs/index.json` を
  ハンド保存ごとに atomic 再生成（失敗してもクラッシュしない防御実装）。
- **テスト**: `pytest tests/ -q --ignore=tests/test_vision.py` → **155 passed**（回帰なし）。
- **docs**: CLAUDE.md（ディレクトリ構成 / 実装状況 / コマンド）、CHANGELOG を更新。

### Step 2 以降 — 未着手（要・方針確定）

### Step 2 — 系統A hand logger エンジン uplift（bayesian 再ベースライン） ✅ 完了

ユーザー決定: (1) **bayesian-action-estimation を base に再ベースライン**、(2) 設計計画の核は
**main（contract-first / registry-ledger）**、(3) 手動入力は **bayesian のエンジン経路（5-I〜5-K）**。

- **方法（非破壊）**: 作業ブランチ `integrate/bayesian-base` を bayesian tip から作成 → base 単体
  テスト緑（682 passed, pokerkit 導入後）を確認 → main 由来の追加分を層状に適用:
  - 新規ファイル（衝突なし）: `core/player*.py`, `gui/player_registry.py`, `docs/contracts/**`,
    `viewer/**`, `tests/test_{player_repository,player_registry_gui,contracts}.py`。
  - 共有ファイルのマージ: `output/json_writer.py`（viewer の `_refresh_index`）、`main.py`
    （`--players` + `run_player_registry()`）、`requirements.txt`（`jsonschema`）、`.gitignore`
    （`players.json`）。
  - docs 統合: ADR は bayesian 0001/0002 + main 0003/0004/0005 を合成、issue は main 由来を
    **0003-0006 に renumber**（bayesian 0001/0002 温存）し全クロス参照を更新、CLAUDE.md /
    CHANGELOG / decision-log をマージ。CLAUDE.md は main の計画を核に hand logger 内部を実装済み反映。
- **公開**: 公開済み main の force-push を避け、`git merge -s ours origin/main` で旧 main(7fb2ad3) を
  親に取り込み、main を **fast-forward**（merge commit `f201ca1`）。非破壊・旧 main 復元可。
- **テスト**: `pytest tests/ --ignore=tests/test_vision.py` → **711 passed**（bayesian 682 +
  registry/contracts/viewer 29）。docs 相互参照すべて解決を確認。

### Step 3 — tournament timer 統合（from `add-tournament-timer`） ✅ 完了

- **方法**: `git cherry-pick -n 4fb02da`。timer コミットは Phase 5-F 上の単一コミットで、main
  （bayesian 5-K）は 5-F を祖先に持つため大半が auto-merge。**衝突は `gui/dashboard.py` の 1 ブロックのみ**
  （HEAD=patch-details 系 / 4fb02da=timer 操作系の別メソッド）→ 両方を残して解決。
  `main.py`（timer 自動ロード）/ `tests/test_gui.py`（timer GUI ロジックテスト）は auto-merge 成功。
- **新規ファイル**: `core/tournament_timer.py`, `core/tournament_state.py`, `gui/tournament_display.py`,
  `tournament_structure.json`, `tests/test_tournament_timer.py`, `tests/test_tournament_state.py`。
- **テスト**: timer 関連 113 passed（timer/state/GUI ロジック）、全体 **759 passed**（回帰なし）。
- **GUI 視覚確認**: この環境は tkinter（`_tkinter.so`）非搭載のため customtkinter（dashboard /
  tournament_display / player_registry）の**実描画は不可**。GUI ロジックは mocked customtkinter で検証済。
  実描画確認はローカル環境（`python main.py` / `--players`）が必要。web viewer は静的のため別途確認可能。

### 採用しなかった / 破棄

- **manual-action-pad（5-Ia GUI パッド）**: 手動入力はエンジン経路（bayesian 5-I〜）を採用したため不採用。
- **docs-traceability**: bayesian の subset + プレースホルダ docs（main の実 docs に劣後）→ 破棄。
- **Dlhng**: Flask/別アーキ全面再実装で main 路線と非互換 → 破棄。
- **enhance-voice**: 固有 betting_state はあるが系統A trunk に概ね先取られ実質劣後 → 保留（未統合）。
- **tournament timer**: 未統合（独立モジュール、別途 cherry-pick 可能。次の候補）。

## Expected vs Implemented

- Step 1（viewer）: Expected=低衝突追加でテスト緑維持。Implemented=cherry-pick 衝突なし、155 passed。
- Step 2（再ベースライン）: Expected=bayesian base に main 追加分を載せテスト緑。Implemented=
  期待どおり 711 passed、非破壊 forward merge で main 反映。

## Mismatches Found During Testing

- Step 1: なし。
- Step 2: bayesian base 単体で 15 件失敗 → 原因は `pokerkit` 未インストール（requirements 記載済）。
  導入後 682 passed。issue renumber 後に docs 相互参照 1 件（`docs/contracts/shared-ids.md` の
  hand-id 参照が旧番号 0004 のまま）を検出 → contracts 配下も remap し解決。

## Remaining Gaps / Out-of-Scope

- [x] tournament timer の統合（Step 3 完了, cherry-pick 4fb02da）。
- [ ] enhance-voice の固有 betting_state を取り込むか再評価（現状は系統A 実装で代替済み）。
- [ ] customtkinter GUI（dashboard / tournament_display / player_registry）の実描画確認 →
      本環境は tkinter 非搭載のため不可。ローカル環境での確認が必要。
- [ ] web viewer の視覚確認（静的のため headless でも headless browser で確認可能）。
- [ ] 不要ブランチの削除（環境が ref 削除不可。ユーザー環境で実施）。
- [ ] 既定ブランチを main に切替（MCP に設定変更ツールなし。ユーザー環境で実施）。

## Related Commits

- `874cf79` — feat(viewer): web viewer（cherry-pick of `8314e45`、Step 1）
- `063bdca` — wip(integrate): layer registry/contracts/viewer onto bayesian base（Step 2）
- `9972b30` — docs(integrate): merge docs-as-code（Step 2）
- `f201ca1` — merge: consolidate branches into main（非破壊 forward merge）

## Related ADRs / Issues

- ADR-0001/0002（手動入力エンジン経路 / strict reject）= 採用した手動入力方式の根拠。
- ADR-0003/0004/0005（domain 拡張 / contract-first / contracts layout）= 設計計画の核。
- 旧 main(7fb2ad3) は履歴に残存し復元可能。
