# ADR-0049: 制御語ガード・actor 証拠健全性・recorder 再構築（G1/G3/G4/T4）

- Status: Accepted
- Date: 2026-08-19
- 関連: ADR-0009 / ADR-0033 / ADR-0047 / ADR-0048 / ISSUE-0009

## Context

- **G1**: `new_hand` / `winner` / `showdown` は状態を大きく動かす制御語なのに、Whisper 信頼度も
  状態妥当性も見ずに即実行していた。initial_prompt がアクション語彙の箇条列挙だったため、無音時に
  プロンプトをオウム返しするハルシネーションが**偽の new_hand / winner** になり得た。
- **G3**: actor 推定の RFID 最近傍 pop（`_pop_nearest_rfid_seat`）が席の状態を見ず、fold 済み席の
  カード読み（チップ整理・回収時など）が actor 証拠として採用され得た。
- **G4**: `apply_corrections` は whisper_conf を持ち越すだけで判定に使っておらず、ADR-0009 §6
  条件②「高信頼 ASR × 規則矛盾 → review」が未実装だった。
- **T4**: recorder（`audio/recorder.py`）は推論がキャプチャループを同期ブロックし（推論中の発話を
  取りこぼす）、無音バッファも推論に送り（ハルシネーションの温床）、フラッシュ判定が wall-clock
  依存だった。ユニットテストも 0 件だった。

## Decision

### G1 — 制御語ガード

- `IntegrationThread(control_conf_threshold=...)`（config `engine.control_conf_threshold`,
  既定 **0.0 = 無効**で挙動不変）。閾値 > 0 のとき、`confidence < 閾値` の制御語は状態を動かさず
  **保留レコード**（`reason="low_conf_control_held"`, needs_review, on_action のみ）にする。
  `confidence=None`（GUI ボタン / CLI / control queue 由来）はガード対象外（意図的操作）。
- **状態妥当性**: 進行中ハンド（勝者未宣言・アクションあり）への `new_hand` は、記録を捨てずに
  **異常確定**（winner は ADR-0047 B5 の fallback 連鎖、review_required=True）してから新ハンドを
  開始する。従来は進行中ハンドの記録が黙って消えた。
- `WHISPER_PROMPT_JA` を自然文に変更（キーワード箇条列挙 → 文脈文。オウム返し抑制）。
  `WhisperTranscriber` は segment の `no_speech_prob` で confidence を減衰させる
  （無音区間のハルシネーションはここが高く出るため、G1 ガードの入力として意味を持つ）。

### G3 — actor 証拠の健全性

- `_pop_nearest_rfid_seat` は **active（未 fold・実在）席の読みだけ**を actor 証拠として採用する。
  fold 済み席の読みは debug ログを残して無視（corroboration にも使わない）。
- cap 超過などで採用しなかった sensed 席は、当該アクションの監査 reason に
  `actor_conflict_capped(sensed=N)` として残す（従来は log のみで record から逆引き不能）。
  合成成功時は `actor_sensed_over_prior` を残す。

### G4 — 高信頼 ASR × 射影での action 変化 → review

- `apply_corrections` に `HIGH_CONF_ASR = 0.85` を導入。whisper_conf がこれ以上なのに射影で
  action が変わった場合（実質 bet↔raise 再マップ = 他の射影は既に review）、
  `reason="high_conf_asr_projection"` + review。高信頼の発話が状態と食い違うのは
  ストリート遷移漏れ等**状態側の疑い**であり、無言の再マップにしない。

### T4 — recorder 再構築（`audio/recorder.py`）

- **キャプチャと推論を分離**: キャプチャループは録音のみを行い、発話チャンクを内部キュー
  （上限 8、満杯時は最古を捨てて警告）に積む。推論は worker スレッド（`AudioInference`）。
- **有音ゲート**: RMS 閾値以上のチャンクを含まないバッファは推論に送らない。発話開始時に
  直前チャンク 1 個を pre-roll として取り込み、開始時刻を `utterance_start_ts` として記録
  （ADR-0048 T1）。最小長判定は**有音サンプル数**（末尾無音でかさ増ししない）。
- **フラッシュ判定をサンプル数ベース**に（wall-clock ではなく取得サンプル量）。
- **テスト用 seam**: `read_chunk` 注入（`_capture_loop`）と `transcriber` 注入で PyAudio /
  faster-whisper なしにテスト可能（`tests/test_reconstruction_hardening.py::TestRecorderT4` =
  recorder 初のユニットテスト）。

## Consequences

- 偽制御語への防御が 3 層になる: 自然文プロンプト（発生抑制）→ 有音ゲート（無音を推論に
  送らない）→ 制御語ガード（低信頼を保留）。閾値は実運用ログを見て設定する（既定 off）。
- 推論中の発話取りこぼしが解消（実機 E2E = Phase H で体感差を確認する）。
- fold 済みプレイヤーのカード読みで actor が飛ぶ誤検出がなくなる。
- ガード閾値 > 0 の運用では、低信頼の本物 winner も保留される。ディーラーの再読み上げ（または
  staff iPad のハンドタブ）が回復手段で、保留はレコードとして GUI に可視化される。
