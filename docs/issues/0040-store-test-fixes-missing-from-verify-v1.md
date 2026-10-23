# Issue 0040: 10/04〜10/08 の店舗テストの直しが開発版 verify-v1 に入っていなかった

## Date

2026-10-13（見つけた日）

## Status

Fixed（マージ `d3e9071` を作業ブランチと verify-v1 の両方に push。再発の防ぎ方は下）

## Severity / Priority

- Severity: High（そのままリリース版 `stable` に進めると、店舗で直した誤り（フォールド・チェックアラウンド・訂正・読み取り装置の
  再起動など）が戻った版が他の店に配られる）
- Priority: P1

## Area

release / git（`tools/release.py`, ADR-0066）

## Expected Behavior

店舗テストの直しは、リリース版に進める（`python tools/release.py promote`）前に開発版 `verify-v1` に入っている。
ADR-0066: リリース版 `stable` は `origin/verify-v1` の先頭（または指定のコミット）から前に進めるだけで、店で確かめた版を配る。

## Actual Behavior

- 店舗テストの直しは作業ブランチ `claude/confident-hawking-5e4hff` で続けていて、**2026-10-06〜10-09 の 40 コミット**
  （マージのメッセージでは「10/04〜10/08 の店舗テストの直し」。最初は `c41ae68` = 10/06 の進行中のハンドを配ったときから、
  最後は `50e4b6f` = 10/09 のテスト方針の結果）が作業ブランチにだけあった。
- 製品化（ADR-0063〜0067）の作業は **2026-10-11 に verify-v1 の `f14671b`（2026-10-03）から**始まり、verify-v1 に 10 コミット
  （`c912187` ADR-0063 〜 `21e06da` フェーズ 2d）が積まれた。
- 2 つの系列の分かれ目は `f14671b`（`git merge-base` で確認）。verify-v1 の先頭（`21e06da`）には 10/06 以降の店舗の直しが
  1 つも入っていなかった。このまま `promote` すると、リリース版は 10/03 の店舗の版 + 製品化になる。
- 2026-10-13 に見つけた（製品化の作業と店舗テストの直しを突き合わせたとき）。

## Reproduction

1. `git log --oneline f14671b..50e4b6f`（作業ブランチ側）→ 40 件。
2. `git log --oneline f14671b..21e06da`（verify-v1 側）→ 10 件。
3. `git merge-base 21e06da 50e4b6f` → `f14671b`。

## Root Cause

- 店舗テスト中の軽い運用（CLAUDE.md §7b）では、直しをプッシュしてすぐ店舗 PC の更新コマンドを渡していた。店舗 PC は作業
  ブランチを取っていたので、店では直しが動いていたが、**作業ブランチを verify-v1 に戻す手順が無かった**。
- 製品化の作業を別の流れで verify-v1 から始めたとき、verify-v1 が店舗の最新でないことを確かめなかった。
- `tools/release.py promote` は「`origin/verify-v1` の先頭を前に進めるだけ」で、ほかのブランチに verify-v1 に無いコミットが
  あるかは見ない。

## Fix

- マージコミット **`d3e9071`**（2026-10-13, 親 = `21e06da`（verify-v1）と `50e4b6f`（作業ブランチ））で 2 つの系列を合わせ、
  `origin/verify-v1` と `origin/claude/confident-hawking-5e4hff` の両方に push した（どちらのブランチも `d3e9071` を含むことを
  `git branch -r --contains d3e9071` で確認）。

## Prevention

- `python tools/release.py promote` の前に、店舗テストの作業ブランチが verify-v1 に入っているかを確かめる:
  `git fetch origin && git log --oneline origin/verify-v1..origin/<作業ブランチ>` が空であること（空でなければ先にマージする）。
- 作業ブランチで店舗の直しを続けている間は、区切り（店舗のログの 1 回分）ごとに verify-v1 へマージする。
- 2026-10-13: マージ `d3e9071` のあとの文書の追いつき（`183cb4b` 〜 `c5eaa87`）も、作業ブランチと verify-v1 の両方に push した
  （2 つの先頭は同じ）。
- （任意の後続）`tools/release.py promote` に、指定したブランチの未マージのコミットを出して止める確かめを足す。

## Regression Test

- なし（運用の手順。上の `git log origin/verify-v1..<branch>` の確かめ）。

## Affected Files

- なし（git のブランチ）。関係: `tools/release.py` / `docs/adr/0066-release-channel-stable-branch.md`

## Related Worklog

- `docs/worklog/2026-10-11-release-channel.md` / `docs/worklog/2026-10-11-productization-plan.md`
- 作業ブランチ側の最後: `docs/worklog/2026-10-08-test-strategy.md`

## Related ADRs

- ADR-0066（リリース版は `stable`）/ ADR-0063（製品化）

## Related Commits

- `f14671b` — 分かれ目（2026-10-03）
- `c41ae68` 〜 `50e4b6f` — 作業ブランチだけにあった 40 コミット（2026-10-06〜10-09）
- `c912187` 〜 `21e06da` — verify-v1 の製品化の 10 コミット（2026-10-11）
- `d3e9071` — マージ（2026-10-13）

## Notes

- 店舗テスト中の軽い運用（§7b: CHANGELOG と作業ログだけ・CI を待たずに更新コマンド）は続けてよいが、リリースの前の確かめを
  1 つ足す。
