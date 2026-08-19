# Hand reconstruction engine — rules-constrained state estimation (design draft)

> **Status: draft / 設計フェーズ**（planning 専用, コード未変更）。本 doc は ADR-0009（pokerkit を live
> ルール権威に ＋ 状態推定・融合）の **設計詳細**で、`hand-integration.md` が ADR-0008 の companion で
> あるのと同じ位置づけ。`hand`/`action` の inline schema sketch を含むが **freeze しない**（実ファイル化・
> freeze は ADR-0010 の R4 / ISSUE-0011）。record/replay は `event-replay.md`（ADR-0010）。
>
> 関連: ADR-0009 / ADR-0010 / ISSUE-0008（pokerkit API）/ ISSUE-0009（actor / silent-fold）。

## 1. 再構築の再定義（data flow）

現状は「ASR を parse して append」「RFID を信頼度に足す」という**素通しパイプライン**で、ポーカールールが
どこにも無い。再構築を **「各時点で合法な状態の中から、センサー証拠が最も支持するものを選ぶ」状態推定**
として捉え直す。

```
マイク ──► AudioThread ──► AudioEvent(action, amount, raw_text, conf, seat?) ─┐
RFID  ──► RFIDThread  ──► RFIDEvent(card, seat?, board_index?)               ─┤
camera──► CameraThread──► CameraEvent(seat)                                   ─┤
                                                                              ▼
                                              IntegrationThread（境界での推定）
                            ┌──────────────────────────────────────────────────────┐
                            │ 0. record envelope → logs/{sid}.events.jsonl  (ADR-0010)│
                            │ 1. legal_ctx = engine.legal_context()                   │
                            │      (actor prior / 合法手 / amount_to_call / min_raise) │
                            │ 2. actor = resolve_actor(ev, legal_ctx, rfid, cam)  §4   │
                            │ 3. corrected = apply_corrections(parse(ev), legal_ctx,   │
                            │                                  ev.conf)            §5   │
                            │ 4. legal_op = project_to_legal(corrected, legal_ctx)     │
                            │ 5. ok = engine.apply(actor, legal_op)  → pokerkit.State  │
                            │ 6. conf, review = fuse(L=ok, A=…, Q=…)              §6   │
                            └──────────────────────────────────────────────────────┘
                                                                              ▼
                       ActionRecord(seat=actor, action, amount, …, confidence, needs_review)
                                  HandSummary(… + pots, committed, …)  §7
                                                                              ▼
                       json_writer → logs/{sid}.json   PHHExporter → {hand}.phh （additive / 無改変）
```

**権威の所在**: `pokerkit.State`（ADR-0009）。**ノイズの隔離**: raw ASR は直接 State に入れず、合法手へ
射影してから入れる（手順 1–5 の「境界での推定」）。State は常に内部整合（side-pot / min-raise）を保つ。

## 2. `PokerEngine` interface 草案（安定境界）

`integration/engine.py` / `main.py` が現在 `GameStateManager` に対して呼ぶ公開 I/F を **そのまま** Protocol 化し、
`pokerkit.State` 実装と legacy 実装を差し替え可能にする（ADR-0009）。`config.engine.backend` で選択。
**R2 実装済**: `core/poker_engine.py`（`PokerEngine` Protocol / `PokerkitGameState` / `create_game_state`）, default-off。
**D0 実装済（#7）**: 下表「新規メソッド」を Protocol に追加し `fold_through` を `PokerkitGameState` に実装、
legacy に rules-aware でない stub を追加。`LegalContext` は `core/engine_types.py` へ移設（後方互換 re-export）。

| 既存メソッド（不変） | 役割 |
|---|---|
| `new_hand() -> int` / `advance_street(Street)` / `end_hand(winner_seat)` | ハンド・ストリート管理 |
| `apply_action(seat, action, amount)` | アクション適用（内部で turn 進行） |
| `get_current_player() -> int` / `advance_turn() -> int` | 手番（pokerkit は actor_index 由来） |
| `hand_id` / `street` / `pot` / `get_stack(s)` / `get_player_name` / `get_active_seats` | 照会 |
| `update_stack` / `rebuy` | 手動修正 |
| （`engine.py:409` が読む `_sb` / `_bb`） | blind 参照（property で露出維持） |

| 新規メソッド（additive, 推定が読む） | 役割 |
|---|---|
| `legal_context() -> LegalContext` | `{actor_seat, legal_actions:set, amount_to_call:int, min_raise:int, max_raise:int}` |
| `is_legal_actor(seat) -> bool` | その席が今合法に行動できるか（silent-fold 合成の判定, §4） |
| `fold_through(until_seat)` | prior〜until の間の席を fold 合成して同期（§4, additive helper） |
| `pots() -> list[Pot]` | main/side pot（`hand.pots`, §7） |
| `committed(seat) -> int` | 当該ストリートのコミット額（`apply_corrections` の amount 判定） |

> pokerkit がこれらを incremental に露出できるかは **ISSUE-0008** で spike 検証。露出が薄い項目は engine
> 実装内の shadow tracker（committed / amount_to_call 等）で補完してよい（interface は不変）。

## 3. seat ↔ pokerkit index 写像

pokerkit は player を 0..n-1 の連番で扱い、ドメインは疎な `seat_no`（1..9）。`new_hand()` で **BTN 基準の
安定全単射**を固定し、hand 内で不変にする。`output/phh_exporter.py:119` の `seat_to_idx`（players_info 順の
flatten）を**正式な単一実装に格上げ**し、engine と PHH が同じ写像を共有する（PHH の seat 順依存バグも同時に解消）。

## 4. アクター推定アルゴリズム（ADR-0009 §6 の詳細）

> **実装状況: D2b（#7）で silent-fold 合成まで結線済** — `integration/engine.py` の
> `_handle_rules_aware_action` / `_resolve_actor` が rules-aware backend で `apply_corrections` をライブ
> 適用し、actor を **RFID seat 読み > 明示発話席(`event.seat`)** の優先順位で推定。prior と異なれば
> `fold_through(sensed, max_folds=SILENT_FOLD_CAP=2)`（atomic）で中間席を **silent fold 合成**し actor を
> 進める。cap 超過/到達不可は prior 維持。合成 fold は `_append_synth_fold` で fold アクションとして記録
> （`confidence=0.3`・常に `needs_review`）。actor 推定に使った RFID 読みは消費し（滞留防止）、最終 actor
> と一致すれば corroboration に再利用。golden fixtures `silent-fold` / `out-of-turn-rfid`（Phase F #8）で
> 検証済。**残**: camera 源・派生 confidence の重み較正（D3）。legacy は空 legal_context で従来経路。

**入力**: prior（engine の `actor_seat`）＋観測（同窓内 RFID seat read / audio 明示 seat / camera seat）。
audio 明示 seat は `engine.py:433` `_extract_seat_from_text`（現状 winner 専用）を全 action へ一般化して得る。

```
P  = legal_ctx.actor_seat                      # ルール上の手番（prior, 強い）
S_rfid = nearest RFID seat read in window      # 物理証拠（最強）   _pop_matching_rfid_event
S_aud  = seat parsed from raw_text (シートN)     # 明示発話（強）
S_cam  = camera seat in window                 # 弱い

if すべての存在ソースが席 X で一致:
    actor = X                                  # 高 agreement（§6 の A=1）
else:                                          # 不一致
    actor = first_present([S_rfid, S_aud, S_cam, P])   # 物理/明示 > prior
    if actor != P:
        if engine.is_legal_actor(actor):
            engine.fold_through(until=actor)    # P〜actor 間の席を silent fold 合成
            #   ↑ ディーラー未宣言の fold（最頻の現実のズレ）を初めて正しく処理
        else:
            actor = P                           # ルール上不可能 → prior 維持
            needs_review = True
```

**根拠**: prior は「プレイが手番どおり進む」モデル期待で、まさに我々が捕えたいケース（out-of-turn、未宣言
fold）で破れる。RFID-at-seat / 明示発話 seat は**現実の直接観測**なので prior より優先する。ただし engine が
「その席は今行動できない」と言うなら（合法手番でない）prior を信じて `needs_review` を立てる。silent-fold の
合成は誤 fold を作る危険があるため、適用条件・件数上限は **ISSUE-0009** で確定する。

確定 actor は従来どおり `ActionRecord.seat` に載る（下流不変）。`AudioEvent` に optional `seat`（default None,
additive）を足し、recognizer が明示 seat を見つけたら埋める。

## 5. `apply_corrections()` 設計（ADR-0009 §7 の詳細）

> **実装状況: Phase D part 1（#7）で実装済** — `audio/recognizer.py` の `apply_corrections(action, amount,
> ctx, whisper_conf) -> Correction`（純関数・pokerkit 非依存、`tests/test_phase_d_corrections.py`）。
> 下表の射影と call/check の状態一意化を実装。engine への結線（actor 推定 = D2）も実装済。
>
> **ADR-0047/0049 での強化（2026-08-19, `tests/test_reconstruction_hardening.py`）**:
> `LegalContext` に `bb` / `committed` を additive 追加し、
> ① 金額 snap の review 閾値を同次元比較 **`gap >= bb`** に（bb=0 は従来 `gap > m` fallback）
> ② **bb 倍数への round 寄せ**（合法レンジ内のみ, `reason="rounded_to_bb"`）
> ③ raise の **to/by 曖昧性**（to 解釈が非合法だが by 解釈なら合法 → `raise_to_vs_by_ambiguous` + review）
> ④ **高信頼 ASR（>= HIGH_CONF_ASR=0.85）× 射影で action 変化 → review**（`high_conf_asr_projection`）。
> reason は "+" 区切りで複合し、ActionRecord.reason に配線される（G2）。

純関数 `apply_corrections(parsed, legal_ctx, whisper_conf) -> Corrected`。`audio/recognizer.py` に置くが
ゲーム状態を持たず、engine が `legal_ctx` を渡して呼ぶ（recognizer をゲーム状態から疎結合に保つ）。

**修復表**（`amount_to_call = c`, `min_raise = m`, `stack = s`, heard 額 = `a`）:

| ASR action | legal_ctx 条件 | corrected | amount | needs_review |
|---|---|---|---|---|
| check | `c == 0` | check | 0 | — |
| check | `c > 0`（非合法） | call または fold（尤度） | `c` or 0 | 曖昧なら yes |
| call | `c > 0` | call | **`c`**（heard 無視） | — |
| call | `c == 0` | check（実質チェック） | 0 | flag |
| bet | 当ストリート未ベット | bet | `snap(a, [m..s])` | snap 大なら flag |
| bet | 既にベットあり | **raise** に再マップ | `snap(a, [m..s])` | flag |
| raise | raise 合法 | raise | `snap(a, [m..s])` | snap 大なら flag |
| raise | raise 非合法（call のみ） | call | `c` | yes |
| allin | 常に | bet/raise/call（all-in） | `s` | side-pot 反映 |
| fold | 常に | fold | 0 | — |

- **call/check の一意化は状態から決定的**（`c>0→call`, `c==0→check`）。JA キーワードの曖昧さに依存しない。
  これが PHH/JSON で call と check を初めて区別できるようにする核心（現 `phh_exporter.py:174` は両方 "cc"）。
- **`snap()`**: heard 額を合法レンジへ丸め、さらにブラインド単位の round number へ寄せる
  （`recognizer.py` の kanji 桁優先ロジックの後段）。レンジ外への大幅 snap は flag。
- **`whisper_conf`**: 低信頼ほど state からの上書きを許す。**高信頼 ASR が非合法**なときが最強の
  `needs_review` トリガ（モデルと規則が確信を持って食い違う＝人が見るべき）。Whisper per-segment logprob を
  `AudioEvent.confidence`（additive）として運ぶ（現 `recognizer.py:218` は破棄している）。

## 6. 派生 confidence モデル（ADR-0009 §8 の詳細）

> **実装状況: D3（#7）実装済** — `integration/engine.py:derive_confidence`（3 因子 L/A/Q）を rules-aware
> 経路に結線（legacy の固定 8 行 `calc_confidence` は不変）。needs_review 5 条件も `_handle_rules_aware_action`
> で明文化（⑤ `REVIEW_THRESHOLD`）。重み（`_CONF_W_A/_CONF_W_Q/_CONF_BASE/_CONF_L_PENALTY/REVIEW_THRESHOLD`）
> は暫定で、golden fixtures / Phase F で較正。これで **Phase D 完了**（残 R: F3 の side-pot/freeze）。

固定 8 行テーブル（`engine.py` `calc_confidence`）を rules-aware では廃し、**解釈可能な 3 因子の合成**にする:

```
L = 1.0 if pokerkit が action を受理 else penalty        # 合法性ゲート（新・最重要）
A = （actor と action で一致した「存在ソース」数） / （存在ソース数）   # 合意度（有無 → 一致）
Q = base_weight(RFID>audio>camera) × whisper_conf          # ソース品質
confidence = clamp(w_L·L · (w_A·A + w_Q·Q), 0, 1)
```

- 重みは「全ソース一致・合法」のとき現 `calc_confidence` 値（RFID+audio=0.95 等）に近づくよう較正し、円滑移行。
- 既存 `calc_confidence` / 照合窓 / board-position 累積は捨てず、`Q` の base_weight と窓内一致判定の**下位入力**
  として再利用する。
- **`needs_review = True` 条件**（明文化）: ① pokerkit 拒否（hard 非合法） ② 高信頼 ASR と規則の矛盾
  ③ actor で prior が sensor を上書き（§4 else 枝） ④ amount を許容超過 snap ⑤ `confidence < review_threshold`
  （config）。これにより `HandSummary.review_required`（現状「どれか needs_review」だけ）が**監査可能な意味**を持つ。

## 7. `hand` / `action` schema（**freeze 済, F3b / ISSUE-0011 Fixed**）

> **実装状況: F3b（#8）で `1.0` freeze 済** — 実ファイル `docs/contracts/schemas/{hand,action}.schema.json`
> （`additionalProperties:true` で 1.0、ISSUE-0011 の決定）。`test_contracts.py` の `_MODELS` に登録し、
> code↔contract（`test_core_hand_action_match_contract`）+ golden→schema
> （`test_golden_output_conforms_to_hand_action_schema`）で drift 検知。required は安定 core のみ（`pots` /
> `player_id` / `committed` 等の additive は optional）。以下は設計時の sketch（実ファイルが正）。

ADR-0008 §8.1 の HandSummary draft sketch を出発点に、ADR-0009 の additive フィールドを足した sketch。
`additionalProperties: true`（既存出力に多数フィールドがあるため）。

```jsonc
// action（= ActionRecord, additive）
{
  "title": "Action", "version": "0.x-draft-sketch",
  "type": "object", "additionalProperties": true,
  "required": ["hand_id","timestamp","street","seat","player_name","action","amount",
               "pot_after","stack_after","source","needs_review","confidence"],
  "properties": {
    "action": {"enum": ["bet","call","raise","check","fold","allin","showdown","winner","new_hand"]},
    "source": {"type":"object","properties":{"camera":{"type":"boolean"},
               "audio":{"type":"boolean"},"rfid":{"type":"boolean"}}},
    "confidence": {"type":"number","minimum":0,"maximum":1},
    // ── additive（ADR-0009 の推定が出力, 監査用, optional）──
    "legal_actions":  {"type":"array","items":{"type":"string"}},
    "amount_to_call": {"type":"integer","minimum":0},
    "corrected_from": {"type":["string","null"],"description":"修復前の生 ASR action"},
    "actor_source":   {"type":["string","null"],"enum":["turn","rfid","audio","camera",null]},
    "asr_confidence": {"type":["number","null"],"minimum":0,"maximum":1}
  }
}
```

```jsonc
// hand（= HandSummary, ADR-0008 sketch + ADR-0009 additive）
{
  "title": "Hand", "version": "0.x-draft-sketch",
  "type": "object", "additionalProperties": true,
  "required": ["hand_id","session_id","started_at","ended_at","players","actions"],
  "properties": {
    "session_id": {"type":"string","minLength":1},   // ADR-0008: UUID4 hex 推奨, legacy 文字列も valid
    "players": {"type":"array","items":{"type":"object","additionalProperties":true,
      "required":["seat","name"],
      "properties":{
        "seat":{"type":"integer","minimum":1,"maximum":9},
        "name":{"type":"string"},
        "player_id":{"type":["string","null"],"pattern":"^[0-9a-f]{32}$"},  // ADR-0008 additive
        "committed":{"type":["integer","null"],"minimum":0}                 // ADR-0009 additive
      }}},
    // ── additive（ADR-0009）──
    "pots": {"type":"array","items":{"type":"object",
             "properties":{"amount":{"type":"integer"},"eligible_seats":{"type":"array"}}}},
    "actions": {"type":"array"}
  }
}
```

`hand` は ADR-0008 の `players[i].player_id` と本提案の `pots`/`committed` の **共通の additive 受け皿**。
PHH は無改変（player_id を載せない, ADR-0008 §8.3）。

## 8. open 論点（→ issues）

- **ISSUE-0008**: pokerkit `>=0.5.0` の online API（actor / legal-actions / min-raise / amount-to-call /
  side-pot の incremental 露出）の実現性。不可なら shadow tracker で補完。**ADR-0009 の gate**。
- **ISSUE-0009**: §4 の競合解決と silent-fold 自動合成の適用条件・件数上限・`needs_review` 閾値、§6 の重み較正。
- `apply_corrections` の尤度（check→call/fold の判定）に何を使うか（chip-motion / amount 有無）の確定。
  **D1 暫定**: ベットに直面した "check" は **call + `needs_review`**（プレイヤーを勝手に外さない側）。
  chip-motion 等の尤度導入は後続（ISSUE-0009）。

## 9. 参照

- ADR-0009（pokerkit live 権威 ＋ 推定・融合）/ ADR-0010（contract・record/replay）
- `docs/contracts/event-replay.md`（record/replay harness, `reconstruction_event`）
- `docs/contracts/hand-integration.md`（ADR-0008, HandSummary draft sketch の出発点）
- `core/game_state.py`（差し替え対象の安定 façade）/ `integration/engine.py`（推定の置き場）/
  `audio/recognizer.py`（`apply_corrections`）/ `core/events.py`（`AudioEvent.seat?/confidence?`）/
  `core/hand_log.py`（`hand`/`action` の基）/ `output/phh_exporter.py`（seat→index, call/check, side-pot）
