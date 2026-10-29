# 店舗のテスト記録

店舗でのテストのログを、ほかのセッションからも使えるように置いている（オーナー 2026-10-28）。新しいログを受け取ったら
`tools/store_data.py add` で足す（店舗ログのメモは CLAUDE.md の「店舗ログのメモ」のとおり毎回すべて読む）。

## 置き場所

- **文字のログ**: `data/store/<YYYY-MM-DD>/`（開発のブランチ）。
  - `pokerlogs_<日時>_text.zip`: pack_logs の zip から音声（`*.wav`）を除いたもの。
    - 中身は記録・events・書き起こし・聞き流した発話・卓状態・真のアクション（店舗で入れたもの）・pokerapp.log・config（トークンは伏せ済み）。
  - `pokertruth_<日時>.zip`: 家で入れた真のアクション（`tools/home_truth.py`）。複数の版を受け取ったら最新。
  - `listen_<sid 8 桁>.json`: 聞き取りの印（`tools/listen_missed.py` のページで付けたもの）。
  - `pokerlogs_<日時>_audio.json`: 音声の zip の名前と置き場所。
- **発話の音声**: データ専用のブランチ `store-data` の `audio/<YYYY-MM-DD>/`（元の分割 zip のまま）。
  - 開発のブランチとは履歴がつながっていない。clone しても取ってこない。
  - 公開（Artifact など）はしない。オーナーも元の zip を手元に保管する。

## 使い方

```bash
# 評価（真のアクションは後に並べた zip が上書きする）
TZ=Asia/Tokyo python tools/eval_store.py data/store/2026-10-26/pokerlogs_*_text.zip data/store/2026-10-26/pokertruth_*.zip

# 音声が要るとき（聞き取りのページ・Whisper の採点し直し）
python tools/store_data.py audio 2026-10-26 --out /tmp/audio_1026
TZ=Asia/Tokyo python tools/listen_missed.py /tmp/audio_1026/*.zip --truth data/store/2026-10-26/pokertruth_*.zip
python tools/listen_missed.py --read data/store/2026-10-26/listen_2bbd480e.json

# 新しいログを足す（音声は store-data にコミットしてから push）
python tools/store_data.py add pokerlogs_<日時>_*.zip --truth pokertruth_<日時>.zip --labels listen_<sid8>.json
python tools/store_data.py stash-audio pokerlogs_<日時>_*.zip && git push origin store-data
```

- 開発データ（推定器の物差し `tests/fixtures/store/`）に入れるかは、別に決める（`tools/eval_store.py --export-fixture`）。
- 入れていないセッションは標本外として使える。

## 一覧

<!-- index:start -->
| 日付 | セッション | ハンド | 真のアクション | メモ | 聞き取りの印 | 開発データ | ログの置き場所 |
|---|---|---|---|---|---|---|---|
| 2026-10-14 | 15cad555 | 20 | 20（店舗で入力） | 3 | — | 入れた | `2026-10-14/` |
| 2026-10-14 | 62cad44b | 20 | 20（店舗で入力） | 4 | — | 入れた | `2026-10-14/` |
| 2026-10-14 | 65c412b2 | 18 | 18（店舗で入力） | 1 | — | 入れた | `2026-10-14/` |
| 2026-10-14 | 845da8e6 | 20 | 20（店舗で入力） | 1 | — | 入れた | `2026-10-14/` |
| 2026-10-19 | 520177d4 | 1 | — | — | — | 入れていない | `2026-10-19/` |
| 2026-10-19 | a64ec4ad | 41 | 41（pokertruth_20261019_224945.zip） | 3 | — | 入れていない | `2026-10-19/`・`2026-10-26/` |
| 2026-10-19 | d01277f8 | 80 | 79（pokertruth_20261019_224945.zip） | 24 | — | 入れていない | `2026-10-19/` |
| 2026-10-26 | 143a86ff | 3 | 3（店舗で入力） | — | — | 入れていない | `2026-10-26/` |
| 2026-10-26 | 2bbd480e | 38 | 26（pokertruth_20261026_215250.zip） | 5 | listen_2bbd480e.json | 入れていない | `2026-10-26/` |
| 2026-10-26 | 58e694c8 | 5 | 2（店舗で入力） | — | — | 入れていない | `2026-10-26/` |
| 2026-10-29 | ce31c79e | 121 | 121（店舗で入力） | 5 | — | 入れていない | `2026-10-29/` |
<!-- index:end -->
