# ADR-0070: RFID の中継（RDP のセッションの外で読む）と読み取り装置の再起動

## Status

Accepted（2026-10-13 に記録。中継は 2026-09-30、使えるリーダーの一覧と再起動は 2026-10-06 に実装済。再起動の命令は
firmware の書き換えのあとに使える = 店舗の書き換えは未確認）

## Date

2026-10-13（決定は 2026-09-30・2026-10-06）

## Context

店舗 PC は画面なし（HDMI ダミープラグ）で、RDP で操作する（`docs/installation.md` §0）。RFID は ESP32-S3 + PN5180 ×11 を
USB CCID の PC/SC リーダーとして読む（ADR-0015 / 契約 `docs/contracts/rfid-usb-ccid.md`）。

1. **RDP のセッションの中から PC/SC に届かない**（店舗 2026-09-30）。`start_logger.cmd` で始めたセッションで 2 ハンドが
   記録されなかった。RDP のセッションの中のアプリは `SCardEstablishContext` が `0x8010001D`（「スマート カード リソース
   マネージャーが実行されていません」）。サービス（`SCardSvr`）は動いていてデバイスも OK。スマートカードの転送をサーバ
   （`fEnableSmartCard=0`）・接続元（`redirectsmartcards:i:0`）の両方で止め、サービスを再起動し、サインアウトして入り直しても
   同じ。システムの権限（スケジュールタスク, セッション 0）からは 11 台すべて読めた。さらにロガーの `RFIDThread` は、1 台も
   つながらないとログのファイルにだけ書いてスレッドを終えていた（画面には何も出ず、配布の検出が無いのでハンドも始まらない）。
2. **リーダーが黙って読まなくなる**（店舗 2026-10-06）。真ん中のボードのリーダーが 14:01〜14:58 に一度も札を読まず、
   エラーも出なかった。firmware は起動のときに初期化できなかったリーダー・途中で SPI / BUSY が答えなくなったリーダーにも
   Get UID で「札なし」（`6A 81`）と答え続けるので、host からは札が載っていないのと区別できない。PN5180 の RST は 11 台で
   共通なので、1 台だけのやり直しはできない。オーナー:「また真ん中のリーダーが調子悪くなっちゃって、再起動したら読める
   ようになった。この再起動をソフト的にできない？」
3. **USB の抜き挿しでは再起動しない卓がある**（店舗 2026-10-06）。こちらの案内で「コードの挿し直し → ESP32 の USB の
   抜き挿し」をしても直らず、オーナーが ESP32 の**給電アダプタを抜いて再起動**すると読めた。この卓の ESP32 は給電アダプタから
   電源を取っており、PC との USB を抜き挿ししても再起動しない（`pn5180_init` は起動のときだけ）。

## Decision

### D1. 中継: システムの権限のタスクがリーダーを読み、PC の中だけに渡す（2026-09-30）

- **中継**（`rfid/relay.py` の `RelayPoller` + `serve`、`tools/rfid_relay.py serve`）: `config.json` の `pcsc_readers` の
  `(name, reader)`（重複は 1 つ）を順に読み（既定 50 ms おき）、リーダーごとに最新の UID と読んだ時刻を持つ。`127.0.0.1` だけで
  待ち受け（既定 8792, config `rfid.relay_port`）、`GET /` にいまの読み取りを返す。`config.json` が変わったら読み直す。
- 動かし方: スケジュールタスク **`PokerRFIDRelay`**（`installer/rfid_relay_task.ps1`。`NT AUTHORITY\SYSTEM`・PC の起動時・時間
  制限なし・失敗したら 1 分後に再起動・二重起動しない）。登録は管理者で 1 回、消すときは `-Remove`。アプリの更新のあとも
  もう一度実行すると新しい版で動き直す。`uninstall.cmd` は中継のタスクも消す。ログは `logs/rfid_relay.log`。
- **ロガー**（CLI・GUI）は `AutoRFIDSource` を `RFIDThread` に渡す: 中継が動いていれば中継から（`RelayBridge`。1 周に 1 回だけ
  取りに行く、3 秒より古い読み取りは「札なし」）、無ければ直接（`PCSCBridge`）。**札の扱い（在否・ボードの位置・配り直し,
  ADR-0058）は直接読むときと同じ**で、firmware から見た APDU 列も同じ（契約は変えない）。`RelayClient` はプロキシを使わない。
- **黙って止まらない**: `RFIDThread` は 1 台もつながらなくても終えず、`reconnect_sec`（5 秒）ごとにリーダー名が見えたら
  つなぎ直す（中継があとから動いても・USB を挿し直しても、起動し直さずに使い始める）。CLI はつながり具合を 1 行出し
  （「RFID: リーダー 11/11 台を中継（RDP の外の読み取り）から読んでいます。」）、つながらないときは中継を登録するコマンドを出す。
- 確認の道具（`probe_pcsc list / check / watch`・`register_cards run`）も、中継が動いていれば中継を通して読む。
  中継だけの確認は `python tools/rfid_relay.py status` / `watch`。

### D2. 使えるリーダーの一覧と再起動の命令（firmware, 契約 v1.11, 2026-10-06）

- `FF CA 00 FE 00` = 使えるリーダーの一覧（2 byte little endian のビット列 + `90 00`。起動のときに初期化できなかった・実行中に
  RF の ON/OFF・RF 設定のロードが 20 回続けて失敗したリーダーは 0。初期化の途中は `69 85`）。
- `FF CA 00 FD 00` = `90 00` を返してから 300 ms 後に `esp_restart()`（USB はいったん切れて列挙し直し、起動のときの共有 RST で
  全 PN5180 がリセットされる）。
- 旧 firmware はどちらも `6A 86`（範囲外の P2）を返し、host は「一覧なし・命令なし」として動く（`rfid/bridge.py:
  query_ready_readers` / `request_device_restart`）。物理リーダーの番号の上限は 254 → 252（0xFD〜0xFF を予約）。
- **firmware の書き換えが 1 回要る**（オーナー「できる」= 9 月と同じ PC・手順。店舗 PC でのビルドは
  `docs/worklog/2026-10-07-firmware-build-store-pc.md`）。

### D3. host: 知らせる・`rr`・卓が空のときだけ自動で再起動（2026-10-06）

- `READY_CHECK_SEC`（10 秒）おきに一覧を聞き、設定のリーダーが一覧に無ければ CLI に「● ⚠ RFID: 使えないリーダーがあります:
  ボードの左から 2 台目のリーダー（reader 9）…」。
- **ボードのリーダーの見張り**（一覧に出ない不調用）: ボードに 3 枚以上配ったハンドで 1 枚も読まなかったボードのリーダー
  （いちばん右は 4 枚以上のハンドだけ数える）が `SILENT_HANDS`（2）ハンド続いたら知らせる。店舗 10/06 の健全な 59 ハンドでは
  どのボードのリーダーも毎ハンド札を読んでいた。古い firmware でも知らせは出る。
- **`rr`**（CLI）: 卓に札が無い状態が 2 秒続いたら再起動を頼む（札が載っていれば片付くのを待つ）。中継から読んでいるときは
  中継に頼む（`POST /restart`。RDP のセッションの中からはリーダーに命令を送れない）。直接読んでいるときは接続を捨ててつなぎ
  直す。再起動のあいだは読まない。`python tools/rfid_relay.py restart` / `python tools/probe_pcsc.py restart` でも頼める。
- **自動**（config `rfid.auto_restart`, 既定 true）: 使えないリーダーがあり、**卓に札が無い状態が 10 秒**
  （`AUTO_RESTART_EMPTY_SEC`）続いたら頼む。1 つの不調につき 2 回まで（`AUTO_RESTART_MAX`）・60 秒あける。false = 知らせるだけ。
  旧 firmware・古い中継・命令が届かないときは繰り返さず、手での再起動を案内する。
- 卓に札が無いときだけにする理由: 再起動のあいだ（数秒）は全リーダーが読めず、札が消えたのと同じに見える（席の札の離脱 =
  フォールドの合図, ADR-0068 R1）。店舗の卓の状態の記録で、ハンドの途中に全リーダーが空になるのはプリフロップで全員が札を
  持ち上げたときだけで、10 秒以上続いた例は無かった（ハンドの間の片付け・シャッフルの空きは半分以上が 10 秒以上）。

### D4. 手での再起動は「給電アダプタを抜いて挿し直す」と案内する（2026-10-06 の店舗, 案内の文は 2026-10-13）

- 給電アダプタで動かしている卓では、PC との USB を抜き挿ししても ESP32 は再起動しない。host の案内（旧 firmware・命令が
  届かない・再起動で直らない・ボードのリーダーの見張り）は「読み取り装置（ESP32）の給電アダプタを抜いて数秒待ってから挿し
  直してください（給電アダプタで動かしている卓では、USB の抜き挿しでは再起動しません）」（`rfid/reader_thread.py:_POWER_CYCLE`）。
- 運用: リーダーのコードが抜けた・挿し替えた・読めないリーダーがあるとき → 給電アダプタを抜いて数秒待って挿す →
  `tools/rfid_relay.py watch` で左・真ん中・右と席に札を置いて確かめる。ボードのケーブルは元の位置（左 #10・真ん中 #12・右 #13）。
  配り直しの「同じか隣のリーダー」の判定は config の左からの並びを使うため。

## Alternatives Considered

- **RDP のスマートカードの転送を止める** — サーバ・接続元の両方で止め、サービスの再起動・サインアウトもしたが `0x8010001D` の
  まま。不採用（効かなかった）。
- **ロガー自体をシステムの権限・セッション 0 で動かす** — ロガーは CLI の入力・マイク（RDP の音声の設定に依存）・画面への
  知らせを使うので、RDP のセッションの中に置く必要がある。読み取りだけを外に出した。
- **RDP をやめて手元のディスプレイで操作する** — 店舗 PC は画面なしで RDP 運用（ADR-0057 の導入時からの前提）。運用を変えない。
- **RFID を HTTP 経路（`rfid/http_receiver.py`）にする** — firmware は USB CCID（ADR-0015 / 契約）。HTTP は debug / remote 用の
  副経路で、Wi-Fi の firmware が別に要る。中継は PC の中の localhost だけで、契約も firmware も変えない。
- **読めないリーダーだけをやり直す** — PN5180 の RST は 11 台で共通なので 1 台だけのやり直しはできない。ESP32 の再起動にした。
- **ハンドの途中でも自動で再起動する** — 全リーダーが空になり、全員のフォールドに見える。卓が空のときだけにした。
- **USB の抜き挿しを案内する（v1.11 の最初の文）** — 給電アダプタで動かす卓では効かなかった（D4）。

## Consequences

- Positive:
  - RDP で操作していてもリーダーが読める（中継のタスクは PC の起動時に動く）。つながらないときは CLI に理由と直し方が出る。
  - 黙って読まないリーダーに気づける（一覧・見張り）。卓が空のときにロガーから再起動でき、店員が装置に触らずに済む。
- Negative / trade-offs:
  - 中継の登録に管理者の権限が 1 回要る。アプリを更新したら中継のタスクも登録し直さないと古い版で動く。
  - 中継の分だけ読み取りに遅れが乗る（1 周に 1 回取りに行く・40 ms）。3 秒より古い読み取りは「札なし」。
  - 再起動の命令は firmware の書き換えが要る。書き換えまでは知らせと手での再起動の案内だけ。
  - 見張りは「真ん中に札が載らない置き方」でも数える。再起動のあと、そのリーダーがまた札を読むまでは回数を戻さない。
- Neutral / new constraints:
  - 中継は `127.0.0.1` だけで待ち受ける（LAN に出さない）。
  - 契約 `rfid-usb-ccid.md` は v1.11（D2）+ 2026-10-13 の注記（D4 と中継の記述, 版は変えない）。

## Validation / Follow-up

- [x] `tests/test_rfid_relay.py`（中継の読み取り・つなぎ直し・古い読み取り・再起動の命令）/ `tests/test_rfid_restart.py`
  （一覧に無いリーダーの知らせ・`rr` は札が片付くまで待つ・自動は卓が空いて 10 秒・2 回で諦める・古い firmware の案内・
  ボードのリーダーの見張り）。
- [x] 店舗 2026-10-06: 中継から 11 台を読んでいる（ロガーのログに直接読む行が無い）。
- [ ] 店舗で firmware を書き換えて、`probe_pcsc list` の一覧・`rr` での再起動・自動の再起動を確かめる（実機は未確認）。
- [ ] `docs/troubleshooting.md` に給電アダプタの抜き挿しをまとめる（§7b の後回し分）。

## Related Files

- `rfid/relay.py` / `tools/rfid_relay.py` / `installer/rfid_relay_task.ps1` / `uninstall.cmd`（中継のタスクを消す）
- `rfid/bridge.py`（`pcsc_reader_present` / `query_ready_readers` / `request_device_restart`）
- `rfid/reader_thread.py`（`reconnect_sec` / 一覧・見張り・`request_restart` / `auto_restart` / `_POWER_CYCLE`）
- `main.py`（`AutoRFIDSource` の結線・`rr`・つながり具合の表示）/ `config_default.json`（`rfid.relay_port` / `rfid.auto_restart`）
- `firmware/esp32s3-pn5180-ccid/`（一覧 `FF CA 00 FE 00` / 再起動 `FF CA 00 FD 00`）
- `tools/probe_pcsc.py`（`list` の一覧・`restart`）/ `docs/contracts/rfid-usb-ccid.md`（§4 中継・§6 v1.11・§10）

## Related Tests

- `tests/test_rfid_relay.py` / `tests/test_rfid_restart.py`

## Related Commits

- 2026-09-30 の中継: このリポジトリの履歴が浅く引けない（`docs/worklog/2026-09-30-rfid-relay.md`）
- `98f56e1` — 読み取り装置（ESP32）をロガーから再起動する（一覧・再起動の命令 = 契約 v1.11、`rr` と自動、見張り）
- `044011a` — docs: ボードのリーダーが読めないときは ESP32 の給電アダプタを抜いて再起動
- `183cb4b` — 読み取り装置の再起動は給電アダプタの抜き挿しを案内（`_POWER_CYCLE`）

## Supersedes / Superseded by

- Supersedes: —（ADR-0034 の契約に v1.11 を additive に足した。ADR-0015 の PC/SC canonical は不変）
- Superseded by: —
- 関連: ADR-0015 / ADR-0034 / ADR-0051（slot 常時 present）/ ADR-0052（P2 で物理リーダー選択）/ ADR-0057（店舗 PC・RDP 運用）/
  ADR-0058 / ADR-0068 / ISSUE-0023（init の耐性）
