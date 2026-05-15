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
│   └── hand_log.py                ← ActionRecord, HandSummary データクラス
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
│   ├── action_inference.py        ← BettingState, InferredAction, infer_action()
│   └── action_order.py            ← ディーラーボタン回転 / SB-BB / actor 順 helpers
│
├── output/
│   ├── json_writer.py             ← JsonWriter (セッション JSON ログ書き込み)
│   └── phh_exporter.py            ← PHHExporter (PHH 形式エクスポート)
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
    ▼ faster-whisper / Vosk
ASR テキスト (raw)
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
    ▼ AudioEvent { action, amount, timestamp, raw_text }
    │
    ▼ audio_queue
```

### 統合処理パイプライン

```
AudioEvent (audio_queue)
    │
    ▼ IntegrationThread._handle_audio_event()
    │
    ├─ action="new_hand"  → GameState.new_hand(), BettingState.reset_for_new_hand()
    ├─ action="showdown"  → GameState.advance_street(SHOWDOWN)
    ├─ action="winner"    → finalize_hand()
    │
    └─ それ以外:
         seat = betting_state.actor_seat  ← button/blind 確定時の actor を最優先
                ?? game_state.get_current_player()   (fallback)
         │
         ▼ infer_action(event, betting_state, seat)
         │   ├─ action="amount_only" → BettingState から BET/CALL/RAISE を推定
         │   └─ 明示アクション       → ゲームステート整合性検証 (check+opened=needs_review)
         │
         ▼ game_state.apply_action(seat, inferred.action, inferred.amount)
         │
         ▼ betting_state.update_after_action(...)  ← actor_seat も次へ進める
         │
         ▼ camera/RFID コリオブレーション (±2秒ウィンドウ)
         │
         ▼ calc_confidence(has_rfid, has_audio, has_camera)
         │
         ▼ ActionRecord → on_action コールバック + JSON 書き込み
```

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
- `infer_action(event, state, actor_seat) → InferredAction`: 中心 API

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
→ 243 passed  (test_vision.py は cv2 未インストールのため収集エラー、既知問題)
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
| PokerRuleEngine | ❌ 未実装 (v6.0) | legal_actions 算出なし |
| AudioStreamBuffer 状態機械 | ❌ 未実装 (v6.0) | 確認型発話の PENDING なし |
| 音声/RFID 確率融合エンジン | ❌ 未実装 (v6.0+) | §「将来計画」を参照 |
| ディーラー別オンライン学習 | ❌ 未実装 (v6.0+) | §「将来計画」を参照 |

---

## 将来計画 (v6.0+): 音声/RFID 時刻ベース確率融合

現状の `infer_action` は「単一の AudioEvent から 1 つの (action, amount) を確定し、矛盾なら `needs_review`」という二値判定。これを **「観測ごとに尤度ベクトルを作り、ポーカールール制約下で時刻整合性付きの最尤アクション列を MAP 推定する確率フレーム」** に拡張する。

### 設計の核

1. **観測の区間化**: `RFIDEvent` に `t_end` を追加し、`(t_start, t_end)` で「seat_n がカードを持っていた期間」を表現する。フォールドはこの区間の終端で発生したと解釈できる強観測になる。ASR 側も word-level timestamps と N-best 仮説を保持して `EvidenceInterval` に統一する。

2. **観測尤度のベクトル化**: 1 観測 → 各候補 action への尤度 dict (`{"CALL": 0.6, "RAISE": 0.3, ...}`) に分解。`infer_action` の戻り値を単一 `InferredAction` から「複数仮説リスト」に拡張する。

3. **Beam Search 推論**: K=64 程度の partial action sequence を時刻順に並走し、各到着観測で全粒子を更新。legal_actions はハード制約 (確率 0)、position prior はソフト制約 (log prior 加算)。

4. **音声/RFID 時刻アライメント**: ディーラー固有の遅延分布 `(μ_d, σ_d)` を学習し、確率窓 `N(τ - t_obs; μ_d, σ_d²)` で固定 ±2s ウィンドウを置換。

5. **ハンド終了時の後方修正**: WINNER 宣言・最終ポット額・残スタックは Oracle 級の強観測。粒子集合をこれらの制約で再フィルタし、MAP 列に collapse。残った曖昧粒子のみ `needs_review` で GUI へ。

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
