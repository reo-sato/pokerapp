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

系統A の hand logger エンジン群（button rotation / Bayesian 推論 / reconstruction / pot settlement /
manual action pad / tournament timer）は main の hand logger コアと構造的に大きく分岐（+20k 行規模、
高衝突）。統合方式（main へ merge / bayesian を base に再ベースライン / 低衝突分のみ先行）と、
手動入力の設計分岐（GUI パッド vs エンジン経由）の決定が必要。ユーザー確認の上で逐次実施する。

## Expected vs Implemented（Step 1）

- Expected: viewer を低衝突で main に追加し、テスト緑を維持。
- Implemented: 期待どおり。cherry-pick で衝突なし、155 passed 維持。

## Mismatches Found During Testing

- なし（Step 1）。`_refresh_index` 追加で既存 `test_logger.py` 等に回帰なしを確認。

## Remaining Gaps / Out-of-Scope

- [ ] Step 2: 系統A hand logger エンジン uplift（統合方式の決定が前提）。
- [ ] manual 入力の設計分岐（manual-pad GUI vs bayesian エンジン経由）の選択。
- [ ] tournament timer の統合（系統A base 前提）。
- [ ] 破棄予定: docs-traceability（dominated）、Dlhng（別アーキ obsolete）。enhance-voice は要再評価。
- [ ] viewer の手動 UI 動作確認（headless のため未実施）。

## Related Commits

- `874cf79` — feat(viewer): add web-based hand history viewer（cherry-pick of `8314e45`）

## Related ADRs / Issues

- 既存 ADR-0004 / 0005（contract-first / parallel development）を統合方針の土台とする。
- 統合方式の決定が architectural に重ければ新規 ADR を起こす。
