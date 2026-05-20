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
│   ├── hand_reconstructor.py      ← HandReconstructor (Phase 3 実装済): hand window を BeamEngine + HandFinalizer で再生し online と diff
│   ├── patch_proposal.py          ← Phase 5-A: HandPatchProposal / FieldPatch / compute_patch_proposal (diff → 修正提案、apply はしない)
│   └── patch_apply.py             ← Phase 5-G: apply_patch_proposal_to_summary (whitelist field のみ HandSummary に手動 apply、永続化なし)
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
│   ├── reconstruct_session.py     ← CLI (Phase 3): session JSON + evidence JSONL を読んで各 hand を再構成、online↔offline diff を書く
│   └── inspect_reconstruction.py  ← CLI (Phase 4-C1): reconstruct_<session>.jsonl を読んで [OK]/[REVIEW]/[SKIPPED] 単位の一覧を表示
│
├── gui/
│   ├── dashboard.py               ← GUIDashboard (customtkinter)
│   └── reconstruction_badges.py   ← Phase 4-C2: HandReconstructionResult → ReconstructionBadgeState 投影 helper (status / RAW バッジ / diff_fields / 履歴行整形)
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

### `core/patch_proposal.py` (Phase 5-A 実装済)

online HandSummary と offline HandSummary' の **diff から修正案 (patch proposal)
を構造化** する。**apply は Phase 5-A スコープ外**: CLI / GUI で「提案」を見せる
だけで、online JSON / PHH / GameStateManager は自動で書き換えない。

- `PATCHABLE_FIELDS = ("resolution_type", "seat_payouts", "winner_seat",
  "pot_total", "showdown_revealed_cards")` — Phase 5-A で対象とする canonical
  settlement 系 field。``actions`` は heavy なので別フェーズ
- `FieldPatch(field, online, offline, note)`: 1 field 分の修正提案
- `HandPatchProposal(hand_id, can_patch_automatically, fields, summary_note)`:
  1 hand 分。``can_patch_automatically`` は Phase 5-A では **常に False**
  (Phase 5-B 以降で安全条件 + apply 判定を入れる想定の placeholder)
- `compute_patch_proposal(hand_id, online, offline, diff) → Optional[HandPatchProposal]`:
  - ``diff`` is None / 空 / 対象 field 無し → None
  - ``PATCHABLE_FIELDS`` の宣言順で FieldPatch をリスト化 (deterministic 出力)
  - field 毎に short note を ``_NOTE_TEMPLATES`` から format (online / offline 値を埋める)
  - ``summary_note`` は ``"resolution_type, seat_payouts differ; candidate to update settlement fields"`` 形式

**シリアライズ**: ``HandReconstructionResult.patch_proposal`` は dataclass。
``reconstruct_session`` CLI は ``dataclasses.asdict`` で plain dict に変換して
``reconstruct_<session>.jsonl`` に乗せる。``inspect_reconstruction`` CLI は
その dict をそのまま読んで ``  PATCH: <field> online=... offline=...`` 行を組み立てる。
GUI の ``summarize_reconstruction`` は dataclass / dict 両対応で
``ReconstructionBadgeState.patch_fields`` (field 名だけ) を埋める。

### `core/patch_apply.py` (Phase 5-G 実装済)

GUI / API 経由で **patch proposal を ``HandSummary`` に手動 apply** するための
pure helper。**永続化はしない** (= in-memory の advisory correction のみ)。
JSON / PHH / GameStateManager / settlement / live BettingState には触らない。

- `PATCH_APPLY_FIELDS = frozenset({"resolution_type", "seat_payouts", "pots",
  "showdown_revealed_cards", "blinds"})` — Phase 5-G で apply 対象とする
  settlement 系 + Phase 5-D 由来の blinds 5 field。Phase 5-A の
  ``PATCHABLE_FIELDS`` とは別概念 (= ``PATCHABLE_FIELDS`` は proposal 生成側、
  ``PATCH_APPLY_FIELDS`` は apply 側)。``winner_seat`` / ``pot_total`` /
  ``actions`` は明示的に **whitelist 外** (compatibility field や集計値、
  ``seat_payouts`` との整合性が崩れるリスクがあるため)
- `applicable_patch_fields(proposal) → list[str]`: proposal.fields のうち
  whitelist 内の field 名リスト (`PATCHABLE_FIELDS` 順を保持)
- `apply_patch_proposal_to_summary(summary, proposal) → HandSummary`:
  - 元 summary を **deepcopy** して新オブジェクトを返す (= 元は mutate しない)
  - whitelist 内 field のみ ``offline`` 値を適用
  - ``seat_payouts`` / ``showdown_revealed_cards`` の dict 形 field は
    **int key 正規化** (JSON round-trip 後の str key にも対応)
  - dict 形 proposal (= JSONL から読み戻し) と dataclass 形 proposal の両方を
    duck-typed に処理
  - 不正な値 (例: ``seat_payouts.offline`` が dict でない) は skip して他を適用

**約束**: apply 結果は in-memory のみ。``JsonWriter`` で session JSON を
書き換えることは Phase 5-G では **しない** (= 永続化分離。GUI / 監視ツール
向けの review correction として扱う)。``IntegrationThread.apply_patch_proposal``
が ``_last_summary_by_hand_id[hand_id]`` を patched copy で置き換え、対応
``result.patch_applied=True`` / ``applied_fields=[...]`` を立てる。

### `core/hand_reconstructor.py` (Phase 3 実装済)

hand window 単位の retrospective inference。EvidenceLog の events をその hand だけ
頭から再生して **offline HandSummary'** を生成し、online HandSummary と機械可読な
diff を出す。**online の HandSummary / JSON / PHH は一切 mutate しない**
(追加のオフラインパスとして動作)。

#### Phase 3 MVP + Phase 4-B の bootstrap 仕様 (明文化)

1. **bootstrap 3 段階 (Phase 4-B)**:
   `_bootstrap(events_sorted, initial_state, online_summary)` は次の優先順位で
   試行し `(bs, bootstrap_source, bootstrap_meta)` を返す:
   1. ``initial_state`` (deepcopy。テスト等で明示注入されたとき) → `"initial_state"`
   2. raw events (`_bootstrap_from_events`、Phase 4-B 追加) → `"raw"`
   3. ``online_summary`` (Phase 3 由来の逆算) → `"online_summary"`

   **raw bootstrap の成立条件 (Phase 4-B gate)**:
   - RFID `role="seat"` で 2 seat 以上の hole_card 観測 (= primary signal、依然必須)
   - コンストラクタの `default_sb` / `default_bb` が設定済み
   - `BettingState.start_hand` が例外を出さない

   **Phase 5-B での signal 拡張** (gate は据置き、active set / button 推定だけ広げる):
   - **active_seats**: ``RFID ∪ audio_seat_hints`` の union を採用。
     audio_seat_hints は ``AudioEvent.raw_text`` から ``シート N`` / ``seat N``
     を抽出した seat 集合 (+ 将来の ``AudioEvent.seat`` 拡張に備えた前向き互換)。
   - **button heuristic**: ``self._prev_button_seat`` (直近 *successfully*
     bootstrapped hand で start_hand に渡した button) が現 hand の active set に
     含まれていれば「ring 上の左隣」を採用 (= ライブの button 進行に一致)。
     それ以外は ``min(active_seats)`` fallback。skipped hand を挟んでも
     ``_prev_button_seat`` は壊さない (= 直近 *成功* hand の seed が残る)。
     `bootstrap_meta["button_inferred_from_prev"]` で消費側にどちら経由かを伝える。
   - meta に `signals.audio_seat_hints` / `prev_button` /
     `button_inferred_from_prev` / `sb_amount` / `bb_amount` を追加 (最後の 2 つは
     blinds level 変更検出の将来フック)。

   **Phase 5-B+ の signal refinement**:
   - audio_seat_hints を ``signals.audio_seat_hint_sources`` で 3 カテゴリに
     細分化 (``action`` / ``winner`` / ``other``)。flat list は backwards compat
     のため残置。
   - ``confidence`` を固定 0.5 から staged operational metric ``[0.4, 0.7]`` に変更
     (baseline 0.4 + 3 種 boost)。winner / other audio は active union には寄与
     するが confidence boost には含めない (active 推定としては弱い signal という
     整理)。詳細は ``_compute_raw_bootstrap_confidence`` の docstring。

   **online_summary bootstrap の成立条件**:
   - `online_summary.players[*].stack_start > 0` の seat が 2 以上
   - `online_summary.actions` に SB_POST seat が含まれる
   - SB_POST seat が active set に含まれる
   button は HU なら BTN=SB、non-HU なら SB の 1 つ前 (active 内循環順)。

   3 段階すべて失敗 → `reason="reconstruction_skipped"` で抜ける
   (`bootstrap_source=None`)。

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

- `HandReconstructionResult(actions, summary, needs_review, reason, diff, confidence, bootstrap_source, bootstrap_meta)`:
  - `actions`: 再構成 ActionRecord 列 (SB_POST/BB_POST + beam MAP の player actions)
  - `summary`: offline HandSummary' (bootstrap 失敗時は None)
  - `needs_review`: online との diff があれば True (online_summary 未指定なら False)
  - `reason`: `"reconstructed_no_diff"` / `"reconstructed_with_diff"` / `"reconstructed"` / `"reconstruction_skipped"` / `"reconstructed_with_blind_mismatch"` (Phase 5-D: blind 起因の advisory が立った hand)
  - `diff`: 差分 dict `{field: {"online": ..., "offline": ...}}` (一致 or 比較不能なら None)
  - `confidence`: **operational metric** = consumed_count / audio_count (上記参照、確率ではない)
  - `bootstrap_source` (Phase 4-B): `"initial_state"` / `"raw"` / `"online_summary"` / None
  - `bootstrap_meta` (Phase 4-B): raw bootstrap 時の active_seats / sb_seat / bb_seat /
    button_seat / button_inferred / blinds_inferred / signals / confidence などの診断 dict
- `HandReconstructor(beam_K=8, prior=None, finalizer=None, default_sb=None, default_bb=None)`:
  テスト inject 可能。`default_sb` / `default_bb` は raw-only bootstrap 用 (Phase 4-B)
- `HandReconstructor.reconstruct_from_events(events, initial_state=None, online_summary=None) → HandReconstructionResult`:
  1. `_bootstrap(...)` で 3 段階 bootstrap (initial_state → raw → online_summary)。
     失敗で `reason="reconstruction_skipped"`
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
  "reason": "reconstructed_no_diff" | "reconstructed_with_diff" | "reconstruction_skipped" | "reconstructed_with_blind_mismatch",   // Phase 5-D 追加
  "diff": {...} | null,
  "confidence": float | null,
  "bootstrap_source": "initial_state" | "raw" | "online_summary" | null,  // Phase 4-B
  "bootstrap_meta": {...} | null,                                          // Phase 4-B (raw 時のみ非 null)
  "patch_proposal": {                                                       // Phase 5-A
    "hand_id": int,
    "can_patch_automatically": false,                                       // Phase 5-A 常に false
    "fields": [
      {"field": "resolution_type", "online": "fold_win", "offline": "showdown", "note": "..."},
      {"field": "seat_payouts",    "online": {...},      "offline": {...},      "note": "..."}
    ],
    "summary_note": "resolution_type, seat_payouts differ; ..."
  } | null,                                                                 // diff があれば proposal、無ければ null
  "online_summary": {...},          // JSON dict (mutate されない)
  "offline_summary": {...} | null   // 再構成 HandSummary
}
```

`bootstrap_source="raw"` の場合、`bootstrap_meta` は次の dict (Phase 5-B / 5-B+ 拡張):
```json
{
  "source": "raw",
  "active_seats": [int, ...],            // RFID ∪ audio_seat_hints (Phase 5-B で union 化)
  "sb_seat": int,
  "bb_seat": int,
  "button_seat": int,
  "button_inferred": true,               // truth claim ではない (deterministic seed or prev 由来)
  "button_inferred_from_prev": bool,     // Phase 5-B: True なら prev_button の左隣、False なら min(active_seats)
  "prev_button": int | null,             // Phase 5-B: 直近 *successfully* bootstrapped hand の bs.button_seat
  "blinds_inferred": true,               // SB/BB amount は default_sb/bb 由来
  "sb_amount": int,                      // Phase 5-B: blinds 変更検出フック (将来の mismatch 判定用)
  "bb_amount": int,                      // 同上
  "blind_source": "current_state" | "session_default",  // Phase 5-C: その hand が
                                         // GUI 更新後の current state か初期 default かを示す
  "signals": {
    "rfid_seat_observations": [int, ...],
    "audio_seat_hints": [int, ...],      // Phase 5-B: 全 audio seat ヒントの union (backwards compat の flat list)
    "audio_seat_hint_sources": {         // Phase 5-B+: 出所カテゴリ別の細分化
      "action": [int, ...],              //   - 通常 poker action (fold/call/...) で言及された seat (= 強)
      "winner": [int, ...],              //   - 終局 WINNER 発話の seat (= 弱、confidence boost 対象外)
      "other":  [int, ...]               //   - new_hand / showdown 等の seat 言及 (= 弱)
    }
  },
  "confidence": float                    // Phase 5-B+: staged operational metric。値域 [0.4, 0.7]
                                         // (calibrated probability ではない、下記 staging 内訳参照)
}
```

**Phase 5-B+ staged `confidence` の内訳** (依然 operational metric、posterior ではない):

| 条件 | 増分 | 累計の典型 |
|---|---|---|
| baseline (RFID >= 2 seat の conservative gate を満たした) | +0.4 | 0.4 |
| RFID >= 3 seat (multi-seat 観測は 2 seat より信頼度が上) | +0.1 | 0.5 |
| ``audio_seat_hint_sources.action`` が RFID seat と overlap (cross-modal corroboration) | +0.1 | 0.5 / 0.6 |
| ``button_inferred_from_prev=True`` (history-grounded) | +0.1 | 0.5 / 0.6 / 0.7 |

合計値域は ``[0.4, 0.7]``。**winner / other カテゴリの audio は active_seats union には
寄与するが、cross-modal boost の対象には入らない** (winner / other は active 推定として
は弱い signal という Phase 5-B+ の整理)。値の動的計算は ``_compute_raw_bootstrap_confidence``。
モデル事後確率 (top-1 / top-2 log 差 / Brier-calibrated) への置換は将来課題。

Phase 4-B CLI 動作: session JSON の `blinds.sb` / `blinds.bb` (トップレベル、
または各 hand `blinds` から fallback) を読み HandReconstructor の default として
渡す。これにより raw-only bootstrap が成立し得る hand では online_summary を
読む前に raw を試行する。

オプション: `--output PATH` / `--evidence PATH` (省略時は session.parent / "reconstruct_<id>.jsonl" / "evidence_<id>.jsonl")。`--quiet` で info ログを抑制。

**約束**: online JSON (`<session>.json`) も PHH も一切 mutate しない。差分情報の
発信は別ファイル (`reconstruct_<session>.jsonl`)。後段の GUI / 監視ツール / Phase 4+
の自動 patch ロジックがこれを読んで判断する想定。

### `output/inspect_reconstruction.py` (Phase 4-C1 実装済)

`output.reconstruct_session` が出す ``reconstruct_<session>.jsonl`` を読んで、
hand 単位に **1 行サマリ** を標準出力に出す read-only CLI。

```
hand 1 [OK] bootstrap=online_summary reason=reconstructed_no_diff
hand 2 [REVIEW] bootstrap=raw reason=reconstructed_with_diff diff_fields=resolution_type,seat_payouts
hand 3 [SKIPPED] bootstrap=None reason=reconstruction_skipped
```

**使い方**:
```bash
python -m output.inspect_reconstruction --reconstruct logs/reconstruct_session_xxx.jsonl
```

**オプション**:
- `--only-needs-review`: ``needs_review=true`` の hand のみ表示
- `--fields A,B,C`: diff のうち指定 field 名のみを ``diff_fields=`` に出す
  (例: monitoring で settlement 系の差分だけ拾いたいときは
  `--fields resolution_type,seat_payouts`)
- `--show-patches` (Phase 5-A): hand 行の直後に ``  PATCH: <field>
  online=... offline=...`` 行を出す (proposal がある hand のみ)。proposal は
  ``reconstruct_session`` が JSONL に乗せた ``patch_proposal`` を読むだけで、
  apply は **しない** (= 完全に read-only)
- `--quiet`: loader の info ログを抑制

**`--show-patches` 出力例**:
```
hand 2 [REVIEW] bootstrap=raw reason=reconstructed_with_diff diff_fields=resolution_type,seat_payouts
  PATCH: resolution_type  online=fold_win  offline=showdown
  PATCH: seat_payouts  online={'2': 300}  offline={'1': 150, '2': 150}
```

**ステータスラベル** (優先順位順):
1. `[SKIPPED]` — ``offline_summary`` が ``None`` または
   ``reason == "reconstruction_skipped"``
2. `[REVIEW]` — ``needs_review`` が truthy または
   ``reason == "reconstructed_with_diff"``
3. `[OK]` — それ以外 (= reconstruct 成功 + diff なし)

**約束**: ``inspect_reconstruction`` は ``reconstruct_session`` が吐いた JSONL
を読むだけの read-only ツール。online JSON / PHH / GameStateManager /
``_last_reconstruction_by_hand_id`` のいずれにも触らない。GUI / 監視ツールが
このログ要約を取り込むまでの当座の可視化手段。Phase 5-A の ``--show-patches``
も同様で、patch を apply するコマンドはまだ存在しない (Phase 5-B 以降の課題)。

### `gui/reconstruction_badges.py` (Phase 4-C2 実装済)

``HandReconstructionResult`` を GUI 表示用の小さな状態
(``ReconstructionBadgeState``) に投影する pure helper。``customtkinter`` /
``tkinter`` に依存しない (= 通常の pytest で検証できる)。badge の判定ルールは
Phase 4-C1 CLI (`output.inspect_reconstruction`) と統一する。

- `ReconstructionBadgeState(status, show_raw_badge, reason, bootstrap_source,
  diff_fields, button_inferred)`: GUI が表示するためのフラット dataclass
- `summarize_reconstruction(result) → ReconstructionBadgeState`:
  - status 判定 (優先順位):
    1. ``result`` が None / ``summary`` が None / ``reason=="reconstruction_skipped"``
       → ``"skipped"``
    2. ``needs_review`` truthy / ``reason=="reconstructed_with_diff"`` → ``"review"``
    3. それ以外 → ``"ok"``
  - RAW バッジ: ``bootstrap_source == "raw"`` の hand のみ ``show_raw_badge=True``
  - diff_fields は ``result.diff`` の keys を sorted (alphabetical) で返す
- `format_history_line(hand_id, winner_seat, pot_total, badge_state) → str`:
  1 hand を 1 行のテキストに整形 (Phase 4-C1 CLI と同じ語彙)

### GUI 側の advisory パネル (Phase 4-C2 実装済)

``gui/dashboard.py:GUIDashboard`` に「ハンド履歴 (advisory)」パネルを追加:

- レイアウト: ヘッダー / プレイヤー一覧 + アクションログ / **ハンド履歴**(新) /
  コントロール の 4 行
- 履歴行: hand 終局時に追加。tag 色は status と同名 (``ok`` / ``review`` /
  ``skipped``) で、`reconstruction_badges` の ``BADGE_COLOR_*`` を使用
- "Latest advisory" ラベル: 最新ハンドの status / reason / bootstrap / diff /
  button_inferred を 1 行に圧縮表示

**advisory 取得経路**:
- ``IntegrationThread`` が hand 終局時 (``_finalize_hand`` 末尾、
  ``_invoke_reconstructor_hook`` の直後) に
  ``on_hand_finalized(hand_id)`` callback を発火
- GUI の ``GUIDashboard.on_hand_finalized(hand_id)`` が
  ``_hand_finalized_queue`` に hand_id を積む (スレッド安全)
- main thread の ``_poll_updates`` が queue を消費 →
  ``_apply_hand_finalized(hand_id)`` を呼ぶ
- ``_apply_hand_finalized`` は ``IntegrationThread.get_last_summary(hand_id)`` /
  ``get_reconstruction_result(hand_id)`` を呼んで advisory を read-only で取得
- ``summarize_reconstruction`` で badge state に投影し、``_history_box`` に
  1 行追加 + ``_lbl_latest_advisory`` を更新

**accessor (Phase 4-C2 追加、IntegrationThread)**:
- ``get_reconstruction_result(hand_id) -> Optional[HandReconstructionResult]``:
  該当 hand 無しなら None。GUI が ``_last_reconstruction_by_hand_id`` に直接
  触らないようにする read-only API
- ``get_last_summary(hand_id) -> Optional[HandSummary]``: 同上で
  ``_last_summary_by_hand_id`` の read-only ラッパ

**約束**: GUI の advisory パネルは **read-only**。online JSON / PHH /
GameStateManager / settlement の挙動には一切触れない。badge / 詳細は
クリックできない静的 indicator として実装 (Phase 4-C3+ で interactive 化検討)。

### GUI blind advisory 表示 (Phase 5-E 実装済)

Phase 5-C / 5-D で蓄積された blind 関連 advisory 情報 (``summary.blinds`` /
``bootstrap_meta.blind_source`` / ``patch_proposal`` に含まれる
``field="blinds"`` FieldPatch) を、operator が一目で確認できるように
``gui/dashboard.py`` から表示する。

**4 つの read-only helper** (``gui/dashboard.py``、external module には触らない):

- ``_blind_source_text(result) -> str``:
  ``bootstrap_meta["blind_source"]`` を short label に正規化
  (``current_state`` / ``session_default`` 既知値はそのまま、それ以外 /
  None / 不正形式は ``unknown``)
- ``_has_blind_patch(result) -> bool``:
  ``patch_proposal.fields`` に ``field="blinds"`` があれば True。
  dataclass instance (live hook 経由) と dict 形 (JSONL から読み戻し)
  の両方に対応 (= ``summarize_reconstruction`` と同じ duck-typed 寛容性)
- ``_format_blind_for_advisory(summary, result) -> str``:
  Latest advisory 用 ``blinds=SB/BB (source=...)`` 文字列。``summary.blinds``
  が無ければ ``?/?`` フォールバック、``blind_source`` が無ければ ``unknown``
- ``_format_blind_suffix_for_history(summary, result) -> str``:
  history 行 用の lightweight suffix。``blind_source="current_state"``
  のときだけ ``blinds=SB/BB (current_state)`` を出し、``session_default``
  ではノイズ削減のため省略する。``_has_blind_patch=True`` のときは末尾に
  ``(blind_mismatch)`` を付ける

**Latest advisory ラベルへの統合** (``_apply_hand_finalized`` 内):

順序は ``bootstrap`` と ``diff`` の間に挿入:

```
Latest advisory hand #2  |  status=review  |  reason=reconstructed_with_blind_mismatch
  |  bootstrap=raw  |  blinds=200/400 (source=session_default)  |  blind_mismatch=yes
  |  diff=none  |  patch_fields=blinds
```

- ``blinds=SB/BB (source=...)`` は **常時表示** (degrade 時は ``?/?`` /
  ``unknown``)
- ``blind_mismatch=yes`` は Phase 5-D の blind FieldPatch がぶら下がる hand
  のときだけ追加 (``=no`` は出さない、ノイズ削減方針)

**history 行 への suffix**:

- ``blind_source=current_state`` の hand: ``blinds=SB/BB (current_state)``
  を末尾に付ける
- ``blind_source=session_default`` の hand: ``blinds=…`` suffix を **省略**
  (キャッシュゲームの定常状態なのでノイズになる)
- blind FieldPatch ありの hand: ``(blind_mismatch)`` を末尾に追記
  (current_state / session_default いずれの場合も)

**約束**: GUI の blind 表示は **read-only**。online JSON / PHH /
GameStateManager / settlement / reconstruct ロジックには触らない。
変更ファイルは ``gui/dashboard.py`` (helper 追加 +
``_apply_hand_finalized`` 内拡張) と ``tests/test_gui.py`` のみ。

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
→ 618 passed  (test_vision.py は cv2 未インストールのため収集エラー、既知問題)
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
| test_reconstructor_live_hook.py | IntegrationThread から advisory reconstruct を呼ぶ live hook (online_summary 注入 / 複数 hand / 例外時 online 不変 / online_summary=None で skipped or raw bootstrap) (Phase 4-A + 4-B, 10 件) |
| test_hand_reconstructor_bootstrap.py | bootstrap 3 段階優先 (initial_state / raw / online_summary) + skipped + CLI round-trip で `bootstrap_source` 反映 (Phase 4-B, 11 件) + Phase 5-B: Audio 補助 signal / prev_button 左隣 heuristic / meta 拡張 / safety fallback (Phase 5-B, +13 件) + Phase 5-B+: audio_seat_hint_sources 分類 / staged confidence (4 levels) / skipped hand を跨いだ prev_button 保持 (Phase 5-B+, +9 件) + Phase 5-C: HandReconstructor.update_blinds + blind_source field (Phase 5-C, +4 件) + Phase 5-D: blind mismatch advisory (Pattern A propagation health / Pattern B amount mismatch / 既存 proposal への append / e2e) (Phase 5-D, +10 件) |
| test_reconstructor_live_hook.py (Phase 5-C 追加) | IntegrationThread.update_blinds で canonical state を同期 / 不正値で ValueError / 過去 hand の summary は変更しない (Phase 5-C, +3 件) |
| test_gui.py (Phase 5-C 追加) | _cmd_update_blinds が IntegrationThread.update_blinds を呼ぶ / 不正値拒否 / thread 未接続時の warning (Phase 5-C, +3 件) |
| test_gui.py (Phase 5-E 追加) | Latest advisory に `blinds=SB/BB (source=...)` 表示 / `blind_mismatch=yes` (patch_proposal に blinds 含む時) / history 行に current_state のみ blinds suffix / `(blind_mismatch)` marker / dict 形 proposal 互換 / source 正規化 (Phase 5-E, +10 件) |
| test_gui.py (Phase 5-F 追加) | history Textbox の bounded retention (`_trim_history_lines`: over/under/exactly-at-limit、bad index degrade、`_apply_hand_finalized` から呼ばれる)、evicted hand_id (accessor が None) で `_apply_hand_finalized` が `[SKIPPED]` + `blinds=?/? source=unknown` に degrade (Phase 5-F, +7 件) |
| test_reconstructor_live_hook.py (Phase 5-F 追加) | `IntegrationThread._evict_old_advisory_entries`: symmetric / asymmetric dicts / under-limit no-op / `_invoke_reconstructor_hook` 経由の自動 eviction / evicted hand_id への get_* accessor が None / `MAX_ADVISORY_HANDS` default sanity (Phase 5-F, +6 件) |
| test_patch_apply.py | `core.patch_apply.apply_patch_proposal_to_summary` の whitelist 内/外、mutation 防止、int key 正規化、dict 形 proposal、`applicable_patch_fields` filter (Phase 5-G, 22 件) |
| test_reconstructor_live_hook.py (Phase 5-G 追加) | `IntegrationThread.apply_patch_proposal`: in-memory summary 置き換え / missing hand_id で False / non-whitelist 単独で False / GameStateManager / JsonWriter に触らない / applied_fields list 正確 (Phase 5-G, +7 件) |
| test_gui.py (Phase 5-G 追加) | `_cmd_apply_patch` 確認 yes/no / 各 abort パス (latest hand_id 無 / thread 無 / proposal 無 / dialog 不可 / apply 例外 / apply False) + Latest advisory に `patch_applied=yes` / `applied_fields=...` 表示 + `append_to_history=False` で insert スキップ (Phase 5-G, +13 件) |
| test_inspect_reconstruction_cli.py | inspect_reconstruction CLI (label 判定 / --only-needs-review / --fields filter / 壊れた JSONL skip) (Phase 4-C1, 14 件) |
| test_reconstruction_badges.py | gui.reconstruction_badges 単体 (status / RAW / diff_fields / button_inferred / format_history_line) (Phase 4-C2, 20 件) |
| test_gui.py (Phase 4-C2 追加) | GUIDashboard.on_hand_finalized / _apply_hand_finalized: queue 経由、advisory accessor 呼び出し、tag 反映 (Phase 4-C2, +8 件) |
| test_reconstructor_live_hook.py (Phase 4-C2 追加) | IntegrationThread.on_hand_finalized callback 発火 + get_reconstruction_result / get_last_summary accessor (Phase 4-C2, +5 件) |
| test_patch_proposal.py | compute_patch_proposal 単体 (各 patchable field の note 形成 / PATCHABLE_FIELDS 順序 / 非対象 field 無視 / asdict round-trip) (Phase 5-A, 13 件) |
| test_inspect_reconstruction_cli.py (Phase 5-A 追加) | --show-patches + format_patch_lines (Phase 5-A, +8 件) |
| test_reconstruction_badges.py (Phase 5-A 追加) | TestPatchFields: HandPatchProposal (dataclass/dict) → patch_fields 投影 (Phase 5-A, +4 件) |
| test_gui.py (Phase 5-A 追加) | _apply_hand_finalized で patch_fields= が Latest advisory ラベルに含まれる / proposal=None 時は出ない (Phase 5-A, +1 件) |
| test_hand_reconstructor.py (Phase 5-A 追加) | diff 検出時に patch_proposal がぶら下がる / 無 diff/actions-only では None / CLI JSONL で patch_proposal が serialize される (Phase 5-A, +1 件 + 既存 4 件強化) |

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
| GUI advisory パネル (hand 履歴 + 最新 advisory 詳細) | ✅ 完了 (Phase 4-C2) | `gui/dashboard.py`: hand 終局時に履歴行追加 + 最新ハンドの reason/bootstrap/diff を表示。`gui/reconstruction_badges.py:summarize_reconstruction` が status/RAW バッジ判定を担う。online JSON / PHH / GameStateManager には触らない |
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
| Live advisory reconstruct hook (IntegrationThread から online_summary 注入) | ✅ 完了 (Phase 4-A) | `integration/engine.py`: `_finalize_hand` 末尾で `_invoke_reconstructor_hook(hand_id, online_summary=summary)` を呼び、結果を `_last_summary_by_hand_id` / `_last_reconstruction_by_hand_id` に保持。online JSON / PHH / GameStateManager は mutate しない |
| Raw-only bootstrap (online_summary 不要の HandReconstructor 起動) | ✅ 完了 (Phase 4-B) | `HandReconstructor._bootstrap_from_events`: RFID `role="seat"` 観測 + コンストラクタ `default_sb`/`default_bb` で BettingState を起こす。button は最小 seat 番号 (deterministic, `button_inferred=True`)。`HandReconstructionResult.bootstrap_source` / `bootstrap_meta` で診断情報を返す。CLI 出力 / live hook の双方で稼働 |
| Raw bootstrap signal 拡張 (Audio 補助 + prev_button heuristic) | ✅ 完了 (Phase 5-B) | `_bootstrap_from_events`: RFID は依然 primary (>= 2 seat gate)、Audio raw_text の `シート N` 抽出を補助 signal として active_seats に union。``self._prev_button_seat`` を経路問わず更新し、現 hand の active set に含まれていれば「ring 上の左隣」を button に採用 (`button_inferred_from_prev`)。`bootstrap_meta` に `audio_seat_hints` / `prev_button` / `button_inferred_from_prev` / `sb_amount` / `bb_amount` (blinds 変更検出フック) を追加 |
| Raw bootstrap signal 細分化 + staged confidence | ✅ 完了 (Phase 5-B+) | `signals.audio_seat_hint_sources` で audio seat hint を ``action`` / ``winner`` / ``other`` の 3 カテゴリに分割 (action のみ confidence boost 対象)。`confidence` を固定 0.5 から ``_compute_raw_bootstrap_confidence`` による staged ``[0.4, 0.7]`` に変更 (baseline 0.4 + RFID>=3 / action overlap / button_inferred_from_prev の 3 boost)。依然 operational metric であって calibrated probability ではない。``_prev_button_seat`` は **直近 successfully bootstrapped hand** の seed を保持 (skipped hand を跨いでも壊さない) |
| Blind level 変更の canonical state 同期 | ✅ 完了 (Phase 5-C) | `IntegrationThread.update_blinds(sb, bb)` が **唯一の書き込み口** (canonical)。``GameStateManager._sb/_bb`` と ``HandReconstructor._default_sb/_bb`` は **projection** として canonical を反映 (独立に書き換えない)。GUI に SB/BB 入力 + "Blinds 更新" ボタンを追加 (`_cmd_update_blinds`)。`bootstrap_meta.blind_source` で `"current_state"` (runtime 更新後) / `"session_default"` (初期値のまま) を区別。**次 hand から有効**、現 hand の HandSummary.blinds は不変。音声起源の blind 推定はしない (= GUI 操作起点のみ)。`session_default` を自動 ``needs_review`` の根拠にはしない (キャッシュゲームでノイズが多すぎる) |
| Blind mismatch advisory (reconstruct への反映) | ✅ 完了 (Phase 5-D) | `HandReconstructor._apply_blind_mismatch_advisory`: (A) `_blinds_updated_at_runtime=True` だが meta が `session_default` のままの **propagation health check**、(B) `online_summary.blinds` と `bootstrap_meta.sb_amount/bb_amount` の **amount mismatch** を検出。検出時は `needs_review=True`、reason を ``reconstructed_with_blind_mismatch`` に昇格 (settlement diff が既にある場合はそのまま)、`patch_proposal` に `FieldPatch(field="blinds", online=..., offline=...)` を append。proposal が無ければ blind-only proposal を新規作成。`can_patch_automatically=False` (apply は依然しない) |
| GUI blind 表示 (advisory ラベル + history 行) | ✅ 完了 (Phase 5-E) | `gui/dashboard.py` の helper (`_format_blind_for_advisory` / `_format_blind_suffix_for_history` / `_has_blind_patch` / `_blind_source_text`) で `summary.blinds` / `bootstrap_meta.blind_source` / `patch_proposal.fields` を read-only に参照。Latest advisory に `blinds=SB/BB (source=current_state\|session_default\|unknown)` を常時表示、`blind_mismatch=yes` を Phase 5-D の blind FieldPatch ありの hand のみ表示。history 行 suffix は `current_state` のときだけ `blinds=SB/BB (current_state)` を出す (`session_default` はノイズ削減のため省略)、blind mismatch は `(blind_mismatch)` marker。online JSON / PHH / reconstruct ロジックには触らない |
| Advisory state の bounded retention | ✅ 完了 (Phase 5-F) | `integration.engine.MAX_ADVISORY_HANDS=500` で `_last_summary_by_hand_id` / `_last_reconstruction_by_hand_id` を eviction (union of hand_ids 最古から)。`_evict_old_advisory_entries` を `_invoke_reconstructor_hook` 末尾 + `_finalize_hand` 末尾の両方から呼んで `_apply_boundaries` 経路もカバー。evicted hand への `get_last_summary` / `get_reconstruction_result` は ``None`` を返すので GUI は Phase 4-C2 / 5-E の degrade パスで `[SKIPPED]` + `blinds=?/? (source=unknown)` 表示。GUI 側も `MAX_HISTORY_LINES=1000` で history Textbox を bounded retention (`_trim_history_lines` を `_apply_hand_finalized` の insert 直後に呼ぶ)。`_completed_hands` (hand window events) は本フェーズの eviction 対象外 (= Phase 6+ 候補) |
| GUI からの手動 patch apply (in-memory のみ) | ✅ 完了 (Phase 5-G) | `core/patch_apply.py:apply_patch_proposal_to_summary` で whitelist field (`resolution_type` / `seat_payouts` / `pots` / `showdown_revealed_cards` / `blinds`) のみ deepcopy 後上書き → 新 `HandSummary` を返す pure helper。`IntegrationThread.apply_patch_proposal(hand_id) → bool` で in-memory `_last_summary_by_hand_id[hand_id]` を patched copy で置き換え、`result.patch_applied=True` / `applied_fields=[...]` を立てる。GUI 側に "Apply patch" ボタン (`_cmd_apply_patch`) + 確認ダイアログ hook (`_ask_apply_patch_confirmation`)、Latest advisory に `patch_applied=yes` / `applied_fields=...` 表示。`winner_seat` / `pot_total` / `actions` は明示的に whitelist 外。**JSON / PHH / GameStateManager / settlement / live BettingState は一切触らない** (永続化は別フェーズ) |
| Reconstruct 結果可視化 CLI (read-only) | ✅ 完了 (Phase 4-C1) | `output/inspect_reconstruction.py`: `reconstruct_<session>.jsonl` を読んで `[OK]` / `[REVIEW]` / `[SKIPPED]` ラベル付きで hand 単位サマリを出す。`--only-needs-review` / `--fields A,B` フィルタ対応。online JSON / PHH / live hook 結果には触らない |
| Patch proposal (差分 → 修正案、apply は無し) | ✅ 完了 (Phase 5-A) | `core/patch_proposal.py`: `HandPatchProposal` / `FieldPatch` / `compute_patch_proposal`。対象 field は resolution_type / seat_payouts / winner_seat / pot_total / showdown_revealed_cards。`HandReconstructionResult.patch_proposal` に乗り、CLI `--show-patches` と GUI ``patch_fields=`` 表示で見える。``can_patch_automatically=False`` (Phase 5-B 以降で apply 判定) |
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
pytest tests/ -v --ignore=tests/test_vision.py                                   # 全 suite: 618 件 pass (280 baseline + 66 M1–M3 + 6 Phase 1 + 20 Phase 2-A + 11 Phase 2-B + 19 Phase 2-C + 14 Phase 3 + 10 Phase 4-A + 11 Phase 4-B + 14 Phase 4-C1 + 33 Phase 4-C2 + 27 Phase 5-A + 13 Phase 5-B + 9 Phase 5-B+ + 10 Phase 5-C + 10 Phase 5-D + 10 Phase 5-E + 13 Phase 5-F + 42 Phase 5-G)
pytest tests/test_observation_model.py tests/test_inference_equivalence.py -v    # M2
pytest tests/test_beam_search.py tests/test_bayesian_e2e.py -v                   # M3
pytest tests/test_settlement_models.py -v                                         # Phase 1 + Phase 2-B engine E2E
pytest tests/test_settlement_logic.py -v                                          # Phase 2-A (settlement core)
pytest tests/test_hand_finalizer.py -v                                            # Phase 2-B (HandFinalizer 単体)
pytest tests/test_hand_boundary.py -v                                             # Phase 2-C (boundary + replay + integration)
pytest tests/test_hand_reconstructor.py -v                                        # Phase 3 (HandReconstructor + CLI)
pytest tests/test_reconstructor_live_hook.py -v                                   # Phase 4-A (IntegrationThread live advisory hook)
pytest tests/test_hand_reconstructor_bootstrap.py -v                              # Phase 4-B (3-stage bootstrap + CLI round-trip)
pytest tests/test_inspect_reconstruction_cli.py -v                                # Phase 4-C1 (inspect_reconstruction CLI)
pytest tests/test_reconstruction_badges.py tests/test_gui.py -v                   # Phase 4-C2 (GUI advisory パネル + helper)
pytest tests/test_patch_proposal.py -v                                            # Phase 5-A (compute_patch_proposal 単体)
python -m output.inspect_reconstruction --reconstruct logs/reconstruct_session_xxx.jsonl --show-patches  # Phase 5-A: PATCH 提案も併記
python -m output.inspect_reconstruction --reconstruct logs/reconstruct_session_xxx.jsonl  # Phase 4-C1: reconstruct 結果一覧
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

(Phase 3 / 4-A / 4-B 完了) HandReconstructor が hand window を頭から再生して
                              offline HandSummary' を生成 → online との diff → needs_review
                              自動判定。online JSON / PHH は **mutate しない**。
                              - 後処理 (Phase 3): `output/reconstruct_session` CLI で
                                別 JSONL (`reconstruct_<id>.jsonl`) に書く
                              - live (Phase 4-A): IntegrationThread が hand 終局時に
                                advisory として呼び、結果は `_last_reconstruction_by_hand_id`
                                に in-memory 保持。GUI / 監視ツールがここを読む想定
                                (online JSON / PHH / GameStateManager は不変)
                              - bootstrap (Phase 4-B): initial_state → raw events
                                (RFID role="seat" + default_sb/bb) → online_summary
                                の 3 段階で BettingState を起こす。`bootstrap_source` /
                                `bootstrap_meta` で診断情報を返す
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

**Phase 4-A 完了済み (live advisory reconstruct hook)**:
- ✅ `integration/engine.py` の `IntegrationThread`:
  - `_last_summary_by_hand_id: dict[int, HandSummary]` を追加
  - `_last_reconstruction_by_hand_id: dict[int, HandReconstructionResult]` を追加
  - `_last_reconstruction` は最新エントリへの convenience pointer として残す
- ✅ `_invoke_reconstructor_hook(hand_id, online_summary: Optional[HandSummary]=None)` シグネチャ拡張
- ✅ `_apply_boundaries`: end の `reason == "audio_winner"` のときは invoke を遅延
  (直後の `_finalize_hand` 経由で summary 付きで呼ぶため)。`board_cleared` /
  `audio_new_hand_implicit_end` 等の他 reason では従来通り `online_summary=None` で invoke
- ✅ `_finalize_hand`: `JsonWriter.append_hand_summary(summary)` 直後に
  `_last_summary_by_hand_id[summary.hand_id] = summary` を保存し、対応 hand window
  が ``_completed_hands`` にあれば `_invoke_reconstructor_hook(hand_id, online_summary=summary)`
- ✅ reconstructor で例外が出ても online path は不変 (`_last_reconstruction = None` で抜ける)
- ✅ online HandSummary / `logs/<session>.json` / PHH / `GameStateManager.stacks` は
  Phase 2-B と完全同一 (advisory layer は読み取り専用)

**Phase 4-A 保持ポリシー**:
- `_last_summary_by_hand_id` / `_last_reconstruction_by_hand_id` は **in-memory のみ**。
  canonical な book of record は `logs/evidence_<session>.jsonl` (raw 観測) +
  `logs/<session>.json` (online HandSummary) + `output/reconstruct_session` CLI が
  出力する `logs/reconstruct_<session>.jsonl` (offline reconstruction) で永続化済み。
  advisory 結果はプロセス再起動で揮発する設計
- `_last_reconstruction_by_hand_id[hand_id].needs_review` を GUI / 監視ツールが
  読むフックは Phase 4-B+ で予定 (現状は in-memory に置くだけ)

**Phase 4-B 完了済み (raw-only bootstrap 強化)**:
- ✅ `core/hand_reconstructor.py:HandReconstructor._bootstrap_from_events` 新設。
  RFID `role="seat"` 観測の seat 集合 (2 seat 以上) + コンストラクタの
  `default_sb` / `default_bb` から BettingState を起こす
- ✅ button heuristic: deterministic に最小 seat 番号を button と仮定
  (`bootstrap_meta["button_inferred"]=True` で truth ではない旨を記録)
- ✅ SB/BB seat は `integration.action_order.compute_blinds` で算出
  (HU: BTN=SB、non-HU: SB = BTN の左隣)
- ✅ 3 段階 bootstrap: `_bootstrap(...)` が
  `initial_state` → raw events → `online_summary` の順に試行し、
  `(bs, bootstrap_source, bootstrap_meta)` を返す
- ✅ `HandReconstructionResult` に `bootstrap_source: Optional[str]` /
  `bootstrap_meta: Optional[dict]` を追加。reason / diff / confidence と並列して
  CLI / live hook の双方で参照可能
- ✅ CLI (`output/reconstruct_session.py`): session JSON の `blinds.sb` / `bb` を
  `_extract_session_blinds` で抽出し HandReconstructor に渡す。出力 JSONL の各
  行に `bootstrap_source` / `bootstrap_meta` を含める
- ✅ Live hook (`integration/engine.py`): `_sb_amount` / `_bb_amount` を確定後に
  HandReconstructor を構築 (Phase 4-A から構築順を入れ替え)。これにより
  `online_summary=None` の hand window でも RFID hole_cards が揃っていれば
  raw bootstrap が成立する

**raw-only bootstrap の制約 (明文化)**:

- **Phase 4-B raw bootstrap は RFID-centric (audio は bootstrap signal ではない)**:
  active seats / hole cards は ``RFID role="seat"`` 観測のみから取る。AudioEvent
  は seat 情報を持たないので、現時点では bootstrap の signal source ではない。
  Phase 4-C 以降で「シート N が fold」等の自然言語からの seat 抽出や camera
  dependency を加えるのは別 commit の候補。
- **`button = min(active_seats)` は deterministic seed であって truth 推定ではない**:
  「最も button らしい seat」を確率的に推定したものではなく、
  ``BettingState.start_hand`` を起こすために確定的に選ぶ値。実際の button が
  誰だったかは raw からは分からないので
  ``bootstrap_meta["button_inferred"]=True`` で消費側にこの事実を伝える。
  実際の button と異なれば後段の `_compute_diff` で `actions` の差異として
  現れ、`needs_review=True` が立つ。
- **`bootstrap_meta["confidence"] = 0.5` は fixed heuristic confidence であって
  calibrated probability ではない**: モデルが計算した posterior でも
  Brier-calibrated な値でもなく、**「この heuristic は truth ではない」という印**
  (= 結果を 0.5 weight で扱って下さい、というメッセージ)。signal 強度に応じた
  動的計算は Phase 4-C+ の課題。
- audio events には seat 情報が無いため、SB_POST/BB_POST の seat 推定は不可。
  blinds 額は呼び出し側 (CLI: session JSON のトップ / 各 hand から、
  live hook: GameStateManager / IntegrationThread から) を介して default として
  渡す必要がある
- raw bootstrap 失敗条件:
  - RFID `role="seat"` 観測が 2 seat 未満
  - `default_sb` または `default_bb` が None / 0 以下
  - `BettingState.start_hand` が例外を投げる
  これらのいずれかなら `online_summary` fallback (Phase 3 経路) → skipped

**Phase 5-B 完了済み (raw bootstrap signal 拡張)**:
- ✅ ``_bootstrap_from_events`` に **Audio 補助 signal** を追加:
  ``AudioEvent.raw_text`` から ``シート N`` / ``seat N`` を抽出した seat 集合を
  ``signals.audio_seat_hints`` として meta に記録、active_seats は
  ``RFID ∪ audio_seat_hints`` の union を使う (= RFID で観測されなかった seat も
  音声言及があれば active 候補に追加)。``AudioEvent.seat`` 属性が将来追加された
  場合も拾えるよう ``getattr`` で前向き互換も入っている
- ✅ **prev_button heuristic**: ``HandReconstructor._prev_button_seat`` は
  **直近 successfully bootstrapped hand** の ``bs.button_seat`` を保持する
  (= bootstrap 失敗で skipped になった hand は ``_prev_button_seat`` を更新しない)。
  経路問わず (raw / online_summary / initial_state) 成功時に更新する。次 hand の
  raw bootstrap で active set に含まれていれば「ring 上の左隣」を button に採用
  (= ライブポーカーの button 左回り進行に一致)。meta の
  ``button_inferred_from_prev`` が ``True`` のときが prev 由来、``False`` なら
  ``min(active_seats)`` fallback (Phase 4-B 互換)。
  *Skipped hand を跨いだ semantics*: ``[成功 A → skipped B → hand C]`` の場合、
  hand C の raw bootstrap は **hand A の button** を seed に使う (= button history は
  失われない)
- ✅ **conservative gate は据置き**: ``len(rfid_seats) < 2`` は依然失敗扱い。
  Audio ヒントだけで bootstrap には踏み込まない (= 誤検知より skip を優先)
- ✅ **blinds 変更検出フック**: ``bootstrap_meta["sb_amount"]`` /
  ``bootstrap_meta["bb_amount"]`` に default 値を埋めて、将来 session 中に
  blinds level が上がった場合の検出 / mismatch ハンドリングを Phase 5-C 以降で
  載せやすくする

**Phase 5-C 完了済み (blind level 変更の canonical state 同期)**:
- ✅ ``IntegrationThread.update_blinds(sb: int, bb: int)`` を追加。GUI 操作起点で
  以下を **アトミックに同時更新**:
    - ``self._sb_amount`` / ``self._bb_amount`` (`_finalize_hand` で読む)
    - ``self._game_state._sb`` / ``self._game_state._bb``
      (`HandSummary.blinds` 生成時に読む)
    - ``self._hand_reconstructor`` の runtime blinds (raw bootstrap で読む)
  不正値 (非数値 / 0 以下) では ``ValueError`` を投げる (= GUI 側で気づける)
- ✅ ``HandReconstructor.update_blinds(sb, bb)`` setter を追加。内部に
  ``_blinds_updated_at_runtime`` フラグを持ち、``bootstrap_meta["blind_source"]``
  に反映:
    - ``"current_state"``  ← runtime に ``update_blinds`` が呼ばれた後
    - ``"session_default"`` ← constructor 渡しの初期値のまま
- ✅ GUI (`gui/dashboard.py`): "Blinds 更新" 行を controls に追加。
  ``_cmd_update_blinds`` で SB / BB 入力をバリデーションし
  (非数値・負値・``sb >= bb`` をログで弾く)、``IntegrationThread.update_blinds``
  を呼ぶ。失敗時もクラッシュしない
- ✅ **「次 hand から有効」semantics**: 現在進行中の hand は既に
  ``bs.start_hand`` で旧 blind を固定済みのため、その hand の
  ``HandSummary.blinds`` は変わらない (= 過去 hand を retroactive に書き換えない)。
  テストでこれを保証

**state ownership (canonical / projection の関係)**:

実装上は blind 額を保持する場所が 3 ヶ所あるが、責務は次のように分離する:

- **canonical (single source of truth)**: ``IntegrationThread._sb_amount`` /
  ``_bb_amount``。``IntegrationThread.update_blinds`` を経由した値だけが格納される
  唯一の正となる書き込み点。
- **projection (canonical を反映するだけで独立に書き換えない)**:
    - ``GameStateManager._sb`` / ``_bb``
      — ``HandSummary.blinds`` 出力時の参照源
    - ``HandReconstructor._default_sb`` / ``_default_bb``
      + ``_blinds_updated_at_runtime`` フラグ
      — raw bootstrap の blinds amount + ``blind_source`` 判定

``update_blinds`` 以外の経路で ``GameStateManager._sb`` や
``HandReconstructor._default_sb`` を直接書き換える呼び出しは作らない (= projection
側を canonical 抜きで動かさない)。将来 ``GameStateManager`` /
``HandReconstructor`` を「参照時に IntegrationThread から pull する」真の
projection にリファクタする可能性は別フェーズで検討。

**Phase 5-C スコープ外 / 注意事項 (やらない理由の明文化)**:

- **``blind_source="session_default"`` を ``needs_review`` の根拠にしない**:
  単純に「session_default のまま hand_id が進んでいたら review」というルールは
  ノイズが多すぎる:
    - blind を一度も変更しない session (= 殆どのキャッシュゲーム) では完全に正常
    - tournament でも、まだ最初の blind level の間はずっと ``"session_default"``
  なので、``blind_source`` を review trigger に使うなら、別 signal (= blind level
  変更があったことを示す independent な observation) との ``and`` で初めて意味を
  持つ。Phase 5-C 時点では ``blind_source`` は **監査用の出所マーカー** に留め、
  自動 ``needs_review`` 化はしない。
- **blind 変更の audit trail (= 「いつ誰が変えたか」)** は Phase 5-C スコープ外。
  現状は変更直後の hand から ``blind_source="current_state"`` になる **edge 検出**
  しかできない。将来候補:
    - blind 変更時刻と直後の hand_id 境界を結ぶ ``bootstrap_meta`` field
      (例: ``"blinds_updated_before_hand_id": N``)
    - optional な operator action log (GUI 操作の構造化トレース、JSONL append-only)
  これにより blind level 変更時点を後から特定でき、reconstruction の audit
  容易性が上がる。

**音声起源の blind 推定は意図的に **しない**** (= 仕様):
- blind level 変更は **GUI 操作起点のみ** を canonical input とする
- 音声は基本的に騒音が多くて blind 額の信頼できる source ではないため、
  raw_text からの「ブラインド XX YY」のような自然言語パースは Phase 5-C では
  入れない (将来の dealer-callout 認識まで保留)

**Phase 5-D 完了済み (blind mismatch advisory)**:
- ✅ ``HandReconstructor._apply_blind_mismatch_advisory(result, online_summary)``
  を新設。``reconstruct_from_events`` の末尾 (patch_proposal 計算後) で呼ばれ、
  検出したパターンに応じて ``result`` を mutate する (online JSON / PHH /
  settlement には触らない)。
- ✅ **Pattern (A) propagation health check**:
  ``self._blinds_updated_at_runtime is True`` (= ``update_blinds`` が呼ばれた
  session) なのに ``bootstrap_meta["blind_source"] == "session_default"`` のまま
  → ``"blinds_session_default_after_update"`` を issue として記録。Phase 5-C
  の配線が正しく動いていれば発生しない defensive consistency check。
- ✅ **Pattern (B) amount mismatch**:
  ``online_summary.blinds.sb/bb`` と ``bootstrap_meta.sb_amount/bb_amount`` が
  ズレている → ``"blind_amount_mismatch"`` を issue として記録 +
  ``FieldPatch(field="blinds", online={...}, offline={...},
  note="blind amounts differ ...")`` を patch_proposal に追加。典型例: blind
  level 変更後に過去 hand を replay すると過去 online は旧 blind、reconstructor
  は新 blind を使うので mismatch する。
- ✅ **advisory への反映** (= ``result`` mutate):
  - ``needs_review = True``
  - ``reason``: 既存が ``"reconstructed_no_diff"`` / ``"reconstructed"`` のとき
    のみ ``"reconstructed_with_blind_mismatch"`` に昇格。``"reconstructed_with_diff"``
    の場合はそのまま (settlement diff が canonical signal なので)
  - ``patch_proposal``: 既存 proposal があれば blind FieldPatch を append +
    ``summary_note`` に ``"blind mismatch: ..."`` を追記。proposal が無い純粋
    blind-only ケースでは blind FieldPatch (Pattern B 時) + summary_note のみの
    proposal を新規作成
  - ``can_patch_automatically=False`` は維持 (Phase 5-A 約束、apply はしない)

**Phase 5-D スコープ外 / 注意事項**:
- **patch_proposal の自動 apply は依然しない**: blind mismatch を検出しても
  ``can_patch_automatically=False`` のまま、operator がレビューする前提
- **blinds は ``PATCHABLE_FIELDS`` に追加しない**: Phase 5-A の対象 field 集合は
  settlement 系 (resolution_type / seat_payouts / winner_seat / pot_total /
  showdown_revealed_cards) に絞っており、blinds は ``_compute_diff`` の対象に
  もなっていない。Phase 5-D の blind FieldPatch は **advisory 専用の特例追加**
  であって、``compute_patch_proposal`` の責務拡張ではない (= 既存 helper の
  scope を変えていない)
- **音声からの blind 値推定はしない** (Phase 5-C と同じ仕様継続)

**Phase 5-E 完了済み (GUI への blind 表示)**:
- ✅ ``gui/dashboard.py`` に 4 つの read-only helper を追加
  (``_blind_source_text`` / ``_has_blind_patch`` /
  ``_format_blind_for_advisory`` / ``_format_blind_suffix_for_history``)。
  ``summary.blinds`` / ``bootstrap_meta.blind_source`` / ``patch_proposal``
  から表示用の文字列を組み立てるだけで、reconstruct ロジックには触らない
- ✅ **Latest advisory ラベル**: ``bootstrap=...`` と ``diff=...`` の間に
  ``blinds=SB/BB (source=current_state|session_default|unknown)`` を **常時表示**
  (degrade 時は ``?/?`` / ``unknown``)、``blind_mismatch=yes`` を Phase 5-D の
  blind FieldPatch がぶら下がる hand のみに表示
- ✅ **history 1 行 suffix**: ``blind_source="current_state"`` の hand のときだけ
  ``blinds=SB/BB (current_state)`` を末尾に付ける (``session_default`` はキャッシュ
  ゲームの定常状態なのでノイズ削減のため省略)。blind FieldPatch ありの hand は
  ``(blind_mismatch)`` marker を末尾に追記
- ✅ ``_has_blind_patch`` は dataclass / dict 両形の patch_proposal に対応
  (= live hook と JSONL ロードのどちらでも動く duck-typed 設計)
- ✅ 変更ファイルは ``gui/dashboard.py`` と ``tests/test_gui.py`` のみ。
  ``gui/reconstruction_badges.py`` / ``HandReconstructor`` /
  ``HandReconstructionResult`` / ``patch_proposal`` のロジックは無変更

**Phase 5-E スコープ外**:
- ``ReconstructionBadgeState`` に blind 関連フィールドを追加するのは見送り
  (= ``summarize_reconstruction`` の責務拡張を避け、dashboard 側の helper で
  ``summary`` / ``result`` を直接読む設計を採用)。将来 ``inspect_reconstruction``
  CLI でも同じ表示が欲しくなった場合は badges.py 側に移すか検討
- 色 / アイコン強調はしない (= テキストレベルの indicator のみ。``[REVIEW]``
  と同じ赤系 tag は既存ロジックで自動付与される)

**Phase 5-F 完了済み (advisory state の bounded retention)**:
- ✅ ``integration.engine.MAX_ADVISORY_HANDS = 500`` を module 定数として導入。
  hand_id 単調増加を前提に、両 dict の **union of hand_ids** が上限を超えたら
  **最古から** evict する設計 (``_evict_old_advisory_entries`` メソッド)
- ✅ 呼び出し点を 2 ヶ所に: ``_invoke_reconstructor_hook`` 末尾 (=
  ``_apply_boundaries`` 経由の非 audio_winner end 経路) + ``_finalize_hand`` 末尾
  (= ``_last_summary_by_hand_id`` だけ更新されて hook が走らない安全網)。
  両方とも ``MAX_ADVISORY_HANDS`` 未満では即 return するので冗長 call の cost は
  O(1)。実質 1 hand 1 回しか eviction が走らない
- ✅ **asymmetric dict** (= 片方の dict にしか entry が無い hand_id) も union 経由で
  正しく evict される。``pop(.., None)`` を使うので片方欠落でも安全
- ✅ evicted hand_id への ``get_last_summary`` / ``get_reconstruction_result`` は
  ``dict.get`` 経由で ``None`` を返し、GUI は Phase 4-C2 (= ``[SKIPPED]``) +
  Phase 5-E (= ``blinds=?/? (source=unknown)``) の degrade パスに自動で乗る
- ✅ GUI ``gui/dashboard.py`` に ``MAX_HISTORY_LINES = 1000`` を追加、
  ``_trim_history_lines`` を ``_apply_hand_finalized`` の ``insert`` 直後に呼ぶ。
  ``index("end-1c").split(".")[0]`` から行番号を読み、超過分を ``"1.0"`` ～
  ``f"{excess+1}.0"`` 範囲で削除。bad index / AttributeError 等は silent
  degrade (例外で GUI を巻き込まない)

**Phase 5-F スコープ外**:
- **``_completed_hands`` (hand window events) の eviction は対象外**:
  これは個別 hand あたりの payload が大きく (1 hand 数 KB〜数十 KB)、advisory
  dict の sentinel-sized entries とは memory 圧の質が違う。``logs/evidence_<session>.jsonl``
  に raw observations が既に永続化されているので、必要なら canonical からの
  再構成は可能。``_completed_hands`` 用の eviction policy は Phase 6+ の課題 (= LRU
  with ``MAX_COMPLETED_HANDS`` 等)
- **``MAX_ADVISORY_HANDS`` / ``MAX_HISTORY_LINES`` の config 化は将来 TODO**:
  現状は module 定数。``config.json`` から読み込めるようにする余地は将来の
  Phase で対応 (= 短時間 dev test では monkeypatch で十分、production では 500 /
  1000 が妥当な default)

**Phase 5-G 完了済み (GUI からの手動 patch apply、in-memory のみ)**:
- ✅ ``core/patch_apply.py`` を新設。``apply_patch_proposal_to_summary(summary,
  proposal)`` が whitelist field のみ deepcopy 後に上書きした **新 HandSummary**
  を返す pure helper (= 元の summary を mutate しない)。``PATCH_APPLY_FIELDS``:
  ``resolution_type`` / ``seat_payouts`` / ``pots`` / ``showdown_revealed_cards``
  / ``blinds`` の 5 field。dict 形 / dataclass 形どちらの proposal にも対応
- ✅ ``HandReconstructionResult`` に ``patch_applied: bool = False`` と
  ``applied_fields: list[str]`` を default 付きで追加 (非破壊)
- ✅ ``IntegrationThread.apply_patch_proposal(hand_id) → bool``: in-memory
  ``_last_summary_by_hand_id[hand_id]`` を patched copy で置き換え、
  ``result.patch_applied=True`` / ``applied_fields=[...]`` を立てる。
  proposal 無 / 適用可能 field 無 / 対象 hand 無 → False。``GameStateManager``
  / ``JsonWriter`` / ``PHHExporter`` / live ``BettingState`` には触らない
- ✅ GUI (``gui/dashboard.py``): history frame の Latest advisory 右側に
  "Apply patch" ボタン (`_btn_apply_patch`、row=2 column=1) を追加。
  ``_cmd_apply_patch`` ハンドラが ``_ask_apply_patch_confirmation`` (testable hook)
  で確認ダイアログを出し、OK なら ``IntegrationThread.apply_patch_proposal`` を呼ぶ
- ✅ apply 成功時: Latest advisory ラベルに ``patch_applied=yes`` と
  ``applied_fields=...`` を追加表示。``_apply_hand_finalized(hand_id,
  append_to_history=False)`` で history 行を重複させず label のみ refresh
- ✅ ``_ask_apply_patch_confirmation`` は ``Optional[bool]`` を返す:
  ``True`` (OK) / ``False`` (cancel) / ``None`` (dialog 表示不可)。
  None の場合は safe abort (= apply しない)
- ✅ ``_latest_advisory_hand_id`` で最新 advisory hand_id を追跡し、Apply ボタンが
  正しい hand を指す

**Phase 5-G スコープ外**:
- **永続化はしない**: apply 結果は in-memory ``_last_summary_by_hand_id`` のみ。
  ``logs/<session>.json`` (online JSON) / PHH ファイルは書き換えない。
  Phase 6+ で「patched summary を別 file (例: ``patched_<session>.json``) に
  落とす」/「review log として append-only に記録する」等の永続化レイヤを
  検討する想定
- **``winner_seat`` / ``pot_total`` / ``actions`` は whitelist 外**:
  - ``winner_seat`` は ``seat_payouts`` の primary winner と整合性を取る
    derived field。直接 apply すると ``seat_payouts`` と矛盾するリスク
  - ``pot_total`` は ``seat_payouts.values()`` / ``pots[].amount`` の集計値。
    同上の理由で whitelist 外
  - ``actions`` は重い修正で、``pot_after`` / ``stack_after`` の連鎖再計算など
    副作用が大きいため別フェーズ
- **GameStateManager の live stacks は再計算しない**: apply 後の summary は
  「修正済みの記録」だが、GameStateManager の stacks (= 次 hand 開始時の
  buy-in 計算源) には反映されない。これは「過去 hand を retroactive に直すと
  以降の hand の stacks がズレる」のを避けるための意図的な scope-out

**Phase 5-B+ (Phase 5-B 直後の改善、同フェーズ扱い)**:
- ✅ **Audio seat hint の出所カテゴリ化**: ``signals.audio_seat_hint_sources``
  という dict (``action`` / ``winner`` / ``other``) を meta に追加。winner /
  other は active_seats union には寄与するが confidence boost の対象外、という
  Phase 5-B+ の整理を明示。``signals.audio_seat_hints`` (flat list) は backwards
  compat のためそのまま維持
- ✅ **staged operational `confidence`**: 固定 0.5 をやめ、
  ``_compute_raw_bootstrap_confidence`` で signal 強度に応じて
  ``0.4 / 0.5 / 0.6 / 0.7`` の 4 段階に分けた。baseline 0.4 + 3 種 boost
  (RFID >= 3 / action audio が RFID と overlap / button_inferred_from_prev)。
  依然 calibrated probability ではなく、operational な signal-stacker の枠は
  維持 (= モデル posterior 化は Phase 5-C+ の課題)

**Phase 5-C 以降スコープ外 (Phase 6 以降の候補)**:
- 差分検出時の **自動 patch apply** (現状は ``can_patch_automatically=False`` の
  proposal だけ、apply ロジック未実装)
- raw bootstrap の更なる強化: camera dependency、SB_POST/BB_POST の音声明示認識
- blind 変更の **audit trail** (Phase 5-C 注意事項にも記載):
  「いつ誰が blind を変えたか」を ``bootstrap_meta`` や advisory log に残す。
  具体的には blind 変更時刻と直後の hand_id 境界、optional な operator action log
  (GUI 操作の構造化トレース)。現状は ``blind_source`` の edge (= session_default
  → current_state 遷移) を後から探す形でしか追えない
- 確率モデル拡張: prior の hand-specific 調整 (例えば過去 N hand の MAP 平均で
  smoothing)、`confidence` を operational metric から **モデル事後確率** (top-1 vs
  top-2 log 差 / エントロピー / Brier score) に置き換え。``bootstrap_meta.confidence``
  は Phase 5-B+ で staged [0.4, 0.7] になったが calibrated probability ではない
- 完全 replay 型 ActionRecord (pot_after / stack_after を再構成時の bs から正確に算出)
- ``GameStateManager`` / ``HandReconstructor`` を真の projection (= 参照時に
  IntegrationThread を pull する) にリファクタ。現状 Phase 5-C は 3 ヶ所同時更新で
  整合性を取っているが、長期的には canonical 1 + projection 0 にしたい
- `_last_reconstruction_by_hand_id` の eviction policy (長時間セッションで増え続ける場合の対策)

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
