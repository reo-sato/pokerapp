# ADR-0047: アクション履歴復元の正当性修正バッチ（S1-S6 / B1-B5 / G2 / G6）

- Status: Accepted
- Date: 2026-08-19
- 関連: ADR-0009（rules-aware 再構築）/ ADR-0011（決定的 replay）/ ADR-0033（confidence 較正）/
  ADR-0036（ハンド訂正）/ ADR-0048（時刻セマンティクス）/ ADR-0049（制御語ガード）/
  ADR-0050（split pot）/ ISSUE-0009 / `docs/dogfood/measurement-plan.md`

## Context

Phase A の主 KPI は「hand coverage / action 一致率 / board 一致率の 3 軸 ≥95%」。3 系統の深掘り
調査（音声解析 / 統合エンジン / 設計文書）で、**needs_review が付かず GT 照合まで発見できない
「静かな誤り」経路**と**文書未記載のバグ**を特定した。決定的 replay + golden fixtures + P1-P8
較正プロパティの回帰装備があるため、修正は安全に行える。本 ADR は正当性系の修正
（バッチ 1 相当）を記録する。設計方針: **pokerkit 権威は不変**。修正は「権威への入力品質」
「権威からの出力読み出し」「監査可視化」に限定。legacy backend は rollback path として通常出力を
不変に保つ。schema は additive のみ。

## Decision

### S1 — 金額パースの拡充 + 曖昧 flag（`audio/recognizer.py:parse_amount_ex`）

- 「N千」「N百」「N千N百」「小数万（1.5万）」「小数 K（1.5K）」を追加。従来は「2千」が
  `_DIGIT_ONLY` の「2」とだけマッチ → min-raise に clamp され**無警告**で誤額が確定していた
  （最頻の静かな誤り）。候補収集は従来どおり最左・同位置は大きい値優先。
- 「4万2」「四万二」のような**万 + 単位なし 1 桁**は口頭省略（=42000）と桁欠落（=40002）の両解釈が
  あるため、千単位解釈（42000）を採用しつつ `ambiguous=True` を返す。`parse_action` はこれを
  `AudioEvent.parse_flags=("ambiguous_amount",)` に変換し、engine が needs_review + reason を付ける。
- 旧 `parse_amount(text) -> int` は互換ラッパーとして残す。パースは NFKC 正規化済みテキストに
  対して行う（全角数字・全角ピリオド対応。`raw_text` は原文保持）。

### S2 — 席番号表現の strip / 抽出を単一実装に統一

- `_SEAT_PATTERN` を strip と抽出で共有し、`(?:シート|seat)\s*([0-9０-９]+|[一二三四五六七八九])` に
  統一。従来は strip 側が「シート 3」（空白入り）「seat ３」（全角）を取りこぼし、**席番号が金額
  として parse_amount に流入**していた。
- `integration/engine.py:_extract_seat_from_text` の重複実装を廃止し
  `audio.recognizer._extract_seat_no` への薄いエイリアスに（実装は 1 箇所）。
  複数席抽出 `_extract_all_seat_nos`（ADR-0050 の chop 用）も同パターンを使う。

### S3 — raise の to/by 曖昧性 flag（`apply_corrections`）

- `LegalContext` に `bb` / `committed` を additive 追加（legacy stub は 0 = 従来挙動）。
- heard 額が **to 解釈では min-raise 未満（非合法）** だが **追加額（by）解釈
  （committed + to-call + heard）なら合法** の場合、`reason="raise_to_vs_by_ambiguous"` + review。
  採用は従来どおり to 解釈（min へ snap）。
- **両解釈とも合法な通常レイズは flag しない**（プランの「両解釈が乖離するなら flag」を字義どおり
  実装すると事実上すべての合法 raise が flag され review が飽和するため、「to 解釈が非合法 =
  従来なら無言で snap されていた」場合に絞った。ディーラー読み上げ慣例は to）。

### S4 — 金額 snap の review 閾値を同次元比較に

- 従来の `gap > min_raise_to` は次元不整合（gap は移動量、m は to 総額）で、「2千→2 誤読 →
  min へ 598 clamp」も m=600 未満なので無警告だった。`bb > 0` なら **`gap >= bb` で review**
  に変更。`bb == 0`（legacy stub / 直接構築の LegalContext）は従来閾値に fallback し既存挙動を保存。
- gap > 0 なら review の有無に関わらず `reason に "amount_snapped"` を残す（監査可能性）。
- V4（ADR 範囲では本 ADR に含める）: heard 額が bb の倍数でない場合、bb 倍数への丸めが合法
  レンジ内なら丸める（`reason="rounded_to_bb"`、review なし）。ASR の端数誤認識対策。

### B1 — fold 合成後の stale legal_ctx 再取得（`_handle_rules_aware_action`）

- silent-fold 合成（`fold_through`）後は盤面（actor / to-call / min-raise / legal set）が変わるため、
  `apply_corrections` は**必ず合成後の `legal_context()` に対して**行う。従来は合成前の ctx を
  使い続け、例えば out-of-turn call の記録額が実際のコミット額とズレていた
  （golden `out-of-turn-rfid` の call 200 は誤 pin。正しくは 100）。

### B2/B4 — イベントの無音消失の全廃 + live/replay 例外セマンティクス統一

- ハンド外（未開始/終了後）のベッティング発話や処理中例外は、従来 `run()` の広域 except で
  握り潰され**イベントごと消えていた**（replay 直呼びでは逆に例外が伝播 = 非対称）。
- `_handle_audio_event` を dispatch + 捕捉の入口に変え、適用できないイベントは必ず
  **適用不能レコード**（`actor_source="unresolved"` / `apply_ok=false` / `reason="no_active_hand"` 等 /
  needs_review）として `on_action` に流す。ゲーム状態は変更しないため `_current_actions`
  （= HandSummary.actions）には積まない。進行中ハンドがあれば review_required に波及させる。
  live / replay は同一セマンティクスになる。

### B5 — winner 席不明時の fallback 連鎖 + junk summary 抑止

- 席が読み上げから取れない winner は: ① active 席が 1 つ → その席（決定的・review 不要）
  ② 最後のアグレッサー + review ③ engine の手番席 + review ④ どれも不可 → 保留レコード化
  （finalize しない）。従来は `get_current_player()` の RuntimeError で**イベント消失**していた。
- 確定対象が何も無い（進行中ハンドなし・アクション/カード/review 状態なし）winner はハルシネーション
  疑いとして保留し、**空 summary を書かない**。`end_hand` が失敗しても記録は review 付きで書き出し、
  actions を消失・持ち越しさせない。

### S5 — result のブラインド分ズレ修正

- `_stack_start` を `gs.new_hand()` の**前**に取得する。pokerkit backend は new_hand でブラインドを
  自動 post するため、従来は post 後スタックが stack_start になり、`result` がブラインド分ずれていた
  （BB が break-even に見える等。golden fixtures は誤値を pin していた — 下の差分表）。
  legacy は new_hand がスタックを変えないため挙動不変。

### S6 — pot_total の権威を engine に

- `pot_total` は従来「bet/raise/call/allin の amount 加算」で、rules-aware 経路では raise の
  "to" 総額を多重加算し、かつブラインドを含まなかった。engine の pot スナップショット
  （`gs.pots()` 合計）を権威にする。
- betting round 途中の winner 宣言では pokerkit の `st.pots` が未回収で空/過少になるため、
  `PokerkitGameState._snapshot_pots` が実コミット総額（開始スタック合計 − 現スタック合計）との
  残差を合成して常に `sum(pots) == 実ポット` を保証する。
- legacy は `pots() == []` のため従来加算に fallback（挙動不変）。

### G2 — 監査フィールドの実配線（action schema 1.0 → 1.1, additive）

- `ActionRecord` に `actor_source`（"rfid" | "spoken_seat" | "engine_prior" | "unresolved"）/
  `corrected_from` / `reason`（"+" 区切り）/ `asr_confidence` / `apply_ok` を optional 追加し、
  rules-aware 経路で実際に emit（従来は schema に定義だけあり常に欠落 → Phase A の切り分け表が
  運用不能だった）。None のフィールドは to_dict に出さない = legacy 出力は不変。
  synth fold は `reason="synth_silent_fold"`。schema へ `reason` / `apply_ok` を additive 追加（1.1）。

### G6 — 計測のシーケンスアライメント（`tools/measure_capture_accuracy.py`）

- `measure_hand` の index 厳密比較を `difflib.SequenceMatcher`（キー = (street, seat, action)）の
  アライメントに変更。従来は誤合成 fold 1 件の挿入で以降の全アクションがズレ、action_accuracy が
  崩壊していた（1 件の誤りが N 件に化ける = KPI が実力を過小報告）。
- 挿入（phantom, 例: 誤合成 fold）/ 欠落（missed）は**各 1 誤り**。分母 = GT アクション数 + 挿入数。
- **GT 規約**（measurement-plan.md に明文化）: GT は実世界の全アクション（実際の fold 含む）を記録
  する。正しい合成 fold は GT の実 fold と align して一致、誤合成 fold は挿入 1 件として数える。
- S5/S6 は players.result / pot_total の修正であり、計測 3 軸（coverage / action / board）の
  定義には影響しない。

### B3 — whisper 欠測の保守的既定（ADR-0033 追記）

- `derive_confidence` への whisper_conf 欠測（None）は従来 **1.0（満点）補完**で「情報が無いほど
  confidence が上がる」逆転があった。`MISSING_WHISPER_CONF = 0.5` に変更（audio-only では
  REVIEW_THRESHOLD 未満 = 欠測 audio 単独のアクションは review 側に倒れる）。
- 較正プロパティ **P9**（missing < full かつ missing < threshold）を
  `tools/calibrate_confidence.py` / `tests/test_confidence_calibration.py` に追加して回帰ロック。
- 意図的なテキスト駆動入力（`tools/play_hand_text.py`）は confidence=1.0 を明示する。

## Golden fixtures の再 pin（誤値の訂正差分）

「現行出力をそのまま pin しない」原則に従い、全ケースを手計算で検証してから再 pin した
（`docs/contracts/event-replay.md` §7 に規約化）。監査フィールドの追記（G2）は全ケース共通のため省略。

| case | field | 旧値（誤） | 新値（検証済） | 原因 |
|------|-------|-----------|---------------|------|
| check-facing-bet | stack_start (s1/s2) | 9900 / 9800 | 10000 / 10000 | S5 |
| 〃 | result (s1/s2) | +500 / 0 | +400 / −200 | S5 |
| 〃 | pot_total / pots | 200 / [] | 500 / [500] | S6 |
| call-amount-from-state | 同上 | 同上 | 同上 | S5/S6 |
| silent-fold | result (s1/s2) | +300 / 0 | +200 / −200 | S5 |
| 〃 | pot_total / pots | 600 / [] | 800 / [800] | S6 |
| out-of-turn-rfid | call amount (s1) | 200 | 100 | **B1**（stale ctx の call 額） |
| 〃 | pot_total / pots | 200 / [] | 400 / [400] | S6+B1 |
| 〃 | result (s1/s2) | +300 / 0 | +200 / −200 | S5 |
| unequal-allin | stack_start (s1/s2) | 900 / 2800 | 1000 / 3000 | S5 |
| 〃 | result (s1/s2) | −900 / +4200 | −1000 / +4000 | S5 |
| 〃 | pot_total | 6700 | 7000 | S6（= main 3000 + side 4000） |

fixtures は 5 → 13 に拡充（postflop-street-transition / full-ring-6max / multi-hand-session /
rfid-vs-spoken-seat-conflict / camera-corroboration / low-whisper-confidence /
cap-exceeded-negative / split-pot-chop）。

## Consequences

- 「2千」型の誤読・result のブラインドずれ・pot_total の虚偽値・イベント無音消失が解消し、
  review が付くべき経路（曖昧額・大幅 snap・欠測 whisper・複数キーワード）に必ず flag が付く。
- legacy backend の**通常出力は不変**（bb=0 fallback / pots=[] fallback / 監査フィールド非 emit）。
  例外パス（従来イベントが無音消失していた箇所）のみ unresolved 通知レコードが増える。
- 欠測 whisper の保守化により、confidence 無しの audio-only アクションは review になる
  （意図的入力は 1.0 を明示する契約）。
- HandSummary/ActionRecord の `timestamp` は event 時刻由来に統一（ADR-0048）。

## やらないこと（バッチ 3 = 実データ後）

HMM/particle filter 化（決定的修正で 95% に届くかを先に計測）、confidence 数値の実データ較正、
複数アクション発話（V2）の本実装、camera actor 寄与の新規開発（vision 廃止方針）、
legacy backend の挙動変更、schema 2.0。
