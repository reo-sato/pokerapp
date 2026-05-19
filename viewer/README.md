# Poker Hand Viewer (web)

`logs/*.json` を読むだけのブラウザ閲覧 UI。素の HTML/CSS/JS。

## 起動

リポジトリ直下で静的サーバを立てる:

```bash
python -m http.server 8765
# → http://localhost:8765/viewer/  を開く
```

`file://` 直開きだと CORS で `../logs/index.json` の fetch がブロックされるので
必ず HTTP 経由で開く。

## ルート

| URL | 画面 |
|---|---|
| `#/sessions` | 全セッション一覧 (画面 00) |
| `#/sessions/:sid` | セッション内ハンド一覧 (画面 01) |
| `#/sessions/:sid/hands/:hid` | ハンド詳細・ストリート列 (画面 02) |

## データソース

- `logs/index.json` — 各セッションの aggregate (hands / reviews / biggest_pot / started_at / ended_at / blinds)
  - `output/json_writer.py:JsonWriter.append_hand_summary` がハンド完了ごとに自動で再生成する
- `logs/<session_id>.json` — `HandSummary` の配列をそのまま読む

## キーボード

| キー | 動作 |
|---|---|
| `←` `→` | ハンド詳細で前後のハンドへ |
| `Esc` | 1 階層戻る |

## Hero seat の指定

ハンド詳細での "YOU" バッジ表示・セッションの net/VPIP/PFR 計算は、
セッション画面の "HERO SEAT" 行から seat を選ぶと有効化される
(`localStorage` に `viewer.heroSeat` として保存)。
ログ自体には hero 情報が無いので明示指定が必要。
