# テストのダイエット 監査 1（計画の監査, Fable 5.1, 2026-10-06, コミット d3e2276）

読み取り専用で監査した（リポジトリは変更していない。全体のテストは回さず、推定器の 4 ハンドの cProfile と数件の単発実行だけ）。

## 総評

- 方向は正しい。時間は 20 件（125 秒 = 56%）と後片付けの待ち（10 ファイルで約 57 秒）に集中しているので、段階 1 だけで半分近く縮む。
- ただし目標の数字は 3 つとも根拠が足りない。CI 90 秒以下は `test_store_hands` の 84 秒を抱えたまま 1 ジョブでは届かない。速い組 30 秒以下は 1 プロセスでは約 60 秒が下限。行 3 割減は、読み取りの群と古い段階の群（合わせて 8,922 行）を半分にしても 15% 止まり。
- 「そのファイルだけが通る行」は消す根拠に使えない（計画もそう書いている）。代わりに「入力と期待の組の重複」「変異」「分岐の重なり」を物差しにする。
- 段階 2 の前に、テストどうしの import（`_Table` など）を `tests/harness.py` へ出す。これをしないと日付のファイルを消した瞬間に他のファイルが壊れる。

## 所見

### D1. CI 90 秒以下は 1 ジョブでは達成できない — 必須
理由: GitHub Actions の直近 12 回は壁時計 182〜251 秒。10/04 の内訳は pip 7 秒・pytest 174 秒（pip のキャッシュは効かない）。`test_store_hands` 4 件で 84 秒、`9d1d8536-4` だけで 54 秒。後片付けを −40 秒しても 189 秒。
やること: 目標を「CI は 2 ジョブ（`-m "not slow"` と `-m slow`）、各ジョブの Run tests ステップ ≤ 90 秒」に直す。slow ジョブは `pytest-xdist -n 4` で 4 件を並べると壁時計 ≈ 55〜65 秒（ランナーのコア数は監査 2 で `nproc` を出して確かめる）。推定器を変えない限り 54 秒が床（D2）。数字は Actions API のステップ秒で測り、手元の数字と並記する（手元 229 秒 / CI 174 秒と差がある）。

### D2. `test_store_hands` の 54 秒の正体と、できること — 必須
理由（cProfile, 約 3 倍遅くなる計測）: `9d1d8536` ハンド 4 は再生 681 回・候補 229・直しの候補 81（insert 42 / read 24 / drop 12 / button 2 / lift 1）。ハンドの行は 11 なのに**窓が 685 秒・書き起こし 145 行**。セッションの最後のハンドなので `windows()` が窓の終わりを「最後のイベント + 60 秒」にし、プレイ後の約 11 分を候補ごとに再生している。再生時間の 86% は `advance` の空回り（`periodic` 177 万回、`_poll_departures` + `PresenceTimeline.snapshot` 233 万回。`TICK_SEC = 0.25` で 1 再生あたり約 2,700 回）。`fded6f75` ハンド 1（窓 95 秒）は 565 再生で 17 秒、空回りはそれでも約半分。
やること: (1) 段階 1 では `slow` の印だけ。`beam` / `depth` / `expand` を小さくして速くしてはいけない（本番の探索を固定する意味が消える）。(2) 速くするなら推定器の側: 最後のハンドの窓を `ended_at + 60 秒` で切る（`_departure_terms` と同じ考え方）、または `advance` で「何も変わり得ない区間」の空回りを飛ばす。どちらも `CONTENT_FILES` の指紋が変わる推定器の変更なので、ダイエットの中ではやらず別タスク・別コミットにし、`tools/bench_hands.py --twice` と `tools/compare_versions.py` で「前とすべて同じ」を確かめてから入れる。窓を切るだけで 54 秒 → 15 秒前後の見込み。(3) 4 件は物差し（`bench_hands`）と重なるが、ボタン + 言い直し（9d1d8536-4）と札の離脱（fded6f75-1）の筋を固定する唯一のテストなので消さない。

### D3. 速い組 30 秒は xdist が前提 — 必須
理由: 1 秒未満のテスト 2,843 件で 99 秒。うち後片付けの重い 10 ファイル（`test_tools_ground_truth_ui` 22.7 / `test_rfid_http` 10.6 / `test_cli_session_layer` 6.2 / `test_rfid_relay` / `test_phase7` / `test_tools_simulate_rfid` / `test_viewer_api` / `test_read_corpus` / `test_integration` / `test_main_audio_optional`）が 56.5 秒。これを −45 秒しても 1 プロセスで約 55 秒 + 収集 3 秒。
やること: `pytest-xdist` を `requirements-dev.txt` に足し、速い組は `pytest -m "not slow" -n auto`（この箱は 4 コア → 20〜25 秒の見込み）。目標は「1 プロセス ≤ 60 秒・`-n auto` ≤ 30 秒」の 2 本立てにし、両方を記録する。並列の前提は確かめた: ポートは `port 0` / `_free_port()`、ファイルは `tmp_path`、リポジトリ直下の `players.json` は `data_dir` fixture が逃がしている（全体を回したあとの `git status` もきれい）。信用する前に `pytest-randomly` で順序を変えて 3 回回す（D14）。

### D4. 後片付けの直し方（具体） — 必須
理由: `rfid/http_receiver.py:118` の `serve_forever(poll_interval=0.5)` は固定値。`shutdown()` は見回りを待つので 1 件あたり最大 0.5 秒（`test_rfid_http` 実測 0.46 秒 / 件、中身の仕事は 0.02 秒）。`tests/test_tools_ground_truth_ui.py` の `_serve` は `server.serve_forever` を既定（0.5）で呼ぶ。`tests/test_cli_session_layer.py` の `fake_input` はコマンドごとに `time.sleep(0.15)` + 見回り。`tests/test_phase7.py` / `test_integration.py` / `test_rfid.py` は `time.sleep(0.3)` や `0.15` のあと `join(timeout=2)`（テスト全体で `time.sleep` 33 か所）。
やること: 製品側は `RFIDHTTPReceiver.POLL_INTERVAL = 0.5` のクラス属性にして（挙動不変）テストで 0.02 に。`tools/rfid_relay.py:116` も同じ。`_serve` は `serve_forever(poll_interval=0.02)`（module scope にしない: `make_server` が `log_dir` を抱えるので function scope のままでよい）。`sleep` は「処理された数」（`on_action` / `on_notice` / queue が空）を待つ形に。`join` のあとに `assert not thread.is_alive()`（いまは止まらないスレッドが静かに通る）。見込み −40 秒は妥当。

### D5. 「そのファイルだけが通る行」が誤解を招く場所と、代わりの物差し — 必須
理由: `test_store_2026_09_29`（0 行）は `_in_replay_order` の順序・配った直後のフォールドの再生（d0f055fb）・`ALLIN_RESTATE_SEC` 内の言い直し・「2千500」を固定している。`test_contracts`（0 行）の価値は code↔schema の突き合わせと invalid fixture が落ちること。`test_garbled_amount` は 65 件で 4 行だがオーナー 10/01 の決定。`test_phonetic` は 75 件で 1 行（音の表）。さらに parametrize の各ケースは coverage では 1 つの文脈なので、表にすると「1 テストだけが通る行」はいっそう減る（段階 2 のあとは数字が自然に悪く見える）。逆に `main.py` 48.9%・`gui/seat_selection.py` 24.8%・`tools/bench_hands.py` 35.8% は足りない側で、重なりの話ではない。
やること: 消す判断は (1) **入力と期待の組**の重複（「チェックアラウンド」41 回 / 12 ファイル、「ヘッズアップ」41 / 10、「ご視聴ありがとうございました」21 / 10、「六百」73 / 8。組が同じなら 1 行に、違えば残す）、(2) `coverage run --branch` で `overlap.py` を取り直した**分岐（arc）の重なり**（同じ行でも違う枝を通る読み取りを見分けられる）、(3) まとめた話題の**変異**（D10）で行う。行の重なりは「見直す順番」にだけ使う。

### D6. 先にテストどうしの import をほどく — 必須
理由: `_Table`（`tests/test_rfid_folds.py:44`）を 13 ファイルが import。`tests/test_amount_space.py` は 5 つのテストファイル（`test_rfid_folds` / `test_store_2026_09_27` / `_09_29` / `_10_01` / `test_estimator`）から `STORE_HOLES` / `_acts` / `_replayed_open` / `_rebuilt` などを借りている。`test_tools_second_ear` は `test_tools_rescore_audio` の `FakeWhisper`。`_make_game()` の複製 3 つ、`_Table` 系の複製が 10 ファイル（`tests/test_spoken_amounts.py` にも別の `_Table`）。
やること: 段階 2 の 0 番目に `tests/harness.py`（`Table` / `board` / `to_flop` / `STORE_HOLES` / `make_game`）と `tests/fakes.py`（`FakeWhisper` 等）を作り、全部をそこへ向ける。`from tests.test_` を禁じる小さなテスト（D13）を同時に入れる。

### D7. 行 −30% は守りを削らないと届かない — 必須
理由: 読み取り・場面の群 28 ファイル 6,314 行、古い段階の群 20 ファイル 2,608 行。両方を半分にしても −4.5k = −15%。大きい 14 ファイル（9,270 行: `test_rfid_table_flow` 1,222・`test_rfid` 1,067・`test_tools_ground_truth_ui` 905 …）は道具と RFID のテストで、そのファイルだけが通る行が多い（215〜449）。
やること: 必須 −15%（26k 行以下）、−30% は任意。主の指標は時間と形にする: ファイル 134 → 90 以下・日付と段階の名前のファイル 0・同じ（入力, 期待）の組は 1 か所・`from tests.test_` 0。

### D8. 読みの表の形 — 推奨
やること: 話題ごとに 1 ファイル（`tests/readings/check_around.py` / `amounts.py` / `action_words.py` / `announcements.py` / `positions.py` / `hand_names.py` / `multi_action.py` / `questions.py`）。冒頭 docstring = いまの規則（現在形）。行は

```python
R("チェッカーランド", ["check_around"], flags={"fuzzy_keyword"}, src="読み上げ集 2 回目 2026-09-30 Kei")
R("チェック、ハンド", ["check_around"], src="台本 2026-10-01 / オーナー 2026-10-06「終わりの ンド はアラウンド」")
R("チェック、ターンカード", ["check"], src="規則 2: ストリートの言葉は around でない")
R("ゴールド", [], src="一般語は読まない")
```

期待は `tools/read_corpus.py:event_key` の文字列（`"raise 2000"` / `"check_around"` / `"players_left 4"` / `"call @BTN"`）。読み上げ集の正解が既にこの形なので、店舗の読み上げ集 → 表の行が同じ言葉で往復する。テストは話題ファイルに 1 つ（`parametrize(ids=text)`）: `parse_keys(text) == keys` と `flags <= parse_flags`。`is_announcement` / `is_question` / `amount_only` / `ambiguous_amount` は flags の列で。**出所（日付・誰の言葉・短い引用）は行の `src` に残す**（オーナーの決定が表のどの行かを grep で引ける。いま「オーナー」は 25 ファイルに散っている）。日付のファイルの長い docstring（なぜ）は、話題の規則に書き直せる分だけ残し、経緯は既にある worklog（`2026-10-06-store-logs-review.md` など）へ日付で結ぶ。
順番: 行がほぼ重なっている `test_corpus_0930_readings`（59 件）→ `test_corpus_0930_round2`（44）→ `test_reading_rules`（76）→ `test_parser`（6）→ `test_recognizer_amounts`（31）→ `test_question_utterances`（16）→ `test_lower_digits`（45）→ `test_phonetic`（75。`sounds_like` の表は別の表）→ `test_spoken_amounts` / `test_store_2026_09_27` / `_09_29` / `_10_01` / `test_hand_name` / `test_garbled_amount` の **parse だけのクラス**。エンジンの場面（`deal` → `say` → `played()` だけを見るもの）は `Scene` の表にできるが、`notices` や `reason` を見るものは関数のまま話題ファイル（`test_restatement.py` / `test_allin.py` / `test_replay_order.py`）へ。

### D9. 古い段階のファイル — 推奨
理由: `MATCH_WINDOW` の照合も `calc_confidence` の固定表も生きたコード（`integration/engine.py:58, 200`）なので「古い = 消す」ではない。
やること: `test_parser` → 表へ。`test_game_state`（3）+ `test_phase_d0_engine` の legacy 分 + `test_phase_g_default` → `test_legacy_backend.py`（rollback 経路の固定 = CLAUDE.md の約束）。`test_phase_d_corrections` + `test_phase_d2_wiring` → `test_rules_aware.py`。`test_phase_d3_confidence` + `test_phase7::TestCalcConfidence`（固定表。唯一の 4 行）+ `test_phase_bc_events` → `test_confidence.py`（`test_confidence_calibration` は別のまま）。`test_phase_e_session_integration` + `test_engine_session_setter` → `test_session_write_through.py`。`test_integration` + `test_phase7` のスレッドの照合 → `test_sensor_matching.py`（sleep を外す, D4）。`test_phase_a_hardening`（A1 = 未解決の札で review_required）→ `test_rfid_table_flow` へ 1 件。`test_action_street_label`（ISSUE-0029 の意味の固定）と `test_logger` は消さず 3 行の表に。

### D10. 変異の確かめ方（具体） — 推奨
やること: まとめる対象のモジュール（`audio/recognizer.py` 1,875 行 / `audio/phonetic.py` / `core/bet_sizing.py` / `core/positions.py`）ごとに、手で入れる変異 5〜10 個を worklog に書いて固定する（例: `ALLIN_RESTATE_SEC` 8 → 0、「ベッド」の別名を外す、片仮名の隣のひらがなを区切りにしない、`check_around` の印を付けない、`amount_only` を `raise` にする、`fuzzy_keyword` の閾値を 1 段ゆるめる）。`git worktree` に当てて**その話題のテストだけ**（< 2 秒）を回し、殺した / 生き残ったを表にする。古いファイルを消してよいのは全部殺せたとき。生き残りは「行を足す」理由であって「古いファイルを残す」理由にしない。`mutmut` の全量は任意（話題のテストだけなら 1 変異 1 秒、1 時間程度）。

### D11. 通過率の条件は全体ではなくモジュールごと — 推奨
理由: 全体は 22,750 文。`core/showdown.py`（86 文）のテストを全部失っても全体は 0.4 ポイントしか動かない。
やること: まとめる対象の床を記録する: `audio/recognizer.py` 98.4%・`audio/phonetic.py` 96.6%・`integration/engine.py` 91.8%・`core/poker_engine.py` 90.7%・`integration/estimator.py` 95.2%・`integration/world_replay.py` 95.1%・`core/showdown.py` 96.5%・`core/positions.py` 98.8%・`rfid/reader_thread.py` 97.3%・`tools/eval_store.py` 93.1%。各 −0.5 ポイント以内（recognizer なら 5 文）。あわせて `--branch` の分岐の通過率を監査 2 で基準化する（目標はまだ置かない）。

### D12. 印と設定の規律 — 推奨
理由: `[tool.pytest.ini_options]` も `conftest.py` も無い。印は `parametrize` 128・`skipif` 1 だけ。CI の `--ignore=tests/test_vision.py` は手元の指定と食い違いやすい。
やること: `pyproject.toml` に `markers = ["slow: 単独で 2 秒超"]`・`addopts = "--strict-markers --durations=15 --durations-min=1.0"`・`testpaths = ["tests"]`、`tests/conftest.py` に `collect_ignore = ["test_vision.py"]`。`pytest-timeout` を入れ、速い組は `timeout = 20`（新しい遅いテストは落ちて知らせる。slow には `@pytest.mark.timeout(300)`）。`test_installer.py::TestPowerShell` の 5 件は手元で skip（pwsh 無し）・CI で 0 と明記。

### D13. 段階 3 は仕組みで止める — 推奨
やること: (a) `tests/test_suite_rules.py`: `test_store_20*` / `test_phase_*` の名前を禁止、`from tests.test_` を禁止、docstring 無しのファイルを禁止。(b) 変更ごとに回すテストの選び方: coverage の文脈から `tests/test_map.json`（製品ファイル → テストファイル）を監査のたびに作り直し、`git diff --name-only` で引く。エンジンを変えたら `test_store_fixtures` を必ず足す。CLAUDE.md §7b にこの手順を 1 行。(c) 店舗の読み取りの誤りは表の 1 行、ハンド丸ごとは `--export-fixture`。新しいファイルを作るのは話題が新しいときだけ。(d) CI は `--junitxml` と `--durations` を artifact に残す（監査が CI の数字を読める）。

### D14. 順序依存と並列の衛生 — 任意
理由: `tests/test_estimate.py:22` の `logging.disable(logging.CRITICAL)` は収集の時点で全体に効く。`test_reading_rules` 等の autouse fixture が `NOTSET` に戻す。`test_play_gate` の caplog を `test_estimate` の後に回して確かめた: 通る。いまは壊れていないが、xdist / 順序入れ替えの前に片付ける。
やること: module-level の `logging.disable` を autouse fixture に。`join(timeout=…)` の後に `is_alive()` の assert（D4）。監査 2 で `pytest-randomly` 3 回。

### D15. 段階 1 で製品コードに触れるのは最小に — 任意
やること: 段階 1 で変える製品コードを先に列挙する（目安は `POLL_INTERVAL` のクラス属性だけ）。推定器の窓（D2）は別タスク・別コミット。

## 監査 2（段階 1 のあと）の確認表
- CI: 直近 3 回の各ジョブの Run tests ステップ秒（API）とランナーのコア数。手元: 全体 1 プロセス / 速い組 1 プロセス / 速い組 `-n auto`。
- junit のファイル別秒の表（付録 A と同じ形）と差分。slow の印の一覧と各秒。`--durations=15` に印なしの 2 秒超が 0。
- 件数 2863 ± 足した分、通過率 85.0 ± 0.1、`pytest-randomly` 3 シードで失敗 0、`tests/fixtures/store/*` の内容が不変。
- 段階 1 で触った製品コードの一覧。推定器の窓を変えたなら、別タスクとして `bench_hands --twice` と `compare_versions` の「前とすべて同じ」。

## 監査 3（段階 2 のあと）の確認表
- 消した / まとめた表: 旧ファイル → 新ファイル、（入力, 期待）の組の数 前 / 後 / 重複で減った数、「オーナー」の固定の数 前 / 後と移った先。
- 変異の表（モジュール × 変異、殺した / 生き残り）。生き残りに足した行。
- 通過率: 全体と D11 の床。分岐の通過率（監査 2 の基準との差）。
- `test_store_fixtures` と `test_contracts` が無変更で緑。ファイル数・行数・`from tests.test_` = 0・日付 / 段階の名前 0。
- 時間の数字を監査 2 と同じ形で再掲。

---
根拠にした主なファイル: `docs/worklog/2026-10-06-test-diet.md`, `docs/worklog/2026-10-06-test-diet-inventory.md`, `tests/test_estimator.py`, `integration/estimator.py`（`windows()` 421 行目付近・`estimate_hand` 872）, `integration/world_replay.py`（`TICK_SEC` 50・`advance` 384）, `rfid/http_receiver.py:118`, `tests/test_tools_ground_truth_ui.py`（`_serve`）, `tests/test_rfid_folds.py:44`（`_Table`）, `tools/read_corpus.py:232`（`event_key`）, `.github/workflows/ci.yml`, `pyproject.toml`。cProfile の出力はスクラッチ（リポジトリ外）。
