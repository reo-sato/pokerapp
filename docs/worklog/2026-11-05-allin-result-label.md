# 2026-11-05 ハンド履歴: オールインのハンドの収支を「all-in」で出す

## ゴール

オーナーの依頼。持ち点を数えないモードでオールインになったハンドで、プレイヤーのハンド履歴のチップ増減の欄が
「null」になっていた。これを「all-in」と出す。

オーナーが決めたこと:

- 勝った人は緑(いまのプラスと同じ)、負けた人は赤。
- オールインの前に降りた人は、出した額で出す。

## 変えたファイル

- `core/stackless.py`
  - `allin_seats` を足した。関わった席 = オールインした席・最初のオールインのあとでチップを出した席・勝った席。
  - `mark_allin` を足した。関わった席に `allin` = won / lost / unknown を付ける。冪等。
  - `strip_hand` が全員の `result` を消していたのを、関わった席だけにした。
- `api/read_models.py:_load_hand_log`: 印の無い古い記録にも、読むときに印を付ける。
- `shared/hand_replay/handReplayModel.ts`
  - `resultLabel` を足した。数 → 符号付き、all-in → 勝ち緑・負け赤・分からない色なし。
  - `ReplayPlayer.allin` を足した。
- `shared/hand_replay/HandReplay.tsx`(席)・`handReplayText.ts`(共有の文)で `resultLabel` を使う。
- `mobile/src/screens/common.tsx` に `handResult` を足し、`MyHandsScreen`(一覧)・`HandDetailScreen`(見出し)で使う。
- `mobile/src/api/types.ts`: `result: number | null`、`allin` を足した。
- `docs/contracts/schemas/hand.schema.json`: 1.12(`players[].allin`)。
- ビルド: `api/static/player/`・`cloud/static/player/`・`api/static/staff/`(`scripts/sync_shared_ui.py` のあと)。

## 試験

- 速い組: 3610 passed / 22 skipped。遅い組: 16 passed / 5 skipped。ruff OK。
- mobile(62 passed)・staff(66 passed)・tsc OK。
- 新しい試験:
  - `tests/test_stackless.py`: 関わった席の印、降りた席の収支が残る、冪等、勝者が決まらない。
  - `handReplayModel.test.ts`: `resultLabel` の 6 通り。
- 10/29 の記録で読み取りを確かめた。

  | ハンド | 結果 |
  |---|---|
  | 121(真のアクション) | 席5 won・席7 lost |
  | 65(真のアクション) | 席1 won、席2・5・7 lost、オールインの前に降りた席4 は -800 |

## 残り

- 画面の見た目はブラウザで確かめていない。型と試験だけ。店舗のスマホで、オールインのハンドを 1 つ見てもらう。
