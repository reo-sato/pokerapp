# 2026-10-19 お客さんに真のアクションを見せる・ロガーの持ち点を真のアクションで直す

## Goal

オーナー: 今日、プレイヤーを入れた実践環境でテストする。プレイと並行して全部のハンドで真のアクションを入れるので、
(1) お客さんに真のアクションのハンド履歴を見せたい、(2) 次のアクションの推定に使う各プレイヤーの持ち点も真のアクションに
合わせたい。オーナーの答え: 真のアクションを保存したハンドだけ見せる / 今日のセッションも評価に数える（直しは記録に残す）。

## 変更

| ファイル | 内容 |
|---|---|
| `core/truth_hand.py`（新） | 真のアクションの行を pokerkit で流す共通の部分（入力画面の `replay_legal` から切り出した）+ `materialize_truth`（記録と同じ形: 行ごとの `pot_after`・`stack_after`、`players[]` の持ち点・収支、`pots`・`showdown`・`truth: true`）+ `truth_deltas` + `load_truth`（ファイルが変われば読み直す）+ `truth_view` |
| `tools/ground_truth_ui.py` | `replay_legal` は共通の部分を使う（動きは同じ）。保存したら `send_truth_stacks`（`ground_truth.feed_stacks` のとき、リクエストごとに config を読む） |
| `api/read_models.py` / `api/server.py` | お客さんの読み取り（`list_player_hands`・`get_hand`）に `truth`。`create_app(show_truth=)`、`auth_kwargs_from_config` が `viewer_api.show_truth` を読む |
| `core/control_queue.py` | 制御 `truth_stacks`（`{hand_id, results}` → raw_text `hand=N results=席:収支,…`） |
| `integration/engine.py` | `_handle_truth_stacks`: 席ごとに「真のアクションの収支 − 記録の収支」を持ち点に足す。ハンドが閉じる前に届けば閉じたときに、別のハンドの途中なら `_stack_corrections`（ハンドのあと）に。同じハンドの保存し直しは前に足した分との差だけ |
| `main.py` | `--cli` も `ground_truth.feed_stacks` のとき制御を受け取る（前は `--script` だけ） |
| `config_default.json` | `viewer_api.show_truth` / `ground_truth.feed_stacks`（どちらも既定 false） |

## 決めたこと

- ショーダウンの勝者: 見せた手札とボードで pot ごとに判定（`award_with_unknown_cards`）。判定が真のアクションの勝者と違う・
  判定できないときは真のアクションの勝者に全部。どちらも無ければ「終わっていない」= 見せない・送らない。
- ボードは真のアクションに無ければ記録のボード（入力画面が記録の札で自動に入れる）。
- 差は「収支の差」なので、前のハンドの持ち点が違っていても・ハンドの間に買い足しがあっても足し算で合う。
- お客さんの画面（`mobile/`）は変えていない（真のアクションのハンドも記録と同じ形 = ビルド不要）。

## 確かめ

- `tests/test_truth_hand.py`（16）: 全員フォールド / 手札で判定 / マックは強くても負け / サイドポット / 入れた勝者 / 未完 /
  画面に真のアクションだけ / ファイルの読み直し / viewer API（on・off）/ 制御の往復 / ロガー: 次のハンドが真のアクションから・
  閉じる前の保存・保存し直しは差だけ・別のハンドの途中は終わったあと / 入力画面の保存で制御が書かれる。
- 開発データ 185 ハンドの真のアクション: 記録と同じ形にできた 170（できない 15 = 評価から外したハンド・手札で勝者が決まらず
  勝者も入っていないハンド）、収支の合計はどれも 0、記録の収支と違うハンド 62。
- 速い組 3307 passed、ruff。開発データの記録の再生は変わらない（`score_live.py`）。
- 手元で入力画面のサーバーに PUT → `logs/<sid>.control.jsonl` に `truth_stacks`。

## 店舗 PC で使うとき

`session_layer.enabled`・`viewer_api.show_truth`・`ground_truth.feed_stacks` を true にして、ロガー・入力画面・お客さんの
画面（店舗サーバーか `start_viewer.cmd`）を起動し直す。

## 追記: お客さんの画面に今日の分だけを出す（同日, オーナー）

`viewer_api.today_only`（`create_app(today_only=, clock=)`）: `/api/players` は今日始まったセッションで席に着いた人だけ
（`recent_players`）、`/api/players/{id}/sessions` は今日始まったセッションだけ、前の日のセッションのハンドの一覧は空。
「今日」は営業日 = 朝 6 時から（`business_day_start`, 深夜 0 時をまたぐセッションを途中で消さない）。スタッフの読み取り
（`/api/staff/...`）は変えない。テスト `tests/test_viewer_today_only.py`。

## 追記: スタックを減らすコマンド（同日, オーナー）

CLI の `d <席> <金額>` → `AudioEvent(action="reduce_stack")` → engine `_handle_reduce_stack`: ハンドの外ならすぐ
`update_stack`（0 より下にはしない）、その席が進行中のハンドに入っていれば `_stack_corrections` でハンドのあとに。
テスト `tests/test_reduce_stack.py`。

## 追記: BB アンティ（同日, オーナー）

オーナーの答え: BB の人だけが払う（BB と同額）、コマンドは `ante on` / `ante off`。

- `core/poker_engine.py`: `set_bb_ante(on)`（ハンドの途中なら次のハンドから、`_KEEP_ON_RESTORE` に保留の値）、`bb_ante`
  （オンなら BB の額）。`create_state` に `ante_trimming_status=False, raw_antes={1: bb}`（True のままだとアンティが返金されて
  消える。オフは `raw_antes=0` で今と同じ）。heads-up でも BB の人に付く。legacy は受けるだけ（アンティを置かない）。
- `integration/engine.py`: アクション `set_bb_ante`（`raw_text` = on/off）→ `_handle_set_bb_ante` のお知らせ、
  `_CONTROL_KEEPS_CORRECTION` に追加。記録の `blinds` にオンのときだけ `bb_ante`（hand schema の blinds に optional）。
- 表示: `core/hand_log.py:street_flow` / `shared/hand_replay/handReplayModel.ts:streetFlow` は BB の人のプリフロップの始まりから
  アンティを引く（「出した合計」にアンティを入れない）。中央のブラインドに「(BBA X)」、テキスト・入力画面に「BB アンティ X」。
- 再生: `core/truth_hand.py:start_state`・`integration/world_replay.py:replay_world(bb_ante=)`（推定器はハンドの記録の
  `blinds.bb_ante` を渡す = 切り替えの event が前の窓にあっても合う）。
- **短いスタックの BB**: pokerkit はアンティをブラインドより先に取る（TDA はブラインドが先）。持ち点が BB + アンティに足りない
  ときの配り方は TDA と違いうる。今回は手を入れていない。
- 確かめ: `tests/test_bb_ante.py`（7）・TS の `streetFlow` のテスト、速い組 3322 passed、ruff、staff E2E 8、開発データの
  ライブの再生は変わらない（`score_live.py`）、推定器の物差し（アンティなし = 結果は前と同じ、内容の指紋は変わる = estimator.md §6）。
