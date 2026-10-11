"""tools/release.py — リリース版（`stable` ブランチ）を出す（ADR-0066）。

店舗 PC は `update.cmd` で「覚えたブランチ」の最新を取ってくる（ADR-0057）。開発中の変更が他の店に入らない
ように、**店で確かめた版だけ**を `stable` に置き、店舗 PC はそこから更新する（`update.cmd -Branch stable` を
1 回実行すると以後それを覚える）。

- `status`: いまのリリース版と開発版（既定 `verify-v1`）の差（まだリリースしていないコミット）を出す。
- `promote`: 開発版の指定のコミット（既定 = origin の `verify-v1` の先頭）を `stable` に進め、日付のタグ
  `release-YYYYMMDD`（同じ日の 2 つ目からは `-2`, `-3` …）を付けて push する。
  **前に進めるだけ**（`stable` がそのコミットの祖先でなければ断る = 強制 push はしない）。

使い方（リポジトリの直下で）:
    python tools/release.py status
    python tools/release.py promote --dry-run      # 何をするかだけ表示
    python tools/release.py promote                 # origin/verify-v1 の先頭をリリース版にする
    python tools/release.py promote --ref 9ffc978   # そのコミットをリリース版にする
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

RELEASE_BRANCH = "stable"
DEV_BRANCH = "verify-v1"
TAG_PREFIX = "release-"


class ReleaseError(Exception):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} に失敗しました: {res.stderr.strip()}")
    return res.stdout.strip()


def _rev(repo: Path, ref: str) -> Optional[str]:
    out = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out or None


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    res = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
                         capture_output=True)
    return res.returncode == 0


def fetch(repo: Path, remote: str) -> None:
    _git(repo, "fetch", "--quiet", "--tags", remote,
         f"+refs/heads/{DEV_BRANCH}:refs/remotes/{remote}/{DEV_BRANCH}")
    # stable はまだ無いことがある（最初のリリースの前）
    _git(repo, "fetch", "--quiet", remote,
         f"+refs/heads/{RELEASE_BRANCH}:refs/remotes/{remote}/{RELEASE_BRANCH}", check=False)


def next_tag(repo: Path, today: date) -> str:
    base = f"{TAG_PREFIX}{today:%Y%m%d}"
    existing = set(_git(repo, "tag", "--list", f"{base}*").splitlines())
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def pending_commits(repo: Path, remote: str, ref: str) -> list[str]:
    """リリース版にまだ入っていないコミット（新しい順の 1 行ずつ）。"""
    stable = _rev(repo, f"{remote}/{RELEASE_BRANCH}")
    rng = f"{stable}..{ref}" if stable else ref
    out = _git(repo, "log", "--oneline", "--no-decorate", rng)
    return out.splitlines() if out else []


def status(repo: Path, remote: str = "origin") -> str:
    fetch(repo, remote)
    stable = _rev(repo, f"{remote}/{RELEASE_BRANCH}")
    dev = _rev(repo, f"{remote}/{DEV_BRANCH}")
    lines = []
    if stable:
        tag = _git(repo, "describe", "--tags", "--exact-match", "--match", f"{TAG_PREFIX}*", stable, check=False)
        lines.append(f"リリース版 {RELEASE_BRANCH}: {stable[:7]}{f'（{tag}）' if tag else ''}")
    else:
        lines.append(f"リリース版 {RELEASE_BRANCH}: まだありません（最初の promote で作ります）")
    lines.append(f"開発版 {DEV_BRANCH}: {dev[:7] if dev else '?'}")
    if dev:
        pending = pending_commits(repo, remote, dev)
        lines.append(f"まだリリースしていないコミット: {len(pending)}")
        lines.extend(f"  {c}" for c in pending[:30])
        if len(pending) > 30:
            lines.append(f"  …ほか {len(pending) - 30}")
    return "\n".join(lines)


def promote(repo: Path, ref: Optional[str] = None, remote: str = "origin", dry_run: bool = False,
            today: Optional[date] = None) -> dict:
    """`ref`（既定 = remote の開発版の先頭）をリリース版にする。結果（tag・commit・前の版）を返す。"""
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("手元に未コミットの変更があります（コミットするか片付けてから）。")
    fetch(repo, remote)
    target_ref = ref or f"{remote}/{DEV_BRANCH}"
    target = _rev(repo, target_ref)
    if not target:
        raise ReleaseError(f"{target_ref} が見つかりません。")
    dev = _rev(repo, f"{remote}/{DEV_BRANCH}")
    if dev and not is_ancestor(repo, target, dev):
        raise ReleaseError(f"{target[:7]} は開発版 {DEV_BRANCH} に入っていないコミットです（{DEV_BRANCH} に合流してから）。")
    previous = _rev(repo, f"{remote}/{RELEASE_BRANCH}")
    if previous == target:
        raise ReleaseError(f"{target[:7]} は既にリリース版です。")
    if previous and not is_ancestor(repo, previous, target):
        raise ReleaseError(f"いまのリリース版 {previous[:7]} が {target[:7]} の祖先ではありません"
                           "（リリース版は前に進めるだけ。巻き戻しはしません）。")
    tag = next_tag(repo, today or date.today())
    result = {"tag": tag, "commit": target, "previous": previous,
              "commits": pending_commits(repo, remote, target)}
    if dry_run:
        return result
    message = f"リリース版 {tag}（{len(result['commits'])} コミット）"
    _git(repo, "tag", "-a", tag, target, "-m", message)
    try:
        # 先にブランチ: 前に進めるだけなので、相手側で前に進んでいなければ（non fast-forward）断られる
        _git(repo, "push", remote, f"{target}:refs/heads/{RELEASE_BRANCH}")
    except ReleaseError:
        _git(repo, "tag", "-d", tag, check=False)
        raise
    _git(repo, "push", remote, f"refs/tags/{tag}")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent), help="リポジトリ（既定 = この checkout）")
    ap.add_argument("--remote", default="origin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="リリース版と開発版の差を出す")
    p = sub.add_parser("promote", help="開発版のコミットをリリース版にする")
    p.add_argument("--ref", help=f"リリース版にするコミット（既定 = {{remote}}/{DEV_BRANCH} の先頭）")
    p.add_argument("--dry-run", action="store_true", help="何をするかだけ表示する")
    args = ap.parse_args(argv)
    repo = Path(args.repo)
    try:
        if args.cmd == "status":
            print(status(repo, args.remote))
            return 0
        result = promote(repo, args.ref, args.remote, dry_run=args.dry_run)
    except ReleaseError as exc:
        print(f"[release] {exc}", file=sys.stderr)
        return 1
    verb = "（dry-run: まだ何もしていません）" if args.dry_run else ""
    print(f"リリース版 {result['tag']} = {result['commit'][:7]}{verb}")
    print(f"前のリリース版: {result['previous'][:7] if result['previous'] else 'なし（最初のリリース）'}")
    print(f"含まれるコミット: {len(result['commits'])}")
    for line in result["commits"][:30]:
        print(f"  {line}")
    if not args.dry_run:
        print("店舗 PC をリリース版に切り替える（1 回だけ。以後の update.cmd は stable から取る）:")
        print(r"  C:\PokerHandLogger\update.cmd -Branch stable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
