# ISSUE-0041: リポジトリが公開で、店舗の音声と書き起こしが誰でも取り出せた

- Status: Mitigating（オーナーが非公開にする・2026-11-20）
- 見つけた日: 2026-11-20（今後の計画の Fable の監査で指摘 → GitHub の API で確かめた: `visibility: public`、フォーク 0）

## expected

- 店舗の音声は公開しない（CLAUDE.md「店舗ログのメモ」・`data/store/README.md`）。
- 卓の会話を含む書き起こしも外に出さない（ADR-0072 D2・紙の文面）。

## actual

リポジトリ `reo-sato/pokerapp` が公開だった。誰でも取り出せたもの:

- `store-data` ブランチ: 店舗の発話の音声（分割 zip、10/14 以降）。
- `data/store/<日付>/`: 文字のログ。書き起こし・pokerapp.log・config（トークン類は伏せ済み）・真のアクション。
- `tests/fixtures/store/`: 開発データ。書き起こし・真のアクション・席の名前。

公開になった時期は分からない（GitHub は公開の履歴を返さない）。店舗 PC の更新（`irm … bootstrap.ps1 | iex`・`codeload`）が
認証なしで動いていたので、店舗 PC を入れた 2026-09-24 には公開だったとみられる。

## root cause

- 店舗 PC の更新を認証なしの取得で作った（ADR-0057）。リポジトリが公開であることが前提になっていた。
- そのあと、店舗のデータを同じリポジトリに置く運用（2026-10-28 オーナー）にしたとき、公開であることを確かめなかった。

## fix

- オーナー: リポジトリを非公開にする（GitHub → Settings → Change visibility → Private）。
- コード（2026-11-20）:
  - インストーラ（`installer/bootstrap.ps1`・`install.ps1`）が、読み取りだけのトークン（ユーザーの環境変数
    `POKERAPP_GITHUB_TOKEN`）があれば認証つきで取得する（API の commits・zipball・contents）。
  - 更新の 1 行は、トークンを伏せ字で訊く形にした（`docs/installation.md`）。
  - 店舗 PC の今の版はトークンを知らないので、bootstrap が先に新しいインストーラに入れ替える。

## 残る危険・あとで決めること

- 公開だった間に取られた写しは消せない。フォークは 0。
- 非公開にすれば、GitHub 上の履歴は外から見えない。キャッシュ・検索の写しを急いで消したいときは GitHub のサポートに頼む。
- コードを公開に戻す場合は、店舗のデータを別の非公開のリポジトリに分け、このリポジトリの履歴から消す（履歴の書き換え = やり直せない。オーナーの判断）。
- 紙の文面（ディーラー用・お客さん用）に、外部への送信・保存の扱いをもう一度照らす（今後の計画 0）。

## regression test

- `tests/test_installer.py::TestFieldFindings::test_a_private_repository_is_fetched_with_the_token`。
