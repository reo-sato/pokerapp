# Worklog: Phase H part 1 — パッケージング(H1) + CI(H4)

## Date

2026-06-05

## Scope / Task

v1 リリーストラックの **Phase H**（GitHub issue #11）のうち、この環境で完結する **H1（依存整理・
pyproject）+ H4（CI、skip 0 の品質ゲート）** を実施。**H2（PyInstaller ビルド）/ H3（コード署名）/
実機 E2E** は Windows 環境・証明書が必要なため後続。統合ブランチ `v1-integration`、作業ブランチ
`claude/phaseH-packaging-ci`。

## Goal

- `pyproject.toml` を正とし、依存を core / `[pcsc]` / `[vision]` / `[dev]` に分割。vision を core から除外。
- compatible-release pin。`requirements.txt`（core）/ `requirements-dev.txt`（test）を整理。
- `.github/workflows/ci.yml` で push/PR 時に `pytest`（vision 除外）を **skip 0** で実行。

## Changed Files

- `pyproject.toml`（新規, setuptools, `1.0.0.dev0`, `requires-python>=3.11`, entry `pokerapp=main:main`,
  packages 明示で vision/tests/tools 除外）。
- `requirements.txt`（core のみ・vision 除外・上限付与）/ `requirements-dev.txt`（新規, numpy/pokerkit/
  jsonschema/pytest）。
- `.github/workflows/ci.yml`（新規）。
- `.gitignore`（`*.egg-info/` 等 packaging artifacts）。
- docs: CHANGELOG / CLAUDE.md（構成・コマンド）/ 本 worklog。

## 設計判断

- **テストは core の重い依存を要しない**: faster-whisper / pyaudio / customtkinter / pyscard は **lazy
  import**（実行時のみ）、cv2/easyocr は vision/ のみ（製品未使用）。numpy のみ `core/events.py` が
  module-level import。よって CI は **numpy/pokerkit/jsonschema/pytest** だけ入れれば本体を直接 import して
  全テストを skip 0 で回せる（system lib 不要＝高信頼）。フル install 検証は H2（Windows）。
- **vision を core から除外**（廃止予定）。`[vision]` extra に退避。
- pokerkit は `>=0.7,<0.8`（golden fixtures の凍結挙動に整合、ADR-0012）。

## Test Results

- `pip install -e . --no-deps` → package discovery / entry point OK、全製品パッケージ import 可。
- CI 相当 `pytest tests/ -q --ignore=tests/test_vision.py` → **286 passed, 0 skipped**。

## Mismatches Found During Testing

- なし。`pip install -e .` が生成する `pokerapp.egg-info/` を `.gitignore` に追加。

## Fixes Applied

- なし（新規パッケージング設定 + CI）。

## Remaining Gaps / Out-of-Scope（Phase H の残り）

- [ ] **H2**: PyInstaller `.spec`（Whisper モデル同梱・ctranslate2 DLL hidden-import・customtkinter assets）。
      **Windows 環境が必要**。
- [ ] **H3**: コード署名（証明書取得は外部リードタイム）。
- [ ] **実機 E2E**: クリーン Windows でインストーラ→起動→1 ハンド（音声のみ・pokerkit 既定）→JSON/PHH。
- [ ] CI 拡張: フル install 検証（portaudio/tk 等の system 依存）を別ジョブで（任意）。

## Related ADRs / Issues

- ADR-0012（pokerkit 既定・pin）。ロードマップ Phase H（H1/H4）。
- GitHub issue #11（packaging/CI）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
