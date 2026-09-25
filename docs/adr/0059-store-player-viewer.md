# ADR-0059: 店舗でお客さんが自分のハンドを見る — ハンドロガー（CLI）が席を記録し、viewer API が画面も配信する

## Status

Accepted（2026-09-25, 実装済。店舗 PC での確認は未）

## Date

2026-09-25

## Context

お客さん向けのハンド閲覧（`mobile/` の web 版 = M2 + 読み取り専用 viewer API = M1, ADR-0017）は
実装済みだったが、**店舗の運用（ADR-0057 の店舗 PC + `start_logger.cmd` = `--cli`）では使えなかった**。
調べると次の 5 点が塞いでいた（ISSUE-0036）。

1. **CLI が「誰がどの席か」を記録しない**。viewer の「その人のハンド」は `sessions.json` の席の記録
   （seat_assignment）を起点にハンドログを `(session_id, hand_id)` で引く（M1 の read model）。席の記録は
   GUI の座席設定（E1〜E3, ADR-0008）だけが書き、店舗で使っている `--cli` は書かない。
2. **画面を配信するものが無い**。`mobile/README.md` は「web export（`dist/`）を任意の静的サーバーで配信」
   = 店舗 PC に Node と別サーバーが要る（ADR-0057 Stage 2 の未着手項目）。API の URL
   （`EXPO_PUBLIC_API_URL`）は**ビルド時に埋め込む絶対 URL**で、PC の IP が変わると作り直しになる。
3. **viewer API は起動時にしか読まない**。hand logger は別プロセスで `sessions.json` / `players.json` を
   書くので、API を先に起動するとその後のセッション・ハンドが見えない（API の再起動が要る）。
4. **お客さんに要らない導線が出る**。staff token 付きでビルドするとハンド訂正が出る。会場が本人確認
   （`viewer_api.player_auth`）を使っていなくても PIN / LINE・Google の導線が出る。
5. **Windows ではファイルの置き換えが別プロセスの読み取りと衝突する**。`os.replace` は相手が開いている間
   `PermissionError`（共有違反）になり、書き込みが失われる（`Failed to write session DB`）。viewer が要求の
   たびに読むようにすると起きやすくなる。

オーナーの決定（2026-09-25）:

- **公開範囲**: 当面は「記録したホールカードを全員分見せる」（選択肢 3）。**後でセッションごとに公開範囲を
  決められるようにする**（future scope）。
- **席の入力**: ハンドロガーで入力する（別の画面・スタッフアプリは使わない）。

## Decision

### D1. `--cli` の席記録（`session_layer.enabled=true` のとき）

- 起動時に **session を作る**（UUID4、label = 開始時刻、blinds = 入力した SB/BB）。ログファイル名も
  この session_id になる（viewer の join キー）。
- 起動時に入力した**名前を player に結び付ける**（`PlayerRepository.find_or_create`: 前後空白を除いた
  完全一致 = 同じ名前は同じ人。merge 済みなら統合先。作る前にディスクを読み直し、別プロセスが作った
  同名を二重に作らない）。**名前を入れなかった席（空 Enter = `PlayerN`）は結び付けない**。
- ハンドの開始で席 → player を `sessions.json` に書き、ハンドの記録に `player_id` を載せる
  （E1 の既存経路 `IntegrationThread(session_repo=, seat_player_map=)` をそのまま使う）。
- `q`（終了）で **session を閉じる**（お客さんの画面で「進行中」のまま残さない）。
- 無効（既定）は従来どおり（timestamp の session_id、席の記録なし = rollback 経路）。

### D2. 席替えコマンド `seat <席> <名前>` / `seat <席> -`

- 次のハンドからその席の人を変える（`-` は空席 = 結び付けない）。席 → player の対応は
  `set_seat_player_map`、ゲーム状態の名前は `rename_seat` の AudioEvent を queue に積んで integration
  スレッドで変える（ゲーム状態の変更は integration スレッドに一元化, ISSUE-0012）。
- **ハンドの途中なら次のハンドの開始まで待つ**（1 ハンドの中で名前と player_id が食い違わない）。
- 全角の数字・空白も通す（他のコマンドと同じ, ISSUE-0030）。卓に無い席は弾く。

### D3. viewer API が画面も配信する（同じ origin）

- `create_app(player_web_dir=...)` が **`/` にお客さん向け画面**を配信する（API は `/api/` のまま）。
  全 API のルートの後に mount するので API を覆わない。未知の `/api/...` は JSON の 404 のまま。
- お客さんのスマホは **`http://<PC の IP>:8788/` を開くだけ**。画面は同じ origin の `/api/` を読むので、
  ビルドに IP を埋め込まない（PC の IP が変わっても作り直し不要）。
- 入口（`index.html` 等）は `Cache-Control: no-cache`。JS は名前に hash が入る（`_expo/` 配下）ので
  キャッシュさせてよい。更新後に古い入口が残って消えた JS を読みに行く（真っ白）のを防ぐ。
- `--viewer-api --host 0.0.0.0 --port 8788` で config より優先して待ち受けを指定できる（店舗 PC の
  ショートカットが config を書き換えずに LAN へ出す）。`--ledger` の組み込み API も同じ画面を出す。
- ランチャ `start_viewer.cmd` + デスクトップのショートカット「お客さん用 ハンド履歴 (スマホ)」。

### D4. 画面のビルドはリポジトリに含める

- `scripts/build_player_web.py` が `mobile/` を web export して **`api/static/player/`** に置く。
  設定は固定: API = `/`（同じ origin）、`EXPO_PUBLIC_PLAYER_AUTH=off`（PIN / LINE・Google の導線を
  出さない）、**staff token を渡さない**（ハンド訂正を出さない）、`mobile/.env*` を読ませない
  （開発者の staff token が混ざらない）。
- `build-info.json` に設定とソースの hash を書く。**`mobile/` を変えてビルドし直し忘れると CI が落ちる**
  （`tests/test_player_web_build.py`。hash は改行を揃えるので Windows の checkout でも一致）。
- **店舗 PC に Node は要らない**（ADR-0057 Stage 2 の「スマホ画面の API 配信」のうちお客さん向け画面を
  実現。staff アプリは対象外）。`.gitattributes` でビルドの改行変換を止める。

### D5. 要求ごとに「変わっていれば読み直す」

- `SessionRepository.reload_if_changed()` / `PlayerRepository.reload_if_changed()` が
  `(更新時刻 ns, サイズ)` を前回と比べ、変わっていれば読み直す。viewer API は `/api/` の要求ごとに
  （スレッドプールで）呼ぶ。ファイルはあるのに読めない（置き換えの瞬間など）ときは今の内容を保つ。

### D6. Windows の共有違反を短く待つ

- `atomic_write_json` は `os.replace` の `PermissionError` を、`read_json_file` は `OSError` を
  **最大 10 回 × 50 ms** 再試行する。

### D7. 公開範囲（オーナー決定）

- 当面は**記録したホールカードを全員分**見せる（HandDetail の既存表示 = ADR-0044）。本人確認は無い
  （name-pick, ISSUE-0019 と同じ）ので、**店の Wi-Fi の中だけ**で使う。
- **セッションごとの公開範囲は future scope**。置き場所は session の属性（例 `visibility`:
  全員分 / 自分の札とショーダウンだけ / セッション中は隠す）で、read model（`api/read_models.py`）が
  ハンドを返すときに適用し、`--cli` の起動時に選ぶ形を想定する。

### D8. config の 1 項目をコマンドで変える

- `tools/set_config.py <項目> <値>`（UTF-8・BOM なしで書き戻す）。店舗 PC では
  `venv\Scripts\python.exe tools\set_config.py session_layer.enabled true`。
- `load_config` は BOM 付きも読む（PowerShell 5.1 の `Set-Content -Encoding UTF8` やメモ帳が BOM を付ける）。

## Alternatives Considered

- **別の静的サーバー（`python -m http.server` / Node）で画面を配り、API の絶対 URL をビルドに埋める** —
  プロセスが 2 つ、CORS、PC の IP が変わるたびにビルドし直し。同じ origin で配る方が単純。不採用。
- **店舗 PC でビルドする** — Node が要る（ADR-0057 で不採用にした前提）。不採用。
- **CI でビルドしてダウンロード** — release の仕組み（ADR-0057 Stage 2）が要る。今は ~0.5 MB の
  バンドルをコミットし、drift を CI で検知する方が軽い。Stage 2 で見直す。
- **要求のたびに無条件で全部読み直す** — ファイルが育つと重い。更新時刻とサイズの比較で足りる。
- **ファイル監視 / hand logger からの push** — 部品が増える。viewer の要求頻度（人が画面を開いたとき）に
  対して読み直しで十分。
- **席の入力をスタッフアプリ（`staff/` の座席タブ）か GUI（E3）で行う** — 店舗は `--cli` で運用しており、
  スタッフアプリは店舗 PC に配っていない。オーナーはハンドロガーでの入力を選んだ。
- **公開範囲を今すぐ設定可能にする** — オーナーが後回しを選んだ（D7）。

## Consequences

- Positive: お客さんは店の Wi-Fi で `http://<PC の IP>:8788/` を開き、名前を選ぶだけで、その日のセッション・
  ハンド・カードを見られる。viewer を先に起動しておいても、後から始めたセッションが再起動なしで出る。
- Negative / trade-offs:
  - 本人確認が無いので、誰でも他人の名前を選べる（全員分のカードを見せる決定と整合。店の Wi-Fi 限定）。
  - 名前の一覧は**これまでに入力した全員**が並ぶ（絞り込みは未実装）。表記ゆれは別人になる（完全一致,
    ISSUE-0002 の範囲。直すときは player の merge）。
  - `sessions.json` に書くプロセスが 2 つある運用（`--cli` + `--ledger` の staff API）では、互いの更新を
    失う窓が残る（書く前の読み直しで狭まるが消えない）。店舗は今 `--cli` だけが書く。
  - 画面を作り直すたびに git の履歴が ~0.5 MB 増える。`update.cmd` は削除しないので古いバンドルが残る（無害）。
- Neutral / new constraints: `session_layer.enabled` の既定は false のまま（rollback 経路）。店舗 PC では
  `tools/set_config.py` で有効にする。`mobile/` を変えたら `scripts/build_player_web.py` を実行してコミットする。

## Validation / Follow-up

- [x] CLI の通し（名前 → ハンドごとの席の記録 → 席替えは次のハンドから → 終了で session を閉じる）
- [x] viewer API の起動後に hand logger が書いたセッション・ハンドが再起動なしで出る
- [x] 画面が `/` で出て `/api/` は JSON のまま、入口は no-cache
- [x] ヘッドレス Chromium（390×844）で 名前 → セッション → ハンド一覧 → ハンド詳細 を通し、PIN / サインアップ /
  訂正の導線が出ないこと、コンソールエラー 0 を確認（worklog）
- [ ] 店舗 PC（Windows）で `start_viewer.cmd` → お客さんの iPhone / Android で表示
- [ ] future: セッションごとの公開範囲（D7）/ 名前一覧の絞り込み（開いているセッションの人だけ等）/
  プリフロップのポット表示にブラインドを含める（リプレイ表示の細部）

## Related Files

- `main.py`（`_open_session_layer` / `_close_session_layer` / `_parse_seat_command` / `seat` コマンド / `--host` `--port`）
- `integration/engine.py`（`rename_seat`）/ `core/game_state.py` / `core/poker_engine.py`（`set_player_name`）
- `core/player_repository.py`（`find_or_create` / `reload_if_changed`）/ `core/session_repository.py`（`reload_if_changed`）
- `core/atomic_io.py`（再試行）/ `core/config.py`（BOM）/ `tools/set_config.py`
- `api/server.py`（`PLAYER_WEB_DIR` / `_PlayerWebFiles` / 読み直しの middleware / `run_server(host, port)`）
- `scripts/build_player_web.py` / `api/static/player/` / `mobile/App.tsx` / `mobile/app.json` / `mobile/public/index.html`
- `start_viewer.cmd` / `installer/install.ps1`（ショートカット）

## Related Tests

- `tests/test_cli_session_layer.py` / `tests/test_viewer_api_store.py` / `tests/test_player_web_build.py`
- `tests/test_set_config.py` / `tests/test_installer.py::TestLaunchers`

## Related Commits

- （本 ADR と同じコミット）

## Supersedes / Superseded by

- Supersedes: —（ADR-0057 Stage 2 のうち「お客さん向け画面の API 配信」を実現）
- Superseded by: —
