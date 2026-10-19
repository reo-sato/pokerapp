# 2026-10-19 ハンドの画面をテーブル表示に・お客さんの画面に要確認の印を出さない

## Goal

オーナー: 以前 GGPoker の UI を参考に作った「テーブル上にハンドが表示される」ハンドログの UI にレイアウトを直す。
お客さんの画面に要確認などのタグは要らない。

## 経緯

テーブル表示は 2026-09-11 にブランチ `claude/project-plan-review-byn3lm` で作った（コミット efbaa6b・9b3dd50・1868b5f、
ADR の草稿は同ブランチの `docs/adr/0051-hand-replay-single-screen-table-view.md`。番号 0051 はその後ファームウェアの ADR に
使われたので、文書を追いつかせるときに番号を振り直す）。開発版には入っていなかった。

## 変更

| ファイル | 内容 |
|---|---|
| `shared/hand_replay/HandReplay.tsx` | ブランチの最終版（テーブル図 + 4 列）を土台に、その後の変更を足した: コールはトータルの額（`shownAmount`）・「勝者なし」（`noWinner`）・`showHeard`（聞き取った文と補正の理由を各行の下に）。新しい `reviewMarks`（既定 true）= 要確認・訂正済のストライプと `!` / `✎` |
| `shared/hand_replay/handReplayModel.ts` / `.test.ts` | 3-way マージ（席の配置 `seatRingLayout`・短縮表記・アクションの色・プリフロップで降りた席 + 今の版の額・聞き取り）。テストはブランチの版に今の版の 6 つを足した |
| `mobile/src/screens/HandDetailScreen.tsx` | 1 画面（ScrollView なし）。`selfSeat` = 自分の席、`reviewMarks={false}`、`?test` の表示 |
| `staff/src/screens/HandTab.tsx` / `staff/e2e/staff.spec.ts` | ブランチのとおり（固定の高さ・E2E の文言） |
| `api/static/player/`・`cloud/static/player/`・`api/static/staff/` | 作り直し（`scripts/build_player_web.py`・`--target cloud`・`scripts/build_staff_web.py`） |

## 確かめ

- mobile: tsc・`npm test` 51 pass。staff: tsc・`npm test` 57 pass・E2E 8 passed（Chromium は `/opt/pw-browsers/chromium-1194`）。
- pytest 速い組 3307 passed（画面のビルドと共有の部品の写しのずれのテストを含む）・ruff。
- 見本のセッション（開発データ 845da8e6 + 真のアクション）で iPhone の幅のスクリーンショット: ハンド 4・9 がテーブル表示で
  1 画面に収まる、自分の席が琥珀色、要確認の印なし。

## 追記: 席を 8 席に固定（同日, オーナー）

テーブル図の席を 1〜8 の席番号の位置に固定し（`tableSeats` / `TABLE_SEATS` = 8、席番号が 8 を超えるときだけ広げる）、そのハンドに
居ない席番号は破線の「空席」にした（実際の卓と同じ並び）。前は居る人だけを詰めて並べていた。mobile 52 / staff 58 tests、E2E 8、
pytest 3307、見本の 4 人卓（席 5〜8）と 8 人卓で iPhone 13 の幅のスクリーンショットを確かめた。

手前は席 4 と席 5 の間にした（`seatRingLayout(count, front)` の `front` = `TABLE_FRONT` 3.5、奥は席 8 と席 1 の間。オーナー）。
