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
│   └── action_inference.py        ← BettingState, InferredAction, infer_action()
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
         seat = game_state.get_current_player()
         │
         ▼ infer_action(event, betting_state, seat)
         │   ├─ action="amount_only" → BettingState から BET/CALL/RAISE を推定
         │   └─ 明示アクション       → ゲームステート整合性検証 (check+opened=needs_review)
         │
         ▼ game_state.apply_action(seat, inferred.action, inferred.amount)
         │
         ▼ betting_state.update_after_action(...)
         │
         ▼ camera/RFID コリオブレーション (±2秒ウィンドウ)
         │
         ▼ calc_confidence(has_rfid, has_audio, has_camera)
         │
         ▼ ActionRecord → on_action コールバック + JSON 書き込み
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

- `BettingState`: ストリート単位のベッティング状態追跡
  - `current_bet`, `is_opened`, `player_contrib_this_street`
  - `reset_for_new_hand()`, `reset_for_new_street()`, `update_after_action()`
- `InferredAction`: 推定/検証結果 (`action`, `amount`, `confidence`, `needs_review`, `reason`)
- `infer_action(event, state, actor_seat) → InferredAction`: 中心 API

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
| PokerRuleEngine | ❌ 未実装 | legal_actions 算出なし |
| AudioStreamBuffer 状態機械 | ❌ 未実装 | 確認型発話の PENDING なし |
| ディーラーボタン管理 | ❌ 未実装 | 位置/ポジション算出なし |
