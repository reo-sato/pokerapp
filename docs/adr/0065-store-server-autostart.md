# ADR-0065: 店舗サーバー（画面なし）を店舗 PC のサインイン時に自動で動かす — 会計の書き手は 1 プロセスに限る

## Status

Accepted（2026-10-11, 実装済。店舗 PC（Windows）での確認は未。起動の時点は同日オーナー決定で「サインイン時」）

## Date

2026-10-11

## Context

製品化のフェーズ 1「店舗内の仕上げ」（ADR-0063 D8）の 2 つ目の作業。いまの店舗 PC は、スタッフが RDP でつないで
デスクトップのショートカットを 1 つずつ起動する（ADR-0057）。

- お客さん・iPad 向けの API は、会計画面（`--ledger`, customtkinter の窓）に組み込まれている（ADR-0018）か、
  読み取り専用の `--viewer-api`。どちらも誰かが起動しないと動かず、PC の再起動（Windows Update など）で止まる。
- 他店へ配る製品（ADR-0063）では、営業のたびに RDP で窓を開けて起動する運用は成り立たない。
- 会計（`ledger.json`）と注文（`order_requests.json`）は、メモリの内容をファイルごと書き換える。2 つのプロセスが
  書くと、あとから書いた方が相手の変更を消す（単一書き手, ADR-0018/0020）。いまは「会計画面を 1 つだけ起動する」
  という運用で守っている。
- 録音（ハンドロガー `--cli`）は、セッションの初めに席と名前をスタッフが入れる対話式。RDP のセッションの中で
  マイク（と RFID の中継, `installer/rfid_relay_task.ps1`）を使う。
- スタッフ用 iPad 画面は ADR-0064 で店舗 PC から配信できるようになった（会計の操作は iPad でできる）。

## Decision

### D1. 画面を持たない「店舗サーバー」 `python main.py --serve`

- 1 プロセスで次を動かす（`api/store_server.py`）:
  - **会計の書き手の API** = 会計画面に組み込まれていた API と同じもの（`create_store_app`。注文 write・スタッフの
    会計 write が有効、お客さん向け画面 `/`・スタッフ用画面 `/staff/`）。会計画面もこの関数を使う（同じ API が出る）。
  - **卓モニタ**（`tools/table_monitor.py`, 8790, 既定で有効）と、任意で**真のアクション入力**（8791, 既定は無効）。
- 待ち受けは `--host` / `--port` > config `store_server.bind_host` / `bind_port` > `viewer_api.bind_port` > 既定
  （`0.0.0.0:8788`）。目的が iPad・スマホからの利用なので既定で LAN に出す（受け付けは Windows のファイアウォールで
  店内の LAN に限る = `docs/installation.md` §0 の規則がそのまま効く）。staff_token・player_auth は `viewer_api` の値。
- ログは `logs/store_server.log`（5 MB × 5 世代）。
- **録音は含めない**（対話式で RDP のセッションの中で動かすため）。

### D2. 会計の書き手は 1 プロセス — OS のファイルロック（`core/store_lock.py`）

- 店舗サーバーと会計画面（`--ledger`）は起動時に `store_writer.lock` の排他ロックを取り、取れなければ起動しない
  （誰が持っているかを表示する。会計画面は「会計は iPad の /staff/ で」と案内する）。
- ロックは OS のもの（Windows `msvcrt.locking` / それ以外 `fcntl.flock`）で、プロセスが落ちれば OS が外す
  （残ったファイルが次の起動を妨げない）。Windows はロックした範囲を他から読めないため、ロックは先頭から離れた
  1 バイトにかけ、先頭に持ち主（mode・pid・時刻）を書く。

### D3. Windows のタスクスケジューラで、サインイン時に・止まっても起動し直す

- 店舗 PC は**通電で起動し `pokerpc` で自動サインインする**（店舗 PC の手順書: BIOS の AC Power Loss = Power On、
  netplwiz の自動サインイン、スリープなし）。その**サインインのとき**に動かす（オーナー決定 2026-10-11）。
- `installer/store_server_task.ps1`（ダブルクリックは `server_autostart.cmd`。**管理者の権限は要らない**）がタスク
  `PokerStoreServer` を登録して起動し、`/api/health` の応答を確かめる。`-Remove`（`server_autostart.cmd remove`）で消す。
- トリガー: **サインイン時**（`-AtLogOn`、このユーザー）+ **5 分ごと**（`MultipleInstances IgnoreNew` = 動いていれば
  何もしない。止まっていれば起動し直す）。加えて「失敗したら 1 分ごとに再起動」（`RestartCount 999`）。二重に起動しても
  D2 のロックで 2 つ目は終わる。
- **サインインしたユーザーの権限**（`LogonType Interactive`, `RunLevel Limited`。パスワードは保存しない）。実行は
  **`pythonw.exe`**（窓を出さない。サインインした画面に窓が出ると、RDP でつないだスタッフが閉じて止めてしまう）。
  ログは `logs/store_server.log`。
- **サインアウトすると止まる**（RDP は右上の × で閉じる = 切断。`docs/installation.md` の既存の運用と同じ）。
- プロセスの中: 卓モニタなどは止まったら起動し直す（5 秒から倍々に最大 60 秒、60 秒以上動けば戻す）。API が待ち受け
  られない（ポートが使われている）ときは終了コード 1 で終わり、タスクが起動し直す。
- インストーラ（`install.ps1`）: 更新では依存の入れ替えの前にタスクを止め、終わったら新しい版で起動する（どちらも
  失敗しても続ける = 5 分以内に自動で起動する）。アンインストールでタスクを消す。デスクトップに「店舗サーバー
  自動起動の登録」。手で動かして確かめる `start_server.cmd`。
- 店舗 PC の手順書 §10 の「PokerLogger AutoStart」（古い置き場所の `python main.py` をサインイン時に起動）は、店舗 PC に
  登録されていない（オーナー確認 2026-10-11）。本タスクがその役目（のうち画面を持たない部分）を担う。

## Alternatives Considered

- **会計画面（`--ledger`）をログオン時に自動起動する** — 新しいコードが要らない。
  - Why rejected: 窓（customtkinter）があるので誰かがログインしていないと動かない。RDP を閉じ方によって止まる。
- **Windows サービスにする（pywin32 / NSSM）** — サービスの管理画面で扱える。
  - Why rejected: 依存（pywin32 / 外部の exe）が増える。タスクスケジューラで要件（起動時・再起動）が満たせ、既に RFID の
    中継で使っている。
- **見張り役のプロセスが店舗サーバーを子プロセスとして起動し直す** — 再起動を自前で制御できる。
  - Why rejected: タスクを止めたときに子プロセスが残り得る（ポートとロックを持ったまま）。5 分ごとのトリガー +
    IgnoreNew + ロックで同じことができる。
- **PC の起動時に、サインインしていなくても動かす（`-AtStartup` + `LogonType S4U`）** — サインアウトしても止まらない。
  - Why rejected: 店舗 PC は自動サインインするので要らない。登録に管理者の権限（確認の画面）が要る。オーナー決定は
    「サインイン時に起動」（2026-10-11）。サインアウトで止まる点は、既存の運用（RDP は × で切断）で避ける。
- **SYSTEM の権限で動かす（RFID の中継と同じ）** — 登録が単純。
  - Why rejected: ネットワークに出る API が最大の権限で動く。サインインしたユーザーの権限で足りる。
- **ハンドロガーも自動起動する** — 起動の手間がさらに減る。
  - Why rejected: 席と名前の入力が要る対話式。マイクと RFID の中継は RDP のセッションの中で使う。iPad からの遠隔操作
    （ADR-0039）で席を入れる形は、後のフェーズで検討する。

## Consequences

- Positive: PC が起動して自動サインインすれば、お客さん・iPad の画面と注文・会計の操作が使える（RDP で窓を開ける必要が無い）。
  落ちても 5 分以内に戻る。会計の書き手が 2 つになる事故をロックで防ぐ。
- Negative / trade-offs: 店舗サーバーが動いている間は、会計画面（`--ledger`）・`start_viewer.cmd`（同じ 8788）・
  `start_monitor.cmd`（同じ 8790）は起動できない（会計は iPad の `/staff/` で行う）。起動のたびにデータのバックアップ
  （`core/backup.py`, 30 世代）が取られるので、起動と停止を繰り返すと古いバックアップが早く押し出される。
- Neutral / new constraints: 自動起動は任意（登録しなければ従来どおりショートカットで起動）。`store_writer.lock` は
  リポジトリに入れない（`.gitignore`）。

## Validation / Follow-up

- [x] `tests/test_store_server.py`（ロック = 同じプロセス・別のプロセス・落ちたプロセス / 店舗サーバーの API と画面 /
  設定の優先順 / 起動し直しの間隔 / ロックを持たれていれば起動しない / ポートが使われていれば 1 で終わりロックを手放す /
  登録スクリプトとインストーラの中身）
- [x] Linux で `python main.py --serve` を実際に起動し、API・お客さん向け画面・スタッフ用画面・卓モニタの応答と、
  2 つ目の起動がロックで断られることを確認
- [ ] 店舗 PC（Windows 11）で: `server_autostart.cmd` → 電源を抜き差し（自動起動・自動サインイン）→ RDP でつながずに iPad から `/staff/` が開く。
  タスクの 5 分ごとのトリガーが無期限で繰り返すこと（`-RepetitionDuration` を省いた場合の扱い）を確かめる。
  プロセスを止めて 5 分以内に戻ること。`update.cmd` の前後でタスクが止まり・起動し直すこと
- [ ] フェーズ 1 の残り: リリース版での更新 / 同期の常駐（`SyncScheduler` を店舗サーバーの中で動かす）
- [ ] 起動を繰り返すときのバックアップの押し出し（起動時バックアップの間隔に下限を設けるか）

## Related Files

- `api/store_server.py`, `core/store_lock.py`, `main.py`（`--serve`, `run_ledger_view` のロック）, `config_default.json`（`store_server`）
- `installer/store_server_task.ps1`, `server_autostart.cmd`, `start_server.cmd`, `installer/install.ps1`, `.gitignore`

## Related Tests

- `tests/test_store_server.py`, `tests/test_installer.py`（.cmd の一覧）

## Related Commits

- 本 ADR と同じ commit

## Supersedes / Superseded by

- Supersedes: —（ADR-0057 Stage 2 の「自動起動」を実現。ADR-0018 の「単一プロセス所有」をロックで強制）
- Superseded by: —
