# ADR-0067: クラウド最小（フェーズ 2）— Cloud Run（東京）+ Supabase Postgres（東京）、店舗 PC からの一方向の写し、店外での閲覧は店で渡すリンクで

## Status

Accepted（2026-10-11 設計。コードは含まない。実装は下の 2a〜2e の別タスク）

## Date

2026-10-11

## Context

製品化の方針（ADR-0063）でフェーズ 2「クラウド最小」を次の中身と決めた: Cloud Run + Postgres / 店舗 ID と同期の受け口 /
会計の書き込みを閉じる / 店外での履歴閲覧。関門は「公開前のセキュリティ確認がすべて済む」。ADR-0063 の Follow-up で、
着手時に次の 4 つを決めることにしていた。

1. クラウドモードで公開するエンドポイント（有効・無効）
2. 店舗 ID と端末トークン
3. Postgres の事業者
4. ホールカードの公開範囲の設定の形

フェーズ 1（ADR-0064〜0066）で、店舗 PC は画面なしの店舗サーバー（`--serve`）がサインイン時に自動で動き、リリース版
（`stable`）から更新できるようになった。同期の常駐（`SyncScheduler`）は、相手のクラウドが無いのでフェーズ 2 で一緒に行う。

オーナーの決定（2026-10-11）: ホスティングは推奨（Cloud Run 東京）、**データベースは Supabase**。

現状の同期（ADR-0022/0032）は「全ストアの snapshot を丸ごと渡し、相手が `core/sync.py` で merge する」対称な方式で、
LAN の 2 台を前提にしている。クラウドに向けるには、(a) 店舗をまたいで混ざらないこと、(b) ハンドが増えても毎回すべてを
送らないこと、(c) クラウドが乗っ取られても店舗 PC のデータが変わらないこと、が足りない。

## Decision

### D1. フェーズ 2 の同期は「店舗 PC → クラウド」の一方向だけ

- 店舗 PC がクラウドへ**送るだけ**。店舗 PC はクラウドから何も受け取らない（フェーズ 2 ではクラウドで生まれるデータが無い）。
  → **クラウドが乗っ取られても、店舗 PC の会計・ハンド・お客さんの記録は変わらない**。
- クラウドで生まれるデータ（LINE / Google のサインアップ、店外からの注文）はフェーズ 3 以降。そのときクラウド → 店舗 PC の
  取り込みを足すが、**取り込むのは種類を限る**（サインアップした player と pending の注文だけ。会計・ハンドは取り込まない）。
- 送るもの: players / sessions（席の割り当て込み）/ ledger_entries / point_entries / settlements / order_requests /
  ハンド（1 ハンドずつ）/ ハンドの訂正（ADR-0036）/ 店舗の設定（公開範囲の既定など, D6）。
- **送らないもの**: 音声・録音（`recording`）、PIN の credential・`auth_identity`（node-local）、遠隔操作の queue、
  真のアクション（`ground_truth`, 評価用）、卓状態、推定器の影のファイル（正本になったら送る = Follow-up）。
  ハンドの中の**聞き取った文（`raw_text`）と監査用の項目**も送る前に落とす（クラウドの画面で使わない。持たないのが一番安全）。

### D2. 送り方: 変わったレコードだけを、まとめて HTTPS で

- 受け口は `POST /api/cloud/v1/sync/push`（端末トークン, D3）。本文は
  `{"protocol": 1, "store_pc": {版の情報 = ADR-0066}, "records": {種類: [レコード…]}, "hands": [{session_id, hand_id, hand}…],
  "corrections": [...], "store_settings": {...}}`。1 回の本文は 4 MB まで（超えれば店舗 PC が分けて送る）。
- 店舗 PC は「クラウドが受け取ったと答えたレコードの指紋（sha256）」を `cloud_sync_state.json`（git 管理外・更新で保持）に覚え、
  **指紋が変わったレコードだけ**を送る。ハンドは 1 ハンドずつ指紋を取る（セッションのファイル丸ごとは送らない）。
- クラウドは受け取ったレコードを、**契約の schema（`docs/contracts/schemas/`）で検査**してから、店舗ごとに
  `core/sync.py` の解決関数（`_resolve_player` / `_resolve_session` / `_resolve_settlement` / `_resolve_order` …）で
  1 件ずつ merge する（同じものを 2 回送っても・順番が前後しても同じ結果 = ADR-0022 の性質をそのまま使う）。
  schema に合わないレコードは断り、理由を返す（店舗 PC はログに 1 回だけ出し、次の変更まで送り直さない）。
- 店舗 PC の常駐: 店舗サーバー（`--serve`, ADR-0065）の中で `SyncScheduler`（ADR-0032）を動かす。既定 60 秒ごと。
  失敗したら間を空ける（60 秒から倍々、最大 15 分）。ネットが切れていても店内の運用は何も変わらない。
  状態（最後に成功した時刻・送れていない件数・最後のエラー）を `/api/health` の `cloud` に出す（スタッフ画面にも出す）。
- 設定（`config_default.json` に項目の追加だけ）: `cloud.enabled`（既定 false）/ `cloud.url` / `cloud.interval_sec`（60）。
  端末トークンは config ではなく `cloud_credentials.json`（git 管理外・更新で保持・バックアップ対象）に置く。

### D3. 店舗 ID と端末トークン

- クラウドの `stores`（店舗 ID = UUID・店名・契約状態・作成日）と `store_devices`（店舗 ID・端末トークンの sha256・
  作成日・最後に使った時刻・取り消した時刻）。**トークンそのものはクラウドに保存しない**（ハッシュだけ）。
- 端末トークンは 32 バイトの乱数。クラウドは受け取ったトークンのハッシュで店舗を決め、**以降の読み書きはすべてその店舗 ID で絞る**。
  店舗 PC が送るデータに店舗 ID は入れない（送り手が名乗った店舗 ID は信じない）。
- **クラウドの中の主キーはすべて「店舗 ID + 元の ID」**（`(store_id, player_id)` / `(store_id, session_id)` /
  `(store_id, session_id, hand_id)` …）。UUID は店舗をまたいで重ならないが、別の店舗が同じ ID を送って他店の記録を
  上書きすることを、仕組みで起こらないようにする。店舗をまたぐ同じ会員（ADR-0063 D4）はフェーズ 3 で `auth_identity`
  から結ぶ（店舗 PC の player_id は店舗ごとのまま）。
- 店舗の登録（フェーズ 2 = 自店と試験店だけ）: 運営用の**管理トークン**（Secret Manager にだけ置く、未設定なら管理の受け口は無効）で
  `POST /api/cloud/v1/admin/stores` を呼ぶ道具 `tools/cloud_link.py` を店舗 PC で 1 回実行する
  （管理トークンを伏せ字で入力 → 店舗を作り → 返ってきた端末トークンを `cloud_credentials.json` に保存。管理トークンは保存しない）。
  端末トークンの取り消し・発行し直しも同じ道具で行う。運営用の画面とちゃんとした運営アカウントはフェーズ 4。

### D4. クラウドモード = 別のアプリ（許可した受け口だけを足す）

- `api/server.py` の `create_app` に「無効にするフラグ」を足すのではなく、**`cloud/app.py` に `create_cloud_app()` を新しく作り、
  クラウドで出す受け口だけを登録する**（許可リスト方式。会計を書く受け口は、そもそもクラウドのアプリに存在しない）。
  読み取りの中身（`api/read_models.py`）は店内と共通にする。
- フェーズ 2 でクラウドが出す受け口:

  | 受け口 | 誰が | 中身 |
  |--------|------|------|
  | `GET /api/health` | 誰でも | 生きているか（版は出さない = 攻撃の手がかりを減らす） |
  | `POST /api/cloud/v1/sync/push` | 店舗 PC（端末トークン） | D2 |
  | `POST /api/cloud/v1/viewer-links` | 店舗 PC（端末トークン） | 閲覧リンクを発行・取り消し（D5） |
  | `POST /api/cloud/v1/auth/link` | お客さん（閲覧リンク） | リンクを閲覧用のログインに換える（D5） |
  | お客さんの読み取り（`/api/players/{自分}`・自分のセッション・ハンド・会計の要約） | お客さん（ログイン） | **自分の player_id だけ**。他人の ID を指定すれば 404 |
  | `POST /api/cloud/v1/admin/...` | 運営（管理トークン） | 店舗の作成・端末トークンの発行と取り消し（D3） |
  | `/`（お客さん向け画面） | 誰でも | `mobile/` の web 版（クラウド用のビルド, 2c） |

- **出さないもの**: スタッフの受け口すべて（`/api/staff/...` = 会計・注文の確定・座席・遠隔操作・同期の snapshot/merge）、
  スタッフ用画面 `/staff/`、名前選び（L0 = 全員の名前の一覧 `/api/players`）、注文の POST、PIN のログイン（店内用, ADR-0063 D5）。
- 受け口の一覧はテストで固定する（クラウドのアプリに `/api/staff/` で始まる受け口が 1 つでもあれば失敗）。
- 画面と API は同じ origin（Cloud Run の 1 コンテナ）なので CORS は開けない。

### D5. 店外での閲覧（フェーズ 2）は「店で渡す閲覧リンク」

- LINE / Google のログインはフェーズ 3。フェーズ 2 では、**スタッフが iPad で QR を出し、お客さんが自分のスマホで読み取る**
  閲覧リンクで店外から自分の履歴を見られるようにする（会計の精算のときなどに渡す想定）。
- リンクは `https://<クラウド>/#link=<32 バイトの乱数>`。**乱数は URL の `#` の後ろに置く**（`#` 以降はサーバーに送られないので、
  アクセスログ・途中の機器に残らない）。画面が乱数を `POST /api/cloud/v1/auth/link` に送り、ADR-0027 と同じ形の署名付きトークン
  （有効 30 日、player_id + 店舗 ID 入り）に換えて端末に保存する。以後はそのトークンで読む（期限が切れたら同じリンクで入り直せる）。
- クラウドは乱数のハッシュだけを `viewer_links`（店舗 ID・player_id・作成・取り消し・最後に使った時刻）に持つ。
  **1 人 1 本**（発行し直すと前のリンクは使えなくなる）。スタッフ画面から取り消せる（スマホをなくした・他人に見られた）。
  署名付きトークンには発行したリンクの番号を入れ、リンクを取り消せばそのトークンも使えなくする。
- 発行は店舗 PC が端末トークンでクラウドに頼む（スタッフ用画面 → 店舗 PC のスタッフ API → クラウド）。
  ネットが切れているときは発行できない（その旨を画面に出す）。
- フェーズ 3 で LINE / Google のログインを足しても、閲覧リンクは「LINE を使わない人」向けに残せる
  （LINE で入った人は、閲覧リンクで見ていた player と結べる = ADR-0030 の merge）。
- 帰結: リンクを他人に渡せばその人も見られる（URL を知っていれば見られる、という性質）。画面に「このリンクは人に渡さないで
  ください」と出し、取り消しを簡単にする。ホールカードの公開範囲（D6）が「自分の手札 + 見せた手札」なら、漏れて見えるのは
  その人自身の手札まで。

### D6. ホールカードの公開範囲（店舗の既定 + セッションごとの上書き）

- 値は 2 つ: `all`（記録した手札を全員分, いまの動き = ADR-0059 D7）/ `own_and_shown`（自分の手札 + ショーダウンで見せた手札だけ）。
- **店舗の既定**: 店舗 PC の config `viewer.hole_cards`（既定 `all` = いまの動きを変えない）。同期で `store_settings` として送る。
- **セッションごとの上書き**: session に任意の項目 `hole_card_visibility` を足す（session schema `1.0` → `1.1`, 任意の項目の追加 =
  additive）。スタッフ API / スタッフ画面のセッションの設定から変える。無ければ店舗の既定。
- 適用は**読み取りの中身（`api/read_models.py`）の 1 か所**で、店内・クラウドの両方に効く。見ている人（player_id）が分からない
  読み取り（店内の名前選び）では「見ている人 = 選んだ人」とみなす（名前選びは誰でも誰にでもなれるので、店内では公開範囲は
  目安にとどまる。店外 = クラウドでは閲覧リンク / ログインで本人が決まるので守られる）。
- 「見せた手札」= ハンドの `showdown`（ADR-0062）に載っている席の手札。見せずにマックした手札・フォールドした手札は見せない。
  手札が入る項目（`players[].hole_cards`・`showdown`・推定の重ね合わせ・リプレイ用の要約…）は**テストで列挙**し、どれか 1 つでも
  漏れたら失敗するようにする（新しい項目が増えたときに漏れないように）。
- スタッフの読み取り（`/api/staff/...`）には適用しない（スタッフはすべて見る）。

### D7. 保存: Supabase の Postgres（東京）を「ただの Postgres」として使う

- Supabase のプロジェクトを **Northeast Asia（Tokyo）** に作る。使うのは **Postgres だけ**。Supabase の Auth・自動の REST API・
  Storage・Realtime は使わない。
- テーブルは**公開されないスキーマ `pokerapp`** に置く（Supabase の自動 API が出すのは `public` だけ）。念のため全テーブルで
  RLS（行単位のアクセス制御）を有効にし、ポリシーは作らない（= Supabase の匿名キーでは何も読めない）。クラウドのアプリは
  専用のデータベースユーザーで接続する。
- 形: レコードは**契約の JSON をそのまま `jsonb` で 1 行**に持つ（`store_id`・キー・`body jsonb`・`updated_at`）。
  検索に要る項目（player_id・session_id・started_at）だけ列にする。**契約の schema と同じ形なので、店舗 PC の JSON との
  変換を書かずに済み、契約が additive に増えてもテーブルを変えずに済む**。
- 読み取りは frozen の repository interface（ADR-0020）に合わせた Postgres 版（店舗 ID で絞る）を `cloud/` に作り、
  `api/read_models.py` に渡す。
- 接続: Cloud Run から **Supavisor の transaction モード（ポート 6543, IPv4）** で接続する。このモードは prepared statement が
  使えないので、ドライバ（psycopg 3）で無効にする。接続数は小さいプールで足りる。
- テーブルの作成・変更は `cloud/migrations/NNNN_*.sql` を起動時に順に当てる（適用済みの記録表 + 同時に 2 台が当てないための
  advisory lock）。
- プラン: 開発中は Free（$0。1 週間使わないと停止・自動バックアップなし）、**本物のお客さんのデータを入れる前に Pro（月 $25〜、
  毎日のバックアップを 7 日分）**に上げる。Pro にしたら 1 回、バックアップからの戻し方を試す。

### D8. Cloud Run の置き方と秘密の値

- `asia-northeast1`（東京）。コンテナ 1 つ（`cloud/Dockerfile`。リポジトリの直下を元に作る）。最小インスタンス 0（使わない時間は
  課金されない。最初の 1 回の表示が数秒遅れることがある）、最大 2（費用の上限）、メモリ 512 MiB。
- 秘密の値（Postgres の接続文字列・トークン署名の鍵・管理トークン）は **Secret Manager** に置き、環境変数として渡す。
  リポジトリと `config_default.json` には置かない（ADR-0029 D4）。署名の鍵は固定（再起動で全員のログインが切れないように）。
- デプロイ: Cloud Build のトリガーを GitHub につなぎ、**`stable` ブランチに進んだら自動でデプロイ**する（ADR-0066 と同じ「確かめた版だけ」。
  店舗 PC とクラウドの版がずれても動くよう、同期の `protocol` は番号で互換を保つ = クラウドは古い店舗 PC を受け付ける）。
  開発中の試験は別のサービス名（`pokerapp-cloud-dev`）に手で（Cloud Build の手動実行で）デプロイする。
- ドメインは当面 Cloud Run の `*.run.app`（HTTPS は自動）。独自ドメインは外販の前に決める。
- 費用の目安: Cloud Run は無料枠に収まる見込み、Supabase は Free $0 → Pro 月 $25 前後、ほかはわずか。
  Google Cloud には予算アラート（例: 月 3,000 円）を必ず設定する。

### D9. 公開前のセキュリティ確認（フェーズ 2 の関門）

次がすべて済むまで、お客さんに閲覧リンクを渡さない。

1. 店舗の分離: 店舗 A の端末トークン・閲覧リンクで店舗 B のデータを読めない・書けない（テスト）。
2. 受け口の許可リスト: クラウドのアプリにスタッフ・会計を書く受け口が無い（テスト）。お客さんは自分の player_id しか読めない（テスト）。
3. 公開範囲: `own_and_shown` で他人の手札が出ない（手札の入る項目の列挙テスト）。
4. 秘密の値: リポジトリ・イメージ・ログに無い（Authorization・トークン・リンクの乱数をログに出さない。接続文字列をエラー文に出さない）。
5. Supabase: テーブルが `pokerapp` スキーマにあり RLS が有効、匿名キーで読めないことを実際に確かめる。データベースのパスワードは長い乱数。
6. 回数制限: 閲覧リンクの交換・管理の受け口に回数制限（アプリの中で、IP ごと）。
7. 送る前の削り: `raw_text`・監査項目が送られていない（テスト）。
8. バックアップ: Pro にして戻し方を 1 回試した。
9. お客さんに見せる文面: プライバシーポリシー（利用目的・保存するもの・削除の頼み方）を画面から読める。閲覧リンクの取り消しと
   データの削除の手順がある（ADR-0029 D5）。
10. 版の固定: クラウドは `stable` から作り、依存は `requirements.txt` の範囲で固定。

### D10. 実装の順番（フェーズ 2 の中）

| 段 | 中身 | 主なファイル |
|----|------|-------------|
| 2a | クラウドの骨組み: `create_cloud_app`・Postgres の保存と読み取り・migrations・端末トークン・管理の受け口・同期の受け口。テストは保存の部分を差し替えられる形にし、Postgres の実物は CI のサービスコンテナで | `cloud/`（新設）、`.github/workflows/ci.yml` |
| 2b | 店舗 PC の送り手: 変わったレコードだけを送る・送る前に削る・`cloud_sync_state.json`・店舗サーバーの中で常駐・`/api/health` の `cloud`・`tools/cloud_link.py` | `core/cloud_sync.py`（新設）、`api/store_server.py`、`config_default.json`（追加だけ）、`installer/`（保持するファイル） |
| 2c | 閲覧リンク: クラウドの発行・交換、スタッフ API の中継、スタッフ画面の QR、お客さん向け画面のクラウド用ビルド（リンクで入る・名前選びを出さない） | `cloud/`、`api/server.py`（スタッフ API 1 つ）、`staff/`、`mobile/`、`scripts/build_player_web.py` |
| 2d | 公開範囲: config・session schema `1.1`・read model の 1 か所・スタッフ画面の設定 | `api/read_models.py`、`core/session*.py`、`docs/contracts/` |
| 2e | 置く: Dockerfile・Cloud Build・Secret Manager・予算・D9 の確認 | `cloud/Dockerfile`、`docs/cloud-setup.md` |

推論・音声（`integration/`・`audio/`・推定器の `tools/`）には触れない（ADR-0063 D8）。

## Alternatives Considered

- **今の snapshot/merge（ADR-0022）をそのままクラウドに向ける** — 新しく書くものが最も少ない。
  - Why rejected: 毎回すべてを送る（ハンドが増えるほど重い）、送り手が他店の ID を混ぜても防げない、双方向なのでクラウドが
    乗っ取られると店舗 PC に会計が入り込む。解決関数（merge の規則）は使い回し、運び方だけ変える。
- **Cloud SQL（Google の Postgres）** — Google Cloud の中で完結し、IAM で接続できる。
  - Why rejected: いちばん小さい構成でも Supabase Pro より高め、管理画面が初めての人には難しい。オーナーが Supabase に決めた。
    Postgres としてしか使わないので、あとで Cloud SQL に移すのは接続先の変更とデータの移し替えで済む。
- **Supabase の Auth・自動 REST API を使う** — ログインや読み取りを書かずに済む。
  - Why rejected: 読み取りの規則（公開範囲・自分だけ）が FastAPI と Supabase の 2 か所に分かれる。店内（Supabase なし）と同じ
    `api/read_models.py` を使えなくなる。LINE ログインも Supabase Auth の標準には無い。
- **レコードを列に分けたテーブル（正規化）** — SQL での集計がしやすい。
  - Why rejected: 契約が additive に増えるたびにテーブルの変更が要る。集計は運営画面（フェーズ 4）で必要になったときに、
    `jsonb` の項目への索引か集計用のテーブルを足す。
- **閲覧はフェーズ 3（LINE ログイン）まで出さない** — 新しい仕組み（閲覧リンク）を足さずに済む。
  - Why rejected: 店外閲覧は中核機能（ADR-0063 決定 3）で、LINE の審査・規約を待たずに自店で試せる。閲覧リンクは LINE を
    使わないお客さん向けにも残せる。
- **店舗 PC の 6 桁の PIN（L1）で店外から入る** — 仕組みはある。
  - Why rejected: PIN は店舗 PC にだけある（同期しない, ADR-0027）。インターネットに 6 桁の PIN を出すと総当たりに弱い。
- **クラウドの中でも `api/server.py` の `create_app` に「クラウドでは無効」のフラグを足す** — 1 か所で済む。
  - Why rejected: 受け口が増えるたびに無効にし忘れる危険がある。許可したものだけを登録するほうが漏れない。

## Consequences

- Positive: クラウドが乗っ取られても店舗 PC は変わらない（一方向）。店舗の分離が主キーで保証される。会計を書く受け口が
  クラウドに存在しない。読み取りの規則（自分だけ・公開範囲）は店内と共通の 1 か所。契約の JSON をそのまま保存するので
  変換のずれが起きない。
- Negative / trade-offs: クラウドの運用（Supabase・Cloud Run・Secret Manager・予算・バックアップ）が始まる。閲覧リンクは
  「URL を知っていれば見られる」性質を持つ（取り消しで対処）。開発中の Free は 1 週間使わないと止まる。
- Neutral / new constraints: 店舗 PC に `cloud_sync_state.json` と `cloud_credentials.json` が増える（保持・バックアップ対象）。
  session schema が `1.1` になる（任意の項目の追加）。

## Validation / Follow-up

- [ ] 2a〜2e の実装（各段で worklog）
- [ ] D9 のセキュリティ確認がすべて済む → 自店で閲覧リンクを渡し始める
- [ ] 推定器が正本になったら、推定の結果も送る（ADR-0056）
- [ ] フェーズ 3 の ADR: LINE / Google の実連携・クラウド → 店舗 PC の取り込み（種類を限る）・スタッフ個人アカウント・閲覧リンクと会員の結び付け
- [ ] 外販の前: 独自ドメイン・運営画面での店舗の登録（管理トークンの置き換え）

## Related Files

- 既存: `core/sync.py`（解決関数を使い回す）, `core/sync_scheduler.py`, `api/read_models.py`, `api/server.py`, `api/store_server.py`,
  `core/auth_token.py`, `docs/contracts/schemas/`
- 新設予定: `cloud/`（`app.py`・保存と読み取り・migrations・Dockerfile）, `core/cloud_sync.py`, `tools/cloud_link.py`, `docs/cloud-setup.md`

## Related Tests

- 実装時に追加する（店舗の分離・受け口の許可リスト・公開範囲の列挙・送る前の削り・同じものを 2 回送っても同じ）。

## Related Commits

- 本 ADR と同じ commit（設計のみ、コードなし）

## Supersedes / Superseded by

- Supersedes: なし。ADR-0063 Follow-up「フェーズ 2 着手時の ADR」に答える。ADR-0029 D6（クラウドの受け口の限定）を具体化し、
  ADR-0059 D7 の future scope（公開範囲）を D6 で決める。ADR-0022/0032 の snapshot/merge は店内の 2 台のあいだ用として残す。
