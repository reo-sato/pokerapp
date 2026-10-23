# 2026-09-29 ログを音声付きでまとめるショートカット（大きければ zip を分ける）

## Goal

オーナー: 「音声を含めてアップロードできるショートカットを作ってください」。ボタンの救済などの確認で音声付きのログを
何度も頼んでいた（`pack_logs.cmd --audio` を打ってもらっていた）。ダブルクリックで音声を必ず入れ、チャットに添付できる
大きさに収める。

## Changed files

- `pack_logs_audio.cmd`（新規, ASCII + CRLF）: `tools\pack_logs.py --audio %*`。
- `installer/install.ps1`: デスクトップのショートカット「ログをまとめる (音声付き・送付用)」。
- `tools/pack_logs.py`: `split_entries` — zip が `PART_LIMIT`（25 MB）を超えるときは、ログ（と manifest）を 1 つ目に入れ、
  音声を順に詰めて入りきらなければ次の zip へ（`_1of3.zip` の名前、manifest に `parts`）。大きさは圧縮前で見積もる
  （WAV は無圧縮で入れるので見積もりどおり）。`--part-mb`（0 = 分けない）。分けたら全部の名前と「N 個すべてを添付」を出す。
- `tools/eval_store.py`: 入力に zip を複数渡せる（同じ一時フォルダに展開 = 1 つの zip と同じ）。
- tests: `tests/test_tools_pack_logs.py`（分ける・全部を合わせると 1 つと同じ・2 つ目以降は音声だけ・小さければ 1 つ・
  main の表示）、`tests/test_tools_eval_store.py`（分けた zip をまとめて読む）、`tests/test_installer.py`（ランチャと
  ショートカット）。

## Expected vs implemented

- これまでの音声付きの zip（9/29 の 3 セッションで 18.5 MB）は 1 つのまま。長いセッションで 25 MB を超えたら分かれる。
- 従来のショートカット（音声は 25 MB までなら入れる）は変えていない。

## Test results

- 変更部分: pack_logs / eval_store / installer のテスト通過。
- 全体: pytest 通過、ruff 通過。

## Remaining gaps

- チャットの添付の上限が 25 MB より小さい環境があれば `--part-mb` で下げる。
- `docs/usage.md` のショートカットの説明は、まとめて直すとき（CLAUDE.md §7b）に。

## Related commits

- （この worklog と同じ commit）
