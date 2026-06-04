# ADR-0009: Remove camera / vision input per sprc_v4.docx (2-source: RFID + audio)

## Status

Accepted

## Date

2026-06-04

## Context

`sprc_v4.docx`（正の要件定義, v4.0）は **カメラ入力の廃止** を明記している
（「カメラ入力は本バージョンより廃止。映像解析に関する要件はすべて削除する」/
「opencv-python、easyocr はカメラ廃止に伴い削除」）。一方コードベースは v2/v3 期の
camera パイプライン（`vision/`、`CameraEvent`、camera confidence、`--calibrate`）を
温存しており、CLAUDE.md でも camera は *legacy（未使用）* と注記されるに留まっていた
（削除はされていなかった）。

本タスクは `claude/dazzling-brown-COUkU`（v4 設計ベース）へ `claude/sharp-bardeen-dYeA6`
（player registry / session / contracts を additive 拡張した子孫ブランチ）を統合する流れの中で、
**「本ブランチの v4 設計を基本にする」** 方針に従い、温存されていた camera/vision を仕様どおり
削除して 2 ソース構成（RFID + 音声）に確定するもの。

制約（forces）:

- **音声 + RFID の hand logger 挙動を退行させない**。camera は実運用で未接続（`config.camera.roi`
  が空のとき camera スレッドは起動しない）だったため、削除しても通常運用の挙動は変わらない。
- **JSON / PHH 出力を壊さない**。`sprc_v4.docx` §6.1 の `ActionRecord.source` は元来
  `{"audio", "rfid"}` であり `camera` キーを含まない。よって camera キー除去は仕様準拠。
- **RFID 照合の共有インフラを壊さない**。`MATCH_WINDOW` と buffer 失効ロジックは RFID seat
  照合にも使われるため保持する（camera 専用部分のみ除去）。
- sharp-bardeen が additive に足した player/session レイヤ（config-gated）には一切触れない。

関連: `sprc_v4.docx`（要件）/ ADR-0003（hand logger を base とした domain 拡張）。

## Decision

camera / vision 経路をコードから **完全削除** し、confidence を 2 ソース行列に再構成する。

1. **ディレクトリ / ファイル削除**: `vision/`（`camera.py` / `motion_detector.py` /
   `calibration.py` / `__init__.py`）、`tests/test_vision.py`。
2. **依存削除**: `requirements.txt` から `opencv-python` / `easyocr`。`core/events.py` の
   numpy 依存（`CameraEvent.frame` 専用だった）も除去。
3. **設定削除**: `config_default.json` の `camera` ブロック。`main.py` の `--calibrate`
   サブコマンド（ROI キャリブレーション）。
4. **イベント / キュー**: `core/events.py` の `CameraEvent` データクラス、`core/event_queue.py`
   の `make_camera_queue` と `EventItem` 内の `CameraEvent` を削除。
5. **confidence 再構成**: `integration/engine.py` の `calc_confidence` を
   `calc_confidence(has_rfid, has_audio)` に変更。行列は **RFID+audio=0.95 / RFID=0.70 /
   audio=0.50 / なし=0.00**。camera confidence 定数（`_CONF_*_CAMERA`）を削除。
6. **照合ロジック**: camera バッファ・`_drain_camera_queue` / `_pop_matching_camera_event` を
   削除。RFID buffer 失効に使っていた `CAMERA_BUFFER_TTL` は **`BUFFER_TTL` に改名して保持**。
   `MATCH_WINDOW` は RFID 照合用に保持。
7. **source 形**: `ActionRecord.source` を `{"audio", "rfid"}` に確定（`camera` キー廃止、
   sprc_v4.docx §6.1 と一致）。GUI のソース表示からも「カメラ」を除去。
8. **GUI / エントリポイント**: `gui/dashboard.py` / `main.py` から camera スレッド起動・
   `camera_queue` DI を削除。

## Alternatives Considered

- **camera を legacy として残置（従来の sharp-bardeen の扱い, 不採用）**
  - Pros: 削除作業ゼロ。将来 camera を復活させたくなったとき残っている。
  - Cons: sprc_v4.docx の明記に反する。dead code・未使用依存（opencv/easyocr）・camera を含む
    confidence 行列が「現状仕様」を曖昧にし、新規実装者を混乱させる。
  - Why rejected: 「v4 設計を基本にする」本統合方針と矛盾。死蔵コードの維持コストが上回る。

- **camera を feature flag 化して無効化（不採用）**
  - Pros: コードを残しつつ既定 off にできる。
  - Cons: session_layer のような *将来使う* flag と異なり、camera は v4 で **廃止が確定** している。
    flag は「まだ使う可能性」を示唆してしまい誤解を招く。依存も残る。
  - Why rejected: 廃止確定機能に flag は不適切。必要なら git 履歴から復元できる。

- **`source` の `camera` キーだけ残す（後方互換, 不採用）**
  - Pros: 旧 JSON reader が `source["camera"]` を期待していても壊れない。
  - Cons: sprc_v4.docx §6.1 は元から `camera` キーを持たない。常に `false` の死キーになる。
  - Why rejected: 仕様に無いキーを惰性で残す理由がない。`.get("camera")` 利用側は除去済み。

## Consequences

- Positive
  - 「現状仕様 = RFID + 音声の 2 ソース」がコード・docs・spec で一致する。
  - 未使用の重量依存（opencv-python / easyocr / numpy@events）が消え、インストールが軽くなる。
  - confidence ロジックが 4 分岐 → 2 分岐に単純化し読みやすい。
- Negative / trade-offs
  - camera パイプラインを将来復活させる場合は git 履歴（本コミット以前）から取り戻す必要がある。
  - confidence 上限が 1.00（RFID+audio+camera）→ 0.95（RFID+audio）に下がる。ただし camera は
    実運用で未接続のため実測 confidence に影響しない。
- Neutral
  - sharp-bardeen の player/session レイヤ（config-gated, additive）には無影響。flag off/on とも不変。

## Validation / Follow-up

- [x] 全コードから camera/vision 参照を除去（`*.py` / `*.json` の grep 0 件）。
- [x] `python -m py_compile` 全 `.py` green、JSON 妥当性 OK。
- [x] `tests/test_phase7.py` / `tests/test_integration.py` を 2 ソースへ更新。
       `test_gui` / `test_logger` / `test_phh_exporter` の source dict から camera キー除去。
- [x] フルテスト **199 passed**（pytest + jsonschema + numpy。GUI/audio/PHH は遅延 import で collect 可）。
- [x] CLAUDE.md（概要 / confidence 行列 / ディレクトリ / 実装状況 / commands）と CHANGELOG を更新。

## Related Files

- 削除: `vision/*`, `tests/test_vision.py`
- 変更: `integration/engine.py`, `core/events.py`, `core/event_queue.py`, `core/hand_log.py`,
  `main.py`, `gui/dashboard.py`, `config_default.json`, `requirements.txt`
- テスト: `tests/test_phase7.py`, `tests/test_integration.py`, `tests/test_gui.py`,
  `tests/test_logger.py`, `tests/test_phh_exporter.py`

## Related Tests

- `tests/test_phase7.py`（calc_confidence 2 ソース + RFID 照合）
- `tests/test_integration.py`（audio 単独 confidence）

## Related Commits

- 本 ADR と同じコミット（camera/vision removal, v4 alignment）

## Supersedes / Superseded by

- Supersedes: —（camera を legacy 残置とする扱いは ADR ではなく CLAUDE.md の注記だった）
- Superseded by: —
- 関連: `sprc_v4.docx`（カメラ廃止の要件）、ADR-0003（hand logger base）
