# ADR-0006: RFID センサーを PN5180 + ESP32-S3 に移行する（HTTP transport 契約は不変）

## Status

Accepted

## Date

2026-06-01

## Context

RFID NFC ソースのハードウェア構成を、これまでの **PN532 + ESP32（ESP32-WROOM-32）**
から **PN5180 + ESP32-S3** に変更する仕様変更が入った。

このリポジトリには ESP32 側ファームウェアは含まれておらず（別管理）、ハードウェアと
Python アプリの境界は `rfid/http_receiver.py` が受ける HTTP JSON 契約のみである:

```
POST /rfid  Content-Type: application/json
{ "reader_id": "seat_3", "tag_id": "04A1B2C3D4E5F6", "timestamp": "2026-..." }
```

この契約は `reader_id` / `tag_id`（16 進文字列）/ `timestamp` の 3 フィールドだけで構成され、
**どの NFC フロントエンド / MCU を使うかに依存しない**。したがって今回の変更の本質は
ファームウェア（別管理）+ ドキュメント整合であり、Python コアのロジック改修は伴わない。

ハードウェア世代差で考慮すべき点:

- **PN5180 vs PN532**: PN5180 は ISO15693（vicinity, 8 バイト UID）を読める。RF 出力も高く
  読取距離が伸びるため、隣接席間のクロストーク対策（アンテナ/ファーム側）が新たな関心事になる。
  SPI 専用で、駆動ライブラリも PN532 系とは別。
- **ESP32-S3 vs ESP32-WROOM-32**: S3 はネイティブ USB-OTG を持つ。固定卓配置では WiFi より
  USB-CDC シリアル接続の方が安定しうるため、将来 Python 側に新 transport を足す余地がある。

関連: `CLAUDE.md`（アーキテクチャ / 技術スタック / エラーハンドリング方針）、ISSUE-0007。

## Decision

RFID センサーハードウェアを **PN5180 + ESP32-S3** に移行する。Python アプリ側は
**HTTP JSON transport 契約（`reader_id` / `tag_id` / `timestamp`）を変更しない**ことを
本 ADR で確認・固定する。ESP32-S3 + PN5180 ファームウェアは従来と同一形の JSON を
`POST /rfid` に送ること、を境界契約とする。

`tag_id` は引き続き「16 進文字列（区切り任意）」とし、UID 長は **可変** とする。
`rfid/card_master.py` の `normalize_tag_id` / `bytes_to_tag_id` は UID 長を仮定しない
実装であり、ISO14443A の 4/7 バイトと ISO15693 の 8 バイト UID の **両方**をそのまま
扱える。これにより ISO14443A / ISO15693 の **dual-support** を契約レベルで成立させる。

## Alternatives Considered

- **Alternative A — HTTP JSON 契約に hardware 情報（chip 種別等）を追加する**
  - Pros: ファーム世代を Python 側で判別できる。
  - Cons: 契約がハードウェア実装に結合し、将来のハード変更ごとに schema 変更が要る。
    確信度行列・event 処理は chip 種別に依存しないため不要な情報。
  - Why rejected: 境界はハードウェア非依存に保つ方が交換可能性が高い。
- **Alternative B — 今すぐ USB-CDC シリアル transport を追加する**
  - Pros: ESP32-S3 のネイティブ USB を活かし、卓上配置で WiFi より安定しうる。
  - Cons: 新規リーダースレッド + transport 分岐 + テストが必要で、今回のスコープ
    （ドキュメント整合）を超える。WiFi 運用継続でも問題は出ていない。
  - Why rejected: 設計候補として ISSUE-0007 に記録し、必要が出た phase で別タスク化する。
- **Alternative C — tag_id 長を固定（例: 7 バイト）にバリデーションする**
  - Pros: 不正タグの早期検出。
  - Cons: ISO15693（8 バイト）を排除し、dual-support の決定（未定/両対応）と矛盾する。
  - Why rejected: UID 長は可変のままにする。

## Consequences

- Positive: ハードウェア交換が Python コアに波及しない境界が明文化された。ISO14443A /
  ISO15693 の dual-support が契約レベルで保証される。
- Negative / trade-offs: ファームウェアがこの JSON 契約を破った場合（フィールド名変更・
  UID エンコード変更など）の検出は Python 側 transport では行えず、`unknown reader_id` /
  `unregistered tag` の警告ログに依存する。
- Neutral / new constraints: PN5180 の高 RF 出力に伴うクロストーク（隣接席誤検出）は
  ファーム/アンテナ側の責務として残る。USB-CDC transport は未実装の設計候補として残る
  （ISSUE-0007）。

## Validation / Follow-up

- [x] `normalize_tag_id` / `bytes_to_tag_id` が 4/7/8 バイト UID を仮定なく扱えることを確認
      （`rfid/card_master.py:130-148`、長さ非依存実装）。
- [ ] 8 バイト（ISO15693）UID の normalize 回帰テスト追加（別タスク、ISSUE-0007）。
- [ ] ISO15693 採用時の UID エンコード（MSB/LSB 順）をファームと突き合わせ（ISSUE-0007）。
- [x] USB-CDC transport の要否を運用後に判断 → **採用**（USB 直結確定, ADR-0007）。

## Related Files

- `rfid/http_receiver.py` — HTTP JSON 受信（contract 受け口。docstring のみ更新）
- `rfid/card_master.py` — `normalize_tag_id` / `bytes_to_tag_id`（UID 長非依存）
- `integration/engine.py` — RFID ソース統合（docstring のみ更新）
- `CLAUDE.md` — アーキテクチャ / 技術スタック / エラーハンドリング方針

## Related Tests

- `tests/test_rfid.py` — 既存 RFID テスト（contract 不変のため挙動変化なし）

## Related Commits

- `<commit-sha>` — RFID hardware migration docs (PN5180 + ESP32-S3)

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: — （本 ADR は有効。Alternative B〔USB-CDC serial, deferred〕は
  ADR-0007 で Accepted 化された。default transport を http → serial に変更したのも ADR-0007。）
