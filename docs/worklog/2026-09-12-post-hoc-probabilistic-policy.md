# 2026-09-12 — 方針決定: アクション履歴は「事後・確率的」に推定する（ADR-0056）

## Goal

オーナー指示:

> 事後的、かつ確率的にアクション履歴を推定する大方針をもとに、fable5.1 の監査を受けて全体の方針を
> 決めてください。／ というか、ベイズ推定を用いたアクション履歴推定は当初からの方針です

**方針を決める**のが成果物。コードの挙動変更は本タスクでは行わない。

## やったこと

1. **仕様を確認**。`sprc_v4.docx` は拡張子に反して実体がプレーン UTF-8 テキストで、直接読める。
   当初から確率的推定を規定していることを確認した（改訂履歴 v3.0「アクター推定を**尤度ベースに
   全面改訂**」/ FR-26 席ごとの推定確率 / FR-27 集中度 0.3 / §5.4 `estimate_actor -> dict[int,float]`
   / FR-25・FR-35 RFID フォールド検知 / FR-30）。→ **ISSUE-0031**（drift）として起票済。
2. **Fable 5.1 に設計監査を依頼**。途中で「これは新方針ではなく drift」「仕様は
   `sprc_v4.docx`、読める」という訂正を送り、仕様条文を基準に評価し直させた。
3. **監査の中核指摘を自分で実測検証**（鵜呑みにしない）。
4. **ADR-0056 を起こして方針を確定**し、検証できた欠陥を ISSUE-0032 / ISSUE-0033 として起票、
   ADR-0033 を Superseded、ADR-0009 に部分置換の追記。

## 検証した指摘（すべて事実だった）

| 指摘 | 検証方法 | 結果 |
|------|---------|------|
| ディーラーボタンが回らない | 6-handed で `new_hand` → `get_current_player` / `committed` を 3 ハンド | **最初の actor は毎回 seat 3、ブラインドも毎回 seat 1/2**。→ ISSUE-0032 |
| RFID の appear が actor 証拠として明示発話席より優先 | `_resolve_actor` / `_pop_nearest_rfid_seat` を読む | **事実**。席を問わず ±2 秒の最近傍検出を `event.seat` より先に採り、`fold_through` で間の席を fold。→ ISSUE-0033 |
| golden がその挙動を正解として固定 | `tests/fixtures/reconstruction/out-of-turn-rfid/` | **事実**。「新ハンド 0.8 秒後に seat 1 のカード検出 → seat 3 を fold(0.3) + seat 1 に call(0.888)」= **配布と区別できない** |
| pokerkit では `advance_street` が no-op | `core/poker_engine.py:141-153` + 実機ログ | 事実（FR-30 のボード枚数トリガーは既に死んでいる） |
| 生イベント sidecar が既定 off | `config_default.json` | 事実（2026-09-12 の実機セッションは sidecar を残していない） |

## 決めたこと（詳細は ADR-0056）

- **D1** 記録の正本 = 事後推定。ライブは provisional 表示。読み取りは live ⊕ estimate ⊕ corrections。
- **D2** 推定器 = pokerkit の合法手列に対する**制約付きビーム探索**（決定的 / N-best + 事後周辺確率）。
  particle filter は決定性と両立しないので不採用、学習モデルはラベル不在で不採用。
- **D3** pokerkit は「ライブのルール権威」かつ「事後の制約オラクル」。やめるのは**イベント時点で
  唯一の真実を書き切る**役割だけ。
- **D4** RFID プレゼンスは**非対称**な証拠（戻れば強い否定 / ハンド終了まで戻らなければ中程度の支持 /
  不在それ自体は弱い）。fold の時刻は**区間**で報告する。仕様 FR-10/25/35 は改訂。
- **D5** 仕様の改訂と踏襲を表で確定（FR-05b は**そのまま実装**、FR-26/§5.4 は証拠チャネルのみ踏襲、
  FR-27 の 0.3 は統計量を事後 margin に定義し直して踏襲、FR-28/FR-30 は改訂）。
- **D6** sidecar を既定 on にし、観測（音声そのもの / 発話時刻 / n-best / プレゼンス遷移 / poll
  サイクル / 時計アンカー）を落とさない。ISSUE-0010 の「decode 後を境界」判断は撤回。
- **D7** **数値を決める前に計測**（無宣言アクション率 / 時刻ズレ / 不在時間分布）。
- **D8** ヒューリスティック confidence を記録の正本から外す（ADR-0033 Superseded）。

実施順序 P0〜P8 は ADR の表を参照。**破壊的 schema 変更は無い**。

## Changed files

| ファイル | 変更 |
|---------|------|
| `docs/adr/0056-post-hoc-probabilistic-action-history.md` | 新規（方針） |
| `docs/issues/0032-*.md` / `0033-*.md` | 新規（検証済の P0 欠陥 2 件） |
| `docs/adr/0033-*.md` | Status を **Superseded by ADR-0056** に（本文は保持） |
| `docs/adr/0009-*.md` | §4/§6 と Alternatives を ADR-0056 が部分置換する旨を追記 |
| `CLAUDE.md` / `CHANGELOG.md` / `docs/decision-log.md` | 方針・索引・P0 の明示 |

## Test results

コード変更なし。`881 passed`（変化なし）。

## Remaining gaps / 次にやること

1. **P0a（ISSUE-0033）**: RFID appear を actor 証拠から外す + golden `out-of-turn-rfid` 作り直し。
2. **P0b（ISSUE-0032）**: ボタン回転 + `button_seat` / `position_map` / `position` 記録 +
   ポジション名解釈。**既存 golden はアクター順が変わるため作り直しが必要**。
3. P1/P2: sidecar v2（既定 on・プレゼンス遷移・時計統一）+ 音声保存と発話時刻。
4. P3: 計測（D7）。ここで estimator の可否と初期パラメータが決まる。
5. P4 以降: estimator v0 → 読み取りオーバーレイ → 時刻 tier → firmware → confidence 撤去。

**P0 が入るまでは実機でのデータ収集を続けても、fold とアクター帰属が壊れたログが増えるだけ**
であることをオーナーに伝えること。

## Related

- ADR-0056 / ISSUE-0031 / 0032 / 0033
- ADR-0009（部分置換）/ ADR-0033（Superseded）/ ADR-0010・0011（土台）/ ADR-0055（観測の時刻）
- 仕様 `sprc_v4.docx`（FR-05b / FR-10 / FR-15-17 / FR-25 / FR-26 / FR-27 / FR-28 / FR-30 / FR-35 / §5.4 / §7）
