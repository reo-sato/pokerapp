# テストの棚卸し（2026-10-06, コミット d3e2276）

件数・行数は `pytest --collect-only` と行の数。秒は CI と同じ指定の 1 回（229 秒）。通る行・そのファイルだけが通る行は coverage をテストの関数ごとに取ったもの（製品のコード 22,750 文、通過 85.0%）。

| ファイル | 件数 | 行数 | 秒 | 通る行 | そのファイルだけが通る行 | 何のテストか（冒頭） |
|---|---:|---:|---:|---:|---:|---|
| test_estimator | 40 | 563 | 91.0 | 4020 | 114 | 推定器 v1（`integration/estimator.py`）: 生の観測の再生（world_replay）に直し（聞こえなかったアク |
| test_tools_ground_truth_ui | 72 | 905 | 22.7 | 1632 | 230 | 真のアクション入力の画面（`tools/ground_truth_ui.py`）: ハンドログを読み、pokerkit で手番を補い、 |
| test_estimate | 16 | 183 | 18.4 | 2531 | 299 | tests/test_estimate.py — 推定器 v0（tools/estimate.py）とシミュレーション（tools/simu |
| test_rfid_http | 23 | 382 | 10.6 | 90 | 21 | RFIDHTTPReceiver のユニットテスト。 |
| test_cli_session_layer | 36 | 341 | 6.2 | 1588 | 113 | ADR-0059: ハンドロガー（`--cli`）で席とお客さんの対応を記録する。お客さん向け画面（viewer API）は |
| test_rfid_relay | 18 | 330 | 5.3 | 501 | 251 | 店舗 2026-09-30: RDP で操作する店舗 PC で、RDP のセッションの中のアプリからリーダー（PC/SC）が見えず |
| test_estimate_logs | 5 | 102 | 5.0 | 3138 | 115 | 店舗のログのハンドごとに推定器 v1 を回して推定のファイルを書く（`tools/estimate_logs.py`）。推定のハンドは |
| test_test_script | 25 | 385 | 4.5 | 2903 | 156 | 台本のハンド（`tools/test_script.py`, テスト方針 週 1）: 台本の生成・台本の行をそのまま読んだときにエンジンが台 |
| test_world_replay | 5 | 131 | 4.1 | 3378 | 43 | 推定器 v1 の再生器（`integration/world_replay.py`）: ライブの記録の席の信号（ライブのロガーがその場の解釈 |
| test_read_corpus | 45 | 656 | 4.0 | 1710 | 732 | 読み上げ集（`tools/read_corpus.py`, テスト方針 週 1）: 句と正解・マイクの録音と切り出し・読む人の操作・本番と同 |
| test_store_fixtures | 12 | 29 | 3.8 | 3173 | 6 | 店舗で真のアクションを入れたセッションの回帰テスト（ADR-0056 追記 1 の S0）。 |
| test_phase7 | 18 | 335 | 3.5 | 756 | 12 | Phase 7: RFID 統合 confidence スコアリングのテスト。 |
| test_garbled_amount | 65 | 286 | 3.4 | 1962 | 4 | 意味のない単発の語を額と読む（オーナー 2026-10-01「会話として意味のない単語（ゼニューク）などを単発で宣言したとき、 |
| test_tools_simulate_rfid | 16 | 163 | 3.2 | 135 | 25 | tools/simulate_rfid.py（実機なし RFID injector）のテスト。 |
| test_viewer_api | 25 | 342 | 2.9 | 519 | 11 | viewer API の HTTP テスト (ADR-0017, docs/contracts/viewer-api.md)。 |
| test_integration | 8 | 285 | 2.4 | 351 | 0 | Phase 3: IntegrationThread の ±2秒マッチングと confidence スコアのテスト。 |
| test_store_2026_09_30_script | 24 | 219 | 2.4 | 2283 | 77 | 店舗 2026-09-30 の台本のハンド（声だけ, 版 2 = 店の言い方, 6 人・30 ハンド）で見つかったこと: |
| test_main_audio_optional | 28 | 222 | 2.2 | 757 | 38 | `config.audio.enabled` でマイク入力（AudioThread）を切れること（B5: マイク無しの実機テスト）。 |
| test_tools_eval_store | 32 | 594 | 2.2 | 3193 | 511 | 店舗のログをいまのコードで再生して評価するツール（ADR-0056 追記 1 の S0）と、それを支える記録: |
| test_rfid | 86 | 1067 | 1.8 | 415 | 127 | Phase 6: RFID モジュールのテスト。 |
| test_tools_probe_pcsc | 75 | 653 | 1.5 | 492 | 215 | tools/probe_pcsc.py（実機 RFID PC/SC bring-up 診断）のテスト。 |
| test_voice_style | 51 | 281 | 1.4 | 1206 | 661 | tools/voice_style.py: 宣言の声と雑談の声の比べ（オーナーの問い 2026-10-03）。 |
| test_phase_a_hardening | 5 | 168 | 1.2 | 601 | 0 | Phase A (コア堅牢化) の回帰テスト。 |
| test_viewer_api_staff | 15 | 272 | 1.2 | 862 | 68 | Phase S5 staff write API (ADR-0021) — viewer API ↔ Python staff client |
| test_rfid_table_flow | 72 | 1222 | 1.1 | 1105 | 449 | ADR-0058: 卓の流れに合わせた RFID の解釈（本番の起動経路 = main.py で有効）。 |
| test_ledger_view_gui | 31 | 376 | 1.0 | 795 | 389 | Phase S3.2: LedgerViewWindow のロジック部分のテスト（tkinter 不要）。 |
| test_auto_hand | 52 | 517 | 0.9 | 2058 | 8 | ADR-0062: 店舗の音声テスト（2026-09-25）でハンドが一度も始まらなかった（`n` /「ハンド開始」を |
| test_hand_name | 99 | 404 | 0.9 | 2142 | 53 | ショーダウンでディーラーが言う **役名**（オーナー 2026-09-30: アウトオブポジションが見せ、ディーラーが役名を言う。 |
| test_multi_action_utterance | 34 | 179 | 0.9 | 1322 | 6 | ADR-0061: 店舗の音声テスト（2026-09-25）で分かったことへの対応。実運用では席番号を言わず、アクターは |
| test_viewer_api_sync | 5 | 172 | 0.8 | 851 | 39 | Phase S5 (ADR-0022): 双方向 sync の 2 ノード round-trip test。 |
| test_viewer_api_auth | 10 | 208 | 0.7 | 600 | 28 | ADR-0027 (L1): viewer API の player PIN 認証（login / PIN 設定 / self-write  |
| test_viewer_api_staff_lifecycle | 9 | 311 | 0.7 | 909 | 169 | ADR-0038 §A/§B — staff API 拡張（会計 reversal / point grant + session/座席/p |
| test_integration_recording | 2 | 92 | 0.6 | 410 | 0 | R1 (ADR-0010): IntegrationThread に event_recorder を渡すと、生イベントが解釈前に |
| test_phase_bc_events | 10 | 68 | 0.6 | 124 | 1 | Phase B+C (イベント記録基盤) の回帰テスト。 |
| test_rfid_folds | 32 | 594 | 0.6 | 2393 | 30 | フォールドは席の札の離脱で決める（オーナー決定 2026-09-25）: |
| test_viewer_api_correction | 5 | 111 | 0.6 | 578 | 10 | ADR-0036 (B4): ハンド訂正の staff API + viewer オーバーレイ E2E。fastapi/httpx 未導入は |
| test_viewer_api_ground_truth | 12 | 258 | 0.6 | 680 | 90 | ADR-0043: Phase A 計測 ground truth の staff API E2E。fastapi/httpx 未導入は s |
| test_session_viewer_gui | 15 | 258 | 0.5 | 452 | 245 | WS2-α: SessionViewerWindow（read-only）のロジックテスト（tkinter 不要）。 |
| test_viewer_api_store | 8 | 157 | 0.5 | 569 | 31 | ADR-0059: 店舗でお客さんが自分のハンドを見る。 |
| test_reconstruction | 46 | 183 | 0.4 | 1121 | 26 | Phase F1 (#8) — golden-fixture replay 回帰 + round-trip 決定性 (R4)。 |
| test_second_ear | 19 | 173 | 0.4 | 655 | 38 | tests/test_second_ear.py — 第 2 の耳（audio/second_ear.py）。本物のモデル（約 170 MB |
| test_thread_health | 3 | 95 | 0.4 | 178 | 4 | 録音系の死活表示（dashboard 用 health ステータス）のユニットテスト。 |
| test_amount_space | 38 | 354 | 0.3 | 2181 | 26 | 額の候補をその場面で使える額に絞り、ポットに対する大きさで重み付けする（オーナー 2026-10-01）: |
| test_player_registry_gui | 12 | 164 | 0.3 | 246 | 141 | Phase S1: PlayerRegistryWindow のロジック部分のテスト（tkinter 不要）。 |
| test_silent_runs | 13 | 256 | 0.3 | 1961 | 2 | 言われなかったアクション（オーナー, 2026-09-29）: **コール・チェックは毎回言う**運用にした（それまでの「連続する席の |
| test_spoken_amounts | 48 | 346 | 0.3 | 1854 | 8 | 店舗の 3 回目の通しテスト（2026-09-25）への対応: |
| test_viewer_api_client | 4 | 145 | 0.3 | 753 | 6 | Phase S5 (ADR-0020) — viewer API ↔ Python client の round-trip 契約 test。 |
| test_viewer_api_merge | 5 | 128 | 0.3 | 814 | 18 | ADR-0030: player merge の viewer/staff API round-trip。fastapi/httpx 未導入 |
| test_button_rescue | 21 | 301 | 0.2 | 1726 | 82 | ディーラーがボタンを動かし忘れたときの救済（店舗 2026-09-29 9d1d8536 ハンド 4 のメモ: この回に限ってディーラーが |
| test_corpus_0930_round2 | 44 | 107 | 0.2 | 648 | 6 | 読み上げ集 2 回目（2026-09-30 20:55, Kei, 店の言い方に作り直した 135 句）で読めていなかった書き起こし。 |
| test_ledger_repository | 24 | 322 | 0.2 | 424 | 51 | Phase S3.1: ledger / points / settlement の core テスト。 |
| test_lower_digits | 45 | 185 | 0.2 | 770 | 26 | 数の下の桁（オーナー 2026-10-02:「数字の後に、残りの桁などを発声できるタイミングで意味の通らない単語が入った際には、 |
| test_measure_capture_accuracy | 27 | 533 | 0.2 | 230 | 19 | Phase A 計測ハーネス（`tools/measure_capture_accuracy.py` / |
| test_menu_edit | 14 | 167 | 0.2 | 510 | 52 | ADR-0046 — menu master の staff 編集（価格改定・品切れ）。 |
| test_phonetic | 75 | 182 | 0.2 | 1281 | 1 | 音の近さでアクションの語を読む（`audio/phonetic.py`, ADR-0056 追記 1 の S2）。 |
| test_reading_rules | 76 | 176 | 0.2 | 560 | 2 | 読み取りの直しを、見つけた言い方ごとの例外ではなく一般的な規則にした（2026-09-30, オーナー「読み取りの直しについて、 |
| test_session_repository | 19 | 209 | 0.2 | 194 | 2 | Phase S2: session レイヤと hand-based seat assignment の core テスト。 |
| test_store_2026_09_27 | 37 | 318 | 0.2 | 2364 | 20 | 店舗の通しテスト 2026-09-27（6 セッション）のレビューで見つかったこと: |
| test_store_2026_09_29 | 38 | 260 | 0.2 | 2253 | 0 | 店舗の通しテスト 2026-09-29（4 セッション・真のアクション 11 ハンド）のレビューで見つかったこと: |
| test_store_2026_10_01 | 18 | 178 | 0.2 | 1747 | 2 | 店舗の通しテスト 2026-10-01 のレビューで見つかったこと（オーナー「言い直しはそのように修正できるようにしてください」）: |
| test_street_change_words | 15 | 140 | 0.2 | 1700 | 6 | ストリートが変わるときの言葉（店舗 2026-10-06 のログとオーナーの説明）: |
| test_tools_pack_logs | 23 | 320 | 0.2 | 342 | 336 | テストのログを 1 つの zip にまとめる（`tools/pack_logs.py`, レビューに送るため）。 |
| test_viewer_api_hands_staff | 4 | 136 | 0.2 | 501 | 9 | ADR-0044: staff hands read（GET /api/staff/sessions/{sid}/hands）の round |
| test_viewer_api_oidc | 4 | 79 | 0.2 | 440 | 18 | ADR-0031 (L2): POST /api/auth/{provider}/exchange の E2E（fake provider  |
| test_audio_check | 33 | 508 | 0.1 | 990 | 384 | ADR-0060: 店舗 PC での音声テストの前に、マイクと音声認識を確かめる。 |
| test_contracts | 18 | 190 | 0.1 | 437 | 0 | Phase 0a: contract drift detection (最小 bootstrap, ISSUE-0003 対策)。 |
| test_corpus_0930_readings | 59 | 218 | 0.1 | 1520 | 1 | 店舗の読み上げ集（2026-09-30, 2 人 × 163 句, 音楽を流した店内）で読めていなかった書き起こし。 |
| test_gui | 8 | 140 | 0.1 | 179 | 147 | Phase 4: GUIDashboard のロジック部分のテスト（tkinter 不要）。 |
| test_hand_before_deal | 8 | 183 | 0.1 | 1272 | 9 | 配る前に `n`（「ハンド開始」）で始めたハンド（店舗の 5 回目の通しテスト, セッション c9e150f3）: |
| test_hand_estimate | 14 | 183 | 0.1 | 521 | 24 | 記録の本体 = ライブの記録 ⊕ 推定 ⊕ スタッフの訂正（ADR-0056 D1, オーナー 2026-09-30: 推定器を記録の本体に |
| test_ledger_csv_exporter | 7 | 156 | 0.1 | 368 | 44 | Phase S3.3: settlement / cashflow CSV エクスポートのテスト（ISSUE-0018）。 |
| test_live_hand | 10 | 243 | 0.1 | 1901 | 7 | オーナー 2026-09-30: 「ハンド進行中に真のアクションを入力したいので、フロップがディールされるタイミングで UI に |
| test_order_request_repository | 21 | 239 | 0.1 | 368 | 14 | Phase M5 (ADR-0018) — 注文リクエストの core テスト。 |
| test_play_gate | 38 | 285 | 0.1 | 1809 | 5 | 店舗の 2 回目の通しテスト（2026-09-25）への対応: |
| test_player_credential_repository | 8 | 90 | 0.1 | 74 | 13 | ADR-0027 (L1): `PlayerCredentialRepository`（PIN ハッシュ + lockout + 永続）の単 |
| test_player_merge | 13 | 197 | 0.1 | 404 | 12 | ADR-0030: player merge（alias/tombstone + read-time canonicalization）の  |
| test_player_repository | 18 | 148 | 0.1 | 103 | 0 | Phase S1: Player ドメインモデルと PlayerRepository のテスト。 |
| test_positions | 54 | 268 | 0.1 | 197 | 11 | ISSUE-0032 / 仕様 FR-05b〜h — ディーラーボタンの回転とポジション名。 |
| test_reconstruction_hardening | 64 | 553 | 0.1 | 1575 | 16 | ADR-A/B/C/D — アクション履歴復元アルゴリズムの正当性修正バッチのテスト。 |
| test_seats_and_blinds | 24 | 310 | 0.1 | 1393 | 59 | 席の参加・休みとブラインドの変更（オーナーの回答 2026-09-26: 席は操作で決める / 離席は無い / ブラインドは |
| test_second_ear_live | 36 | 280 | 0.1 | 885 | 25 | ライブの聞き直し（2026-09-29, オーナー了承）: Whisper がアクションとして読めなかった発話（幻聴・読めない文）だけを |
| test_showdown_unknown_cards | 5 | 57 | 0.1 | 61 | 0 | ショーダウンで読めていない札（ボードの `??`・手札の不足）があるとき（オーナー 2026-09-29, 店舗 d0f055fb ハンド  |
| test_spoken_fold | 9 | 228 | 0.1 | 2072 | 1 | 「フォールド」と言われたのに札が席に残っていた手番の席（店舗 2026-09-27, セッション c2cd4a53）。 |
| test_sync | 25 | 501 | 0.1 | 256 | 58 | Phase S5 (ADR-0022): state-based merge の純粋関数 + 収束性テスト。 |
| test_table_state | 29 | 402 | 0.1 | 716 | 115 | ADR-0056 D5: **RFID だけから導く卓状態**（カード / 有効席 / ストリート）。 |
| test_timeline_fidelity | 12 | 244 | 0.1 | 1079 | 15 | ADR-0055: アクション履歴を**音声の時系列と突き合わせて再生する**ために必要な時刻の精度。 |
| test_tools_play_hand_text | 6 | 105 | 0.1 | 1355 | 22 | tools/play_hand_text.py（mic 不要のテキスト駆動ドライバ）のテスト。 |
| test_tools_register_cards | 36 | 334 | 0.1 | 362 | 219 | tools/register_cards.py（実機カード UID のタップ駆動登録）のテスト。 |
| test_tools_rescore_audio | 13 | 217 | 0.1 | 351 | 94 | 保存した発話の音声を Whisper で採点し直すツール（`tools/rescore_audio.py`, ADR-0056 追記 1 の |
| test_viewer_read_models | 9 | 177 | 0.1 | 181 | 2 | viewer API の read model テスト (ADR-0017, docs/contracts/viewer-api.md)。 |
| test_whisper_noise | 19 | 185 | 0.1 | 682 | 26 | 雑音への幻聴（店舗の 5 回目の通しテスト, セッション c9e150f3）: Whisper がプロンプトの語を 448 トークンまで |
| test_action_street_label | 3 | 83 | 0.0 | 669 | 0 | ISSUE-0029: ActionRecord.street は「そのアクションが行われたストリート」。 |
| test_atomic_io | 10 | 85 | 0.0 | 57 | 4 | B2: atomic + fsync な JSON 書き込みヘルパ（core/atomic_io.py）のテスト。 |
| test_audio_devices | 30 | 324 | 0.0 | 242 | 118 | マイクを名前で選ぶ（config `audio.device_name`, 店舗 2026-10-01）。Bluetooth のマイク・ヘッ |
| test_audio_records | 4 | 70 | 0.0 | 360 | 16 | 記録の完全化（設計監査 2026-09-25）: 聞き取った発話をすべて JSONL に残す / 発話の音声を WAV で保存する / |
| test_auth_identity_repository | 6 | 64 | 0.0 | 63 | 23 | ADR-0031 (L2): AuthIdentityRepository（(provider, subject)→player_id, n |
| test_auth_token | 9 | 49 | 0.0 | 25 | 6 | ADR-0027 (L1): player principal の stateless 署名トークン（`core/auth_token.py |
| test_backup | 4 | 52 | 0.0 | 23 | 23 | B2 (v1.0 ローンチレビュー): データバックアップ core のテスト。now 注入で決定的に世代を作る。 |
| test_bench_hands | 14 | 116 | 0.0 | 194 | 94 | 目標の数字 = 全部正しいハンドの割合（オーナー 2026-09-30: 発話の読みが 9 割当たっても、ハンドが丸ごと正しい割合は |
| test_call_totals | 7 | 132 | 0.0 | 1104 | 6 | コールの額は画面ではトータル（その人がそのストリートで出した合計）で見せる（オーナー, 2026-09-29）。 |
| test_compare_versions | 5 | 53 | 0.0 | 44 | 44 | 前の版と今の版を比べる道具（`tools/compare_versions.py`, 監査 3 回目の必須 7）: 両方の版の物差しの結果 |
| test_confidence_calibration | 13 | 62 | 0.0 | 60 | 47 | Phase R5/F2 (ADR-0033): 派生 confidence の重み較正をプロパティで回帰ロックする。 |
| test_control_queue | 4 | 129 | 0.0 | 133 | 43 | hand logger 遠隔制御の control-command queue（ADR-0039）の単体テスト。 |
| test_engine_rebuy | 5 | 103 | 0.0 | 488 | 13 | ISSUE-0012: rebuy を GUI/CLI から queue 経由で IntegrationThread に適用する経路。 |
| test_engine_session_setter | 4 | 104 | 0.0 | 594 | 0 | Phase E3 (ISSUE-0006): IntegrationThread.set_seat_player_map の振る舞い。 |
| test_event_recorder | 9 | 122 | 0.0 | 64 | 10 | R1 (ADR-0010): EventRecorder の envelope 変換・JSONL 追記・code↔contract 整合を検 |
| test_game_state | 3 | 33 | 0.0 | 51 | 0 |  |
| test_ground_truth_repository | 13 | 179 | 0.0 | 101 | 19 | ADR-0043: ground truth ストア（LWW、per-session file）の単体テスト。 |
| test_hand_correction | 8 | 109 | 0.0 | 130 | 17 | ADR-0036 (B4): ハンド訂正 core（append-only repo + read-time オーバーレイ）のテスト。 |
| test_installer | 56 | 266 | 0.0 | 0 | 0 | Windows ワンステップインストーラ（`install.cmd` → `installer/install.ps1`）の整合検査（ADR |
| test_logger | 1 | 47 | 0.0 | 68 | 0 |  |
| test_misdeal_correction | 16 | 236 | 0.0 | 442 | 23 | ADR-0054: ミスディールで一度読ませたカードを外し、正しいカードを読み直す経路。 |
| test_no_active_hand_guard | 10 | 139 | 0.0 | 906 | 2 | ISSUE-0028: 進行中のハンドが無いときに届いたアクション / winner 宣言でクラッシュしない。 |
| test_oidc | 7 | 83 | 0.0 | 142 | 3 | ADR-0031 (L2): provider 抽象 + claim→principal 解決（実 IdP なし、FakeOidcProvi |
| test_parser | 6 | 44 | 0.0 | 136 | 0 | 席番号（日本語）が amount に混入しないこと。 |
| test_phase_d0_engine | 14 | 151 | 0.0 | 184 | 11 | Phase D (#7) D0 — PokerEngine 境界の rules-aware additive メソッド。 |
| test_phase_d2_wiring | 8 | 167 | 0.0 | 597 | 0 | Phase D (#7) D2a — rules-aware 経路の結線（apply_corrections ライブ適用 + actor 競 |
| test_phase_d3_confidence | 7 | 97 | 0.0 | 420 | 0 | Phase D (#7) D3 — 派生 confidence（3 因子 L/A/Q）+ needs_review 5 条件（ADR-000 |
| test_phase_d_corrections | 18 | 153 | 0.0 | 58 | 5 | Phase D (#7) part 1 — apply_corrections の単体テスト（ADR-0009 §5 の修復表）。 |
| test_phase_e_session_integration | 3 | 104 | 0.0 | 591 | 0 | Phase E (#10) E1+E2-core — hand logger × S2 session レイヤの write-through |
| test_phase_g_default | 4 | 56 | 0.0 | 134 | 2 | Phase G (#9) — pokerkit を live 既定 backend に切替。 |
| test_phh_exporter | 35 | 290 | 0.0 | 120 | 120 | Phase 5: PHH エクスポーターのテスト。 |
| test_player_web_build | 8 | 82 | 0.0 | 0 | 0 | ADR-0059: お客さん向け画面（`mobile/` の web 版）のビルドはリポジトリに含める（店舗 PC に Node を |
| test_poker_engine | 12 | 153 | 0.0 | 200 | 1 | R2 (ADR-0009): PokerkitGameState（pokerkit を live 権威にした GameStateManage |
| test_poker_engine_fallback | 2 | 34 | 0.0 | 31 | 4 | create_game_state の pokerkit フォールバック (review hardening)。 |
| test_position_evidence | 10 | 144 | 0.0 | 796 | 4 | ISSUE-0032 / 仕様 §7・FR-26 — **ポジション名の読み上げ**を actor 推定の明示証拠にする。 |
| test_question_utterances | 16 | 76 | 0.0 | 514 | 1 | 確認型の発話（仕様 FR-16/17, §7 のディーラー発話プロトコル）: 「コールですか？」「レイズ 2400 でよろしいですか？」は |
| test_recognizer_amounts | 31 | 101 | 0.0 | 138 | 1 | 日本語金額解析の直接ユニットテスト (review hardening)。 |
| test_seat_selection | 9 | 49 | 0.0 | 11 | 11 | Phase E3 (ISSUE-0006): seat 選択ダイアログの純ロジック(customtkinter 非依存)。 |
| test_set_config | 10 | 73 | 0.0 | 53 | 36 | ADR-0059: 店舗 PC で config.json の 1 項目（例: `session_layer.enabled`）をコマンド  |
| test_shared_ui_sync | 2 | 34 | 0.0 | 0 | 0 | ADR-0044 D1: 共有 RN UI（正本 `shared/`）と mobile / staff 内コピーのバイト同一性を検証する。 |
| test_sync_scheduler | 4 | 47 | 0.0 | 19 | 19 | ADR-0032: SyncScheduler（sync の定期 auto-trigger）の決定的テスト（clock 注入）。 |
| test_tools_second_ear | 14 | 174 | 0.0 | 504 | 141 | 保存した発話の音声を第 2 の耳（ReazonSpeech）と Whisper の別のやり方で聞き直すツール（`tools/second_e |
| test_vision | 0 | 147 | 0.0 | 0 | 0 | vision モジュールの単体テスト。 |
