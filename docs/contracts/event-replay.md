# Deterministic record / replay and golden fixtures (design draft)

> **Status: draft / 設計フェーズ**（planning 専用, コード未変更）。本 doc は ADR-0011（hand core の
> contract 化と決定的 record/replay）の **設計詳細**。`reconstruction_event` envelope の inline schema
> sketch を含む。再構築アルゴリズム本体は `hand-reconstruction.md`（ADR-0009/0010）。
>
> 関連: ADR-0011 / ADR-0008（sidecar 非破壊の前例）/ ISSUE-0010（記録境界・決定性）。

## 1. なぜ record/replay か

hand core の入力は **ノイジーな realtime センサー列**（マイク ASR ＋ RFID ＋ camera）で、`IntegrationThread`
の挙動を**オフラインで再現する手段が無い**。そのため ADR-0009/0010 の推定アルゴリズムを ground truth に対して
反復・回帰固定できない。生イベントを**契約化して記録**し、それを**決定的に replay**できれば、心臓部に初めて
fixtures-as-oracle（他層が既に持つ規律）が付く。

## 2. record（append-only event sidecar）

`IntegrationThread` の入口で、**解釈する前**に各イベントを `reconstruction_event` の JSON Lines として
`logs/{session_id}.events.jsonl` へ追記する。

- **非破壊・挙動不変**: 記録は解釈ゼロの純 I/O。`logs/*.json` / PHH には触れない sidecar（ADR-0008 の
  legacy sidecar と同じ非破壊方針）。記録の有無で `logs/*.json` はバイト一致でなければならない。
- **先行 ship 可能**: 推定の変更を伴わないため、R1 で単独導入できる（実センサー列の蓄積を即開始）。
- **`.gitignore`**: `logs/` と同様にプロジェクト方針へ従う（実データは commit しない。golden fixtures だけ
  `tests/fixtures/` に curated して置く, §5）。
- **camera frame は記録しない**: `CameraEvent.frame`（`core/events.py`, `repr=False`）は重く非決定的なので
  envelope から除外し、`seat` と `timestamp` のみ残す。

## 3. `reconstruction_event` envelope schema sketch

`type` で判別する discriminated union。我々が新設する surface なので `additionalProperties: false` で締める
（replay 決定性の要）。`core/events.py` の 3 データクラスを直列化したもの（`frame` 除外）。

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://pokerapp.local/contracts/reconstruction_event.schema.json",
  "title": "ReconstructionEvent", "version": "0.1",
  "type": "object", "additionalProperties": false,
  "required": ["type", "timestamp"],
  "properties": {
    "type":      {"enum": ["audio", "rfid", "camera"]},
    "timestamp": {"type": "number", "description": "time.time() 由来の単調な秒。replay の clock 源（§4）"}
  },
  "allOf": [
    {"if": {"properties": {"type": {"const": "audio"}}},
     "then": {"required": ["action","amount","raw_text"],
       "properties": {
         "action":  {"type":"string"},
         "amount":  {"type":"integer","minimum":0},
         "raw_text":{"type":"string"},
         "seat":    {"type":["integer","null"],"minimum":1,"maximum":9},  // ADR-0010 additive
         "confidence":{"type":["number","null"],"minimum":0,"maximum":1}  // Whisper logprob, additive
       }}},
    {"if": {"properties": {"type": {"const": "rfid"}}},
     "then": {"required": ["tag_id","card","reader_id","role"],
       "properties": {
         "tag_id":{"type":"string"}, "card":{"type":"string"},
         "reader_id":{"type":"string"}, "role":{"enum":["seat","board"]},
         "seat":{"type":["integer","null"]}, "board_index":{"type":["integer","null"]},
         "raw_tag_id":{"type":["string","null"]}
       }}},
    {"if": {"properties": {"type": {"const": "camera"}}},
     "then": {"required": ["seat"], "properties": {"seat":{"type":"integer"}}}}
  ]
}
```

## 4. replay（決定的 re-runner）と決定性の条件

`tools/replay_hand.py`（planned）が `*.events.jsonl` を読み、live と **同一の**再構築ロジックへイベントを
注入して `HandSummary` を再生成する。`replay(record(stream)) == live 結果` を保証するための条件:

1. **timestamp は event 由来**。engine に **clock source を注入**する:
   - live = 実時計（現 `engine.py:429` `_now_iso()` / `:261` `time.time()`）。
   - replay = **観測済み最大 event timestamp**。`MATCH_WINDOW` / `CAMERA_BUFFER_TTL`（`engine.py:43-44`,
     `:260-286`）は event time 上で動かす（live/offline で同一挙動）。
2. **スレッド/キュー競合に依存しない**。replay は event を **timestamp 昇順**で同一 drain ロジック
   （`_drain_camera_queue` / `_drain_rfid_queue` 相当）へ通す。live のキュー順序は「概ね timestamp 順」で
   あり、その近似誤差自体が fidelity risk として **ISSUE-0010** の対象。
3. **RNG / 隠れグローバルに依存しない**。カードは RFID 由来か未知 `????`。乱数で配らない（pokerkit State も
   action 列で完全決定）。

> **記録境界の open question（ISSUE-0010）**: 決定的にするには **ASR decode 後の `action/amount/raw_text/
> confidence` を記録**するのが素直（Whisper を再実行しないので決定的）。ただしこれは ASR モデル自体の
> 改善を offline で測れない。raw audio を別途保存すれば re-ASR できるが重く、whisper バージョン間で
> 非決定的。本提案は **decode 後を記録（決定的 replay）を既定**とし、raw audio 保存は任意の上位レイヤとする。

## 5. golden fixtures（core の oracle）

```
tests/fixtures/reconstruction/<case>/
  ├── events.jsonl        ← reconstruction_event 列（§3 schema 準拠）
  └── expected_hand.json  ← 期待 HandSummary.to_dict()（hand schema, hand-reconstruction.md §7 で検証）
```

`tests/test_reconstruction.py`（`tests/test_contracts.py` 流）が各 case で replayer を回し、出力
`HandSummary.to_dict()` を `expected_hand.json` と突き合わせ、`hand` schema で検証する。**既知バグをそのまま
回帰ケース**に採る（ADR-0010 由来）:

| case | 入力の要点 | 期待（現状バグ → 修正後） |
|---|---|---|
| `check-facing-bet` | ベットに直面して "チェック" | check 非合法 → call/fold へ修復（§ apply_corrections） |
| `call-amount-from-state` | "コール 500"（実 call 額 300） | heard 500 無視 → engine の 300 |
| `silent-fold` | 手番をまたいで次席が行動 | ラウンドロビン誤帰属 → 間の席を fold 合成し actor 正 |
| `unequal-allin` | スタック差のある all-in | 素朴総和 pot → main/side pot 正 |
| `out-of-turn-rfid` | RFID seat が prior と不一致 | prior 固定 → 物理証拠優先で actor 訂正 |

さらに **code↔contract** テスト: `HandSummary(...).to_dict()` / `ActionRecord(...).to_dict()` が
`hand` / `action` schema を通ること（`test_contracts.py:65` `test_core_player_matches_contract` の前例）。
`tests/test_contracts.py` の `_MODELS` に `hand` / `action` / `reconstruction_event` を追加する。

## 6. session 層（ADR-0008）との整合

- event sidecar / replay 出力は `(session_id, hand_id)` 複合キー（ADR-0006）でアドレスでき、live hand と
  同一の cross-app 参照形になる。
- `logs/*.json` / PHH は immutable のまま（ADR-0008 §8.4）。replay は記録列に対する read-only で、出力は
  test/scratch にのみ書き、canonical log を上書きしない。
- `hand` schema が ADR-0008 の `players[i].player_id` と ADR-0009 の `pots`/`committed` の共通受け皿
  （`hand-reconstruction.md` §7）。

## 7. open 論点（→ issues）

- **ISSUE-0010**: 記録境界（decode 後 vs raw audio）と、live スレッド順序 ≈ timestamp 順の許容度。
- **ISSUE-0011**: `hand` / `action` の `additionalProperties:false` 昇格と必須/optional 確定（`reconstruction_event`
  は本 doc で `false` 寄り）。

## 8. 参照

- ADR-0011（contract・replay）/ ADR-0009 / ADR-0010（再構築）/ ADR-0008（sidecar 非破壊）
- `docs/contracts/hand-reconstruction.md`（推定・融合・`hand`/`action` sketch）
- `core/events.py`（3 イベント型 = envelope の素, `frame` 除外）/ `integration/engine.py`（記録境界・clock 注入・
  drain 順序）/ `core/hand_log.py`（`HandSummary`/`ActionRecord`）/ `output/json_writer.py`（immutable）
- `tests/test_contracts.py`（`_MODELS` 拡張先）
