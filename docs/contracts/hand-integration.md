# Hand logger × Session/Seating integration (Phase 2.x draft)

> **Status: draft / 設計フェーズ**。本 doc は **planning 専用**（コードは未変更）。
> S2 core（`core/session*.py`）と既存 hand logger world（`core/hand_log.py` / `output/json_writer.py` /
> `output/phh_exporter.py` / `integration/engine.py` / `main.py`）の段階接続方針を確定するための
> 設計 doc。具体的な実装契約への freeze は ADR-0008 を Accepted にして以降の Phase 2.x で行う。
>
> 関連: ADR-0006（S2 contract）/ ADR-0007（S2 core 永続形）/ ADR-0008（本接続戦略）/
> ISSUE-0005（S2 freeze blockers）/ ISSUE-0006（seating UX）/ ISSUE-0007（legacy log migration）。

## 1. 既存 hand logger world の現状

実装ファイルから読み取れるデータフロー（読み取り専用調査の結果）:

```
main.py
  └── session_id = "%Y-%m-%d_%H%M%S_session1"   ← timestamp 文字列, 1 プロセス 1 ファイル
  └── JsonWriter(log_dir, session_id)            ← logs/{session_id}.json を所有
  └── GameStateManager(players=[PlayerState(seat,name,stack), ...], sb, bb)
  └── IntegrationThread(json_writer, game_state, ...)
        ├── _start_new_hand     → _hand_started_at, _stack_start, _hole_cards={}
        ├── _record_action       → ActionRecord (seat, player_name, ...)
        └── _finalize_hand       → HandSummary(
                hand_id = gs.hand_id (int, session 内連番),
                session_id = json_writer._session_id (timestamp 文字列),
                players = [{seat, name, hole_cards, stack_start, stack_end, result}, ...],
                ...
              )
              → JsonWriter.append_hand_summary → logs/{session_id}.json

output/phh_exporter.py
  └── HandSummary → {hand_id:04d}.phh (TOML, players[i] = name string のみ)
```

要点:

- **player_id は存在しない**。`HandSummary.players[i]` は `name`（人間表示名）止まり。registry の
  `player_id` とは未接続（ISSUE-0005 #2）。
- **session_id は timestamp 文字列**（hand logger 採番）。session レイヤの UUID4 hex とは別 namespace（ADR-0007）。
- **hand_id は session 内連番 int**（ADR-0006 で据え置き確定）。
- **seat→player の対応は GameStateManager にのみ**（`PlayerState.name`）。hand 横断の安定参照は無い。
- **session 的な概念は "1 プロセス = 1 logs/{session_id}.json"** のみ。途中での seat change / 入退場の
  履歴を持たない（mid-session seat change は ISSUE-0005 #3）。
- PHH は player 名のみで出力（external interchange）。

## 2. 目標 (S2.x 完了時点)

- hand logger の出力（`HandSummary` / `logs/*.json`）に **`player_id` と session レイヤの `session_id`
  （UUID4 hex）を additive に通す**。past log は破壊しない。
- 各 hand finalize 時に **`(session_id, hand_id)` が SessionRepository でも参照可能**（`assign_seat`
  が hand 開始時点に呼ばれ、`resolve_hand_ref` が読める）。
- PHH ファイルは external interchange のため **player_id は載せない**（player 名のみ維持）。
- 既存の hand logger 単独運用（session layer 無効化）も `config` フラグで継続可能（rollback path）。

## 3. 接続パターンの比較

| パターン | 概要 | Pros | Cons | 採否 |
|---------|------|------|------|------|
| **A. write-through (hand logger → SessionRepository)** | hand logger 側が `session_id` / `player_id` を保持し、hand 開始時に SessionRepository.assign_seat を呼び、finalize 時に HandSummary に session_id / player_id を埋め込む | 既存 JSON が additive で済む。書き込み起点が 1 つで race が無い。S2 core が既に持つ contract をそのまま使える | hand logger に SessionRepository への依存が入る（DI で吸収可能）。seat 選択 UI が必要（ISSUE-0006） | **採用**（ADR-0008） |
| B. read-side reconciler (SessionRepository → hand logger 出力を読む) | SessionRepository が logs/*.json を後追いで読み、外側で session/hand を紐づける | hand logger は完全に無改修 | 元データに player_id が無いため fuzzy name match に依存、人間が確認する必要。race / 順序 / 削除に弱い | 補助手段（ISSUE-0007 の legacy 取り込み）として一部採用 |
| C. 共通 third store (event-sourced) | hand logger も session layer も同じ event log に書き、両者が view を組む | 将来的に最も拡張性が高い | S2 の規模に対して overkill。複雑度・実装量が S5 並 | **不採用**（S5 で再検討） |

## 4. 推奨アーキテクチャ — Pattern A (write-through, decoupled-but-aware)

```
operator                main.py / GUI
   │                      │
   │ 1. select / create session ──► SessionRepository.create_session()  ─► sessions.json
   │                              ◄── Session(session_id=UUID4 hex)
   │ 2. seat assignments         ──► (S1) PlayerRepository.list_players
   │    (seat → player_id)       ──► initial_seating: dict[int, str]
   │                              │
   │ 3. start hand logger    ──► JsonWriter(log_dir, session_id=UUID4)
   │                         ──► GameStateManager(players_with_player_id, sb, bb)
   │                         ──► IntegrationThread(session_repo=SessionRepo, ...)
   │
   ▼
IntegrationThread
   ├── _start_new_hand(gs.hand_id):
   │     for seat, player_id in current_seating:
   │         session_repo.assign_seat(session_id, hand_id, seat, player_id)  ← idempotent batch
   │   (これで hand_ref が SessionRepository 側で確定)
   │
   ├── _record_action: 既存通り (player_name のみで OK)
   │
   └── _finalize_hand:
         summary = HandSummary(
            ...
            session_id = self._session_id_uuid,            # ← session レイヤの UUID4
            players = [{seat, name, player_id, ...}, ...], # ← additive: player_id
         )
         json_writer.append_hand_summary(summary)
         # PHH は player_id を含めない (external interchange の安定性優先)
```

### 設計上の不変条件

- `IntegrationThread.session_repo` は read+write の依存。`assign_seat` 失敗（unknown_player /
  session_closed 等）は **hand を開始しない / fallback** ハンドリング（rollback section 参照）。
- `HandSummary.session_id` は UUID4 hex（hand logger 単独運用時は legacy timestamp も許容、
  schema 上は opaque 非空）。
- `HandSummary.players[i].player_id` は **optional**。未接続 hand（legacy / fallback）では null /
  absent。schema は additive。
- PHH は **無改変**（external interchange 安定性）。player_id は別経路（session JSON）で参照する。

## 5. player_id をどこでセット・どこまで流すか

| 段階 | 主体 | 何をするか |
|------|------|----------|
| session 開始時 | operator + GUI（S2.x で追加, または CLI 暫定） | `seat → player_id` を選ぶ。`PlayerRepository.list_players()` の選択肢から選ぶ。確定後 `SessionRepository.assign_seat`（hand 0 = "seating only" or 1 hand目開始時にバッチ呼び出し） |
| hand 開始時 | `IntegrationThread._start_new_hand` | 直前 hand の seat_map を carry-forward。seat change がある場合のみ差分 UI で更新（ISSUE-0006） |
| action 記録時 | `IntegrationThread._record_action` | 変更なし。`ActionRecord.player_name` のまま（action は seat-keyed なので player_id を持たせる必要はない。必要なら別 view で join） |
| hand 確定時 | `IntegrationThread._finalize_hand` | `HandSummary.players[i].player_id = current_seating[seat]` を additive に埋める |
| JSON 永続化 | `JsonWriter` | 変更不要（dict 化済みデータが additive に増えるだけ） |
| PHH 出力 | `PHHExporter` | **変更しない**。player_id は載せない |
| reader 側 | 将来 ledger / settlement / replay GUI | `HandSummary.players[i].player_id` を主参照、null なら name fallback |

### legacy log の扱い (ISSUE-0007 参照)

- **既存 logs/*.json は触らない**（unsafe な再書き込みを避ける）。
- 将来必要になったら、別 utility `tools/reconcile_legacy_logs.py`（planned）で:
  1. 各 legacy session を `SessionRepository.create_session(label="legacy:<timestamp>")` で作る
  2. `HandSummary.players[i].name` を `PlayerRepository.list_players()` と fuzzy match
  3. 人間が confirm → backfill `player_id` を新 sidecar ファイル（`logs/{session_id}.player_id.json`）
     に書く（元ファイル不変、別ファイルで join）
- このアプローチで past logs を破壊せずに最終的に 1 つの view にできる。**S2.x scope 外**、Phase 3 以降で必要が出たら実装。

## 6. session_id と hand_ref の決定タイミング

### session_id

- **新規 session**: `SessionRepository.create_session()` が UUID4 hex を採番。`main.py` / GUI の
  「セッション開始」操作で呼ぶ。
- **既存 session 再開**: `SessionRepository.list_sessions()` から選択。`JsonWriter` は対応 UUID4 hex
  名のファイルを既存ファイル再ロード（再開対応は既存実装の延長）。
- **fallback (session layer 無効化)**: `config.session_layer.enabled = false` のとき、現行の
  timestamp 文字列採番にフォールバック（rollback path）。

### hand_ref

- **生成タイミング**: hand 開始時に `IntegrationThread._start_new_hand` が `assign_seat` バッチを
  呼ぶ瞬間に、SessionRepository 内部で `hand_started_at` が記録され、以降 `resolve_hand_ref` で
  読める。**hand_ref オブジェクト自体は HandSummary に埋め込まない**（denormalized 重複は drift 源、
  ADR-0006 Consequences）。session JSON 側に住まわせ、cross-app 読者は `SessionRepository.resolve_hand_ref`
  経由でアクセスする。
- **`HandSummary.session_id` + `HandSummary.hand_id`** で複合キーとして cross-app 参照可能（ADR-0006）。

### 候補比較（hand_ref をどこで決めるか）

| 候補 | Pros | Cons | 採否 |
|------|------|------|------|
| (a) Integration._start_new_hand で assign_seat バッチ | hand 開始時刻が自然に記録される。失敗を hand 開始前に検出できる | seat 選択 UI が hand 開始前に揃っている必要 | **採用** |
| (b) Integration._finalize_hand で一括 assign | seat 変化が hand 終了まで分からなくて済む | hand 開始時刻が finalize 時刻にずれる。seat change の意味が崩れる | 不採用 |
| (c) GameStateManager レベルで全て | GameStateManager が SessionRepository に依存し責務肥大 | 純粋な game state という現責務から外れる | 不採用 |

## 7. Migration outline (Phase 2.x → 3.x)

### Phase 2.0（完了 — 現リポジトリ状態）

- S2 core 完了（`core/session*.py`）。hand logger は **未接続**。
- session レイヤ・hand logger は別 namespace で共存。

### Phase 2.1 — additive schema & config flag（設計のみ、code 未変更）

- 変わるもの:
  - `HandSummary.players[i]` に optional `player_id: string?` を **schema 上は許容**（draft schema
    sketch を本 doc に置く）。runtime field 自体は未追加。
  - `config_default.json` に `session_layer.enabled: bool`（default false）を planned として追記する案。
- legacy のまま: 既存 main.py / JsonWriter / PHHExporter / GameStateManager / IntegrationThread。
- rollback: config flag off で完全に従来動作（影響ゼロ）。

### Phase 2.2 — session_id 切替 & hand 開始時の assign_seat

- 変わるもの:
  - `main.py` GUI/CLI で session 選択 step を追加（create_session または既存 session 選択）。
  - `JsonWriter` の session_id を UUID4 hex（session レイヤ採番）に切替（schema は opaque 文字列のまま）。
  - `IntegrationThread._start_new_hand` で `SessionRepository.assign_seat` のバッチ呼び出し。
  - `HandSummary.players[i].player_id` を additive に埋め始める。
- legacy のまま: PHHExporter 不変。logs/*.json の旧 session_id 形式（timestamp）も読み込み可能。
  GameStateManager は変更不要（player_id は IntegrationThread が seat 単位で別途保持）。
- rollback: config flag off で従来通り。session_repo を None 注入する DI path で fallback 可能。

### Phase 2.3 — seat selection UI (registry 連動)【実装済 M3 = E3, 2026-06-11】

- 変わるもの:
  - `gui/dashboard.py` または別 widget に「seat → player_id」選択 UI（PlayerRepository から選ぶ）。
  - hand 間の seat change 反映 UX（ISSUE-0006）。
- legacy のまま: 単独運用フォールバックパス、PHH。
- rollback: 既存の name 入力 CLI/フォーム継続。

#### seat 選択 UX 仕様（E3 確定, ISSUE-0006 Fixed）

- **初回 seating**: `main.py:_prompt_session_config(player_repo)` がセッション設定プロンプトで
  席ごとに registry の番号選択を行う（CLI/GUI 共通の terminal step）。`n` = その場で
  `create_player`（スレッド起動前のため registry 書き込み安全）、空 Enter = 割当なし
  （自由入力名・player_id なし）。選択 player の `display_name` がその席の表示名になる。
- **session 作成**: session レイヤ有効時は `create_session(label?, blinds)` の UUID4 hex を
  `JsonWriter` の session_id に使う（ADR-0008 §2）。終了時に close するか y/N で確認
  （close は viewer の status に反映）。
- **carry-forward**: 確定した seat map は次の変更まで全 hand に自動適用（毎 hand の再確認なし）。
- **seat change（差分入力）**: dashboard「席設定」ボタン → modal ダイアログで選び直し →
  変更席のみ `AudioEvent(action="seat_assign", seat, raw_text=player_id)` を queue 投入
  （rebuy と同じ一元化規約, ISSUE-0012）。`raw_text=""` は割当解除。IntegrationThread は
  `_pending_seat_changes` に保留し **次ハンド開始時** に `seat_player_map` と表示名
  （`set_player_name`）へ反映する（mid-hand の帰属・名前の揺れを防ぐ）。
  同一 player の複数席割当はダイアログ側で拒否（`player_already_seated` と同じ規則）。
- **sitting_out**: 割当解除のみ（explicit `status=sitting_out` は後続）。
- **記録系イベントとの関係**: `seat_assign` は `reconstruction_event` schema（`action` は自由文字列）
  の範囲内で sidecar に記録され、replay でも同じタイミングで map に反映される。

### Phase 2.4（任意）— legacy log reconciler tool

- 変わるもの: `tools/reconcile_legacy_logs.py`（planned）で過去ログを SessionRepository に
  best-effort 取り込み（ISSUE-0007）。
- legacy のまま: logs/*.json 元ファイルは不変、sidecar 別ファイルで join。
- rollback: tool を走らせなければ何も変わらない。

### Phase 3.x — ledger / point ledger 接続

- 変わるもの:
  - `core/ledger*.py`（新規, S3）が `HandSummary.session_id` / `players[].player_id` を主参照キーに使う。
  - settlement（S4）が `(session_id, player_id)` で集計。
- legacy のまま: hand logger 出力 path は Phase 2.x で確定済のものを継続。
- rollback: ledger 無効化フラグで hand logger 単独運用に戻せる。

## 8. contracts / schema レベルの追加案

### 8.1 HandSummary draft schema sketch (未配置, 凍結しない)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "HandSummary",
  "version": "0.x-draft-sketch",
  "type": "object",
  "additionalProperties": true,
  "required": ["hand_id", "session_id", "started_at", "ended_at", "players", "actions"],
  "properties": {
    "hand_id":     {"type": "integer", "minimum": 0},
    "session_id":  {"type": "string", "minLength": 1,
                    "description": "Phase 2.2 以降は session レイヤの UUID4 hex を推奨。legacy timestamp 文字列も valid。"},
    "started_at":  {"type": "string", "format": "date-time"},
    "ended_at":    {"type": "string", "format": "date-time"},
    "blinds":      {"type": "object", "properties": {"sb": {"type": "integer"}, "bb": {"type": "integer"}}},
    "board":       {"type": "array", "items": {"type": "string"}},
    "board_source":{"type": "string"},
    "players": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["seat", "name"],
        "properties": {
          "seat":      {"type": "integer", "minimum": 1, "maximum": 9},
          "name":      {"type": "string"},
          "player_id": {"type": ["string", "null"],
                        "pattern": "^[0-9a-f]{32}$",
                        "description": "Phase 2.2 で additive 追加。legacy / fallback では null。"},
          "hole_cards": {"type": ["array", "null"]},
          "stack_start": {"type": "integer"},
          "stack_end":   {"type": "integer"},
          "result":      {"type": "integer"}
        }
      }
    },
    "actions": {"type": "array"}
  }
}
```

- **凍結しない理由**: hand logger の既存出力には board / hole_cards_source / pot_total / winner_seat 等
  さらに多くのフィールドがあり、`additionalProperties: false` で締めるには既存形を全て契約に取り込む
  必要がある（範囲過大）。今回はあくまで **`player_id` を additive で足すための受け皿** として draft sketch
  に留め、freeze は Phase 2.2 以降に分けて検討する。

### 8.2 既存 contracts への小さな更新提案

- `docs/contracts/shared-ids.md`: hand logger と session レイヤで session_id が **2 系統並存** する
  期間（Phase 2.x）の許容形を明記。Phase 2.2 以降は UUID4 hex を canonical 化。
- `docs/contracts/session-seating.md`: 「hand logger 接続」節を追加し、本 doc へリンク。
- `docs/contracts/error-shapes.md`: hand 開始時 assign_seat 失敗のフロント側 UX（seat 選択 UI への
  巻き戻し）を Phase 2.3 で具体化する旨をメモ。

### 8.3 PHH の互換戦略

- PHH ファイル形式は **無改変**。`players = ["Alice", "Bob", ...]` のまま。
- player_id を PHH に載せるなら最大でも author/note へのコメント。本 doc では **載せない方針**。
- 理由: PHH は external interchange であり、registry の player_id を外に漏らさない（プライバシ・
  cross-app 安定性）。

### 8.4 互換ルール（既存 logs / PHH を壊さない）

- 旧 logs/*.json（timestamp session_id, players に player_id 無し）は読み取り側で player_id absent =
  null として処理する。
- 新 logs/*.json（UUID4 session_id, players に player_id additive）は旧 reader（player_id を見ない
  もの）からも壊れずに読める（additive 増分のみ）。
- PHH は両世代でバイト一致を維持する。

## 9. まだ open な論点

- **ISSUE-0006**（新規）: seat 選択 UX。hand 間の seat change を operator がどう操作するか。
- **ISSUE-0007**（新規）: legacy log の reconciliation 必要性と方針。
- **ISSUE-0005**（継続）: schema `1.0` freeze は本 doc + ADR-0008 Accepted + Phase 2.2 実装 + ISSUE-0006
  決着が揃ってから。

## 10. 参照

- ADR-0006: S2 contract / 複合キー
- ADR-0007: S2 core 永続形と session_id 採番（decoupled）
- ADR-0008: hand logger × session 接続戦略（write-through, additive）
- ISSUE-0005 / 0006 / 0007
- `core/hand_log.py`, `output/json_writer.py`, `output/phh_exporter.py`,
  `integration/engine.py`, `main.py`（現状の hand logger world）
- `core/session.py`, `core/session_repository.py`（S2 core）
