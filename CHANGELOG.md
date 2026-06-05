# Changelog

本ファイルは [Keep a Changelog](https://keepachangelog.com/) の慣習に沿って、
ユーザー可視の挙動変更および仕様 / docs の重要更新を記録する。
詳細な経緯は `docs/adr/` / `docs/worklog/` / `docs/issues/` を参照。

## [Unreleased]

### Added (Phase F part 2 — side-pot 連携 + golden 5 ケース全緑 / F3a, v1 リリーストラック R5)

- **`HandSummary.pots`（main/side pot スナップショット）を additive 追加**（v1 issue #8 / Epic #4,
  ADR-0009 §3, ロードマップ A3 吸収）:
  - `core/hand_log.py`: `HandSummary.pots: list`（既定 `[]`、`to_dict` に含む）。
    `[{"amount": int, "eligible_seats": [int,...]}, ...]`。
  - `integration/engine.py:_finalize_hand`: `pots=gs.pots()` を埋め込み（rules-aware backend が
    `end_hand` 時に pokerkit から算出、legacy は `[]`）。挙動は additive（既存フィールド不変）。
  - **golden fixtures `unequal-allin` を緑化**: スタック差 all-in（1000/3000/3000）→ main pot
    `3000 [1,2,3]` + side pot `4000 [2,3]`。`tests/test_reconstruction.py` の GREEN_CASES に昇格。
  - **既知バグ 5 ケースが全緑**（check-facing-bet / call-amount-from-state / silent-fold /
    out-of-turn-rfid / unequal-allin）。**DoD #2 達成**。`pots` 追加に伴い既存 4 fixtures を再凍結
    （hand 終了が manual winner のため pots=[]、挙動不変）。
  - tests: `tests/test_reconstruction.py`（`unequal_allin_main_and_side_pots` + 全緑確認）。
    **全 274 passed, 0 skipped**（legacy は `pots=[]` で additive、回帰なし）。
  - 残 F3: `hand`/`action` schema freeze（ISSUE-0011, `_MODELS` 登録）、PHH の call/check 区別。

### Added (Phase D part 5 — 派生 confidence + needs_review 5 条件 / D3, v1 リリーストラック R3。Phase D 完了)

- **解釈可能な 3 因子 confidence + 明示的 needs_review 条件**（v1 issue #7 / Epic #4, ADR-0009 §6,
  ISSUE-0009）— **rules-aware 経路のみ**（legacy の固定 8 行 `calc_confidence` は不変）:
  - `integration/engine.py:derive_confidence` を新設: `confidence = clamp(L·(w_A·A + w_Q·Q), 0, 1)`。
    **L=合法性ゲート**（pokerkit 受理=1.0 / 非受理=`_CONF_L_PENALTY`、最重要）、**A=合意度**（一致した
    存在ソース / 存在ソース）、**Q=ソース品質**（一致ソースの base 信頼度の noisy-OR、audio は whisper で
    スケール、RFID>audio>camera）。固定 8 行テーブルを廃し、単調・解釈可能に。
  - `_handle_rules_aware_action` を D3 化: `derive_confidence` を適用し、**needs_review 5 条件**を明文化
    （①pokerkit 非合法 ②高信頼 ASR×規則矛盾/④amount snap[=`apply_corrections.needs_review`]
    ③actor 競合[prior↔sensor] ⑤`confidence < REVIEW_THRESHOLD`）。これで `HandSummary.review_required`
    が監査可能な意味を持つ。
  - 重み較正（暫定）: 良好な audio-only は閾値超え＝自動 review しない（v1 は音声優先）。camera-only /
    低 whisper / 合成 fold は閾値未満＝review。最終較正は golden fixtures / F。
  - golden fixtures 4 ケースの confidence を再凍結。`call-amount-from-state` は review=False を維持。
  - tests: `tests/test_phase_d3_confidence.py`（derive_confidence の順位/ゲート/whisper/合意 + 閾値条件⑤、7）。
    **全 270 passed, 1 skipped**。**これで Phase D（D0/D1/D2a/D2b/D3）完了**（残 R は F3 の side-pot/freeze）。

### Added (Phase D part 4 — silent-fold 合成 / D2b, v1 リリーストラック R3)

- **silent-fold 合成（未宣言 fold を補い actor を物理/明示証拠へ追従）**（v1 issue #7 / Epic #4,
  ADR-0009 §4, ISSUE-0009）:
  - `core/poker_engine.py:fold_through` を **atomic + cap 対応**に強化: `max_folds` 超過/到達不可は
    `ValueError` で **状態を巻き戻す**（`copy.deepcopy` snapshot/restore、誤 fold を残さない）。
    合成した席列（`list[int]`）を返す。
  - `integration/engine.py:_resolve_actor` を D2b 化: 優先順位 **RFID seat 読み > 明示発話席** で
    sensed を決め、prior と異なれば `fold_through(sensed, max_folds=SILENT_FOLD_CAP=2)` で silent-fold
    合成。cap 超過/到達不可は prior 維持。合成 fold は `_append_synth_fold` で **fold アクションとして
    記録**（`confidence=0.3`、常に `needs_review`）。actor 推定に使った RFID 読みは消費（滞留防止）し、
    最終 actor と一致すれば corroboration に再利用。競合（sensed≠prior）は `needs_review`。
  - **golden fixtures 2 ケースが緑化**: `silent-fold`（audio 駆動）/ `out-of-turn-rfid`（RFID 駆動、
    actor を RFID 席へ補正し corroboration 成立）。`tests/test_reconstruction.py` の GREEN_CASES に昇格。
  - tests: `tests/test_phase_d0_engine.py`（fold_through の返り値/cap/atomic 3 追加）、
    `tests/test_phase_d2_wiring.py`（明示席 → silent-fold 合成に更新）、reconstruction 2 ケース。
    **全 263 passed, 1 skipped**（残 `unequal-allin`=F3）。legacy 既定は不変。
  - 残: 派生 confidence（D3、合成 fold の 0.3 較正含む）。

### Added (Phase F part 1 — 決定的 replay ハーネス + 最初の golden fixtures / F1, v1 リリーストラック R4)

- **決定的 replay ハーネス**（v1 issue #8 / Epic #4, ADR-0011）:
  - `integration/replay.py`（`load_events` / `replay_events` / `replay_fixture`）+ CLI `tools/replay_hand.py`。
    記録済み `events.jsonl`（R1 sidecar）を live と同じ `IntegrationThread` の per-event 処理に **timestamp
    昇順**で通し `HandSummary` を再構築する。
  - `integration/engine.py` に **clock 注入**（`IntegrationThread(clock=..., on_hand=...)`、additive）。
    `_now_iso` を `datetime.fromtimestamp(self._clock())` に、`_expire_buffers` を `self._clock()` に変更。
    **既定 `time.time` で live は完全不変**。
  - **golden fixtures**: `tests/fixtures/reconstruction/<case>/{setup.json, events.jsonl, expected_hand.json}`。
    D1/D2a で既に正しく再構築できる **2 ケースを緑で固定**: `check-facing-bet`（非合法 check→call+review）/
    `call-amount-from-state`（heard 9999 無視→engine の call 額 200）。残り 3 ケース（`silent-fold` /
    `out-of-turn-rfid`=D2b、`unequal-allin`=F3）は実装と同じ増分で追加（skip で明示）。
  - **round-trip 決定性**（DoD #3）: 同一 events.jsonl を 2 回 replay → 完全一致を `tests/test_reconstruction.py`
    で検証。fixtures は `reconstruction_event` schema 適合も確認。
  - tests: `tests/test_reconstruction.py`（7 + pending 3 skip）。**全 255 passed, 3 skipped**（clock 既定で
    既存テスト回帰なし）。

### Added (Phase D part 3 — rules-aware ライブ結線 / D2a, v1 リリーストラック R3)

- **rules-aware 経路（pokerkit）に `apply_corrections` をライブ結線 + actor 競合検出**（v1 issue #7 /
  Epic #4, ADR-0009 §1/§5）:
  - `integration/engine.py`: `_handle_audio_event` のベッティング処理を `gs.legal_context()` で分岐。
    **rules-aware（pokerkit、空でない legal_context）= `_handle_rules_aware_action`**（`apply_corrections`
    で合法手へ射影し ActionRecord に反映、`_resolve_actor` で明示発話席(`event.seat`)が手番(prior)と
    食い違えば `needs_review`）。**legacy（空 legal_context）= `_handle_legacy_action`（従来コードを
    そのまま分離・挙動不変）**。
  - これにより pokerkit backend で **call/check の状態一意化・非合法 action の修復・明示席の out-of-turn
    検出**がライブで効く（既定 legacy は不変）。
  - **silent-fold 合成（`fold_through` 結線で prior を上書き）・RFID/camera を含む多源 actor 解決・派生
    confidence（D3）は後続増分 D2b**（誤 fold リスクと滞留しうる RFID 読みの消費設計のため Phase F #8 の
    golden fixtures で検証）。D2a は prior 固定 + 明示席（イベント単位・滞留しない）の競合 flag に留める。
  - tests: `tests/test_phase_d2_wiring.py`（pokerkit 5: 射影/競合/legacy 分岐）。pokerkit 0.7.4 実走で
    **248 passed**（legacy 既存テストは `_handle_legacy_action` 経由で不変通過）。

### Added (Phase D part 2 — engine 境界の rules-aware メソッド / D0, v1 リリーストラック R3)

- **`PokerEngine` 境界に rules-aware の additive メソッドを追加**（v1 issue #7 / Epic #4, ADR-0009 §2）:
  `legal_context` / `is_legal_actor` / `pots` / `committed` / **`fold_through`** を Protocol に追加し、
  両 backend で conform させた。
  - `core/poker_engine.py`: `PokerkitGameState.fold_through(until_seat)` を**新規実装**（現 actor から
    until_seat 手前までを silent fold 合成 = ディーラー未宣言 fold の表現。到達不能は ValueError。
    上限は呼び出し側=actor 推定が距離で判断, ISSUE-0009）。`legal_context`/`pots`/`committed` は R2 で実装済。
  - `core/engine_types.py`（新規）: `LegalContext` を中立モジュールへ移設（循環 import 回避）。
    `core.poker_engine.LegalContext` として後方互換に再エクスポート。
  - `core/game_state.py`（legacy）: rules-aware でない stub を追加（空 `legal_context` = legacy 印 /
    `fold_through` は `NotImplementedError` / `pots`=[] / `committed`=0）。IntegrationThread は空 context を
    以て legacy 経路（従来挙動）へ分岐する設計（結線は D2）。
  - **ライブ未結線＝挙動不変**。actor 推定の結線（D2）は後続 PR。
  - tests: `tests/test_phase_d0_engine.py`（pokerkit 部は importorskip、legacy stub は常時実行）。
    pokerkit 0.7.4 を導入して実走 **243 passed**（未導入時は pokerkit 部 skip）。

### Added (Phase D part 1 — apply_corrections, v1 リリーストラック R3)

- **合法手への射影 `apply_corrections()` を実装**（v1 issue #7 / Epic #4, ADR-0009 §5）:
  raw ASR の (action, amount) を `LegalContext`（`legal_context()` 由来）の合法手へ射影する**純関数**
  （`audio/recognizer.py`、pokerkit 非依存）。
  - **call/check を状態から決定的に一意化**（`amount_to_call>0→call` / `==0→check`）。JA キーワードの
    曖昧さに依存せず、PHH/JSON で call と check を初めて区別できる核心。
  - bet↔raise を当ストリートのベット有無から再マップ、amount を合法レンジへ snap（大幅 snap / 額不明 /
    非合法は `needs_review`）。`Correction` 結果型（`corrected_from`/`reason`/`asr_confidence` を持つ）。
  - ベットに直面した "check" は暫定で **call + `needs_review`**（ISSUE-0009、尤度導入は後続）。
  - **ライブ未結線＝挙動不変**: actor 推定の engine 結線（D2）/ 派生 confidence 融合（D3）/ silent-fold
    合成は後続 PR（Phase F #8 の golden fixtures と併走）。ISSUE-0009 の初期方針を承認・記録。
  - tests: `tests/test_phase_d_corrections.py`（18, 修復表を網羅）。
    **全 222 passed, 10 skipped**（`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。

### Added (Phase B+C — イベント記録基盤, v1 リリーストラック R)

- **`AudioEvent` に `seat` / `confidence` を additive 追加**（v1 issue #6 / Epic #4, R3/R4 の前提）:
  - `core/events.py`: `AudioEvent.seat`（明示発話席）/ `AudioEvent.confidence`（Whisper 信頼度 [0,1]）を
    optional 追加。既存経路は未使用 = **挙動不変**。
  - `audio/recognizer.py`: `WhisperTranscriber.transcribe_with_confidence()` を追加（segment の
    `avg_logprob` 平均を `exp` で 0..1 に写像）。`transcribe()` は委譲。`parse_action(text, confidence=)`
    で confidence を受け、`_extract_seat_no()` で明示席（"シート3"/"seat 3"/全角）を populate。
  - `audio/recorder.py`: `_process_chunk` を `transcribe_with_confidence` 経由に変更し confidence を伝搬。
  - `output/event_recorder.py`: `event_to_envelope` の audio 分岐に `seat`/`confidence` を additive 露出
    （`reconstruction_event` schema は既に optional 定義済、code↔contract 緑）。
- **ISSUE-0010（記録境界・決定性）を Resolved**: 記録境界 = ASR decode 後（`seat`/`confidence` 含む）、
  clock 源 = 観測済み最大 event timestamp に確定。`docs/contracts/event-replay.md §4` を「決定」に更新。
  clock 注入・replayer・スレッド順序許容度の実証固定は Phase F（#8）の golden fixtures に委譲。
- tests: `tests/test_phase_bc_events.py`（10）+ `tests/test_event_recorder.py` 拡張。
  **全 204 passed, 10 skipped**（`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。

### Fixed (Phase A — コア堅牢化, v1 リリーストラック)

- **RFID カード未解決時にハンドを要レビュー化**（v1 issue #5 / Epic #4）: board / seat RFID
  イベントの `card` がカードマスター未解決（空文字）のままハンドが進んだ場合、その
  `HandSummary.review_required` を `True` にするようにした。従来は `logger.warning` のみで
  ハンドサマリーに反映されず、オペレーターが検出失敗に気付けなかった。
  - `integration/engine.py`: `IntegrationThread._hand_needs_review` フラグを additive 追加。
    card 未解決の board/seat 分岐で立て、`_start_new_hand` でリセット、`_finalize_hand` の
    `review_required` に OR 合成。解決済みカードでは立たない（誤検知ガード）。
  - tests: `tests/test_phase_a_hardening.py`（4）。**全 192 passed, 10 skipped**
    （`pytest tests/ -q --ignore=tests/test_vision.py`、skip は pokerkit 未導入分）。
  - 検証: faster-whisper 未導入時の起動は `audio/recognizer.py` の遅延 import（`__init__` の
    `try/except ImportError`）で既にクラッシュしないことを確認（コード変更不要）。

### Added (Phase R2 — pokerkit game-state backend, preview / default-off)

- **pokerkit を live ルール権威にした game-state backend**（ADR-0009, **default-off の preview**）:
  ノイジー入力からの正確な再構築のため、actor 順（ポジション順）/ 合法手集合 / amount_to_call / min-raise /
  **side-pot** を pokerkit に委ねる backend を追加。**既定 `legacy` で挙動不変**、`config.engine.backend=pokerkit`
  で opt-in。
  - `core/poker_engine.py`（新規）: `PokerEngine` Protocol（legacy/pokerkit 共通 I/F）＋ `PokerkitGameState`
    ＋ `create_game_state` factory。pokerkit は **遅延 import**（未導入でも legacy は動く）。announced winner を
    手動 push（pokerkit auto-showdown はダミーカードのため無効化）、side-pot スナップショット、seat↔index 固定。
  - `main.py`: `_make_game_state(cfg, ...)` で backend 選択（CLI/GUI 両経路）。`config_default.json` に
    `engine.backend: "legacy"` を追加。
  - **ISSUE-0008（Fixed）**: pokerkit 0.7.4 で必要 API（actor / 合法手 / min-raise / amount_to_call / side-pot の
    incremental 露出、不正額の `ValueError`、`HOLE_DEALING` でカード不要駆動）を spike で実機確認。ADR-0009 の
    gate 解除。**ADR-0009 を Accepted**（R2 engine 実装済 / R3 は planned）。
  - tests: `tests/test_poker_engine.py`（11: actor 順 / legal_context / street 自動進行 / 不正・非手番拒否 /
    side-pot / winner award / rebuy / **allin ショートスタック call-all-in**）。**全 198 passed**。
  - review fix: `apply_action("allin")` を「raise 可なら max へ raise、不可だが call 可なら call-all-in」に
    分離（レイズ不可なショートスタックの「オールイン」での pokerkit state desync を防止）。
  - 既知の差（legacy より正確側・preview）: ブラインド自動 post、合法手のみ受理（raw ASR の射影は R3）、
    street は betting 完了で自動進行。**live 既定動作（legacy）は不変**。

### Added (Phase R1 — event recording sidecar)

- **生センサーイベントの append-only sidecar 記録**（ADR-0010, record-only 先行実装）:
  `IntegrationThread` が**解釈する前**に各 `AudioEvent` / `RFIDEvent` / `CameraEvent` を
  `reconstruction_event` envelope（camera frame 除外）として `logs/{session_id}.events.jsonl` へ 1 行追記する。
  再構築ロジックは不変で、**recorder 未指定（既定）なら挙動完全不変**。
  - `output/event_recorder.py`（新規）: `EventRecorder` ＋ `event_to_envelope()`。append-only・スレッド安全・
    I/O 失敗で再構築を止めない。
  - `integration/engine.py`: `IntegrationThread(event_recorder=...)` を additive 追加。3 つの dequeue 点
    （audio get / camera drain / rfid drain）で解釈前に `_record()`。default None = 従来動作。
  - `main.py`: `config.recording.enabled`（既定 false, opt-in）で `EventRecorder` を構築し CLI / GUI 両経路で注入。
    `config_default.json` に `recording.enabled: false` を追加。
  - `docs/contracts/schemas/reconstruction_event.schema.json`（v0.1, `additionalProperties:false`）＋
    `fixtures/reconstruction_event/`（canonical / valid-* / invalid-*）。`tests/test_contracts.py` の `_MODELS` に登録。
  - tests: `tests/test_event_recorder.py`（envelope / JSONL / code↔contract）、
    `tests/test_integration_recording.py`（engine→recorder e2e / recorder 未指定で sidecar 無し）。
    **全 187 passed**（`pytest tests/ -q --ignore=tests/test_vision.py`）。
  - **ADR-0010** を Accepted に更新（R1 実装済。R4/R5 = hand/action freeze・replayer は planned）。

### Docs / Planning (Phase R0 — rules-aware reconstruction & contract-first hand core, 設計提案)

- **ハンド再構築エンジンと contract-first hand core の設計提案**（**docs-only, `.py` / schema / fixtures は
  未変更**）: 目的（ノイジーな ASR＋RFID からの正確な再構築）と思想（contract-first / fixtures-as-oracle）の
  両面のギャップに対し、再構築を「ルール制約付き状態推定」として捉え直し、既存依存 pokerkit を live
  ルール権威に据える方針を提案。
  - **ADR-0009** (Proposed): `pokerkit.State` を live ルール権威として採用し、その合法手制約で再構築する
    （ルール制約付き状態推定）。現 `GameStateManager` の安定 I/F 背後で `engine.backend` フラグ選択、raw ASR を
    直接流さない「境界での推定」、出力は additive。actor 推定（手番 prior × sensor ＋ silent-fold 自動合成）、
    `apply_corrections()`（合法手制約・call/check の状態一意化・amount スナップ）、派生 confidence（8 行固定
    テーブルの置換）と `needs_review` 条件の明文化。`pokerkit>=0.5.0` は宣言済みだが**未 import** である事実を
    明記（`game_state.py` の Phase 3 TODO の具体化）。当初の engine / algorithm 2 案を 1 ADR に統合。
  - **ADR-0010** (Proposed): hand core の contract 化（`hand`/`action`/`reconstruction_event`）と決定的
    record/replay（append-only event sidecar、注入クロック、golden fixtures を core の oracle に）。ADR-0008
    と整合し hand-logger immutability を維持。
  - `docs/contracts/hand-reconstruction.md`（新規 draft）: `PokerEngine` interface 草案 / actor 推定 /
    `apply_corrections` 修復表 / 派生 confidence / `hand`・`action` の inline schema sketch（freeze せず）。
  - `docs/contracts/event-replay.md`（新規 draft）: record/replay harness / 決定性条件 / `reconstruction_event`
    envelope sketch / golden-fixture レイアウトとテスト計画。
  - **ISSUE-0008**（Open）pokerkit online API 実現性（ADR-0009 の gate）/ **ISSUE-0009**（Open）actor 競合・
    silent-fold ポリシー / **ISSUE-0010**（Open）replay 決定性の記録境界 / **ISSUE-0011**（Open）hand/action
    schema freeze blockers（ISSUE-0005 の hand core 版）。
  - **decision-log.md** に ADR-0009/0010 と ISSUE-0008..0011 を登録。**CLAUDE.md** Future Scope に
    rules-aware reconstruction の planned/proposed 行を追加。
  - **実装は別タスク**（提案フェーズ R0）。段階導入順は R1 record-only → R2 pokerkit engine（flag）→
    R3 actor/corrections/fusion → R4 contracts → R5 freeze + session 統合。

### Docs / Planning (Phase S2.x — hand logger × session integration strategy)

- **Hand logger × session/seating integration の戦略 planning**（docs-only, code 未変更）:
  既存 hand logger world（`HandSummary` / `JsonWriter` / `PHHExporter` / `IntegrationThread` /
  `GameStateManager` / `main.py`）と S2 core（`SessionRepository`）の段階接続方針を確定。
  - `docs/contracts/hand-integration.md`（新規 draft）: 現状フロー整理 / 接続パターン A・B・C 比較 /
    推奨アーキテクチャ（Pattern A, write-through）/ player_id・session_id・hand_ref の決定タイミング /
    Phase 2.0〜2.4 → 3.x の段階 migration / HandSummary draft schema sketch / 互換ルール / open 論点。
  - `docs/contracts/session-seating.md` 更新: § freeze 状態 に ADR-0008 と hand-integration.md を相互リンク。
- **ADR-0008** (Accepted): Hand logger × session/seating integration strategy。
  Pattern A（write-through, additive）を採用。`HandSummary.players[i].player_id` を additive、
  `session_id` を session レイヤの UUID4 hex に切替（Phase 2.2）、PHH は無改変、`hand_ref` は
  session レイヤ側に住む、rollback path として `config.session_layer.enabled` フラグ planned、
  legacy logs/*.json は破壊しない。
- **ISSUE-0006**（新規 Open）: hand 開始時の seat→player_id 選択 UX が未確定。Phase 2.3 で確定。
- **ISSUE-0007**（新規 Open）: legacy hand log（timestamp session_id / player_id 無し）の取り込み
  方針が未確定。Phase 2.4 着手判断時に決める。
- **CLAUDE.md** 更新: Phase 2 セクションに「Phase 2.x（hand logger 接続, planning 済 / 実装 planned）」
  サブ節を追加。Pattern A / 細分 phase 2.1〜2.4 を記述。
- **decision-log.md** 更新: ADR-0008、ISSUE-0006、ISSUE-0007 を index に追加。ISSUE-0005 行に
  ADR-0008 リンクを追記。
- **本タスクで `.py` ファイルは変更していない**（planning-only ガード）。

### Added (Phase S2 — session + hand-based seating core)

- **Session & Seating core (S2)**: hand logger とは独立した session レイヤと hand-based
  seating の core 最小実装を追加（contract draft に対する実装。schema は未 freeze のまま）。
  - `core/session.py`: `Session` / `SeatAssignment` / `HandRef` データクラス。
  - `core/session_repository.py`: `SessionRepository`。create / list / get / close session、
    `assign_seat`（hand 単位の seat→player 割り当て）、`list_seat_assignments` /
    `resolve_seat_map_for_hand` / `resolve_hand_ref` / `current_seating`。
  - validation / errors: unknown session（`not_found`）/ already_closed / session_closed /
    seat_taken / **player_already_seated**（新 code）/ unknown_player / invalid_seat。
    `docs/contracts/error-shapes.md` の session セクションと 1:1。
  - 永続化: プロジェクト直下 `sessions.json`（アトミックリネーム、`.gitignore` 追加）。
    seat_assignment は session 配下に hand 単位で入れ子保持（将来 ledger を additive 拡張しやすい配置）。
  - 識別子: `session_id` は session レイヤが UUID4 hex で採番（hand logger の timestamp
    session_id とは別 namespace）。`hand_id` は int 据え置き（ADR-0006）。
  - **hand logger とは未接続**（`HandSummary` への player_id 接続 / reconciliation は S2 scope 外）。
- **ADR-0007** (Accepted): S2 session layer の永続形と `session_id` 採番方式の決定
  （独立採番 + 専用ストア = decoupled）。ISSUE-0005 #1 / #2 を core について確定。
- **ISSUE-0005** 更新: `session_id` 採番（#1）と seat_assignment 永続形（#2）を core について
  Resolved。hand logger 接続・seat change UI 要件は Open のまま（schema `1.0` freeze の残 blocker）。
- **Tests**: `tests/test_session_repository.py` を追加（session CRUD / persistence roundtrip /
  assign 成否 / 各 reject / resolve / code↔contract 整合）。
- **Docs**: `error-shapes.md`（session error を実装済に更新 + `player_already_seated` 追記）/
  `repository-interfaces.md` / `session-seating.md`（core 実装済を反映）/ `CLAUDE.md`
  （§ Session & Seating, 実装状況表, Phase 2 / freeze order）を更新。

### Docs / Planning (Phase 0b — S2 contracts)

- **S2 contract draft (session / seat_assignment / hand_ref)**: `docs/contracts/` に S2 の
  契約草案を追加（**未 freeze**, schema version 0.x）。
  - `session-seating.md`（モデル定義 / hand_id boundary / interface 草案 / freeze 状態・blockers）。
  - `schemas/session.schema.json` / `seat_assignment.schema.json` / `hand_ref.schema.json` と
    各 `fixtures/`（canonical / valid-minimal / invalid-*）。
  - `repository-interfaces.md` に session/seating の interface 草案を追記、`error-shapes.md` に
    S2 error code（`session_closed` / `seat_taken` / `unknown_player` / `invalid_seat` 等）を additive 追記。
  - `tests/test_contracts.py` の `_MODELS` に session / seat_assignment / hand_ref を登録（schema↔fixture 整合）。
- **ADR-0006** (Accepted): S2 session/seating contract boundary と hand_id の cross-app 参照。
  `hand_id` は session 内連番 int 据え置き、cross-app は `(session_id, hand_id)` 複合キー、
  参照単位は `hand_ref`（ISSUE-0004 の選択肢 A 採用）。
- **ISSUE-0004** → **Resolved**（ADR-0006）。**ISSUE-0005**（Open）: S2 freeze の未確定事項
  （`session_id` 採番方式 / seat_assignment 永続形 / seat change 表現）を登録。
- **shared-ids.md / versioning-and-freeze.md / README.md / CLAUDE.md**: hand_id reconcile 済・
  S2 draft 状況・freeze order #3 の状態を更新。
- **decision-log.md**: ADR-0006 / ISSUE-0005 を登録、ISSUE-0004 を Resolved に更新。

### Added

- **Player Registry (Phase S1)**: hand logger とは **別画面** の player 管理機能を追加。
  - `python main.py --players` で Player Registry 画面を起動（hand logger とは別起動）。
  - player の新規作成 / 一覧表示 / display_name リネーム。属性は `player_id`（UUID hex,
    永続・安定）+ `display_name` + `created_at`。
  - `players.json` への永続化（アプリ再起動を跨いで player_id が安定）。
  - validation: 空文字 / 前後空白のみ / 完全一致重複（前後空白除去後）を拒否。
  - 実装: `core/player.py`, `core/player_repository.py`, `gui/player_registry.py`。
  - hand logger とは未接続（session / ledger / settlement 接続は後続 Phase）。

### Docs / Planning

- **Contracts bootstrap (Phase 0a)**: `docs/contracts/` を新設し、contract-first 並行開発の
  単一 source を凍結。
  - `README.md` / `shared-ids.md` / `versioning-and-freeze.md` / `repository-interfaces.md` /
    `error-shapes.md` / `validation-rules.md`。
  - shared ID 契約: `player_id`（UUID4 hex, S1 確定）/ `session_id`（opaque string, S2 確定）/
    `hand_id`（現状 int, cross-app は (session_id, hand_id) 複合, S2 reconcile）。
  - `schemas/player.schema.json` (v1.0) + `schemas/shared-ids.schema.json` と、
    `fixtures/player/`（canonical / valid / invalid）。
  - freeze 定義・versioning（additive vs breaking）・drift detection の最小方針を文書化。
  - `tests/test_contracts.py`（schema 妥当性 / fixtures 整合 / code↔contract）を追加。
    `requirements.txt` に `jsonschema>=4.0.0` を追加。
- **ADR-0005** (Accepted): contracts repository layout & freeze workflow（ADR-0004 の具体化）。
- **Issue 0004** (Open): `hand_id` が int（hand logger）と cross-app 文字列契約で不整合。
  S2 の `hand_ref` で reconcile。
- **Issue 0003**: Phase 0a で部分緩和（contracts bootstrap + 最小 contract test）。
- **decision-log.md**: ADR-0005 / ISSUE-0004 を登録。
- **CLAUDE.md**: ディレクトリ構成に `docs/contracts/` を追加、Parallel development plan に
  契約 source 参照と Phase 0a 完了状況を追記。

- **Parallel development plan**: `CLAUDE.md` に `# Parallel development plan` 節を追加。
  4 workstream（WS0 contract / WS1 core / WS2 desktop / WS3 mobile）の依存関係、
  parallelizable / blocker、contract freeze order、mobile が mock で先行できる範囲、
  desktop / mobile 責務分離、将来 API/sync を入れても壊れにくい境界、phase 0–5 の
  構造化計画（goal / prerequisites / parallel tasks / blockers / done criteria）を明文化。
- **ADR-0004** (Accepted): contract-first parallel development / shared IDs
  (`player_id` / `session_id` / `hand_id`) / separate front-ends の判断。Alternatives
  （core-first 逐次 / front-end-owned logic / implementation-first 暗黙契約 /
  mobile = hand logger 移植）を却下。
- **Issue 0003** (Open): 並行開発の contract drift / 凍結タイミング / mock 乖離の
  blocking risk を登録（ISSUE-0001 が S3 ledger WS の直接 gate）。
- **decision-log.md**: ADR Index に ADR-0004、Major Issue Index に ISSUE-0003 を登録。
- 提案: mobile は **React Native**（iOS / Android 両対応のたたき台、最初は mock repository）を
  技術選定案とし、初期 screen skeleton は player registry の list / add / rename に限定。

### Docs / Spec

- **Bootstrap docs-as-code structure**: `docs/adr/`, `docs/issues/`, `docs/worklog/`,
  `docs/templates/`, `docs/decision-log.md` を新設。`CLAUDE.md` / `CHANGELOG.md` を
  本リポジトリに追加した。
- **CLAUDE.md**: 現時点の正仕様（hand logger Phase 7 まで）を記述するとともに、
  `Future Scope` セクションで **player registry / session ledger / point ledger /
  session settlement / cross-app boundary** を planned scope として明文化。
  `Documentation and Traceability Rules` を恒常ルールとして追加。
- **ADR-0003** (Accepted): hand logging 単体モデルから session ledger / store settlement /
  point ledger へドメインを拡張する判断と、Alternatives（HandSummary 埋め込み /
  後付け JSON / player-to-player settlement / session 単位 seat_assignment）を記録。
- **Issue 0001** (Open): point ledger の残高計算 source of truth が未確定であることを
  open question として登録（S3 着手前に解決必要）。
- **Issue 0002** (Open): player の display_name uniqueness 仕様（大文字小文字 / 全半角の
  同一視）の将来拡張を open question として登録。
- **CLAUDE.md**: § Player Registry (S1, 実装済) を追加し、future scope 表・Phase candidates・
  実装状況表を S1 実装に合わせて更新。
- **decision-log.md**: ADR Index に ADR-0003、Major Issue Index に ISSUE-0001 / ISSUE-0002
  を登録。

### Notes

- S1 は ADR-0003 の player モデル定義の最小実装。別画面分離は ADR-0003 の cross-app boundary
  方針の帰結であり、新規 ADR は起こさず worklog / decision-log に記録した。
- 次フェーズ候補: S2 (session + hand-based seating) → S3 (ledger entries + point ledger) →
  S4 (session settlement + paid/unpaid) → S5 (cross-app contract).
