# Poker Hand Logger

ライブポーカーの**ハンド履歴を声で自動記録**するアプリです。ディーラーがアクションを読み上げると、
音声認識（faster-whisper）がそれを解釈し、ポーカーのルールに沿って整形して **JSON / PHH 形式**で
ハンドログを書き出します。RFID（PN5180 + ESP32-S3, USB CCID / PC/SC）を併用するとカードや席をさらに正確に記録できます。

- **対象**: 小規模クラブ・個人配信
- **必須経路は「音声のみ」**。RFID は任意（無くても動きます）。
- ルール準拠の再構築（手番推定・合法手への補正・未宣言フォールドの補完・サイドポット計算）が
  既定で有効です。

> ⚠️ 本アプリは現在 **v1 開発中**です。Windows ワンクリックインストーラは準備中で、現状は
> 「ソースから実行」する形になります（[インストール手順](docs/installation.md) 参照）。

---

## できること

- 🎙️ **声でハンド記録** — 「シート3 ベット 500」のように読み上げるだけ。
- ♠️ **ルール準拠の整形** — 手番の推定、非合法な発話の自動補正、ディーラーが言い忘れたフォールドの補完、
  オールイン時のメイン/サイドポット計算。
- 📝 **JSON / PHH 出力** — ハンドごとに `logs/` へ追記。PHH（Poker Hand History）への変換も可能。
- 🔎 **要確認フラグ** — 自信が低い・矛盾するアクションには `needs_review` が付き、後から見直せます。
- 🃏 **RFID 併用（任意）** — 席カード/ボードカードの読み取りで精度を補強。

---

## 動作要件

| 項目 | 要件 |
|------|------|
| OS | Windows（主対象）/ macOS / Linux |
| Python | 3.11 以上 |
| マイク | ディーラーの口元マイク推奨（USB 等） |
| RFID（任意） | PN5180 + ESP32-S3（USB CCID を PC/SC で読む, 第一系統）。HTTP 送信は補助（debug/remote） |

初回起動時に音声認識モデル（既定 `medium`）が自動ダウンロードされます（数百 MB・ネット接続が必要）。

---

## インストール（ソースから）

```bash
git clone <このリポジトリ>
cd pokerapp
pip install -r requirements.txt      # または: pip install .
```

- `pyaudio` のインストールには OS 側の PortAudio が必要な場合があります → [インストール手順](docs/installation.md)。
- RFID を PC/SC で使う場合のみ `pip install ".[pcsc]"`。

詳しくは **[インストール手順](docs/installation.md)** を参照してください。

---

## クイックスタート

```bash
python main.py            # GUI で起動（ハンドロガー）
python main.py --cli      # CLI で起動（音声のみ・画面なし）
```

起動したら、ディーラー口調で読み上げます（例）:

```
ハンド開始
シート3 レイズ 600
シート1 コール
シート2 フォールド
シート1 チェック
シート3 ベット 1000
シート1 コール
シート1 ウィナー
```

ハンドが終わると `logs/<セッションID>.json` にハンドログが追記されます。
PHH へ変換するには:

```bash
python main.py --export-phh logs/<セッションID>.json
```

読み上げ方の一覧・ワークフローは **[使い方ガイド](docs/usage.md)** を参照してください。

---

## ドキュメント

| ガイド | 内容 |
|--------|------|
| [インストール手順](docs/installation.md) | 依存関係、マイク設定、初回起動、RFID（任意） |
| [使い方ガイド](docs/usage.md) | 読み上げ語彙、ハンドの記録手順、出力（JSON / PHH） |
| [トラブルシューティング](docs/troubleshooting.md) | マイク・認識精度・RFID・モデルなどの困りごと |
| [設定リファレンス](docs/usage.md#設定-configjson) | `config.json` の各項目 |
| [手動 QA チェックリスト](docs/manual-qa-checklist.md) | 実機なしでのローカル検証手順（テスト / replay / テキスト駆動 / RFID 模擬 / PHH） |

---

## 出力形式

- **JSON**: `logs/<セッションID>.json`。ハンドごとに `actions` / `players` / `pots` / `winner_seat` /
  `review_required` などを含む（契約は `docs/contracts/schemas/hand.schema.json`）。
- **PHH**: `--export-phh` で pokerkit 互換の Poker Hand History に変換。

---

## プロジェクト状態

- v1 開発中。ルール準拠の再構築（pokerkit）が **既定 backend**（`config.json` の `engine.backend`）。
  問題があれば `"legacy"` に切り替えて従来動作に戻せます。
- 仕様・設計の詳細は `CLAUDE.md` および `docs/`（ADR / contracts）を参照。

## ライセンス

未定（プロジェクトオーナーが確定）。配布前にライセンスを明記してください。
