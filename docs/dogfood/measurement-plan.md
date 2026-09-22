# Phase A/B 計測プラン — 自店ドッグフード

**ステータス**: 初稿（rev.1, 2026-06-26）
**作成日**: 2026-06-26
**関連文書**:
- `docs/reviews/2026-06-15-v1.0-launch-review.md`（Phase A/B/C 構造の初出）
- `docs/proposals/2026-06-26-hand-review-integration.md`（rev.1 §6 で本ドキュメントを参照）
- ADR-0036（ハンド訂正オーバーレイ）
- `tools/measure_capture_accuracy.py`（本プランの Phase A 計測ハーネス）

## TL;DR

Phase A（捕捉精度の検証）が「95% 以上」と言葉だけ流通している状態を解消し、
**何を、どのデータで、どう測るか** を確定する。Phase B（上達価値の検証）は
ハンドレビュー × GTO solver 機能（提案 rev.1）が dogfood に出てから 8 週で評価する。

- Phase A 合否 = **action 一致率・board 一致率・hand coverage の 3 軸すべてが ≥ 95%**
- 計測ハーネス = `tools/measure_capture_accuracy.py`（本 PR で実装）
- 入力 = 自動記録された `logs/{session_id}.json` + 人手 ground truth `logs/{session_id}.ground_truth.json`
- 訂正（ADR-0036）は **既定で適用**（ユーザー可視の最終状態を測る）。`--raw` で訂正前の素地も測れる

---

## 1. Phase A の合否基準（rev.1 で確定）

提案 §6 の「捕捉精度 95%」を以下 3 軸に展開する。**すべての軸で ≥ 95%** を Phase A 通過の必要条件とする。
1 軸でも 95% を切ったら、原因を切り分けて修正してから再計測する。

### 1.1 Hand coverage（ハンドそのものが記録されたか）

```
hand_coverage = (ground truth に存在しかつ logs に存在する hand_id 数)
              / (ground truth hand 総数)
```

- ground truth に存在するが logs に無いハンド = **missed hand**（致命）
- logs に存在するが ground truth に無いハンド = **phantom hand**（要調査、分母には入れない）

### 1.2 Action 一致率（アクション列の正しさ）

```
action_accuracy = Σ (各 hand の正しい action 数)
                / Σ (各 hand の gt_action 数 + logged 側の余剰（挿入）数)
```

- 1 action の「正しい」= **action 種別が一致** かつ **amount が一致**（fold / check は amount=0 で一致扱い）
- **シーケンスアライメントで比較する**（ADR-0047 G6）: `difflib.SequenceMatcher`（キー =
  `(street, seat, action)`）で GT と logged を最長一致アライメントし、対応づいたペアだけを比較する。
  - 挿入（logged にだけある。例: 誤合成 silent-fold）= **1 誤り**（分母に加算）
  - 欠落（GT にだけある。取りこぼし）= **1 誤り**（対応ペアなし = 不正解のまま分母に残る）
  - 旧定義（index 厳密比較 + max() 分母）は、挿入/欠落 1 件で以降の全アクションがズレて
    1 誤りが N 誤りに化けるため廃止（実力を過小報告する）。
- **GT 規約**: GT は実世界で起きた**全アクション**（宣言されなかった実際の fold を含む）を記録する。
  正しく合成された silent-fold は GT の実 fold とアライメントされて一致し、誤合成 fold だけが
  挿入 1 件として罰される。

補助指標（Phase A 通過判定には使わないが、診断のために計測する）:
- `action_type_accuracy` = 種別だけ一致した割合
- `action_amount_accuracy` = 金額だけ一致した割合
- 両者の差で「種別は取れているが金額がズレている」「種別自体が違う」を切り分ける

### 1.3 Board 一致率（ボードカード）

```
board_accuracy = (最終 board が gt と完全一致した hand 数) / (ground truth hand 総数)
```

- 順序非敏感: gt と logs を sort して比較
- street ごとの部分一致は Phase A では問わない（最終 board のみ）。診断用に
  per-street 一致率も補助計測する。

### 1.4 補助計測（Phase A 通過判定には不使用）

- `hole_card_accuracy` = ground truth に hole_cards が明記された seat について
  `(一致 seat 数) / (gt に hole_cards がある seat 数)`。preflop fold で gt も None の場合は分母に入れない
- `winner_seat_accuracy` = winner_seat が一致した hand の割合（ショーダウン未到達は分母から除外）

---

## 2. データセット（ground truth の作り方）

### 2.1 規模

| 項目 | 値 | 根拠 |
|---|---|---|
| 並行 dogfood 参加者 | N = 5〜10 名 | 提案 rev.1 §6.1 |
| 計測対象セッション数 | 最低 5 セッション | hand 数で正規化するが、卓ごとのバラつきを見る |
| セッションあたり最低 hand 数 | 20 hands | 統計的に「95%」を主張するための最小サンプル（実用上の安心ライン） |
| 期間 | 8 週 | 提案 rev.1 §6.1。Phase A 自体は 最初の 2〜4 週で通過させたい |

### 2.2 ground truth ファイル形式

**配置**: `logs/{session_id}.ground_truth.json`（プロジェクト直下 `logs/` 配下、`.gitignore`、node-local）

**スキーマ（必要十分な最小形 + ADR-0043 で additive 拡張）**:

session-level の `annotator` / `annotated_at` は「ファイル最終 writer の足跡」（LWW で更新）。
per-hand に **annotator / annotated_at / source（`captured-passthrough` or `manual-edit`）** を additive
追加する（ADR-0043, `additionalProperties:true` 方針）。`tools/measure_capture_accuracy.py` は per-hand
metadata を無視して board/actions/players/winner_seat だけ読む。

```json
{
  "session_id": "<UUID4 hex>",
  "annotator": "<staff identifier, free-form>",
  "annotated_at": "<ISO 8601 UTC>",
  "source": "manual",
  "hands": [
    {
      "hand_id": 1,
      "annotator": "staff1",
      "annotated_at": "2026-06-26T20:15:00Z",
      "source": "captured-passthrough",
      "board": ["As", "Kc", "Qd", "5h", "2s"],
      "actions": [
        {"street": "preflop", "seat": 2, "action": "raise", "amount": 200},
        {"street": "preflop", "seat": 4, "action": "call",  "amount": 200},
        {"street": "flop",    "seat": 4, "action": "check", "amount": 0},
        {"street": "flop",    "seat": 2, "action": "bet",   "amount": 300}
      ],
      "players": [
        {"seat": 2, "hole_cards": ["Ah", "Ad"], "showed_down": true},
        {"seat": 4, "hole_cards": null,         "showed_down": false}
      ],
      "winner_seat": 2,
      "notes": "Hero AA preflop 3bb open, BB call, AHWB cbet"
    }
  ]
}
```

**規則**:

- すべての時刻は ISO 8601 UTC（`...Z`）
- `seat` は 1 以上の整数
- `action` は `{"fold","check","call","bet","raise","allin","blind"}` のいずれか
  （実コード `core/constants.py:ACTION_KEYWORDS` と整合）
- `amount` は **そのアクションでチップが入った量**（コール額・レイズ後の総額のいずれにするかは
  `tools/measure_capture_accuracy.py` の正規化と一致させる。**現状は logs 側の `amount` 慣習に合わせ、
  そのアクションで投入した量**で記録する。実装に揺らぎがある場合は本ドキュメントを更新）
- `hole_cards` は **既知のみ記録**（preflop フォールド等は `null`）。集計時は null seat は分母から除外
- `winner_seat` はショーダウン到達時のみ記録、未到達なら省略 OK
- `notes` は人手メモ、計測に影響しない

### 2.3 アノテーション手順（ADR-0043 で確定 = staff iPad app の計測タブ）

**運用前提**: 観戦・録画担当スタッフ（卓を回さない 1 名）が、ハンド直後（T2）に **staff iPad app**
（`staff/`）の「計測」タブで作業する。録画機材は任意。

1. **ライブセッション中**: 計測タブを開く。新ハンドが終わると一覧に row が追加される（5 秒 polling）
2. 各 row で **winner_seat + winner の chip won + `needs_review` バッジ**を目視照合:
   - 妥当 → **「✓ 流す」** をタップ（GT = 訂正適用後の captured） — 1 タップで完了
   - 違和感 → **「✏ 修正」** をタップ → モーダルで winner_seat / board / notes を override
   - action 単位の誤認識 → 既存の **ハンド訂正画面（mobile/ の CorrectionScreen, ADR-0036）** で訂正
     → 計測タブに戻り「✓ 流す」（訂正適用後は needs_review=False）
3. **C-2 ガード**（ADR-0043 §3）: `has_needs_review=True` の row は「✓ 流す」が無効化される。
   強制 drill-in（訂正 → 流す、または直接「✏ 修正」）
4. キュー追い越し時: 一覧フッタの **「表示中の N 件を流す」一括ボタン** で連続消化
5. 計測ツール `tools/measure_capture_accuracy.py --session logs/<sid>.json
   --ground-truth logs/<sid>.ground_truth.json` を週次で走らせ、結果を保存

### 2.4 ground truth 自体の品質

ground truth 自体が間違っていると Phase A の判定が壊れる。以下で品質を担保する:

- **「✓ 流す」のバイアス**: GT = 捕捉値なので当該ハンドの action_accuracy が trivially 100% になる
  リスク。annotator は winner + chip won を必ず目視照合する運用ルールで補強。dogfood 早期で
  「流す」率が極端に高い場合は、後続 ADR でランダム M% 強制 drill-in を追加検討
- 録画は任意（T2 はハンド直後で記憶新鮮、録画なしで運用可）。録画がある場合は週次に
  サンプルで照合
- 不確実な action は `notes` に明記し、計測結果の解釈時に重みづけする
- **永続化は LWW**（ADR-0043 §4）: 同じ `(session_id, hand_id)` の再送信は最新で上書き

---

## 3. Phase A 通過プロセス

### 3.1 段階

```
[週 1〜2] dogfood 試運転（参加者 N=2〜3 で限定運用）
  ↓ logs と ground truth を蓄積、ハーネスを走らせ初期数字を出す
[週 3〜4] 計測本番 + 修正サイクル
  ↓ 軸別 95% に届かない部分を切り分け（音声 / RFID / ポジション推定）
[週 4 末] Phase A 通過判定
  ├─ 3 軸 ≥ 95% → 通過 → Phase B（M1 解禁）
  └─ いずれか < 95% → 原因切り分け + 修正 + 再計測
```

### 3.2 切り分け手順（< 95% の場合）

| 軸 | 主な原因仮説 | 切り分け方法 |
|---|---|---|
| Hand coverage | session/hand 境界の取りこぼし、`session_layer.enabled` 不整合 | logs に該当 hand_id があるか、session_repo に seat assignment があるかを目視 |
| Action 一致率（種別） | Whisper 誤認識、`apply_corrections` の射影バグ、actor 推定ミス | `action_type_accuracy` vs `action_amount_accuracy` の差を見る、`needs_review=True` を集計、`source` を見て音声 only か RFID も含むか |
| Action 一致率（金額） | `parse_amount` の単位ミス、`KANJI_DIGIT/UNIT` 不足 | 金額の miss を分布で見て桁・kanji 単位の偏りを確認 |
| Board | RFID 取りこぼし、街遷移ミス | `board_source` を見る、per-street 一致率を見る |

### 3.3 Phase A 通過後にやること

1. M1（solver 統合）の実装着手（提案 rev.1 §4）
2. dogfood 期間中も継続して計測 — 95% を下回ったら回帰として扱う

---

## 4. Phase B の計測（M1 解禁後）

提案 rev.1 §6.2 の暫定閾値を本ドキュメントで正式化する。Phase A と独立に、
**solver 機能の利用ログ** を別軸で測る。

| KPI | 暫定閾値 | 観測手段 |
|---|---|---|
| 週次起動率 | N 人中の **過半数が 8 週中 4 週以上で週 3 回以上起動** | `logs/hand_review_usage.jsonl`（M1 実装時に追加） |
| 平均滞在時間 | 1 セッションあたり中央値 ≥ 5 分 | 同上ログ（window open/close timestamp） |
| 能動操作率 | 1 セッションあたり「次の手表示」「別ハンド選択」中央値 ≥ 3 回 | UI イベント計装 |
| ベースライン質問 | 参加者 N の過半数が「他 GTO ツールを使ったことがある」と回答 | dogfood 開始時のオンボーディング 1 ページ |

詳細は M1 実装時に `gui/hand_review.py` の計装と合わせて確定する。本ドキュメントは
Phase B 評価会議を空欄にしないための **暫定値の格納場所** であり、実運用で得られたデータに
基づいて閾値は動かしてよい。

---

## 5. 本計画の境界（やらないこと）

- **音声認識アルゴリズム自体の改善** — 計測は計測。改善は別ストリーム
- **ground truth の自動生成** — 定義上、ground truth は人手の真実が要る
- **複数店舗を横断した集計** — 自店ドッグフードに閉じる（プライバシー・規約）
- **Phase H（実機 E2E）の代替** — RFID firmware は別 repo 依存。本計画は audio + RFID が
  曲がりなりにも動いている前提（前提が崩れると Phase A 計測自体が無意味）

## 6. 参考: 既存テストとの違い

| 既存 | 何を測るか | 本計画 |
|---|---|---|
| `tests/test_reconstruction.py` | 同じ events を 2 回 replay しても同じ HandSummary が出る（決定性） | リアル世界 vs 計算機の一致は測らない |
| `tests/fixtures/reconstruction/` 5 件 | コーナーケース（silent-fold, side-pot 等）の合成テスト | 5 件は合成ハンド。本計画は **実プレイ** が対象 |
| `tools/calibrate_confidence.py` | 派生 confidence の重みプロパティ（ADR-0033） | confidence 較正は計測しない |
| `tools/measure_capture_accuracy.py`（**本 PR で新規**） | **実プレイ ground truth と logs の照合** | 本計画の中核 |

## 7. 改訂履歴

- **2026-06-26 rev.1**: 初稿。Phase A 合否基準 3 軸（hand coverage / action accuracy /
  board accuracy）を確定。ground truth ファイル形式を `logs/{session_id}.ground_truth.json`
  に固定。dogfood 規模 N=5〜10 / 8 週を提案 rev.1 から引き継ぎ。
