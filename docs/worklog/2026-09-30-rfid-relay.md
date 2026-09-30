# 2026-09-30 RFID の中継（RDP のセッションの外でリーダーを読む）+ RFID が黙って止まらないように

## Goal

店舗: `start_logger.cmd` で始めたセッションに 2 ハンドが記録されなかった。原因は RFID:

- RDP で操作する店舗 PC で、RDP のセッションの中のアプリは PC/SC に届かない（`SCardEstablishContext` =
  `0x8010001D`「スマート カード リソース マネージャーが実行されていません」）。サービスは動いていて（`SCardSvr`
  Running）、デバイスも OK。スマートカードの転送をサーバ（`fEnableSmartCard=0`）・接続元（`redirectsmartcards:i:0`）の
  両方で止め、サービスを再起動し、サインアウトして入り直しても同じ。
- システムの権限（スケジュールタスク, セッション 0）からは 11 台すべて読めた（`probe_pcsc list` = 11 件 matched）。
- ロガーの `RFIDThread` は、1 台もつながらないとログのファイルにだけ書いてスレッドを終えていた。画面には何も出ず、
  手札を配っても（RFID の配布の検出が無いので）ハンドが始まらず、声も聞き取らなかった（配布で開く聞き取りの窓）。

## 作り

- **中継**（`rfid/relay.py` + `tools/rfid_relay.py`）:
  - `RelayPoller`: 設定のリーダー（`pcsc_readers` の `(name, reader)`、重複は 1 つ）を順に読み（既定 50 ms おき）、
    リーダーごとに最新の UID と読んだ時刻を持つ。つながらないリーダーは 5 秒おきに、PC/SC に名前が見えたときだけ
    試し直す（ログを埋めない）。1 周の失敗（例外）で止まらない（同じ例外のログは 1 回）。`set_readers` で
    リーダーを差し替える（`config.json` が変わったとき。`serve` が 2 秒おきに mtime・size を見る）。
  - `make_relay_server`: `127.0.0.1` だけで待ち受け（既定 8792, config `rfid.relay_port`）、`GET /` にいまの読み取り
    （`state` / `connected` / `configured` / `readers: {"<name>|<index>": {"uids", "at"}}`）を返す。
  - `serve` はシステムの権限のスケジュールタスク `PokerRFIDRelay`（`installer/rfid_relay_task.ps1`: PC の起動時・
    時間制限なし・失敗したら 1 分後に再起動・二重起動しない。登録したらすぐ起動し `status --wait 20` を出す）。
    ログは `logs/rfid_relay.log`。
- **ロガー**（`main.py`）: `AutoRFIDSource` を `RFIDThread` の `bridge_factory` / `reader_present` に渡す。リーダーに
  つなぐたびに、中継が動いていれば `RelayBridge`（中継の読み取りを 1 周に 1 回だけ取りに行き使い回す = 40 ms。
  3 秒より古い読み取りは使わない = 札なし。直接読むときの読み取り失敗と同じ扱い）、無ければ `PCSCBridge`。
  `RelayClient` はプロキシを使わない（Windows のプロキシ設定があっても 127.0.0.1 をプロキシに送らない）。
- **`RFIDThread`**: 1 台もつながらなくても終えず、`reconnect_sec`（既定 5 秒）ごとに、リーダー名が見えたら
  （`reader_present`、既定は pyscard の一覧を静かに見る `pcsc_reader_present`）つなぎ直す。`reconnect_sec=None` で
  従来どおり終える（既存のテスト 2 件はこれを明示）。
- **CLI**（`_report_rfid_status` / `_rfid_status_message`）: 起動後にリーダーのつながり具合を 1 行出し、変わったら
  出し直す（1 秒おき）。中継から読んでいるときの台数は中継が実際につないでいる台数（ロガーの側は中継につながれば
  全台「つながった」になるため）。つながらないときは中継を登録するコマンド（管理者）を出す。
- **確認の道具**: `probe_pcsc list / check / watch` と `register_cards run` は、中継が動いていれば中継を通して読む
  （ロガーと同じ経路。RDP の中から直接は読めない）。
- **アンインストール**: 中継のタスクを先に消す（中継が venv の python を使っている間は venv を消せない）。
  管理者の権限が無ければ `-Remove` のコマンドを出して止まる。

## 途中で見つけて直したもの

- `RelayPoller` の `self._stop`（`threading.Event`）が `threading.Thread._stop()` を隠し、`join()` が
  `TypeError: 'Event' object is not callable` で落ちた（テストで見つけた）→ `_stop_event` に。

## Changed files

- `rfid/relay.py`（新規）、`tools/rfid_relay.py`（新規）、`installer/rfid_relay_task.ps1`（新規, UTF-8 BOM + CRLF）
- `rfid/bridge.py`: `pcsc_reader_present`。
- `rfid/reader_thread.py`: `reconnect_sec` / `reader_present`、`_connect_readers`。
- `main.py`: `_make_rfid_source` / `_rfid_not_connected_message` / `_rfid_status_message` / `_report_rfid_status`、
  CLI・GUI の `RFIDThread` に中継を渡す。
- `tools/probe_pcsc.py`、`tools/register_cards.py`: 中継が動いていれば中継を通す。
- `installer/install.ps1`: `Remove-RelayTask`（アンインストール）。
- `tests/test_rfid_relay.py`（新規 18）、`tests/test_rfid.py` / `tests/test_thread_health.py`（試し直さない設定を明示）。

## 確かめたこと

- 実プロセス: `python tools/rfid_relay.py --port 18792 serve`（pyscard の無い環境）→ `status --wait 2` が
  「中継: 動いています（リーダー 0/11 台）」+ 11 台「つながっていません」+ FAIL、ログに「5 秒ごとに試し直します」。
- テスト: 中継の HTTP → `RelayBridge` → 実際の `RFIDThread` で `RFIDEvent`（seat 2）が出る / 中継が止まった読み取りは
  使わない / つながらないリーダーを試し直す / config の変更でリーダーを差し替える / 1 周の例外で止まらない /
  プロキシの環境変数があっても届く / `RFIDThread` がリーダーを待って `running` になる / CLI の表示 /
  `probe_pcsc check` と `register_cards run` が中継を通る / タスクのスクリプトの BOM・CRLF・中身。
- `pwsh` で `installer/rfid_relay_task.ps1` と `installer/install.ps1` の構文解析 OK。

## Test results

- `tests/test_rfid_relay.py` ほか RFID 関連 213 passed。
- 全体 `pytest tests/ --ignore=tests/test_vision.py`: 2328 passed（pwsh あり = インストーラのテストも実行）。
  `ruff check .` OK。

## Remaining

- 店舗 PC での確認（タスクの登録 → `rfid_check` が中継を通して PASS → ロガーに「11/11 台を中継から」）。
- 中継のコードを変えたときは、`installer\rfid_relay_task.ps1` をもう一度実行して中継を新しい版で動かし直す
  （更新 `update.cmd` は管理者の権限なしで動くので、動いている中継は止めない）。
- タスクの「失敗したら 1 分後に再起動」は、Python が異常終了したときに効くとは限らない（中継は 1 周の例外では
  止まらないようにしてある）。
