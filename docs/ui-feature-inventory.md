# UI 実運用機能の棚卸し（feature inventory / backlog register）

- **作成**: 2026-07-12（ユーザー確認済みの前提で棚卸し → ◎/○ を 3 ラウンドで実装）
- **最終更新**: 2026-09-11（リプレイ UI 刷新 = ADR-0051 を A11 に反映）
- **前提（ユーザー確定）**:
  1. 運用構成 = **1 卓 + iPad 1 台の自店 dogfood**（録音 PC 1 台 + player スマホ数台、店内 LAN）
  2. mobile 配布 = 当面 LAN web export、**ネイティブ配布（App Store / Play / PWA）も視野**
  3. desktop（customtkinter 5 画面）= **維持最小方針**（店舗操作は staff iPad に寄せ、
     desktop は録音 PC の監視・フォールバック）

各 UI について実運用で必要になりそうな機能を網羅列挙し、着手状態を管理する。
**優先度**: ◎ = dogfood 開始前に必要 / ○ = dogfood 中に欲しくなる / △ = ネイティブ配布・外販期。
**状態**: ✅ 実装済 / 🟡 部分的 / ❌ 未着手。実装詳細は各 ADR / worklog
（`2026-07-12-operational-ui-gaps.md` / `-round2.md` / `-round3.md`）を参照。

> 運用ルール: 新しい機能候補が出たらこの表に追記し、実装したら状態を更新する
> （CLAUDE.md の実装状況表が正仕様、本表は棚卸し・優先度づけの台帳）。

## A. player 用 mobile アプリ（`mobile/`）

| # | 機能 | 優先度 | 状態 | 備考 |
|---|------|--------|------|------|
| A1 | 手動更新（全画面の ↻ 再読込 + エラー再試行） | ◎ | ✅ | useAsync reload。web では RefreshControl が効かないためリンク方式 |
| A2 | 選択 player / ログイントークンの永続化 | ◎ | ✅ | `src/storage.ts`（web localStorage / native in-memory fallback。AsyncStorage 差し替え点を 1 ファイルに隔離） |
| A3 | 注文キャンセル（pending の取り下げ） | ○ | ✅ | ADR-0045。status `cancelled`（schema 1.1）、他人の request は not_found |
| A4 | ハンドの共有・書き出し（テキスト） | ○ | ✅ | `shared/hand_replay/handReplayText.ts` + Share/clipboard。**Phase B KPI「書き出しクリック数」の観測導線** |
| A5 | PIN の自己設定/変更 | ○ | ✅ | AuthScreen（ADR-0027 D6。初回 = pin_self_enroll 会場・変更 = 現 PIN） |
| A6 | 品切れ表示・注文不可 | ○ | ✅ | ADR-0046（menu `sold_out`） |
| A7 | 進行中セッションの自動更新（polling） | ○ | ❌ | 手動更新で代替中。要望が出たら staff と同じ 5 秒 polling を移植 |
| A8 | プッシュ通知（注文確定・精算・営業案内） | △ | ❌ | ネイティブ/PWA 配布が前提。再来店導線の本命 |
| A9 | セッション横断の通算成績（収支・ハンド数） | △ | ❌ | 会員アプリとしての再来店価値。外販期の柱候補 |
| A10 | ハンドのメモ・スター（学習ブックマーク） | △ | ❌ | Phase B のハンドレビュー価値と相性が良い |
| A11 | リプレイのステップ再生・アニメーション | △ | ❌ | scope 外と合意済（ADR-0044 D3 / ADR-0051 D2）。リプレイ自体は 2026-09-11 に**1 画面のテーブル図 + 4 ストリート列**へ刷新済（ADR-0051）。ステップ再生・ストリート連動は要望ベースで additive |
| A12 | 多言語（英語）・文字サイズ対応 | △ | ❌ | 外販期 |
| A13 | 実 LINE/Google ログイン（実 IdP HTTP） | △ | 🟡 | コアは ADR-0031 済。token/JWKS 実 HTTP + hosted は実環境タスク（既存残作業と同枠） |

## B. staff 用 iPad アプリ（`staff/`）

| # | 機能 | 優先度 | 状態 | 備考 |
|---|------|--------|------|------|
| B1 | 新着注文への気づき（pending バッジ自動更新） | ◎ | ✅ | open 卓 5 秒 polling。useAsync は再取得中 stale data 保持（ちらつき解消） |
| B2 | アクション訂正の staff アプリ内導線 | ◎ | ✅ | `HandCorrectionPanel`（リプレイ詳細から action/amount/winner_seat, B4/ADR-0036） |
| B3 | ハンド履歴・座席のライブ自動更新 | ○ | ✅ | open 卓 5 秒 polling |
| B4 | 営業日サマリ（当日全卓の締め目安） | ○ | ✅ | SessionList「本日の集計」= 中間集計のクライアント合算（API 変更なし・確定値ではない旨明示） |
| B5 | menu 編集（価格改定・品切れ・追加削除） | ○ | ✅ | ADR-0046。`MenuScreen` + `PUT /api/staff/menu`（全量置換, last-write-wins, sync 非対象） |
| B6 | player リネーム / merge の iPad 導線 | ○ | ✅ | `PlayersScreen`（作成/リネーム/統合 = ADR-0030） |
| B7 | 座席を明示的に「空ける」操作 | ○ | ✅ | 座席タブ「現在の座席をコピー」→ 行単位で外して次 hand へ |
| B8 | buy-in プリセットの編集 UI | △ | ❌ | config 手編集（ADR-0026 設計どおり）。変更頻度が低く据え置き |
| B9 | session ラベル・ブラインドの後から編集 / reopen | △ | ❌ | reopen は契約上「提供しない」判断済（運用ルールでカバー） |
| B10 | player 検索・フィルタ（チップ選択のスケール） | △ | ❌ | dogfood N=5〜10 では不要。会員数増で必須化 |
| B11 | オフライン時の操作キュー | △ | ❌ | ISSUE-0020 open question。LAN 前提では再試行で足りる |
| B12 | 計測タブ: ランダム M% 強制 drill-in | △ | ❌ | measurement-plan §2.4（「流す」率が高すぎたら後続 ADR） |
| B13 | staff token ローテーション運用 | △ | 🟡 | ログイン UI あり。失効・回転 runbook は外販前（launch review 指摘） |
| B14 | 手動 sync 実行ボタン（複数ノード） | △ | ❌ | API/client は実装済（ADR-0022）。1 卓 dogfood では不要 |

## C. desktop GUI（`gui/`, 維持最小方針）

| # | 機能 | 優先度 | 状態 | 備考 |
|---|------|--------|------|------|
| C1 | hand logger 操作 + 会計/注文/registry/session 閲覧 | — | ✅ | 既存 5 画面（iPad と並存, ADR-0037） |
| C2 | 録音系の死活表示（マイクレベル / RFID 接続数） | ○ | ✅ | dashboard ヘッダー。`AudioThread.health` / `RFIDThread.health`（Phase H の切り分けに使用） |
| C3 | プロセス監視・自動再起動（systemd/runbook） | ◎(運用) | 🟡 | コードでなく運用手順（launch review B10）。dogfood 開始前に必要 |
| C4 | バックアップのリストア導線 | △ | 🟡 | バックアップ自体は B2 で自動化済。リストアは手動手順 |
| C5 | config 編集 UI | △ | ❌ | 維持最小方針のため docs（usage.md）で代替 |

## 方針上「作らない」（棚卸しから恒久除外）

- チップ↔円の自動換算・hand 結果からの自動記帳（ADR-0016/0026）
- player 間精算（1 方向会計, ADR-0016）
- リアルタイム卓上助言 / 他プレイヤーの個別プロファイル分析（solver 提案 §3, 倫理上永続除外）
- B4 未訂正ハンドへの GTO 重ね表示（同）
- 部分 PATCH の menu 編集・menu の sync 対象化（ADR-0046 D3）

## 現況サマリ

- **◎・○ は全消化**（2026-07-12, 3 ラウンド。関連 ADR-0044/0045/0046）。
- 残は **△ 群**（ネイティブ配布・外販期に優先度を再評価）と **C3 の運用整備**。
- 次の計画上の優先は本表の外: **Phase H 実機 E2E** → **Phase A 計測の実運用**（CLAUDE.md ロードマップ）。
