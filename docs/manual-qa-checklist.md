# 手動 QA チェックリスト（実機なしローカル検証）

RFID 実機（ESP32 + PN532）や Windows ビルド環境が無くても、開発機（mic 付きノート PC 想定）で
v1 の品質を確認するための手順。各項目に **コマンド / 期待結果 / 見るべき点** を記す。

- v1 の **必須経路は「音声のみ」**。RFID は HTTP POST 受信方式なので、付属の `tools/simulate_rfid.py`
  で実機なしに再現できる。
- ハードウェアや OS が要る項目は 🖥️/🎤、不要な項目は 💻 で示す。
- インストール詳細は [`installation.md`](installation.md)、読み上げ語彙・設定は [`usage.md`](usage.md)、
  困りごとは [`troubleshooting.md`](troubleshooting.md) を参照。

---

## 1. インストール検証 💻

```bash
pip install -r requirements.txt          # core runtime（または pip install -e .）
pip install -r requirements-dev.txt      # テスト依存（CI と同じ）
python -c "import core, audio, integration, output, gui, main; print('import OK')"
```

- **期待**: いずれもエラーなく完了し `import OK`。
- **見る点**: `gui`（customtkinter）/ `pokerkit` の import 失敗がないこと。OS 別の PortAudio 準備は
  [`installation.md`](installation.md) を参照（音声 E2E（項目 5）でのみ必要）。

## 2. 自動テストスイート 💻

```bash
pytest tests/ -v --ignore=tests/test_vision.py
```

- **期待**: **311 passed, 0 skipped**（CI と同等。`vision/` はレガシー除外）。
- **見る点**: skip が出る場合は依存欠落（`pokerkit` / `jsonschema` / `numpy`）。`requirements-dev.txt` を入れ直す。

## 3. 決定的 replay（記録済みハンドの再構築）💻

```bash
python tools/replay_hand.py tests/fixtures/reconstruction/check-facing-bet
python tools/replay_hand.py tests/fixtures/reconstruction/silent-fold
python tools/replay_hand.py tests/fixtures/reconstruction/unequal-allin
```

- **期待**: 各 golden ケースの `HandSummary` が JSON 配列で表示される。
- **見る点**: `check-facing-bet` は heard "check" が `call`＋`needs_review:true` に射影される。`silent-fold`
  は合成 fold が入る。`unequal-allin` は `pots`（main/side）が出る。5 ケース
  （`check-facing-bet` / `call-amount-from-state` / `silent-fold` / `out-of-turn-rfid` / `unequal-allin`）。

## 4. テキスト駆動 再構築（mic なしで全パイプライン）💻

マイクや Whisper モデルなしで、parse → rules-aware エンジン → JSON まで丸ごと試す。

```bash
printf 'ハンド開始\nチェック\nシート1 ウィナー\n' \
    | python tools/play_hand_text.py - --seats 3 --out-dir ./logs --session-id qa_text
```

ファイルからも実行できる（ディーラーの読み上げを 1 行ずつ）:

```bash
cat > /tmp/hand.txt <<'EOF'
ハンド開始
シート1 レイズ 600
シート2 コール
ショーダウン
シート1 ウィナー
EOF
python tools/play_hand_text.py /tmp/hand.txt --seats 2 --out-dir ./logs --session-id qa_text2
```

- **期待**: `HandSummary` JSON が標準出力に表示され、`logs/<session>.json` が書かれる。
- **見る点**: 1 例目（3-handed で BB 直面の「チェック」）は `actions[].action == "call"`・
  `needs_review:true`・`review_required:true`・`confidence` が派生値になる（合法手射影）。
  語彙は [`usage.md`](usage.md)（`# 始まりの行と空行は無視、認識不能行は警告してスキップ`）。
  backend は既定 `pokerkit`。`--backend legacy` で素朴 round-robin と挙動比較できる。

## 5. 音声のみ ライブ E2E（mic、RFID なし）🎤

v1 の必須経路。実マイクで 1 ハンドを声に出して記録する。

```bash
python main.py --cli
```

- **期待**: 起動後、読み上げ（例:「ハンド開始」「シート1 レイズ 600」…「シート1 ウィナー」）が
  認識され、終了時に `logs/<session>.json` が生成される。
- **見る点**: マイクデバイス選択（`config.json` の `audio.device_id`）、認識テキストのログ、
  ハンド確定時の JSON 追記。精度・語彙・設定は [`usage.md`](usage.md)、不調時は [`troubleshooting.md`](troubleshooting.md)。

## 6. RFID-HTTP 模擬（実機なしで RFID 経路）💻

`tools/simulate_rfid.py` で席カード/ボードカードのタッチを POST し、ストリート自動進行と
corroboration（複数センサー合意）を確認する。**別ターミナルでアプリを起動**しておく。

事前準備（合成タグを登録し、`config.json` で RFID を有効化）:

```bash
python tools/simulate_rfid.py register-demo          # rfid_cards.json に合成デッキ 52 枚を登録
# config.json で "rfid": { "enabled": true, "transport": "http", "bind_port": 8787 } に
python main.py --cli                                 # アプリ起動（RFID 受信開始）
```

別ターミナルから注入:

```bash
python tools/simulate_rfid.py status                 # GET /status（events_received など）
python tools/simulate_rfid.py seat 1 As Ks           # 席 1 にホールカード
python tools/simulate_rfid.py board Ah Kd Qs          # フロップ（board_1..3）
python tools/simulate_rfid.py board Ah Kd Qs 2c       # ＋ターン（board_4）
python tools/simulate_rfid.py board Ah Kd Qs 2c 7h    # ＋リバー（board_5）
python tools/simulate_rfid.py send --reader seat_2 --tag 04AABBCC   # 自前タグの生送信
```

- **期待**: 各 POST が `HTTP 200 ok`。アプリ側ログにカード解決・ボード枚数によるストリート遷移が出る。
- **見る点**: board 3/4/5 枚で flop→turn→river が自動遷移。音声と近接した RFID は confidence を上げる。
  未登録タグ（`--tag` で未知 UID）は `card=""` で `needs_review`。`status` の `events_received` 増加。

## 7. PHH 出力 💻

```bash
python main.py --export-phh logs/qa_text.json
```

- **期待**: `logs/qa_text_phh/0001.phh` 等が生成され、出力先がコンソールに表示される。
- **見る点**: PHH 標準では check/call は同一トークン `cc`（仕様どおり）。区別は JSON ログ側で保持。

## 8. config トグル 💻

`config.json`（無ければ `config_default.json` をコピー）を編集して挙動を確認する。

| キー | 値 | 確認 |
|------|-----|------|
| `engine.backend` | `pokerkit` ↔ `legacy` | 同じ入力で項目 4 を実行し出力差（合法手射影/silent-fold の有無）を比較 |
| `recording.enabled` | `true` | ライブ実行後 `logs/<session>.events.jsonl` が生成され、項目 3 の replay に使える |
| `session_layer.enabled` | `true` | 現状 seat→player 選択 UI 未結線（core のみ）。単体で挙動は変わらない（ISSUE-0006） |

- **見る点**: `engine.backend` を `legacy` に戻せば rules-aware 機能を rollback できる（回帰確認）。

## 9. GUI / registry スモーク 🖥️

```bash
python main.py            # hand logger GUI（customtkinter）
python main.py --players  # Player Registry 画面（別画面、S1）
```

- **期待**: 各ウィンドウが起動する（GUI 環境が必要）。
- **見る点**: registry で player の新規作成 / 一覧 / リネーム、空文字・重複の validation 表示。

---

## ローカルでは完結しない項目（参考）

- **Windows ワンクリックインストーラ（PyInstaller）/ コード署名 / クリーン Windows 実機 E2E**:
  Windows 環境と証明書が必要（Phase H2/H3）。
- **seat → player_id 選択 GUI**: 対話 UX 決定が前提（ISSUE-0006、Phase 2.3）。
- **実マイクでの自動 E2E**: 項目 5 の手動確認に留める（音声入力の自動化は対象外）。
