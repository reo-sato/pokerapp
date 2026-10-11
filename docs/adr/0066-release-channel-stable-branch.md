# ADR-0066: リリース版は `stable` ブランチ — 店で確かめた版だけを他の店に配り、どの店がどの版かを記録する

## Status

Accepted（2026-10-11, 実装済。店舗 PC（Windows）での確認は未）

## Date

2026-10-11

## Context

製品化のフェーズ 1「店舗内の仕上げ」（ADR-0063 D8）の 3 つ目。店舗 PC の更新（`update.cmd`, ADR-0057）は、
覚えたブランチ（`installer/branch.txt`, 既定 `verify-v1`）の**最新**を GitHub から取って上書きする。

- いまは 1 店でテストしながら直しており、直したらすぐ店で試す（CLAUDE.md 7b）。この速さは今後も要る。
- 他の店に配ると、開発中の変更（店で確かめていない変更）がそのまま入る。
- 「どの店がどの版で動いているか」が分からない（`/api/health` の `version` はパッケージの版 `1.0.0.dev0` だけ）。
  問い合わせの最初の質問に答えられない。

## Decision

### D1. リリース版 = `stable` ブランチ（前に進めるだけ）

- 開発版は今までどおり `verify-v1`（テストしている店はこちらから更新）。
- 店で確かめた版だけを `stable` に置く。`stable` は**前に進めるだけ**（巻き戻し・強制 push をしない）。
- 進めるときは日付のタグ `release-YYYYMMDD`（同じ日の 2 つ目から `-2`, `-3` …）を付ける（いつ何を出したかの記録）。
- 道具 `tools/release.py`: `status`（リリース版と開発版の差）/ `promote [--ref] [--dry-run]`（開発版のコミットを
  リリース版に）。開発版に入っていないコミット・巻き戻し・手元の未コミットの変更は断る。
- 店舗 PC をリリース版にするのは `update.cmd -Branch stable` を 1 回だけ（`branch.txt` が覚え、以後の
  `update.cmd` は `stable` から取る）。**新しい仕組みは足さず、既存の「覚えたブランチ」を使う**。
- 既定のブランチ（`install.ps1` / `bootstrap.ps1` の `verify-v1`）は今は変えない（テスト中の店の運用を変えない）。
  他の店に配る前に、既定を `stable` にするかを決める（Follow-up）。

### D2. 取ってきたコミットを固定し、記録する

- 更新（zip）では先に GitHub の API でブランチの先頭のコミット（40 桁）を確かめ、**そのコミットの zip** を取る
  （取っている間にブランチが進んでも、記録と中身が一致する）。確かめられなければ従来どおりブランチの zip。
- 更新とインストールの最後に `installer/installed.json`（`branch` / `commit` / `installed_at`, git 管理外）を書く。
  更新は古いインストーラが取ってきて新しいインストーラが続きをするので、コミットは `-Commit` で引き継ぐ。
- 読む側は `core/version.py`（`installed.json` が無ければ git から）: `/api/health` に `branch` / `commit` /
  `release` を additive で足す（契約 `viewer-api.md`）。`main.py --version` と店舗サーバーの起動ログにも出す。

## Alternatives Considered

- **GitHub の Release（Releases ページ）とその zip を使う** — 「リリース」らしい見た目。
  - Why rejected: インストーラに「最新の Release を探す」処理が要る。ブランチなら既存の更新の仕組みがそのまま使え、
    タグで履歴も残る。Releases ページは後から足せる（タグはある）。
- **テストしている店も含め、全店を `stable` に固定する** — 単純。
  - Why rejected: テスト中は直してすぐ試したい（7b）。チャネルを店ごとに選べるほうがよい。
- **版の記録を zip の中のファイルに入れて配る** — 更新側の処理が要らない。
  - Why rejected: どのコミットか（zip の中身）はビルドしないと書けない。取ってくる側で確かめて書く方が確実。

## Consequences

- Positive: 他の店には確かめた版だけが入る。テストしている店の速さは変わらない。どの店がどの版かを
  `/api/health`（iPad・スマホ・運営からも見える）とログで答えられる。
- Negative / trade-offs: リリースを出す手順（店で確かめる → `promote`）が増える。GitHub の API（未認証で 1 時間
  60 回）に頼る — 店舗 PC の更新の回数では問題にならない。
- Neutral: 既存の店舗 PC は、次の更新から `installed.json` が書かれる（それまでは「記録されていません」）。

## Validation / Follow-up

- [x] `tests/test_release.py`（使い捨ての Git リポジトリで promote / 前に進めるだけ / 同じ日のタグ / 開発版外の
  コミット・未コミットの変更を断る / status、`core/version.py`、`/api/health`、インストーラの中身、`--version`）
- [ ] 店舗 PC で: `update.cmd` → `main.py --version`・`/api/health` に `verify-v1 @ <コミット>` が出る。
  `update.cmd -Branch stable`（最初のリリースのあと）→ 以後 `stable` から取る
- [ ] 最初のリリース: 店舗で ADR-0064/0065 を確かめたあと `python tools/release.py promote`
- [ ] 他の店に配る前に: インストーラの既定のブランチを `stable` にするか決める

## Related Files

- `tools/release.py`, `core/version.py`, `api/server.py`（`/api/health`）, `api/store_server.py`, `main.py`（`--version`）
- `installer/install.ps1`（`Resolve-Commit` / `Write-InstalledVersion` / `-Commit`）, `.gitignore`, `docs/contracts/viewer-api.md`

## Related Tests

- `tests/test_release.py`

## Related Commits

- 本 ADR と同じ commit

## Supersedes / Superseded by

- Supersedes: —（ADR-0057 の更新の取得元に、リリース版のチャネルを足す）
- Superseded by: —
