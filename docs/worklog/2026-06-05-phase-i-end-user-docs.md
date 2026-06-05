# Worklog: Phase I — エンドユーザードキュメント

## Date

2026-06-05

## Scope / Task

v1 リリーストラックの **Phase I**（GitHub issue #12）: 非エンジニアが「ドキュメントだけで」インストール →
運用開始できる README / セットアップ / 使い方 / トラブルシュートを整備する。統合ブランチ
`v1-integration`、作業ブランチ `claude/phaseI-user-docs`。

## Goal

- README + installation / usage / troubleshooting を、**実装と一致**する内容で作成。
- 読み上げ語彙・CLI・設定・既定 backend（pokerkit）を正確に記載。
- 対象は日本の小規模クラブ・配信者のため**日本語**。

## Changed Files

- `README.md`（新規）。
- `docs/installation.md` / `docs/usage.md` / `docs/troubleshooting.md`（新規）。
- `pyproject.toml`: `readme = "README.md"` 追加。
- docs: CHANGELOG / CLAUDE.md（構成に README + user docs を追記）/ 本 worklog。

## 出典（実装との整合）

- 設定: `config_default.json`（session/audio/rfid/recording/engine、既定 `engine.backend=pokerkit`）。
- 語彙: `core/constants.py:ACTION_KEYWORDS`（ベット/コール/レイズ/チェック/フォールド/オールイン/
  ショーダウン/ウィナー/ハンド開始 + 英語）、席は `シートN` / `seat N`、金額は半角/漢数字。
- CLI: `main.py`（既定 GUI / `--cli` / `--players` / `--export-phh`）。`--calibrate` は vision レガシーのため
  ドキュメントから除外。
- 依存/インストール: `pyproject.toml` / `requirements.txt`（PortAudio が pyaudio に必要な点を OS 別に明記）。

## Test Results

- doc 間リンク（installation/usage/troubleshooting の相互参照）解決を確認。
- `pip install -e . --no-deps` → `readme` 付きでビルド OK。
- `pytest tests/ -q --ignore=tests/test_vision.py` → **286 passed, 0 skipped**（docs 追加で回帰なし）。

## Mismatches Found During Testing

- なし。`--calibrate`（vision キャリブレーション）は廃止予定レガシーのため、混乱を避けてユーザー docs に
  載せない判断。

## Fixes Applied

- なし（新規ドキュメント）。

## Remaining Gaps / Out-of-Scope

- [ ] **Phase H（要 Windows）**: ワンクリックインストーラ完成後、README のインストール節を
      「インストーラ実行」に差し替え。
- [ ] スクリーンショット / 図（GUI 画面）。インストーラ E2E と合わせて H 以降。
- [ ] ライセンス確定（README に「未定」と明記済み。配布前にオーナーが決定）。

## Related ADRs / Issues

- ロードマップ Phase I。ADR-0012（pokerkit 既定）と整合。
- GitHub issue #12（エンドユーザー docs）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
