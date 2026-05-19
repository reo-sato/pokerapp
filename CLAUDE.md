# Poker Hand Logger — CLAUDE.md

## プロジェクト概要

ライブポーカートーナメントのハンド履歴を自動記録する Python アプリケーション。
ディーラー口元マイク（faster-whisper / Vosk 音声認識）と RFID NFC（ESP32 + PN532）の 2 ソースを統合し、
JSON/PHH 形式でハンドログを出力する。

- **対象**: 小規模クラブ・個人配信向け
- **仕様書**: `sprc_v4.docx`（本ファイルより詳細な要件定義）
- **カメラ入力**: `vision/` ディレクトリは廃止予定のレガシーコードであり、メイン処理では使用しない

---

## ディレクトリ構成

```
pokerapp/
├── CLAUDE.md                      ← このファイル
├── sprc_v4.docx                   ← 仕様書（要件定義）
├── main.py                        ← エントリーポイント (--cli / GUI)
├── config.json                    ← ユーザー設定（config_default.json からコピー）
├── config_default.json            ← デフォルト設定テンプレート
├── rfid_cards.json                ← tag_id → card_code マスタ (例: {"04A1B2C3": "Ah"})
├── corrections.json               ← 音声誤認識補正テーブル (例: {"ベッド": "ベット"})
├── speech_normalization.json      ← 音声正規化辞書 (action/seat/number aliases)
├── requirements.txt
│
├── core/
│   ├── config.py                  ← config.json ロード・保存
│   ├── constants.py               ← ACTION_KEYWORDS, KANJI_DIGIT/UNIT, WHISPER_PROMPT_JA
│   ├── event_queue.py             ← EventQueue (スレッド間共有キュー)
│   ├── events.py                  ← AudioEvent, CameraEvent, RFIDEvent データクラス
│   ├── game_state.py              ← GameStateManager (スタック/ポット/ターン管理)
│   ├── hand_log.py                ← ActionRecord, HandSummary, PotSettlement, RevealedHand
│   ├── showdown_tracker.py        ← ShowdownTracker (Phase 1 skeleton、本実装は Phase 2-D 以降)
│   ├── settlement.py              ← compute_pot_settlements / evaluate_hand_rank / distribute_split_pot (Phase 2-A 実装済)
│   ├── hand_finalizer.py          ← HandFinalizer.finalize (Phase 2-B 実装済、engine._finalize_hand の主経路)
│   ├── hand_boundary.py           ← HandBoundaryDetector (Phase 2-C): audio / RFID から hand window を検出
│   └── hand_reconstructor.py      ← HandReconstructor (Phase 3 実装済): hand window を BeamEngine + HandFinalizer で再生し online と diff
│
├── audio/
│   ├── recorder.py                ← AudioThread (PyAudio + faster-whisper)
│   ├── vosk_recorder.py           ← VoskAudioThread (Vosk 代替バックエンド)
│   ├── recognizer.py              ← parse_action(), parse_amount(), apply_corrections()
│   └── speech_normalizer.py      ← SpeechNormalizer, NormalizedResult, normalize()
│
├── rfid/
│   ├── http_receiver.py           ← RFIDHTTPReceiver (ESP32 HTTP POST 受信)
│   ├── reader_thread.py           ← RFIDThread (pyscard PC/SC 直接読み取り)
│   ├── bridge.py                  ← RFID ブリッジユーティリティ
│   └── card_master.py             ← CardMaster (rfid_cards.json ロード・検索)
│
├── integration/
│   ├── engine.py                  ← IntegrationThread, calc_confidence()
│   ├── action_inference.py        ← BettingState, InferredAction, infer_action(), infer_action_distribution()
│   ├── action_order.py            ← ディーラーボタン回転 / SB-BB / actor 順 helpers
│   ├── observation_model.py       ← ベイズ尤度関数 (v6.0+ M2): EvidenceInterval, PriorParams, ActionHypothesis, compute_log_likelihood, default_priors
│   └── beam_search.py             ← Beam Engine (v6.0+ M3): BeamParticle, BeamEngine (K=8 決定論的), WINNER 後方修正
│
├── output/
│   ├── json_writer.py             ← JsonWriter (セッション JSON ログ書き込み)
│   ├── phh_exporter.py            ← PHHExporter (PHH 形式エクスポート)
│   ├── evidence_log.py            ← EvidenceLogWriter (v6.0+ M1): logs/evidence_<session>.jsonl, raw 観測の append-only ログ
│   ├── replay_hand.py             ← load_evidence_log / extract_hand_windows (Phase 2-C): hand window 再生ユーティリティ
│   └── reconstruct_session.py     ← CLI (Phase 3): session JSON + evidence JSONL を読んで各 hand を再構成、online↔offline diff を書く
│
├── gui/
│   └── dashboard.py               ← GUIDashboard (customtkinter)
│
├── tests/                         ← pytest テストスイート
│   ├── test_action_inference.py   ← BettingState / infer_action() テスト
│   ├── test_e2e.py                ← E2E ハンドシナリオテスト
│   ├── test_game_state.py
│   ├── test_integration.py        ← IntegrationThread confidence テスト
│   ├── test_normalizer.py         ← SpeechNormalizer / 数値正規化テスト
│   ├── test_parser.py             ← parse_action() / parse_amount() テスト
│   ├── test_phh_exporter.py
│   ├── test_rfid.py
│   ├── test_rfid_http.py
│   ├── test_evidence_log.py       ← EvidenceLogWriter テスト (v6.0+ M1)
│   ├── test_observation_model.py  ← 観測尤度関数 / Dirichlet / Normal / 位置 prior (v6.0+ M2)
│   ├── test_inference_equivalence.py ← infer_action() 薄アダプタの legacy 等価性 (v6.0+ M2)
│   ├── test_beam_search.py        ← BeamEngine 単体 (剪定 / 決定論 / winner filter) (v6.0+ M3)
│   ├── test_bayesian_e2e.py       ← WINNER 後方修正 E2E (v6.0+ M3)
│   └── ...
│
├── vision/                        ← レガシー（未使用）
└── logs/                          ← セッションログ出力先 (.gitignore)
```

---

## 技術スタック

| 用途 | ライブラリ | 備考 |
|------|------------|------|
| 音声認識（メイン） | faster-whisper ≥ 1.0 | CPU int8 モード |
| 音声認識（代替） | Vosk | 軽量・低遅延 |
| マイク入力 | PyAudio ≥ 0.2.13 | |
| RFID (HTTP) | 標準 http.server | ESP32 から HTTP POST 受信 |
| RFID (PC/SC) | pyscard ≥ 2.0.7 | transport="pcsc" 時のみ |
| PHH 出力 | pokerkit ≥ 0.5 | |
| GUI | customtkinter ≥ 5.2 | |
| 数値処理 | numpy ≥ 1.24 | |
| テスト | pytest ≥ 7.0 | |

> opencv-python / easyocr は requirements.txt に残っているが、
> メイン処理では未使用。将来的に削除予定。

---

## アーキテクチャ

### スレッド構成

| スレッド | 役割 | キュー |
|---------|------|--------|
| AudioThread / VoskAudioThread | マイク入力 → ASR → `parse_action()` → AudioEvent 送出 | → audio_queue |
| RFIDHTTPReceiver / RFIDThread | RFID 受信 → card_master 解決 → RFIDEvent 送出 | → rfid_queue |
| IntegrationThread | 3 キュー消費 → ゲーム状態更新 → ActionRecord 生成 → JSON 書き込み | ← 全キュー |
| MainThread | GUI 描画のみ（ブロッキング禁止） | |

### 音声認識処理パイプライン

```
マイク PCM16 16kHz
    │
    ▼ faster-whisper (beam_size=5, best_of=5, word_timestamps=True) / Vosk (SetWords)
ASR 出力:
    │   ├─ text (joined)
    │   ├─ alternatives: [(text, confidence, words)]   ← v6.0+ M1
    │   ├─ word_timestamps: [(word, start, end, conf)] ← v6.0+ M1
    │   └─ t_end (発話終了の絶対時刻)                   ← v6.0+ M1
    │
    ▼ apply_corrections()          ← corrections.json: 誤認識を一括置換
補正済みテキスト
    │
    ▼ speech_normalizer.normalize()  ← speech_normalization.json
    │   ├─ seat_aliases  : "二番" → "シート2"
    │   ├─ action_aliases: "ベッド" → "BET", "椅子" → "RAISE" ...
    │   └─ number normalization: "ろくひゃく" → 600, "一万二千" → 12000
    │
    │  NormalizedResult { action, seat, amount, normalized_text, ... }
    │
    ▼ ACTION_KEYWORDS fallback (action が確定しない場合)
    │
    ▼ amount_only 検出
    │   (アクションなし・数字のみ → AudioEvent(action="amount_only"))
    │
    ▼ AudioEvent { action, amount, timestamp, raw_text,
    │              alternatives, word_timestamps, t_end }   ← M1 で拡張
    │
    ▼ audio_queue
```

### 統合処理パイプライン

```
AudioEvent (audio_queue)
    │
    ▼ IntegrationThread._handle_audio_event()
    │
    ▼ EvidenceLogWriter.write_audio(event)   ← v6.0+ M1: raw 観測を JSONL に常時記録
    │
    ├─ action="new_hand"  → GameState.new_hand(), BettingState.reset_for_new_hand(),
    │                        BeamEngine.reset_with_state(betting_state)   ← M3
    ├─ action="showdown"  → GameState.advance_street(SHOWDOWN)
    ├─ action="winner"    → _reconcile_with_beam(winner_seat) → gs.end_hand()  ← M3
    │
    └─ それ以外:
         seat = betting_state.actor_seat  ← button/blind 確定時の actor を最優先
                ?? game_state.get_current_player()   (fallback)
         │
         ▼ inferred = infer_action(event, betting_state, seat)
         │   └─ M2 から内部実装は infer_action_distribution() の薄いアダプタ:
         │      primary 仮説 (log_likelihood=0.0) が常に top → legacy と完全互換
         │
         ▼ BeamEngine.step_audio(event, seat)   ← v6.0+ M3: K=8 並行宇宙を更新
         │   └─ 各粒子で legal_actions × N-best 展開 → log_weight 加算 → top-K 剪定
         │
         ▼ game_state.apply_action(seat, inferred.action, inferred.amount)
         │
         ▼ betting_state.update_after_action(...)  ← actor_seat も次へ進める
         │
         ▼ camera/RFID コリオブレーション (±2秒ウィンドウ)
         │
         ▼ calc_confidence(has_rfid, has_audio, has_camera)
         │
         ▼ ActionRecord → on_action コールバック + 後に JSON 書き込み (ハンド終了時のみ)
```

### WINNER 後方修正 (v6.0+ M3)

```
AudioEvent(action="winner") 到着
    │
    ▼ winner_seat = _extract_seat_from_text(event.raw_text)
    │
    ▼ _finalize_hand(winner_seat)
    │
    ▼ _reconcile_with_beam(winner_seat):
    │   ├─ BeamEngine.apply_winner_filter(winner_seat, final_pot=None)
    │   │   ├─ winner_seat が fold した粒子 → log_weight = NEG_INF
    │   │   └─ final_pot 不整合粒子        → log_weight = NEG_INF (M3 初版は許容 50-200%)
    │   ├─ 新 MAP 列を取得
    │   └─ self._current_actions と zip (SB/BB_POST は除外):
    │       差分がある record を in-place mutate + needs_review=True
    │       + on_action_revised(record) コールバック発火
    │
    ▼ gs.end_hand(winner_seat)
    │
    ▼ HandSummary 構築 (修正後の _current_actions を読む) → JSON 書き込み 1 回のみ
```

**初版の妥協**: 後方修正では `(action, amount)` のみ in-place mutate し、`pot_after`/`stack_after` は stale のまま残置 (`needs_review=True` で「あとから修正可能性を保持」)。完全 replay 型は将来拡張。

### ハンド開始処理 (button → SB/BB → 自動 post → first actor)

```
AudioEvent(action="new_hand")
    │
    ▼ IntegrationThread._start_new_hand()
    │
    ├─ _resolve_button_seat()
    │   ├─ 1. set_next_button_seat() で予約された seat (一度限りの手動 override)
    │   ├─ 2. advance_button(_last_button_seat, active_seats) で左隣へ自動回転
    │   └─ 3. 未設定なら None (BettingState 未初期化のまま legacy フォールバック)
    │
    ├─ _compute_active_seats()  ← stack > 0 の seat を昇順
    │
    ▼ BettingState.start_hand(button, active, sb_amount, bb_amount)
    │   ├─ compute_blinds() で SB / BB を決定 (heads-up は BTN = SB)
    │   ├─ SB_POST / BB_POST を action_history と player_contrib_* に投入
    │   ├─ current_bet = bb_amount, last_raise_to = bb_amount, is_opened = True
    │   └─ actor_seat = compute_first_actor_preflop()
    │
    ▼ _post_blinds_to_records()
    │   └─ game_state.apply_action(seat, "bet", amount) でスタック/ポット反映
    │   └─ ActionRecord(action="SB_POST"/"BB_POST") を発火
```

### Confidence 行列

| センサー組み合わせ | confidence |
|------------------|-----------|
| RFID + audio + camera | 1.00 |
| RFID + audio | 0.95 |
| RFID + camera | 0.85 |
| RFID のみ | 0.70 |
| audio + camera | 0.80 |
| audio のみ | 0.50 |
| camera のみ | 0.30 |

### ディーラーボタン自動回転

- **基本**: 各ハンド開始時に前ハンドの button から左隣の active seat へ自動移動
- **active seat**: `stack > 0` の seat を昇順整列、欠席/離席/バストは自動スキップ
- **初回**: `IntegrationThread(initial_button_seat=...)` または GUI/CLI で 1 度だけ手動指定
- **手動補正**: `set_next_button_seat(seat)` で次 1 ハンドだけ上書き、適用後は自動進行へ復帰
- **SB/BB 自動 post**: ハンド開始時に `SB_POST` / `BB_POST` を `ActionRecord` および `action_history` に投入
- **first actor**: preflop は BB の左隣 (UTG)、postflop は button の左隣 live seat（HU は postflop=BB）

#### Heads-up 特例

| 局面 | actor |
|------|-------|
| HU preflop | BTN (= SB) が最初 |
| HU postflop | BB が最初 |
| HU blind | BTN=SB、対面=BB |

#### 操作 UI

- **GUI**: 下部コントロール row 0 に `BTN補正:` ドロップダウン。空欄=自動進行、値選択=1回限り手動上書き
- **GUI 表示**: ヘッダー 3 行目に `BTN: 1 | SB: 2 | BB: 3 | Actor: 4 | Street: preflop | To call: 200`
- **GUI 連動**: 手動入力席ドロップダウン (`_manual_seat_var`) は `betting_state.actor_seat` に追従更新
- **CLI**: `n <seat>` で次 1 ハンドだけ button を補正 (例: `n 5`)

#### ログ例

```
Button advance: previous=1 next=2 active=[1, 2, 3, 4, 5, 6]
Hand start: button=2 sb=3 bb=4 first_actor=5 blinds=(100/200)
Auto post: seat=3 action=SB_POST amount=100
Auto post: seat=4 action=BB_POST amount=200
```

### action="amount_only" の補完ルール (BettingState)

| 条件 | 推定アクション | reason |
|------|--------------|--------|
| `is_opened=False` | BET(amount) | amount_only_opening_bet |
| `amount == current_bet, contrib == 0` | CALL(amount) | amount_only_exact_call |
| `amount > current_bet, contrib == 0` | RAISE(amount) | amount_only_above_call |
| `amount > current_bet, contrib > 0` | RAISE(amount) | amount_only_reraise |
| `amount < current_bet` | None, needs_review=True | amount_only_below_call |
| `amount == 0` | None, needs_review=True | amount_only_no_amount |

---

## 主要モジュール詳細

### `audio/recognizer.py`

- `apply_corrections(text)`: `corrections.json` の補正テーブルを適用（ホットリロード対応）
- `parse_amount(text)`: テキストから最初の金額表現を int で返す（漢数字・K単位・カンマ区切り対応）
- `parse_action(text) → Optional[AudioEvent]`: 音声テキスト → AudioEvent 変換のメインエントリ
  - `apply_corrections()` → `normalize()` → `ACTION_KEYWORDS` フォールバック → amount_only 検出の順に処理

### `audio/speech_normalizer.py`

- `SpeechNormalizer.normalize(text) → NormalizedResult`: 音声テキストの正規化
  - `action_aliases`: "ベッド"→"BET" 等の誤認識吸収
  - `seat_aliases`: "二番"→"シート2" 等の席番号正規化
  - `number_aliases` + ひらがな/漢数字パーサ: "ろくひゃく"→600, "二千五百"→2500
  - 金額は BET/RAISE のみ抽出 (CALL/CHECK は `amount=None`)
- `init_normalizer(path)`: アプリ起動時に 1 度だけ呼び出すシングルトン初期化
- `normalize(text)`: モジュールレベルの公開 API

### `integration/action_inference.py`

- `BettingState`: ハンド単位 + ストリート単位の状態を一元保持
  - **ハンド単位**: `button_seat`, `sb_seat`, `bb_seat`, `sb_amount`, `bb_amount`, `actor_seat`, `last_aggressor`, `player_contrib_hand`, `action_history`, `is_initialized`
  - **ストリート単位**: `current_bet`, `is_opened`, `last_raise_to`, `player_contrib_this_street`, `folded_seats`, `all_in_seats`
  - `start_hand(button, active, sb, bb)`: 新ハンド時に SB/BB を自動 post し first actor を確定
  - `reset_for_new_street()`: contrib リセット + postflop first actor 再計算
  - `update_after_action(seat, action, amount)`: contrib 更新 + actor を次の live seat へ進行
  - `call_amount_for(seat)`: 該当 seat がコールするのに必要な追加投入額
- `InferredAction`: 推定/検証結果 (`action`, `amount`, `confidence`, `needs_review`, `reason`)
- `infer_action(event, state, actor_seat) → InferredAction`: 中心 API。v6.0+ M2 から内部実装は `infer_action_distribution()` の薄いアダプタ
- `infer_action_distribution(event, state, actor_seat, prior=None) → list[ActionHypothesis]`: 1 観測 → 仮説リスト (v6.0+ M2)。primary 仮説 (log_likelihood=0.0) を先頭に置き、legal_actions の代替仮説を負の log_likelihood で追加。`max(result, key=h.log_likelihood)` は legacy `_infer_action_core()` の出力と完全一致
- `_infer_action_core(event, state, actor_seat)`: 既存決定木 (内部関数)。amount_only 6 ケース + explicit action 検証

### `integration/observation_model.py` (v6.0+ M2)

ベイズ尤度関数の中核。学習なしの固定 prior。

- `EvidenceInterval(kind, t_start, t_end, payload)`: 観測の正規化表現 (`audio`/`rfid`/`camera`)
- `PriorParams`: 固定 prior の集約 dataclass
  - `mu_audio=0.4, sigma_audio=0.6, mu_rfid=0.2, sigma_rfid=0.4` (時刻整合)
  - `lexicon: dict[action, dict[word, pseudocount]]` (Dirichlet, `speech_normalization.json` から構築、pseudocount=5、`alpha_unknown=0.1`)
  - `position_prior: {early, middle, late}` (3 バケット)
  - `bet_raise_log_sigma=0.7, bet_raise_mean_factor=2.0` (log-Normal amount)
- `ActionHypothesis(action, amount, log_likelihood, reason, needs_review, confidence, raw_text, normalized_text, seat)`: 1 仮説の評価結果。M3 では `seat` フィールドで beam の `apply_winner_filter` が「どの seat が fold したか」を判定
- `compute_log_likelihood(evidence, action, amount, state, actor_seat, prior) → float`: 観測尤度 + 事前 (`log P(E|a) + log P(a|state)`)。legal 違反は `NEG_INF`
- `is_legal(action, amount, state, actor_seat) → bool`: ハード制約 (CHECK は `current_bet ≤ contrib` 時のみ、RAISE は `is_opened` のみ、folded/all-in は不可、etc.)
- `default_amount_for(action, state) → int`: 代替仮説の典型 amount (CALL=current_bet、BET=2bb、RAISE=current_bet*2 など)
- `default_priors(normalization_path=None) → PriorParams`: `speech_normalization.json` から固定 prior を構築
- `evidence_from_audio/rfid/camera(event) → EvidenceInterval`: 各イベント → EvidenceInterval 変換

### `integration/beam_search.py` (v6.0+ M3)

sequence 事後分布の MAP を Beam Search で近似する。

- `BeamParticle(actions, log_weight, state)`: 並行宇宙 1 つ分。`state` は BettingState の deep copy
- `BeamEngine(K=8, prior=None, enable_resample=False, sink=None)`: K 個の粒子集合を管理
  - `MAX_BRANCHING_PER_PARTICLE = 4`: 各 step で 1 粒子が分岐する候補数の上限
  - `reset_with_state(state)`: 新ハンド開始時。全粒子を 1 つの初期粒子に潰す
  - `step_audio(event, actor_seat)`: 各粒子で `infer_action_distribution()` から候補を列挙、`legal_actions × N-best` を展開、log_weight 加算、top-K 剪定
  - `map_action() → ActionHypothesis`: top 粒子の最新アクション (リアルタイム速報用)
  - `map_sequence() → list[ActionHypothesis]`: top 粒子の全アクション列
  - `apply_winner_filter(winner_seat, final_pot=None) → list[ActionHypothesis]`: 「winner_seat が fold した粒子」と「final_pot 乖離が大きい粒子」を `NEG_INF` に落とし、新 MAP を返す
  - `snapshot_top(n=3) → list[dict]`: top-n 粒子を辞書化 (evidence_log の sink 用)
- `enable_resample=False` (M3 固定): 決定論的 top-K 剪定のみ。粒子フィルタへの確率的サンプリング移行口は将来 (v6.0+ B4)

### `output/evidence_log.py` (v6.0+ M1)

raw 観測イベントを append-only な JSONL に記録するロガー。session JSON と完全分離。

- `EvidenceLogWriter(log_dir, session_id)`: `logs/evidence_<session_id>.jsonl` を line-buffered で開く
- `write_audio(event, extra=None)`: AudioEvent を 1 行記録 (action, amount, raw_text, t_end, alternatives, word_timestamps)
- `write_rfid(event, extra=None)`: RFIDEvent を 1 行記録 (tag_id, card, role, seat, t_end)
- `write_camera(event, extra=None)`: CameraEvent を 1 行記録
- `extra` 引数で beam top-3 スナップショットなど追加メタデータを merge 可能
- 書き込み失敗は warning ログを出して継続 (IntegrationThread をクラッシュさせない)

### `core/showdown_tracker.py` (Phase 1 skeleton)

Phase 2 予定: RFID / manual / 派生観測から `RevealedHand` を蓄積し、全 live seat の hole cards が揃った時点で showdown_ready を通知する。

- `ShowdownTracker.observe(hand: RevealedHand)`: seat 単位の hole cards を取り込む (Phase 2 で実装)
- `revealed_hands() → list[RevealedHand]`: 観測集合を返す
- `is_showdown_ready(live_seats) → bool`: 全 live seat の hole cards が揃ったか
- `project_to_summary_dict() → dict[int, list[str]]`: `HandSummary.showdown_revealed_cards` 用の簡略表現に投影
- `reset()`: 新ハンド開始時に状態をクリア

canonical な内部表現は `RevealedHand` (source / observed_at を持つ)。`HandSummary.showdown_revealed_cards` は JSON 公開用の簡略投影。

### `core/settlement.py` (v6.0+ Phase 2-A) ✅ 実装済み

BettingState の seat 別 contribution と RevealedHand から main / side pot を含む全 pot の決済を計算する pure logic 群。HandFinalizer や engine への組み込みは Phase 2-B 以降。

**用語 (関数間で一貫)**:
- **`eligible_seats`**: その pot を**勝ちうる**非 fold seat 集合 = `layer_seats - folded_seats`。fold 済み seat の chips は pot の原資には残るが、彼ら自身は eligible には入らない
- **`contenders`**: `eligible_seats` のうち**現時点で revealed hand があり rank 評価できる** seat 集合 = `eligible_seats ∩ {rh.seat for rh in revealed_hands}`。muck した seat や RFID 未観測 seat は eligible だが contenders から外れる
- **`winning_seats`**: `contenders` の中で最大 rank の seat 群 (split 含む)
- Phase 2-B HandFinalizer は「eligible はいるが contenders が空 / 不足」を `incomplete` の判定材料に使う

**API**:
- `compute_pot_settlements(betting_state, revealed_hands, board) → list[PotSettlement]`:
  Stratified Side Pot Decomposition。`player_contrib_hand` を最小正額で層化し、各層で
  pot_amount = min_pos × len(layer_seats) を計算。eligible は layer_seats から folded を除外。
  contenders = eligible ∩ revealed で `evaluate_hand_rank` を実行し最大 rank の seat 群を
  winners に。最初の層を `"main"`、それ以降を `"side"` でマーク。
- `evaluate_hand_rank(hole_cards, board) → int`:
  pokerkit `StandardHighHand.from_game(hole_str, board_str).entry.index` を返す。
  高値 = 強い手。役種 + キッカーまでエンコードされた序数なので、数値比較で
  winner / tie (split pot) 判定が可能。
- `distribute_split_pot(pot_amount, winning_seats, button_seat=None) → dict[int, int]`:
  base = pot_amount // M、remainder = pot_amount % M。**odd chip は最低 seat 番号
  優先 (lowest-seat priority)** で配分。例: `(1001, [2, 5, 9])` → `{2:334, 5:334, 9:333}`。
  `button_seat` は将来 (button 隣優先など別ルールに切り替え) の拡張ポイントで現状未使用。

**前提とスコープ (重要)**:
- `evaluate_hand_rank()` は **Texas Hold'em + StandardHighHand (high-hand)** 専用。
  「entry.index が大きいほど強い」の単調性は pokerkit の StandardHighHand ルックアップ
  テーブルが保証している性質であり、他 variant には**そのまま使えない**:
  - **Lowball** (2-7 / A-5): 弱い手ほど強い → 単調性の向きが逆
  - **Hi/Lo split** (Omaha Hi/Lo 等): low hand 評価器が別途必要
  - **Short deck (6+)**: フラッシュとフルハウスの順位が変わる
- 他 variant 対応は別の評価関数 (例: `evaluate_lowball_hand_rank`) を追加し、
  HandFinalizer 側で variant に応じて使い分ける設計に拡張する想定
- `compute_pot_settlements` は ``betting_state.player_contrib_hand`` と
  ``betting_state.folded_seats`` だけを参照する duck-typed 設計 (本物の `BettingState`
  とテスト stub の両方を受ける)

### `core/hand_boundary.py` (Phase 2-C 実装済)

audio / RFID / camera 観測の流れから hand の開始・終了境界 (hand window) を検出するステートマシン。

- `BoundaryEvent(kind, hand_id, t_start, t_end, reason)`: 1 境界。`kind ∈ {"start", "end"}`、`reason` は `"audio_new_hand"` / `"audio_new_hand_implicit_end"` / `"audio_winner"` / `"board_cleared"` / `"hole_cards_appeared"` のいずれか
- `HandBoundaryDetector(board_empty_quiet_sec=1.5)`:
  - `observe_audio_event(event)`: `new_hand` で start、`winner` で end。in_hand 中 `new_hand` は `[end, start]` の 2 件を返す
  - `observe_rfid_event(event)`: board / hole の内部状態を更新。state-only
  - `observe_camera_event(event)`: state は変えず `tick(timestamp)` 相当 (board quiet 経過チェック)
  - `observe_board_state(board_cards, now)`: スナップショット API。非空→空 遷移を検出 → quiet 経過で end
  - `observe_hole_state(hole_cards, now)`: スナップショット API。idle + board 空 + 2+ seat に cards で start
  - `tick(now)`: 時刻のみ進める (no event)
  - 返り値はすべて `list[BoundaryEvent]` (0/1/2 件)

### `core/hand_reconstructor.py` (Phase 3 実装済)

hand window 単位の retrospective inference。EvidenceLog の events をその hand だけ
頭から再生して **offline HandSummary'** を生成し、online HandSummary と機械可読な
diff を出す。**online の HandSummary / JSON / PHH は一切 mutate しない**
(追加のオフラインパスとして動作)。

#### Phase 3 MVP の仕様・制約事項 (明文化)

1. **online-bootstrap-assisted reconstruction (raw-only ではない)**:
   Phase 3 は raw EvidenceLog 単独から button / SB / BB を立てる *raw-only*
   reconstruction ではなく、``online_summary`` の `players` / `blinds` /
   `actions[SB_POST,BB_POST]` を bootstrap の入力として使う
   *online-bootstrap-assisted reconstruction*。``_init_betting_state`` は
   ``online_summary.actions`` の SB_POST seat から button を逆算 (HU: BTN=SB、
   それ以外: BTN は active 内で SB の 1 つ前)。``online_summary`` も
   ``initial_state`` も無い場合は bs を起こせず ``reason="reconstruction_skipped"``
   で抜ける。raw-only bootstrap (RFID hole_cards 出現や音声 ``new_hand`` 時点の
   active seats から button を推定する等) は Phase 4-B の課題。

2. **`winner_seat_hint` は oracle ではなく終端の補助制約**:
   ``AudioEvent(action="winner")`` から抽出した seat は **強観測ではなく**、
   ``beam.apply_winner_filter(winner_seat_hint, final_pot=None)`` への入力として
   使う「終端の補助制約の 1 つ」。粒子集合の絞り込み (winner_seat が fold した
   宇宙を弱める / 削る) に使うのみで、settlement の確定は ``HandFinalizer`` に
   委ねる (``HandFinalizer`` は ``winner_seat_hint`` と settlement (= payouts)
   が食い違えば ``review_required=True`` を立てる)。

3. **`confidence` は operational metric であってモデル事後確率ではない**:
   ``HandReconstructionResult.confidence = consumed_count / audio_count`` は
   **audio evidence の消費率** (= ``beam.step_audio`` → ``bs.update_after_action``
   が成功した割合) を示す operational metric。値域は [0, 1] だが、Bayes posterior
   や top-1 確率としては解釈しないこと。低い値は「再構成中に illegal action や
   beam fail が多発した」ことを示すヒントに過ぎない。モデル確率 (top-1 vs top-2
   の log 差、エントロピー、Brier score 等) は Phase 4+ の課題。

#### API

- `HandReconstructionResult(actions, summary, needs_review, reason, diff, confidence)`:
  - `actions`: 再構成 ActionRecord 列 (SB_POST/BB_POST + beam MAP の player actions)
  - `summary`: offline HandSummary' (bootstrap 失敗時は None)
  - `needs_review`: online との diff があれば True (online_summary 未指定なら False)
  - `reason`: `"reconstructed_no_diff"` / `"reconstructed_with_diff"` / `"reconstructed"` / `"reconstruction_skipped"`
  - `diff`: 差分 dict `{field: {"online": ..., "offline": ...}}` (一致 or 比較不能なら None)
  - `confidence`: **operational metric** = consumed_count / audio_count (上記参照、確率ではない)
- `HandReconstructor(beam_K=8, prior=None, finalizer=None)`: テスト inject 可能
- `HandReconstructor.reconstruct_from_events(events, initial_state=None, online_summary=None) → HandReconstructionResult`:
  1. `initial_state` 優先、なければ `online_summary` から `_init_betting_state` で bootstrap
     (button は `actions` の SB_POST/BB_POST から逆算: HU なら BTN=SB、それ以外は SB の左隣)
  2. `BeamEngine(K)` を新規構築 → `reset_with_state(bs)`
  3. events を時刻順走査: audio 通常 action → `beam.step_audio` → `bs.update_after_action`、
     audio "winner" → `winner_seat_hint` に保存 (補助制約として後段に渡す)、
     RFID seat → hole_cards 蓄積、RFID board → board 蓄積 + street 昇格
  4. winner_seat_hint があれば `beam.apply_winner_filter(...)` で粒子集合を絞る
     (= 終端の補助制約として適用、oracle ではない)
  5. `HandFinalizer.finalize(...)` で offline summary を構築
  6. `_compute_diff(online, offline)` で diff → `needs_review` を立てる
- `_compute_diff(online, offline) → Optional[dict]`: 比較対象は
  `resolution_status` / `resolution_type` / `winner_seat` / `pot_total` /
  `seat_payouts` / `showdown_revealed_cards` / `actions` (seat/action/amount のみ、
  timestamp や pot_after はノイズなので無視)

### `output/reconstruct_session.py` (Phase 3 CLI)

```
python -m output.reconstruct_session --session logs/<session>.json
```

`logs/<session>.json` (JsonWriter 出力) と `logs/evidence_<session>.jsonl` (EvidenceLogWriter
出力) を読み、各 hand を `HandReconstructor.reconstruct_from_events(window, online_summary=...)`
にかけ、結果を `logs/reconstruct_<session>.jsonl` に 1 hand 1 行で書く。

出力 schema:
```json
{
  "hand_id": int,
  "needs_review": bool,
  "reason": "reconstructed_no_diff" | "reconstructed_with_diff" | "reconstruction_skipped",
  "diff": {...} | null,
  "confidence": float | null,
  "online_summary": {...},          // JSON dict (mutate されない)
  "offline_summary": {...} | null   // 再構成 HandSummary
}
```

オプション: `--output PATH` / `--evidence PATH` (省略時は session.parent / "reconstruct_<id>.jsonl" / "evidence_<id>.jsonl")。`--quiet` で info ログを抑制。

**約束**: online JSON (`<session>.json`) も PHH も一切 mutate しない。差分情報の
発信は別ファイル (`reconstruct_<session>.jsonl`)。後段の GUI / 監視ツール / Phase 4+
の自動 patch ロジックがこれを読んで判断する想定。

### `output/replay_hand.py` (Phase 2-C 実装済)

- `EvidenceRecord(timestamp, kind, event, payload)`: 1 観測の type-restored 表現
- `load_evidence_log(log_path) → list[EvidenceRecord]`: `logs/evidence_<session>.jsonl` を読んで AudioEvent / RFIDEvent / CameraEvent に再構築 (`alternatives` / `word_timestamps` / `t_end` も含む)
- `extract_hand_windows(records, detector=None) → dict[hand_id, list[EvidenceRecord]]`: detector を頭から流して hand_id ごとに窓化。end 未観測の hand は dict に含めない

### `core/hand_finalizer.py` (Phase 2-B 実装済)

BettingState + showdown 観測 + board から `HandSummary` を組み立て、`integration/engine.py:_finalize_hand` の主経路として使われる。

- `HandFinalizer.finalize(betting_state, board, revealed_hands, pot_total, players_info, *, hand_id, session_id, started_at, ended_at, blinds, actions, board_source="", winner_seat_hint=None) → HandSummary`
  - `live_seats = active_seats - folded_seats` を計算
  - `len(live_seats) == 1` → `_build_fold_win`: PotSettlement 1 件で `resolution_type="fold_win"`
  - `len(live_seats) >= 2` で showdown 経路:
    - board が 5 枚未満 → `_build_incomplete(reason="board_under_5")`
    - revealed_hands が live_seats を覆っていない → `_build_incomplete(reason="revealed_hands_missing")`
    - `settlement.compute_pot_settlements(...)` 例外 / 空 → `_build_incomplete(reason="settlement_exception"/"empty_pots")`
    - 成功時: `len(pots) >= 2` → `sidepot_showdown`、`winning_seats` 複数 → `showdown_split`、それ以外 → `showdown`
  - `len(live_seats) == 0` (退化) → incomplete
  - `seat_payouts` は全 pot の payouts を seat 別合算
  - `showdown_revealed_cards` は `RevealedHand` 集合の seat→cards 投影 (canonical は内部の RevealedHand)
  - `winner_seat` (compatibility field) は最大 payout の seat (tie 時は最低 seat 番号)
  - `winner_seat_hint` (音声 WINNER) は **補助観測**: settlement と食い違うと `review_required=True` を立てる ("Oracle 一発確定" ではなく異常検知材料)
  - incomplete は常に `review_required=True`
- `_pick_primary_winner(seat_payouts, fallback_hint, live_seats, active_seats)`: compat field の決定ヘルパ

### `integration/action_order.py`

ディーラーボタン位置を起点とした SB/BB/actor 算出のヘルパ群（pure functions）。

- `get_next_active_seat(start, active, inclusive=False)`: 左隣の active seat を返す（円環、9 超で 1 へ wrap）
- `advance_button(current_button, active)`: 次ハンドの button 席（左隣の active）
- `compute_blinds(button, active) → (sb_seat, bb_seat)`: 通常時は左隣 / 左 2 隣、heads-up は BTN=SB
- `compute_first_actor_preflop(button, active)`: 通常は BB の左隣 (UTG)、heads-up は BTN
- `compute_first_actor_postflop(button, active, folded, all_in)`: 通常は button 左隣 live seat、heads-up は BB
- `advance_actor(current, active, folded, all_in)`: 次の live seat へ進める

### `core/game_state.py`

- `GameStateManager`: 簡易ゲーム状態管理（ラウンドロビンターン順）
  - `new_hand()`, `apply_action(seat, action, amount)`, `advance_street(street_enum)`
  - `get_current_player()`, `get_stacks()`, `get_stack(seat)`, `rebuy(seat, amount)`
  - PokerKit は PHH エクスポート専用。ゲームロジックには未使用

### `rfid/`

- `RFIDHTTPReceiver`: ESP32 から HTTP POST で受信（`transport="http"` 設定時）
- `RFIDThread`: pyscard PC/SC 直接読み取り（`transport="pcsc"` 設定時）
- `CardMaster`: `rfid_cards.json` の tag_id → card_code マッピング管理

---

## コーディング規約

- 型ヒント必須（`from __future__ import annotations` 使用）
- データクラスは `@dataclass` を使用
- スレッド間通信は `queue.Queue` のみ（共有変数の直接参照禁止）
- GUI スレッドからビジネスロジックを呼ばない
- 定数は `core/constants.py` に集約
- ログは `logging` モジュール使用（`print` 禁止 — ただし `main.py` の CLI 出力は除く）
- コメントは「なぜ」が自明でない場合のみ記述（「何をするか」は記述不要）
- 正規表現はすべて raw string (`r"..."`) で記述

---

## 設定ファイル

### `config_default.json` の構造

```json
{
  "session": { "num_seats": 6, "blinds": {"sb": 100, "bb": 200}, "log_dir": "./logs" },
  "audio": {
    "engine": "whisper",          // "whisper" | "vosk"
    "whisper_model": "medium",
    "language": "ja",
    "normalization_file": "./speech_normalization.json",
    "vosk_model_path": "./models/vosk-model-ja-0.22"
  },
  "rfid": {
    "enabled": false,
    "transport": "http",          // "http" | "pcsc"
    "bind_port": 8787,
    "readers": { "seat_1": {"role": "seat", "seat": 1}, ... }
  }
}
```

### `corrections.json`

```json
{"ベッド": "ベット", "ベッズ": "ベット"}
```

アプリ起動中に編集すると次の認識で自動反映される（mtime ホットリロード）。

### `speech_normalization.json`

```json
{
  "action_aliases": {"ベッド": "BET", "椅子": "RAISE", "ゴール": "CALL", ...},
  "seat_aliases":   {"シート1": 1, "一番": 1, ...},
  "number_aliases": {"ぜろ": 0, "いち": 1, ..., "まん": 10000}
}
```

---

## エラーハンドリング方針

- 認識エラーでクラッシュしない → `try/except` で捕捉し `needs_review=True` を付与
- ハンド完了ごとにディスクへ書き込む（バッファリングしない）
- ログファイルは追記モード（既存セッションデータを上書きしない）
- ESP32 停止・WiFi 切断時 → `rfid.enabled=false` で RFID なしモード継続動作

---

## よく使うコマンド

```bash
# 依存関係インストール
pip install -r requirements.txt

# CLI モードで起動（音声認識 + ログ出力）
python main.py --cli

# GUI モードで起動
python main.py

# テスト実行（test_vision.py は cv2 未インストール時はスキップ）
pytest tests/ -v --ignore=tests/test_vision.py

# JSON ログを PHH 形式にエクスポート
python main.py --export-phh logs/session_xxx.json

# 音声正規化のスタンドアロン動作確認
python audio/speech_normalizer.py
```

---

## テストスイート現状

```
pytest tests/ --ignore=tests/test_vision.py
→ 416 passed  (test_vision.py は cv2 未インストールのため収集エラー、既知問題)
```

| テストファイル | 内容 |
|---------------|------|
| test_action_inference.py | BettingState / infer_action() 34 ケース |
| test_normalizer.py | SpeechNormalizer / 数値正規化 89 ケース |
| test_integration.py | IntegrationThread confidence マッチング |
| test_e2e.py | 全体 E2E シナリオ (フォールド/オールイン) |
| test_game_state.py | GameStateManager |
| test_rfid.py / test_rfid_http.py | RFID 受信 |
| test_phh_exporter.py | PHH 形式出力 |
| test_parser.py | parse_action / parse_amount |
| test_evidence_log.py | EvidenceLogWriter / IntegrationThread への hook (v6.0+ M1, 6 件) |
| test_observation_model.py | 観測尤度関数 / Normal / Dirichlet / 位置 prior / 金額整合 (v6.0+ M2, 26 件) |
| test_inference_equivalence.py | infer_action() 薄アダプタの legacy 等価性 (v6.0+ M2, 16 件) |
| test_beam_search.py | BeamEngine 単体 (剪定 / 決定論 / WINNER フィルタ / snapshot) (v6.0+ M3, 15 件) |
| test_bayesian_e2e.py | WINNER 後方修正 E2E (3-handed seat3 fold → winner=seat3 で flip) (v6.0+ M3, 3 件) |
| test_settlement_models.py | RevealedHand / PotSettlement / HandSummary 新 field / PHH gate / legacy_winner_finalize E2E (Phase 1, 6 件) |
| test_settlement_logic.py | distribute_split_pot / evaluate_hand_rank / compute_pot_settlements (heads-up / 3-way all-in / split / fold / 退化) (Phase 2-A, 20 件) |
| test_hand_finalizer.py | HandFinalizer fold_win / showdown / sidepot_showdown / showdown_split / incomplete / winner_hint mismatch (Phase 2-B, 11 件) |
| test_hand_boundary.py | HandBoundaryDetector (audio / board cleared / hole appeared) + extract_hand_windows + IntegrationThread 結合 + EvidenceLog round-trip (Phase 2-C, 19 件) |
| test_hand_reconstructor.py | HandReconstructor (online↔offline diff / fallback / 複数 hand / _compute_diff) + reconstruct_session CLI round-trip (Phase 3, 14 件) |

---

## 実装状況

| 機能 | 状態 | 備考 |
|------|------|------|
| 音声認識 (Whisper) | ✅ 完了 | recognizer.py |
| 音声認識 (Vosk) | ✅ 完了 | vosk_recorder.py |
| 誤認識補正 (corrections.json) | ✅ 完了 | ホットリロード対応 |
| 音声正規化 (aliases) | ✅ 完了 | speech_normalizer.py |
| 数値正規化 (ひらがな/漢数字) | ✅ 完了 | speech_normalizer.py |
| amount_only 補完 | ✅ 完了 | action_inference.py |
| RFID HTTP 受信 | ✅ 完了 | rfid/http_receiver.py |
| RFID PC/SC 受信 | ✅ 完了 | rfid/reader_thread.py |
| RFID カード照合 | ✅ 完了 | rfid/card_master.py |
| ストリート自動遷移 (RFID) | ✅ 完了 | board枚数3/4/5枚で遷移 |
| Confidence 算出 | ✅ 完了 | センサー組み合わせ行列 |
| JSON ログ出力 | ✅ 完了 | output/json_writer.py |
| PHH エクスポート | ✅ 完了 | output/phh_exporter.py |
| GUI ダッシュボード | 🔨 部分実装 | gui/dashboard.py |
| ディーラーボタン自動回転 | ✅ 完了 | action_order.py, BettingState.start_hand |
| SB/BB 自動 post | ✅ 完了 | BettingState.start_hand, ActionRecord(SB_POST/BB_POST) |
| Preflop/Postflop first actor | ✅ 完了 | compute_first_actor_preflop/postflop |
| actor 自動進行 | ✅ 完了 | BettingState.update_after_action |
| GUI BettingState 表示 / BTN補正 | ✅ 完了 | dashboard.py: _lbl_betting, _button_seat_menu |
| 手動入力席の actor 追従 | ✅ 完了 | dashboard.py: _sync_manual_seat |
| PokerRuleEngine | 🔨 部分実装 (v6.0+ M2) | observation_model.is_legal() でハード制約のみ実装 |
| AudioStreamBuffer 状態機械 | ❌ 未実装 (v6.0) | 確認型発話の PENDING なし |
| 音声/RFID 確率融合エンジン (中核) | ✅ 完了 (v6.0+ M3) | beam_search.py + observation_model.py で Beam Search K=8 動作 |
| ASR 観測の richness (N-best, word_timestamps, RFID t_end) | ✅ 完了 (v6.0+ M1) | core/events.py 拡張、recorder.py / vosk_recorder.py で生 metadata を AudioEvent に詰める |
| 観測ログ (B0) `evidence_<session>.jsonl` | ✅ 完了 (v6.0+ M1) | output/evidence_log.py、IntegrationThread から audio/rfid/camera を append |
| 観測尤度関数 (φ_lex, φ_amount, φ_time, 位置 prior) | ✅ 完了 (v6.0+ M2) | observation_model.py: 固定 prior、Dirichlet 辞書 + log-Normal 金額 + 3 バケット位置 prior |
| 仮説リスト型 infer_action (薄アダプタ) | ✅ 完了 (v6.0+ M2) | infer_action_distribution() を新設、infer_action() はその top-1 アダプタ |
| Beam Engine (K=8, 決定論的) | ✅ 完了 (v6.0+ M3) | beam_search.py: BeamEngine + reset_with_state / step_audio / map_action / apply_winner_filter |
| WINNER 後方修正 (in-place mutate) | ✅ 完了 (v6.0+ M3) | engine.py: _reconcile_with_beam, on_action_revised コールバック |
| ベイズ推定によるアクション推定 (中核モデル) | ✅ M1–M3 完了 (v6.0+) | 学習なし固定 prior、MAP 推定、sequence 事後分布 |
| 共役事前分布によるオンライン学習 (B2/B3) | ❌ 未実装 (v6.0+) | Normal-Gamma / Dirichlet の closed-form 更新は将来 |
| ベイズリスク最小化 (review 自動判定 B5) | ❌ 未実装 (v6.0+) | top-2 対数差 / エントロピーは将来 |
| 階層ベイズ・夜間 MCMC (B6) | ❌ 未実装 (v6.0+) | 卓 hyperprior θ_0 は将来 |
| Active learning (GUI クリック → posterior 更新 B7) | ❌ 未実装 (v6.0+) | GUI 連動学習ループは将来 |
| 粒子フィルタ (確率的サンプリング) | ❌ 未実装 (v6.0+ B4) | beam_search.enable_resample フラグだけ用意済み |
| 完全 replay 型後方修正 (pot/stack 再計算) | ❌ 未実装 (将来) | 現在は (action, amount) のみ in-place mutate、pot_after/stack_after は stale |
| ディーラー別オンライン学習 | ❌ 未実装 (v6.0+) | §「将来計画」を参照 |
| **Settlement 中心データモデル (HandSummary 拡張)** | ✅ 完了 (Phase 1) | `resolution_status` / `resolution_type` / `seat_payouts` / `pots` / `showdown_revealed_cards` |
| **PotSettlement / RevealedHand dataclass** | ✅ 完了 (Phase 1) | `core/hand_log.py` 同居 (暫定配置、Phase 2 で再評価) |
| **PHH 出力 gate (final のみ出力)** | ✅ 完了 (Phase 1) | `phh_exporter.export()` 先頭で意図的 skip |
| **showdown_tracker / settlement / hand_finalizer skeleton** | ✅ 完了 (Phase 1) | NotImplementedError stub、Phase 2 で本実装 |
| side pot 計算 (Stratified Decomposition) | ✅ 完了 (Phase 2-A) | `core/settlement.py:compute_pot_settlements` |
| split pot 配分 (odd chip = lowest seat 優先) | ✅ 完了 (Phase 2-A) | `core/settlement.py:distribute_split_pot` |
| hand evaluator (pokerkit StandardHighHand) | ✅ 完了 (Phase 2-A) | `core/settlement.py:evaluate_hand_rank` |
| HandFinalizer 本実装 (engine._finalize_hand 置換) | ✅ 完了 (Phase 2-B) | `core/hand_finalizer.py`: fold_win / showdown / showdown_split / sidepot_showdown / incomplete を判別、settlement core を呼んで pots を埋める |
| engine._finalize_hand → HandFinalizer 経由化 | ✅ 完了 (Phase 2-B) | `_finalize_hand(winner_seat: Optional[int])` + `_apply_payouts_to_gamestate()` adapter |
| `winner` 音声の補助観測化 (Oracle 級ではない) | ✅ 完了 (Phase 2-B) | `winner_seat_hint` として渡し、settlement と食い違うと review_required=True |
| HandBoundaryDetector (audio + RFID hand window 検出) | ✅ 完了 (Phase 2-C) | `core/hand_boundary.py`: new_hand / winner / board cleared / hole appeared を検出 |
| hand window 抽出 (EvidenceLog → events 窓化) | ✅ 完了 (Phase 2-C) | `output/replay_hand.py:load_evidence_log` / `extract_hand_windows` |
| IntegrationThread の live hand window バッファ | ✅ 完了 (Phase 2-C) | `_current_hand_events` / `_completed_hands` / `_track_evidence` |
| HandReconstructor 本実装 (beam 再生 + finalizer 再呼び出し) | ✅ 完了 (Phase 3) | `core/hand_reconstructor.py`: events → BeamEngine + HandFinalizer 再生 → online との diff、`needs_review` 自動判定 |
| 後処理 CLI (online↔offline diff) | ✅ 完了 (Phase 3) | `output/reconstruct_session.py`: session JSON + evidence JSONL → `reconstruct_<id>.jsonl` |
| ShowdownTracker 本実装 | 🔨 skeleton (Phase 2-D 以降) | `core/showdown_tracker.py` |
| gs.end_hand → end_hand_with_payouts 拡張 | ❌ 未着手 (Phase 2-D 以降) | 現状 Phase 2-B では `_apply_payouts_to_gamestate` adapter が primary winner で legacy gs.end_hand を呼んでいる |

---

## 将来計画 (v6.0+): 音声/RFID 時刻ベース確率融合

現状の `infer_action` は「単一の AudioEvent から 1 つの (action, amount) を確定し、矛盾なら `needs_review`」という二値判定。これを **「観測ごとに尤度ベクトルを作り、ポーカールール制約下で時刻整合性付きの最尤アクション列を MAP 推定する確率フレーム」** に拡張する。

### 設計の核

1. **観測の区間化**: `RFIDEvent` に `t_end` を追加し、`(t_start, t_end)` で「seat_n がカードを持っていた期間」を表現する。フォールドはこの区間の終端で発生したと解釈できる強観測になる。ASR 側も word-level timestamps と N-best 仮説を保持して `EvidenceInterval` に統一する。

2. **観測尤度のベクトル化**: 1 観測 → 各候補 action への尤度 dict (`{"CALL": 0.6, "RAISE": 0.3, ...}`) に分解。`infer_action` の戻り値を単一 `InferredAction` から「複数仮説リスト」に拡張する。

3. **Beam Search 推論**: K=64 程度の partial action sequence を時刻順に並走し、各到着観測で全粒子を更新。legal_actions はハード制約 (確率 0)、position prior はソフト制約 (log prior 加算)。

4. **音声/RFID 時刻アライメント**: ディーラー固有の遅延分布 `(μ_d, σ_d)` を学習し、確率窓 `N(τ - t_obs; μ_d, σ_d²)` で固定 ±2s ウィンドウを置換。

5. **ハンド終了時の後方修正**: 最終ポット額・残スタックは強観測。粒子集合をこれらの制約で再フィルタし、MAP 列に collapse。残った曖昧粒子のみ `needs_review` で GUI へ。**Phase 1 以降は WINNER 音声を canonical な終局表現に使わず**、hand state (live_seats / RevealedHand / board / pot) から自律的に finalization する設計に移行する。

6. **ディーラー別オンライン学習**: 観測モデルパラメータ (音声遅延 / RFID 遅延 / アクション語彙頻度 / 位置別 prior / 数値表現の好み) を Dirichlet/Normal-Gamma で逐次ベイズ更新。Whisper/Vosk 本体は不変、観測モデル側だけで適応する。

7. **キャリブモード**: 起動時に 60 秒の音声サンプルで `(μ_d, σ_d)` の初期値と語彙頻度を取得する設定 UI。

8. **Active learning**: GUI レビュー UI のクリック結果を観測モデルに教師として取り込む (ハンド進行で精度が単調増加)。

### 段階導入ロードマップ

| 段階 | 内容 | 既存コードへの影響 |
|------|------|------------------|
| L0 | `RFIDEvent` に `t_end` 追加、区間化 | events.py に 1 フィールド |
| L1 | Vosk / Whisper の word-level timestamps + N-best を `EvidenceInterval` に格納 | vosk_recorder.py / recognizer.py |
| L2 | 時刻矛盾チェッカ (`check_temporal_consistency`) を engine に挿入 (fold but card present 等) | engine.py に関数追加 |
| L3 | 観測尤度ベクトル化 (`infer_action → list[InferredAction]`) | action_inference.py |
| L4 | Beam Search (K=8 → 64) で複数仮説並走 | engine.py 内部状態を beam に |
| L5 | ハンド終了時の WINNER/POT 検算で粒子フィルタ | `_finalize_hand` |
| L6 | ディーラー別 (μ_d, σ_d) オンライン推定、profile.json 新設 | 設定 + ベイズ更新 |
| L7 | 60 秒キャリブモード GUI | GUI に「キャリブ開始」 |
| L8 | per-dealer 語彙学習 → Vosk hot-word / Whisper initial_prompt に動的反映 | session 開始時に grammar 再生成 |
| L9 | Review UI のクリックを active learning に取り込む | GUI + 学習ループ |

L0–L2 はリスクが小さく即効性があるので優先導入する。L3 以降は `infer_action` の戻り値型変更を伴うため、内部だけ拡張して既存 API は薄いアダプタで維持する。

### 「観測ログ」収集 (Phase 0)

上記のすべての学習は **session 中の全観測を raw で残す** ことが前提。L0 と並行して以下を常時記録するロギング基盤を整備する。

- 全 AudioEvent: PCM (任意) + ASR N-best + word timestamps + conf
- 全 RFIDEvent: raw poll log (present/absent/unknown を毎回)
- 操作ログ: GUI レビュー結果、手動入力、確定アクション
- ハンドメタ: winner, pot, 最終 stack

これによりオフライン分析で per-dealer モデル・遅延分布・position prior すべての学習が可能になる。

---

## 将来計画 (v6.0+): ベイズ推定によるアクション推定

§「将来計画 (v6.0+): 音声/RFID 時刻ベース確率融合」で示した粒子フィルタ / Beam Search は、本質的に **逐次ベイズ推定 (Sequential Bayesian Inference)** のサンプリング近似である。ここでは数式表現を起点に、(1) 何を推定するか、(2) どう更新するか、(3) どう学習するか、(4) どう意思決定するか を明示し、段階的な実装計画に落とす。

### 1. 推定対象とベイズ定式化

求めたい事後分布は **アクション列 $A = (a_1, \ldots, a_n)$ on 観測列 $\mathcal{E}$**:

$$P(A \mid \mathcal{E}, \theta) = \frac{P(\mathcal{E} \mid A, \theta)\, P(A \mid \theta)}{P(\mathcal{E} \mid \theta)}$$

- **事前分布 $P(A \mid \theta)$**: ポーカールールが定める legal_actions、ディーラー $d$ ごとの position prior、stack/pot 制約による条件付き分布。`BettingState.is_initialized` 時はハード制約 + ソフト prior 混合。
- **尤度 $P(\mathcal{E} \mid A, \theta)$**: モダリティごとに独立と仮定し $\prod_t P(\mathcal{E}_t \mid A, \theta)$ に分解。音声・RFID・時刻アライメントそれぞれの観測モデル (§ 4 参照)。
- **モデルパラメータ $\theta$**: ディーラー固有の遅延分布 $(\mu_d, \sigma_d)$、語彙頻度 $\pi_d$、位置別アクション傾向 $\pi_d(\text{action} \mid \text{position})$ など。$\theta$ 自体も事後分布として学習する **階層ベイズ** とする。

正規化定数 $P(\mathcal{E} \mid \theta)$ は不要 (MAP 推定 = $\arg\max_A$ 計算)。粒子フィルタの相対重みもこれを暗黙に消去する。

### 2. 逐次ベイズ更新

観測が時刻順に到着するので、$t$ 時点の事後分布 $\pi_t(A) := P(A \mid \mathcal{E}_{1:t})$ は前ステップから

$$\pi_t(A) \propto P(\mathcal{E}_t \mid A)\, \pi_{t-1}(A)$$

で逐次更新する (定数項を吸収)。実装は **粒子フィルタ** で:

```
# 各粒子 i = 1..K は (action_seq_i, log_weight_i, betting_state_i) を持つ
for evidence e_t in stream:
    for i in 1..K:
        # 観測尤度を log で加算
        log_weight_i += log P(e_t | action_seq_i, theta)
        # 新アクション候補があれば分岐 (resample)
        for cand in legal_actions(betting_state_i):
            spawn new particle with action_seq_i + [cand]
    # 重み正規化 + リサンプリング (Effective Sample Size 監視)
    normalize_log_weights()
    if ESS < K/2:
        resample()
```

これにより **正確な事後分布の Monte Carlo 近似** が得られる ($K \to \infty$ で真の事後に収束)。

### 3. 共役事前分布によるオンラインパラメータ学習

ディーラー個別パラメータ $\theta_d$ は **共役事前分布** を選ぶことで closed-form 更新ができる。session 中に新観測が来るたびに事後分布をベイズ更新する。

| パラメータ | 型 | 共役事前 | 事後 (更新式) |
|---|---|---|---|
| 音声遅延 $(\mu_d^{\text{audio}}, \tau_d^{\text{audio}})$ (精度) | 連続 | Normal-Gamma | $\mu \mid \tau \sim \mathcal{N}(\mu_n, (\kappa_n \tau)^{-1})$、$\tau \sim \text{Gamma}(\alpha_n, \beta_n)$。$n$ 観測後 $\mu_n = (\kappa_0 \mu_0 + n\bar{x})/(\kappa_0 + n)$ |
| RFID 遅延 $(\mu_d^{\text{rfid}}, \tau_d^{\text{rfid}})$ | 連続 | Normal-Gamma | 同上 |
| 語彙頻度 $\pi_d(\text{word} \mid \text{action})$ | カテゴリカル | Dirichlet | $\alpha_n^{(w)} = \alpha_0^{(w)} + c^{(w)}$（観測カウント加算のみ） |
| 位置別 prior $\pi_d(\text{action} \mid \text{position})$ | カテゴリカル | Dirichlet | 同上 |
| 数値表現の好み (`ろっぴゃく` vs `ろくひゃく`) | カテゴリカル | Dirichlet | 同上 |

**初期事前** $\alpha_0$, $\mu_0$, $\kappa_0$, $\alpha_0$ (Gamma), $\beta_0$ は session 全体のグローバル統計から弱い prior として与える (e.g., $\mu_0 = 0.5$s, $\sigma_0 = 0.3$s)。  
**忘却機構**: 古い観測を割引く必要がある場合は exponential forgetting $\alpha_n \leftarrow \lambda \alpha_n + c$ を使う ($\lambda = 0.99 / \text{hand}$ 程度)。

### 4. モダリティ別の観測モデル ($P(\mathcal{E}_t \mid A)$)

**音声観測** (発話 $u$ が時刻 $t^{(u)}$ で N-best $\{w_k, c_k\}$ を出す):

$$P(u \mid a_i, \theta_d) = \underbrace{\mathcal{N}(\tau_i - t^{(u)};\, \mu_d^{\text{audio}}, \sigma_d^{\text{audio}})}_{\text{時刻整合性}} \cdot \underbrace{\sum_k c_k \cdot \pi_d(w_k \mid a_i)}_{\text{意味整合性}}$$

**RFID 観測** (区間 $[t_s^{(n)}, t_e^{(n)}]$ で seat_n がカード保持):

$$P(\text{RFID}_n \mid a_i) = \begin{cases}
\mathcal{N}(\tau_i - t_e^{(n)};\, \mu_d^{\text{rfid}}, \sigma_d^{\text{rfid}}) & \text{if } a_i = \text{FOLD}(n) \\
\mathbb{1}[t_s^{(n)} \le \tau_i \le t_e^{(n)}] & \text{if } a_i \in \{\text{BET, CALL, RAISE}\}(\text{seat}=n) \\
1 & \text{otherwise (uninformative)}
\end{cases}$$

**結合尤度**: 観測モダリティが独立と仮定し積を取る。アクション同士の時刻整合 ($\tau_1 < \tau_2 < \cdots$) はハード制約。

### 5. 意思決定: MAP vs ベイズリスク最小化

事後分布が得られた後の最終決定には 2 通り:

#### 5-1. MAP 推定 (デフォルト)
$$A^* = \arg\max_A \pi_T(A)$$

粒子フィルタなら最大重み粒子。HandSummary に確定書き込み。

#### 5-2. ベイズリスク最小化 (`needs_review` 自動判定)
誤判定コスト関数 $L(\hat{A}, A^*)$ を用いて

$$\hat{A} = \arg\min_{\hat{A}} \mathbb{E}_{\pi_T}[L(\hat{A}, A)]$$

を選ぶ。実用上は近似として:
- top-1 と top-2 の対数事後差 < $\delta$ なら **棄却** → `needs_review`
- エントロピー $H(\pi_T) > h_{\max}$ なら **棄却**
- 期待ポット誤差 $|\mathbb{E}_{\pi_T}[\text{pot}] - \text{observed pot}|$ が閾値超過なら棄却

$\delta$, $h_{\max}$ はディーラーごとに ROC-curve で校正する。

### 6. 階層ベイズ拡張: 卓レベル / 全体レベル

ディーラー個別 $\theta_d$ の上に **卓全体の hyperprior** $\theta_0$ を置く:

$$\theta_d \sim P(\theta_d \mid \theta_0),\quad \theta_0 \sim P(\theta_0)$$

これにより:
- **新規ディーラーの cold start** が cluster prior で初期化される (60 秒キャリブを最小化)
- **過去 session の知識転移** が自然に組み込める
- 全体 prior は夜間バッチで MCMC (Gibbs / HMC) 更新、個別 $\theta_d$ は実時間オンライン更新の **二層構造**

### 7. ベイズ推定導入の段階計画

§「音声/RFID 時刻ベース確率融合」の L0–L9 を、ベイズ推定の視点で再整理する。

| Phase | ベイズ推定要素 | 実装内容 |
|---|---|---|
| **B0** | 観測ログ収集 (事前なし、データ蓄積のみ) | Phase 0 と同じ。raw EvidenceInterval を gzip 保存 |
| **B1** | 一様 prior + 経験的尤度 | `infer_action` を「単一仮説」→「仮説リスト + 対数尤度」に拡張 |
| **B2** | 共役事前分布の導入 (Dirichlet for 語彙、Normal-Gamma for 遅延) | profile.json に共役事前パラメータを persist |
| **B3** | 逐次ベイズ更新 (closed-form posterior update) | 各観測ごとに $\alpha, \beta, \mu, \kappa$ を更新 |
| **B4** | 粒子フィルタ (Sequential Monte Carlo) で事後分布近似 | K=16 → 64 粒子、ESS リサンプリング |
| **B5** | ベイズリスク最小化による意思決定 (review 自動判定) | top-2 対数差 / エントロピーで棄却 |
| **B6** | 階層ベイズ (卓 hyperprior) | 夜間 MCMC バッチで $\theta_0$ 更新、個別 $\theta_d$ をその下に置く |
| **B7** | Active learning (review クリック → 事後分布更新) | Human-in-the-loop で対話的に prior 強化 |
| **B8** | 評価指標 (log-loss / Brier score / ECE) でモデル選択 | A/B test で Phase 別の精度比較 |

B0–B3 は **既存パイプラインに非破壊で追加可能**。B4 以降は `IntegrationThread` 内部状態を粒子集合に置き換える破壊的変更を伴う。

### 8. 既存コードからの最小変更で得られる第一歩 (B1)

```python
# action_inference.py に追加
@dataclass
class ActionHypothesis:
    action: str
    amount: Optional[int]
    log_likelihood: float  # log P(evidence | this action)
    reason: str

def infer_action_distribution(
    evidence: EvidenceInterval,
    state: BettingState,
    theta_d: DealerProfile,
) -> list[ActionHypothesis]:
    """単一の最尤アクションではなく、(候補, log尤度) のリストを返す。

    既存の infer_action() は本関数の薄いラッパで実装し直す:
        best = max(hypotheses, key=lambda h: h.log_likelihood)
    """
    ...
```

この変更だけで、後段 (Beam Search / 粒子フィルタ) を載せる土台ができる。`integration.engine` への影響はゼロ (top-1 を取り出すアダプタを置けば既存 API 互換)。

### 9. 不確実性の可視化 (GUI)

ベイズ推定の利点は **「確率付き候補」が出ること**。GUI の `needs_review` 行に top-3 候補と posterior 確率を表示することで、操作者がワンクリックで選べる:

```
ハンド #42  flop  pot=2400
⚠ レビュー: seat3 の action
  [ 78% ] CALL 600     (audio 0.9, rfid 0.85)
  [ 18% ] RAISE 1200   (audio 0.6, rfid 0.85)
  [  4% ] CHECK        (audio 0.1, rfid 0.85)
```

クリックで確定 → その結果が Dirichlet posterior の更新カウントになる (active learning ループ閉)。

---

## v6.0 実装計画 (M1–M3): ベイズ推定アクション推定レイヤ ✅ 実装済み

§「将来計画 (v6.0+): ベイズ推定によるアクション推定」の中核モデルを 3 マイルストーンに分割して **非破壊** で導入する実装計画 — **3 マイルストーンすべて完了済み** (commits `608976b` / `be54632` / `70a7135`)。学習・階層ベイズ・ベイズリスク最小化はスコープ外（§「将来計画」に残置）。

### 実装サマリ

| Milestone | コミット | 主な追加ファイル | テスト追加 |
|---|---|---|---|
| M1: データ配線 | `608976b` | `output/evidence_log.py`、`core/events.py` 拡張 (alternatives / word_timestamps / t_end)、`audio/recognizer.py` (`TranscriptionResult`) | `test_evidence_log.py` 6 件 |
| M2: 観測モデル + アダプタ | `be54632` | `integration/observation_model.py`、`infer_action_distribution()` | `test_observation_model.py` 26 件 + `test_inference_equivalence.py` 16 件 |
| M3: Beam Engine + WINNER 後方修正 | `70a7135` | `integration/beam_search.py`、`engine.py` の `_reconcile_with_beam()` + `on_action_revised` | `test_beam_search.py` 15 件 + `test_bayesian_e2e.py` 3 件 |

**全体テスト**: 280 (M0 baseline) → 346 (M3 後) = 66 件追加、既存 280 件は全 green を維持。

### 中核モデル決定事項

| 項目 | 決定 |
|------|------|
| 推定対象 | sequence MAP: $A^* = \arg\max_A P(A_{1:T} \mid \mathcal{E}_{1:T})$ |
| アルゴリズム | **Beam Search (K=8〜16, 決定論的)**。`enable_resample=False` で粒子フィルタへの将来移行口を残す |
| 観測 | N-best + word_timestamps + RFID interval (`t_end` 追加) |
| パラメータ θ | 固定 prior（学習なし） |
| 意思決定 | MAP（top-1 確定）。既存の二値 `needs_review` トリガ (check_when_bet_open 等) は維持 |
| 後方修正 | WINNER/POT で粒子集合を再フィルタ → `IntegrationThread._current_actions` を in-place mutate |
| 既存 API | `infer_action()` は `infer_action_distribution()` 上の薄いアダプタとして保存（既存テスト全 green 維持: 280 → 346） |

### 数学的中核

- **状態**: アクション列 $A = (a_1, \dots, a_T)$、各 $a_i = (\text{seat}_i, \text{action}_i, \text{amount}_i, \tau_i)$、$\tau_1 < \dots < \tau_T$（ハード制約）
- **観測**: `EvidenceInterval`（audio: N-best $\{(w_k, c_k)\}$ + 区間 $[t_s, t_e]$ + word_timestamps / rfid: 区間 $[t_s, t_e]$ / camera: 無情報）
- **尤度** (モダリティ独立): $P(E_t \mid a_i) = P_\text{audio}(u_t \mid a_i) \cdot P_\text{rfid}(I_t \mid a_i)$
  - 時刻項: $\mathcal{N}(\tau_i - t_e^\text{audio}; \mu_\text{audio}, \sigma_\text{audio}^2)$、fold-on-release は $\mathcal{N}(\tau_i - t_e^\text{rfid}; \mu_\text{rfid}, \sigma_\text{rfid}^2)$
  - 語彙項: $\sum_k c_k \cdot \pi(w_k \mid \text{action}_i)$
  - ハード制約: $a_i \notin \text{legal}(\text{BettingState})$ → 0、RFID 在席中の FOLD → 0
- **事前**: $P(A \mid s) = \prod_i \pi(\text{action}_i \mid \text{position}_i) \cdot \mathbb{1}[\text{legal}]$
- **意思決定**: $A^* = \arg\max_A \sum_t \log P(E_t \mid A) + \log P(A)$ を Beam Search で近似

### 固定 Prior 初期値

| パラメータ | 値 | 根拠 |
|---|---|---|
| $\mu_\text{audio}$, $\sigma_\text{audio}$ | 0.4s, 0.6s | 既存 `MATCH_WINDOW=2.0` の ±2σ ≈ 窓幅 |
| $\mu_\text{rfid}$, $\sigma_\text{rfid}$ | 0.2s, 0.4s | ESP32 ポーリングは高速 |
| 語彙 $\pi(w \mid a)$ | `speech_normalization.json` の `action_aliases` から Dirichlet pseudocount 5、未知語 $\alpha_0=0.1$ | 既存資産再利用 |
| 位置 prior | early `{FOLD:0.55, CALL:0.30, RAISE:0.10, CHECK/BET:0.05}` / middle `{FOLD:0.40, CALL:0.35, RAISE:0.20, CHECK/BET:0.05}` / late `{FOLD:0.30, CALL:0.30, RAISE:0.30, CHECK/BET:0.10}` | loose-passive 寄り |
| amount\|action | BET/RAISE: log-Normal(ln(2·bb), 0.7) / CALL: degenerate at `current_bet` / CHECK/FOLD: degenerate at 0 | long-tailed |

定数は `integration/observation_model.py` の `PriorParams` dataclass に集約 → 将来 `profile.json` で差し替え可。

### マイルストーン

#### M1 — データ配線（非破壊） ✅ 実装済み (`608976b`)

**目的**: 観測の richness（N-best + word_timestamps + interval）を pipeline に流すだけ、推定ロジックは変えない。

- `core/events.py`:
  - `AudioEvent` 末尾に `alternatives: list[ASRAlternative] = field(default_factory=list)`, `word_timestamps: list[WordTiming] = field(default_factory=list)`, `t_end: Optional[float] = None` を追加
  - `RFIDEvent` 末尾に `t_end: Optional[float] = None` を追加
  - 新 dataclass: `ASRAlternative(text, confidence, words)`, `WordTiming(word, start, end, confidence)`
- `audio/recorder.py`: `WhisperTranscriber.transcribe()` を `best_of=5, beam_size=5, word_timestamps=True` で動かし、segment-level metadata から N-best と timing を抽出して `TranscriptionResult` で返却。`AudioThread._process_chunk` で新フィールドを `AudioEvent` に詰める
- `audio/vosk_recorder.py`: 既存の `SetWords(True)` 出力 JSON の `result` 配列から `WordTiming` を生成（Vosk は単一仮説なので `alternatives` は 1 件）
- `output/evidence_log.py` (新): `EvidenceLogWriter(log_dir, session_id)` で `logs/evidence_<session>.jsonl` に append-only 書き込み（session JSON と完全分離）
- `integration/engine.py`: `EvidenceLogWriter` を `__init__` で生成、`_handle_audio_event` / `_drain_rfid_queue` で raw evidence をログに inject（推定変更なし）

検証: `pytest tests/ -v --ignore=tests/test_vision.py` → 286 件 pass (280 baseline + 6 evidence_log)、`python main.py --cli` で `logs/evidence_*.jsonl` が生成。

#### M2 — 観測モデル + アダプタ ✅ 実装済み (`be54632`)

**目的**: 観測尤度関数を導入し、`infer_action()` を「列挙→max」型にリファクタ。`engine.py` は変更しない。

- `integration/observation_model.py` (新):
  ```python
  @dataclass class EvidenceInterval: kind: Literal["audio","rfid","camera"]; t_start, t_end, payload
  @dataclass class PriorParams: mu_audio, sigma_audio, mu_rfid, sigma_rfid, lexicon, position_prior, amount_prior
  @dataclass class ActionHypothesis: action, amount, log_likelihood, reason, needs_review
  def compute_log_likelihood(evidence, hypothesis, state, prior) -> float
  def default_priors() -> PriorParams  # speech_normalization.json から構築
  ```
- `integration/action_inference.py`:
  - 新規 `infer_action_distribution(evidence, state, actor_seat, prior) -> list[ActionHypothesis]`
  - 既存 `infer_action()` (`:339`) は `infer_action_distribution()` を呼んで top-1 を取り出すアダプタにリファクタ。既存の `_infer_from_amount_only()` (`:230`) 6 ケースと `_validate_provided_action()` (`:299`) は内部で再利用し、各仮説に log-likelihood を埋める
  - **N-best 単一なら top-1 == 既存出力** を `tests/test_inference_equivalence.py` で保証
- 新規テスト: `tests/test_observation_model.py`, `tests/test_inference_equivalence.py`

検証: 全 suite green、既存 `infer_action()` の出力に diff なし。

#### M3 — Beam Engine + WINNER 後方修正 ✅ 実装済み (`70a7135`)

**目的**: sequence 事後分布の MAP を Beam Search で出す。WINNER 到着時に粒子集合を再フィルタしてアクション履歴を遡及修正。

- `integration/beam_search.py` (新):
  ```python
  @dataclass class BeamParticle: actions, log_weight, state  # BettingState を deep copy
  class BeamEngine:
      def __init__(self, K=8, prior=..., enable_resample=False, sink=None)  # sink は evidence_log への hook
      def step(self, evidence) -> None                       # legal_actions × N-best 展開 → top-K 剪定
      def map_action(self) -> ActionHypothesis               # リアルタイム MAP (最新 1 件)
      def map_sequence(self) -> list[ActionHypothesis]
      def apply_winner_filter(self, winner_seat, final_pot) -> list[ActionHypothesis]
      def snapshot(self) -> list[BeamParticle]               # evidence_log 用
  ```
- `integration/engine.py`:
  - `__init__` に `self._beam = BeamEngine(K=beam_K, prior=default_priors(), sink=self._beam_sink_to_evidence_log)` を追加 (新引数 `beam_K=8` と `on_action_revised` コールバックも追加)
  - `_start_new_hand()` で SB/BB auto-post の後に `self._beam.reset_with_state(self._betting_state)` を呼ぶ
  - `_handle_audio_event()` で `infer_action()` (legacy) と `self._beam.step_audio(event, seat)` を **並行実行**。legacy 経路が引き続きリアルタイム `ActionRecord` を駆動し、beam は sequence 仮説を保持
  - `_finalize_hand()` で `gs.end_hand()` より **前** に `self._reconcile_with_beam(winner_seat)` を実行: `apply_winner_filter` → `_current_actions` と zip → 差分 record を **in-place** mutate + `needs_review=True` + `on_action_revised(record)` 発火
  - **初版の妥協**: `pot_after`/`stack_after` は stale のまま残置（完全 replay は将来拡張）。RFID 経路の beam 統合も M3 スコープ外（evidence_log への記録のみ）
  - **プランからの軽微な逸脱**: 「`infer_action()` を beam 呼び出しに置換」ではなく「並行運用」を採用。primary 仮説 (log_likelihood=0) が常に top のため legacy 出力と等価で、後方互換性を最大化するための判断
- 新規テスト:
  - `tests/test_beam_search.py` (15 件): 剪定、決定論性、`enable_resample=False`、winner_filter の fold 粒子排除、snapshot 構造
  - `tests/test_bayesian_e2e.py` (3 件): (a) BeamEngine 単体で seat3 fold 後に `apply_winner_filter(3)` が代替を昇格させる、(b) IntegrationThread 経由で 3-handed の seat3 fold + WINNER=seat3 → `on_action_revised` 発火・record の in-place mutate (action≠fold, needs_review=True)、(c) WINNER=seat2 (整合) → revise が走らないことの健全性確認

検証: 全 suite green、GUI smoke で revise バナー、PHH 往復。

### ActionRecord 書き込み戦略

採用は **(A) リアルタイム MAP + WINNER で in-place revise**。
- `output/json_writer.py:47` `append_hand_summary()` は `_finalize_hand` 末尾でのみディスクへ書き込むため、メモリの `self._current_actions` を mutate する限り **JSON 書き込みは 1 回・確定値のみ**
- GUI のリアルタイム性 (`on_action` `:412`) は維持、revise 時は `on_action_revised(record)` を追加発火
- 完全 replay 型（`pot_after`/`stack_after` を再計算）は将来拡張

### 観測ログ (B0)

`logs/evidence_<session>.jsonl` に 1 観測 = 1 行で記録: `{ts, kind, payload, n_best, beam_snapshot_top3, map_action_id}`。session JSON と完全分離し PHH/GUI は読まない。将来オフライン学習（B2/B3/B6）の入力資料。

### スコープ外（§「将来計画」に残置）

- B2/B3: 共役事前 + 逐次ベイズ更新（学習）
- B5: ベイズリスク最小化（top-2 対数差ベース needs_review）
- B6: 階層ベイズ・夜間 MCMC
- B7: Active learning (GUI クリック → posterior 更新)
- 粒子フィルタの確率的サンプリング（K 増やしと resample flag のみ準備）
- 完全 replay 型後方修正（pot_after/stack_after 再計算）

### 検証コマンド

```bash
pytest tests/ -v --ignore=tests/test_vision.py                                   # 全 suite: 416 件 pass (280 baseline + 66 M1–M3 + 6 Phase 1 + 20 Phase 2-A + 11 Phase 2-B + 19 Phase 2-C + 14 Phase 3)
pytest tests/test_observation_model.py tests/test_inference_equivalence.py -v    # M2
pytest tests/test_beam_search.py tests/test_bayesian_e2e.py -v                   # M3
pytest tests/test_settlement_models.py -v                                         # Phase 1 + Phase 2-B engine E2E
pytest tests/test_settlement_logic.py -v                                          # Phase 2-A (settlement core)
pytest tests/test_hand_finalizer.py -v                                            # Phase 2-B (HandFinalizer 単体)
pytest tests/test_hand_boundary.py -v                                             # Phase 2-C (boundary + replay + integration)
pytest tests/test_hand_reconstructor.py -v                                        # Phase 3 (HandReconstructor + CLI)
python -m output.reconstruct_session --session logs/<session>.json                # Phase 3 CLI: online↔offline diff を出力
python main.py --cli                                                              # M1: logs/evidence_*.jsonl が増える
python main.py                                                                    # M3: GUI で revise バナー確認
python main.py --export-phh logs/session_xxx.json                                 # M3: PHH 出力 (Phase 1: resolution_status=="final" の hand のみ)
```

---

## Phase 1: ハンド終局を settlement 中心に — データモデル整理 ✅ 実装済み

`HandSummary` の終局表現を `winner_seat` 単数中心から、**settlement (resolution_status / resolution_type / seat_payouts / pots) 中心** へ移すための「器」を作るフェーズ。重いロジック (side pot 計算、hand evaluator、finalizer 本体) は Phase 2 以降。

**背景**: all-in 頻発・side pot 必須・split pot 必須のライブポーカーでは、`winner_seat: int` 単数では終局を表現しきれない。複数 pot に対して別々の eligible / winning seat 集合と payouts を持たせる必要がある。

### Phase 1 で導入したデータモデル

`core/hand_log.py` に同居:

| 型 | 役割 |
|---|---|
| `ResolutionStatus = Literal["final", "provisional", "incomplete"]` | PHH 出力対象になるかの 3 値 |
| `ResolutionType = Literal["fold_win", "showdown", "showdown_split", "sidepot_showdown", "legacy_winner_finalize"]` | 終局タイプ。最後の値は migration marker |
| `PotType = Literal["main", "side"]` | pot 種別 |
| `RevealedCardSource = Literal["rfid", "manual", "derived"]` | hole cards の出所 |
| `RevealedHand(seat, cards, source, observed_at)` | showdown 時の hole cards (canonical 内部表現) |
| `PotSettlement(amount, eligible_seats, winning_seats, payouts, pot_type)` | 1 つの pot の決済結果 |

`HandSummary` に追加した 5 field:
- `resolution_status: ResolutionStatus = "final"` — 通常経路は `_finalize_hand` で常に `"final"` 設定 (既存挙動互換)
- `resolution_type: Optional[ResolutionType] = None` — `_finalize_hand` 経由では `"legacy_winner_finalize"` を明示設定。default=None は dataclass 純粋構築用
- `seat_payouts: dict[int, int]` — seat 別の正味払出。legacy 経路では `{winner_seat: pot_total}`
- `showdown_revealed_cards: dict[int, list[str]]` — JSON 公開用の簡略表現 (canonical は内部の `RevealedHand`)
- `pots: list[PotSettlement]` — 各 pot の決済。**Phase 1 では意図的に空のまま** (fake な eligible_seats を入れると Phase 2 で側 pot 計算と矛盾)

### 設計思想

ハンドの終端は次のいずれかで決まる:
- **fold_win**: live player が 1 人に絞れた時点
- **showdown**: board + RFID で revealed hole cards から rank 評価
- **showdown_split**: 同 rank で payouts を均等分配 (odd chip は Phase 2 で議論)
- **sidepot_showdown**: all-in を含む複数 pot の決済
- **legacy_winner_finalize**: Phase 0–M3 経由で閉じた既存 hand の migration marker (canonical な resolution type の集合には最終的に含まれるべきではない)

`winner` 音声入力は **強観測ではなく補助観測** として扱う（M1–M3 で「Oracle 級」と表現したのは過剰、Phase 2 で finalizer が hand state から自律的に決定する設計が canonical）。`needs_review` は「人手レビュー必須」ではなく **「あとから修正可能性を保持」** の意味。

### `winner_seat` の位置づけ（重要）

Phase 1 では `winner_seat: int` を required のまま温存するが、**canonical な終局表現ではなく後方互換のための compatibility field**。今後の canonical は:
- `resolution_status` / `resolution_type`
- `seat_payouts`
- `pots`

split pot や side pot を含む hand では `winner_seat` 単数では表現できないため、Phase 2 以降は `seat_payouts` / `pots` を読むコードを推奨。`winner_seat` は段階的に Optional 化または `primary_winner_seat` 等への rename を検討。

### `showdown_revealed_cards` と `RevealedHand` の関係

JSON 出力で扱う `showdown_revealed_cards: dict[int, list[str]]` は **簡略表現（外部投影）**。canonical な内部表現は `RevealedHand` (source / observed_at を持つ) で、将来 `ShowdownTracker` が保持する。`HandFinalizer` は `RevealedHand` の集合を読み、外部公開時に `showdown_revealed_cards` 形式へ投影する。

### PHH 出力の方針

PHH 出力対象は **`resolution_status == "final"` の hand のみ**。それ以外 (`provisional` / `incomplete`) は意図的 skip であり export エラーではない。`output/phh_exporter.py` の `export()` / `write()` / `write_session()` すべてに gate あり。

Phase 1 戻り値は単純に `""`、Phase 2 で skip reason を構造化 (Enum or `SkipReason` dataclass) 予定。

### アーキテクチャの重心変更

旧 (M1–M3 後):
```
観測 → 推定 → ActionRecord → (WINNER 後方修正) → JSON
```

新 (Phase 1 / 2-A / 2-B / 2-C 後):
```
観測収集 (ASR / RFID / Camera)
    ↓
EvidenceLogWriter で raw 観測を logs/evidence_<session>.jsonl に常時記録
    ↓
hand segmentation (Phase 2-C: HandBoundaryDetector)
    ↓ {hand_id ごとの window: list[EvidenceRecord]}
    ↓
state tracking (BettingState + Beam Engine による sequence MAP)
    ↓
settlement-ready な hand state 形成 (live_seats, hole_cards, board, pot)
    ↓
finalization (Phase 2-B: HandFinalizer) → fold / showdown / split / side pot
    ↓ settlement core (Phase 2-A: compute_pot_settlements / evaluate_hand_rank)
    ↓
HandSummary {resolution_status="final", pots, seat_payouts, ...}
    ↓
final hand のみ PHH export

(オプション、Phase 3 実装済) HandReconstructor が hand window を頭から再生して
                              offline HandSummary' を生成 → online との diff → needs_review
                              自動判定。online JSON / PHH は **mutate しない** (別 JSONL に書く)
```

### ログ / replay / retrospective inference (Phase 2-C)

**EvidenceLog (M1) → hand window → retrospective inference** の経路:

1. `EvidenceLogWriter` が `logs/evidence_<session_id>.jsonl` に raw 観測 (audio / rfid / camera) を append-only で記録 (M1 から既設)
2. `output/replay_hand.py:load_evidence_log(path)` で JSONL を `list[EvidenceRecord]` にデシリアライズ (audio / rfid / camera を型付き event に再構築)
3. `output/replay_hand.py:extract_hand_windows(records)` が `HandBoundaryDetector` を頭から流して `dict[hand_id, list[EvidenceRecord]]` に窓化
4. live 経路でも `IntegrationThread` が `_current_hand_events` バッファと `_completed_hands` dict を保持 (`_track_evidence(kind, event)` で 1 event ずつ追加 / boundary 処理)
5. 終端境界で `_invoke_reconstructor_hook(hand_id)` が `HandReconstructor.reconstruct_from_events(events)` を呼ぶ。Phase 2-C は skeleton (`reason="reconstruction_skipped"`) で online 経路には介入しない

**Phase 3+ で HandReconstructor が担う予定の処理**:
- hand window 内の events を BeamEngine で頭から再生し sequence MAP を再評価
- 当該 hand window の WINNER / 最終 pot / showdown hole cards を `apply_winner_filter(...)` に流し、絞り込み
- HandFinalizer に通して新 `HandSummary` を生成
- online 出力との diff から needs_review / 自動 patch を判断 (action 列の改良、HandSummary 差し替え、needs_review ラベルの再評価)

### `integration/engine.py:_finalize_hand` の Phase 1 挙動

legacy 経路は不変だが、`HandSummary` 構築時に新 field を populate:
- `resolution_status="final"` (既存挙動互換)
- `resolution_type="legacy_winner_finalize"` (migration marker)
- `seat_payouts={winner_seat: pot_total}` (確定情報のみ)
- `pots=[]` (空のまま、Phase 2 で再計算する前提)
- `showdown_revealed_cards={}` (Phase 2 で ShowdownTracker から投影)

`gs.end_hand(winner_seat)` の signature は **不変**。Phase 2 で finalizer に置換する際に `end_hand_with_payouts(payouts)` 等への拡張を検討。

### Phase 2 進捗

**Phase 2-A 完了済み (settlement core)**:
- ✅ `core/settlement.py:compute_pot_settlements()` (Stratified Side Pot Decomposition)
- ✅ `core/settlement.py:evaluate_hand_rank()` (pokerkit `StandardHighHand.entry.index`)
- ✅ `core/settlement.py:distribute_split_pot()` (odd chip は lowest seat 優先)

**Phase 2-B 完了済み (HandFinalizer + engine 置換)**:
- ✅ `core/hand_finalizer.py:HandFinalizer.finalize()` — fold_win / showdown / showdown_split / sidepot_showdown / incomplete を判別、settlement core を呼んで `pots` を埋め、`resolution_type` を canonical 値へ昇格
- ✅ `integration/engine.py:_finalize_hand(winner_seat: Optional[int])` を HandFinalizer 経由に置換。`legacy_winner_finalize` は engine 経路では発行されなくなった
- ✅ `integration/engine.py:_apply_payouts_to_gamestate(summary)` — `seat_payouts` から primary winner を選び legacy `gs.end_hand` を呼ぶ暫定 bridge (Phase 2-C で撤去予定)
- ✅ `winner` 音声を `winner_seat_hint` として **補助観測化**。settlement と食い違うと `review_required=True` を立てる ("Oracle 一発確定" 廃止)
- ✅ pot_total を `betting_state.player_contrib_hand.values()` の総和から計算するよう修正 (Phase 1 では blind only + fold の hand が 0 を返していたバグを解消)

**Phase 2-B 仕様の明文化**:

**`HandSummary.pot_total` の canonical source**:
- 終局時の `pot_total` は **`betting_state.player_contrib_hand.values()` の総和**
  (SB/BB を含む全 seat の hand 累積投入額)。これが終局時の固定値で、
  HandSummary の他フィールドとの不変量は:
  - `pot_total == sum(rec.amount for rec in betting_state.action_history)`
  - `pot_total == sum(p.amount for p in pots)` (settlement 成功時)
  - `pot_total >= sum(seat_payouts.values())` (rake 考慮で等号は崩れる)
- UI 表示用の途中経過 pot (street ごとの累積) や replay 中の動的 pot 表示は
  別管理。`HandSummary.pot_total` は **終局時の固定値** として扱う

**`winner_seat` の縮約ルール (`_pick_primary_winner` の仕様)**:
`winner_seat` は canonical な終局表現ではなく compatibility field。値を決める
ルールは以下の優先順位で固定 (Phase 2-B `_pick_primary_winner` 実装):
1. `seat_payouts` が非空 → **最大 payout の seat** (tie 時は **最小 seat 番号**)
2. `seat_payouts` が空 (incomplete 等) → `winner_seat_hint` (音声 WINNER 観測) を採用
3. hint も無い → `live_seats` の最低 seat 番号
4. live も無い → `active_seats` の最低 seat 番号
5. それも無い → `0` (退化、実運用では到達しない)

JSON / PHH / UI の読み手はこのルールを前提に `winner_seat` を解釈する。
canonical な勝者情報は `seat_payouts` / `pots` 側を見ること。

**`resolution_status="incomplete"` になる条件 (HandFinalizer の reason tag)**:
| reason tag | 条件 |
|---|---|
| `board_under_5` | live_seats が 2 以上いるのに board が 5 枚未満 |
| `revealed_hands_missing` | showdown で必要な hole cards (live_seats のいずれか) が `revealed_hands` に含まれていない |
| `settlement_exception` | `compute_pot_settlements` 内で例外発生 |
| `empty_pots` | `compute_pot_settlements` が空 list を返した |
| `no_live_seats` | 全 seat が folded 等で live_seats が 0 (退化) |

incomplete の hand は `resolution_type=None` / `pots=[]` / `seat_payouts={}` が
立ち、PHH gate で意図的 skip される。Phase 3+ で `HandReconstructor` が
retrospective に再評価して **incomplete → final** に昇格させる経路を作る予定。

**Phase 2-C 完了済み (hand boundary + retrospective hook)**:
- ✅ `core/hand_boundary.py:HandBoundaryDetector` — audio `new_hand` / `winner` を一次シグナル、RFID 由来の board cleared (BOARD_EMPTY_QUIET_SEC=1.5s)、idle 状態で 2+ seat に hole cards が現れる (hole_cards_appeared) を二次シグナルとして start / end を発行 (`list[BoundaryEvent]` 返却で end+start 同時発行に対応)
- ✅ `output/replay_hand.py:load_evidence_log` — `logs/evidence_<session>.jsonl` を `list[EvidenceRecord]` にデシリアライズ。`_build_audio_event` / `_build_rfid_event` / `_build_camera_event` で型付き再構築
- ✅ `output/replay_hand.py:extract_hand_windows` — EvidenceRecord 列を boundary detector で走査し `dict[hand_id, list[EvidenceRecord]]` に窓化
- ✅ `integration/engine.py`: `_track_evidence(kind, event)` + `_observe_state_snapshot(now)` + `_apply_boundaries(...)` + `_invoke_reconstructor_hook(hand_id)` を新設。`_handle_audio_event` / `_process_rfid_event` / `_drain_camera_queue` から `_track_evidence` を呼ぶ。終端境界で `_completed_hands[hand_id]` に window が確定
- ✅ `core/hand_reconstructor.py:HandReconstructor` — Phase 2-C は skeleton (`reason="reconstruction_skipped"` の pass-through)。終端境界 hook が安全に通る経路を固定

**Phase 2-C 初期 heuristics (hand_boundary.py)**:

| シグナル | trigger | 用途 |
|---|---|---|
| `audio_new_hand` | `AudioEvent(action="new_hand")` | 主 start シグナル。in_hand 中なら `[end, start]` の 2 件を返す |
| `audio_winner` | `AudioEvent(action="winner")` | 主 end シグナル |
| `board_cleared` | board が非空→空に転落し、その後 `BOARD_EMPTY_QUIET_SEC` (= 1.5s) 静止 | 二次 end シグナル (audio 補完用) |
| `hole_cards_appeared` | idle + board 空 + 2+ seat に hole cards | 二次 start シグナル (RFID-only シナリオ用) |

**Phase 2-C 仕様の明文化**:

**boundary は ground truth ではなく boundary hint / segmentation 観測**:
- 上記 4 シグナルはいずれも「hand segmentation のための観測」であって**絶対的な
  truth ではない**。音声誤認識、RFID 取り逃し、ディーラーの手順前後など、
  シグナルが間違うことは起こりうる
- 上位レイヤ (HandFinalizer 等) は boundary に依存しない設計を維持する
- Phase 3+ では以下の拡張を予定:
  - シグナル種別ごとに**重み / prior** を変える (例: audio_winner と board_cleared
    が両方観測されたら confidence を上げる、片方だけなら下げる)
  - 矛盾するシグナルから confidence を計算して provisional / incomplete 判定を返す
  - HandReconstructor で hand window を後ろ向きに再評価する際、boundary 自体も
    再評価対象に含める
- Phase 2-C は初期 heuristics として全シグナルを等価な「決定論的シグナル」と
  して扱うが、これは初期仕様であり段階的に確率化される

**`IntegrationThread._completed_hands` の保持ポリシー**:
- Phase 2-C 時点では **セッション中の全 hand を in-memory に保持** (eviction なし)
- 根拠: `logs/evidence_<session>.jsonl` が canonical な book of record として
  既に永続化されているため、`_completed_hands` は揮発的な convenience cache
- 典型的なセッションは 100–200 hand 程度、1 hand あたり EvidenceRecord は数 KB
  なので合計でも 1〜数十 MB に収まる想定
- Phase 3+ で HandReconstructor が events を消費するようになり、長時間セッション
  でメモリ圧が問題になる場合は、LRU eviction (例: 直近 N=50 hand) や処理済み
  hand の即時 drop を導入する

**`extract_hand_windows` の「end 未観測 hand を除外」仕様**:
- 現在の実装は、start が観測されても対応する end が観測されなければその hand を
  返り値 dict に**含めない**
- 根拠: open window は不完全であり settlement が確定していない。これを return
  値に入れると消費側 (HandReconstructor / replay UI) が誤って finalize する
  リスクがあるため**安全側に倒した仕様**
- TODO (Phase 3+ 拡張余地):
  - 「end の無い hand を **provisional window** として返す」モードを追加
    (例: `include_open_hands=True` フラグ、または別 dict `open_hands`)
  - これによりセッション最後の未完了 hand や、replay 時の進行中 hand を診断的に
    取り出せるようにする
  - 消費側で `resolution_status="provisional"` の HandSummary を生成する設計

**Phase 3 完了済み (HandReconstructor 本実装 + CLI)**:
- ✅ `core/hand_reconstructor.py:HandReconstructor.reconstruct_from_events()` — hand window を BeamEngine + bs.update_after_action で頭から再生 → `apply_winner_filter` で winner_seat_hint を投入 → `HandFinalizer.finalize` で offline HandSummary 構築
- ✅ `HandReconstructionResult` を `actions / summary / needs_review / reason / diff / confidence` 6 field に拡張
- ✅ `_init_betting_state` で online_summary から button/SB/BB を逆算 bootstrap (HU は BTN=SB、それ以外は SB の左隣)
- ✅ `_compute_diff(online, offline)` で `resolution_status` / `resolution_type` / `winner_seat` / `pot_total` / `seat_payouts` / `showdown_revealed_cards` / `actions (seat,action,amount)` を比較。差分があれば `needs_review=True`
- ✅ `output/reconstruct_session.py` CLI — session JSON + evidence JSONL → `reconstruct_<session>.jsonl` (1 hand 1 行) を出力。online JSON / PHH を mutate しない
- ✅ JSON round-trip 時の seat key str/int 混在を吸収する正規化

**Phase 3 スコープ外 (Phase 4+ 候補)**:

候補の整理 (今後どちらかから進める):

- **Phase 4-A: live hook に online_summary を渡して advisory reconstruct を有効化**
  - IntegrationThread の `_invoke_reconstructor_hook` で online_summary (= 終局直後の
    HandSummary) を渡してリアルタイム reconstruct を実行し、`needs_review` を
    `_completed_hands` 経由で GUI / 監視ツールに通知する。
  - online JSON / PHH は mutate しないまま、advisory layer として運用する。
  - 現状は `initial_state=None / online_summary=None` で skipped 経路を維持。
- **Phase 4-B: raw-only bootstrap の強化**
  - online_summary 無しでも reconstruct できるよう、RFID hole_cards 出現や音声
    `new_hand` 時点の active seats から button / SB / BB を推定する。
  - Phase 3 MVP では「online-bootstrap-assisted」だったので、これにより
    EvidenceLog 単独で hand を立て直す *raw-only reconstruction* が可能になる。
  - 後段で online_summary を読まずに事後監査 / 失った online ログからの復旧が
    できる。

その他 (どちらか進めた後の課題):
- 差分検出時の **自動 patch** (online HandSummary の resolution / payouts を
  offline で上書きする経路。現状は別 JSONL に書くだけ)
- 確率モデル拡張: prior の hand-specific 調整 (例えば過去 N hand の MAP 平均で
  smoothing)、`confidence` を operational metric から **モデル事後確率** (top-1 vs
  top-2 log 差 / エントロピー / Brier score) に置き換え
- 完全 replay 型 ActionRecord (pot_after / stack_after を再構成時の bs から正確に算出)

**Phase 2-D / 4+ 以降の残タスク**:
- `core/showdown_tracker.py`: `observe()` / `is_showdown_ready()` / `project_to_summary_dict()` 本実装。現状 `engine._finalize_hand` が直接 `self._hole_cards` から `RevealedHand` を組んでいる
- `core/game_state.py`: `end_hand(winner_seat)` を `end_hand_with_payouts(payouts: dict[int, int])` に拡張。これで split / sidepot 時の stack も正しく反映される
- `integration/engine.py`: `_apply_payouts_to_gamestate` adapter を撤去し、`end_hand_with_payouts` 直呼び出しに変更
- `HandSummary.winner_seat` を `Optional[int]` 化 (seat_payouts ベースに完全移行)
- `output/phh_exporter.py`: PHH skip reason を構造化、export 失敗との区別を明示
- 全 seat の hole cards が absent になった瞬間の end シグナル (Phase 2-C スコープ外)
- GUI 上での手動 hand boundary 修正 UI / reconstruct 結果の表示
- RFID 認識品質 (ノイズ / 誤検出) への本格対応
- `legacy_winner_finalize` でマーク済の旧 hand を新 finalizer で再評価する migration ツール (任意)
