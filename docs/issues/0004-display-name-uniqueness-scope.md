# Issue 0004: display_name の uniqueness 仕様の将来拡張が未確定

## Date

2026-05-22

## Status

Open

## Severity / Priority

- Severity: Low（S1 の現仕様は意図どおり動作。将来運用で顕在化しうる）
- Priority: P3

## Area

player registry (S1) / future-scope

## Expected Behavior

S1 の現仕様（意図どおり実装済・テストで固定）:

- `display_name` は前後空白を除去した上で **完全一致** の重複のみを拒否する。
- 大文字小文字・全半角・前後以外の空白（連続スペース等）は **別名として許容** する。
- rename 時も同じ判定（自分自身との一致は許容）。

これは `PlayerRepository._validate_name()` が source of truth であり、
`tests/test_player_repository.py` / `tests/test_player_registry_gui.py` で固定済み。

## Actual Behavior

現仕様どおりに動作する。ただし運用上、以下が将来問題化しうる open question として残る:

1. "Alice" と "alice" を別 player として作れる（大文字小文字を区別する）。
2. "山田太郎"（全角）と "山田太郎"（半角混在）を別 player として作れる。
3. "Bob " と "Bob"（内部以外の空白）は strip で同一視されるが、"Bo b"（内部空白）は別名。
4. 同名の別人（実在の同姓同名）を区別する手段が display_name しかない。

## Reproduction

仕様レビューによる確認（バグではなく scope 判断）:

1. `repo.create_player("Alice")` 後に `repo.create_player("alice")` が成功する。
2. これは CLAUDE.md § Player Registry の「大文字小文字・全半角の厳密同一視は今回 scope 外」と整合。

## Root Cause

S1 では「display_name のみを属性に持つ最小実装」を意図的に選んだため、人を一意に
識別するキーが display_name しかない。厳密な正規化や同名区別は S1 のスコープ外と判断した。

## Fix

未対応（S1 では意図的に対応しない）。将来必要になった場合の選択肢:

- 正規化キー（casefold / NFKC 正規化 / 空白圧縮）での重複判定を追加する。
- 同名を許容しつつ、display_name 以外の識別属性（メモ / 整理番号等）を追加する。
- session 参加履歴など文脈情報での区別 UI を用意する。

いずれも `display_name` 以外の属性追加や正規化ポリシーの確定を伴うため、必要時に
別 ADR を起こして判断する。

## Regression Test

S1 の現仕様は以下で固定済み（将来変更時にこのテストの更新要否を判断する）:

- `tests/test_player_repository.py::TestCreate::test_duplicate_after_strip_rejected`
- `tests/test_player_repository.py::TestCreate::test_create_strips_whitespace`

将来 casefold 等を導入する場合は `test_case_insensitive_duplicate_rejected` 等を追加する。

## Affected Files

- `core/player_repository.py`（`_validate_name`）

## Related Worklog

- `docs/worklog/2026-05-22-s1-player-registry.md`

## Related ADRs

- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`

## Related Commits

- 本 issue と同じコミット（S1 player registry）

## Notes

S1 は「最小実装 + validation をテストに固定」が完了条件。本 issue は仕様の意図的な
線引きを記録する risk register であり、Open のままで問題ない。
