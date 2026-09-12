# ADR-0037: 店舗用 staff iPad アプリ — staff API 上のタッチ統合 front-end

## Status

Proposed

## Date

2026-06-16

## Context

店舗（スタッフ）操作は現状すべて **PC 上の customtkinter デスクトップ GUI**（`gui/`）に集約されており、
かつ **機能ごとに別プロセス・別ウィンドウを CLI フラグで個別起動**する構成になっている
（`main.py`: 既定 GUI=hand logger / `--players` / `--sessions` / `--ledger`）。これには UX 上の問題がある:

- **iPad で動かない**: customtkinter はデスクトップ専用・マウス前提・タッチ非対応。店舗の運用端末を
  iPad にしたいという要件（本 ADR の発端）に対し、現 GUI は移植不能。
- **統一アプリがない / 起動が分断**: registry / session viewer / ledger が別ウィンドウ・別コマンド。
  画面間遷移がなく、スタッフは複数ウィンドウや CLI を行き来する。
- **セッション開始が CLI 依存**: hand logger の初期設定（席数 / 名前 / スタック / SB-BB）が
  `main.py:_prompt_session_config` のターミナル対話入力で、GUI 完結していない。
- **Ledger 画面が高密度**: 620×720 の 1 ウィンドウに 7 ブロック（台帳追加 / buy-in プリセット /
  ポイント付与 / エントリ一覧 / 中間集計 / 精算 / 注文）を縦積み。タッチには情報過多。

一方で、別端末スタッフ向けの **staff write API（`/api/staff/...`, ADR-0021）は実装済み**で、
staff shared token（`Authorization: Bearer <viewer_api.staff_token>`）で会計 write（ledger 追加 /
settlement 確定 / paid-unpaid / 注文確定・却下）と staff read（settlement 中間集計 / 注文 queue）が
すでに公開されている。プレイヤー向け front-end は **Expo / React Native（`mobile/`, ADR-0017）** で
contract-first（`ViewerRepository` interface 越し）に実装され、iPad / web export でも動く。

つまり「店舗用 iPad UX」は実質、**スタッフ向けの新しいタッチ front-end の設計**であり、
裏側の write 境界（staff API）と参照境界（viewer API）は大半が揃っている。本 ADR はその
front-end の **アーキテクチャ・技術選定・画面/遷移モデル・認可・所有境界** を確定する
（不足する staff API の追加設計は ADR-0038 が担う）。

関連: CLAUDE.md「§ Viewer API / mobile / 注文リクエスト」「§ Parallel development plan」/
ADR-0017（viewer API + Expo）/ ADR-0018（注文 staff-in-the-loop）/ ADR-0020（read boundary）/
ADR-0021（staff write API + token）/ ADR-0026（buy-in プリセット）。

## Decision

店舗操作を **単一のスタッフ専用タッチ front-end（staff iPad アプリ）** に統合する。これを
**新規 workstream「WS4 / staff app」** として planned に追加する。本 ADR の確定事項:

### 1. 別アプリ（player 用 `mobile/` とは分離）

staff app は **player 用 `mobile/` とは別の front-end** とする（別ディレクトリ `staff/` を新設、
または別 Expo entry）。理由は **関心分離**（player=自分の情報のみ閲覧 / staff=全卓の write 操作）と
**認可の差**（player=name-pick/PIN・無認証 read / staff=shared token write）。デスクトップ GUI
（`gui/`）は当面 **残置**（PC 運用・rollback 経路）し、staff app と並存させる（破壊しない）。

### 2. 技術 = Expo / React Native（TypeScript）

player `mobile/` と同じ Expo/RN を採用する。理由:

- `docs/contracts/` の型（`mobile/src/api/types.ts` の転記方式）と `ViewerRepository` パターンを
  **共有・再利用**でき、契約 drift を一元管理できる。
- **iPad ネイティブ + web export** の両対応（`mobile/` 既存の LAN web 配布運用がそのまま使える）。
- staff app も **repository interface 越し**（`StaffRepository` interface + mock / HTTP 実装）で
  contract-first に実装し、API 完成を待たずに mock で UI 先行できる（WS3 と同じ方針）。

### 3. 認可 = staff shared token（ADR-0021 を踏襲）

起動時に staff token を 1 度入力（端末に保存、`Authorization: Bearer` で全 write に付与）。
token 未設定/不一致は API 側が 403 `staff_writes_disabled` / 401 `unauthorized` を返すので、
UI はそれをログイン失敗として扱う。**per-staff アカウントや監査は今回 scope 外**（ADR-0021 の
「会計端末を信頼する」モデルを継承。LAN 限定前提も維持）。

### 4. 所有境界 = recording は PC、iPad は「操作」

hand logger の **音声認識 / RFID 受信スレッドは PC 常駐のまま**（マイク・USB CCID は物理的に
PC 接続）。iPad は録音の主体にはならず、**hand logger の状態を read し、制御イベント（新ハンド /
ウィナー / リバイ等）を遠隔送信する**だけにする。この遠隔制御に必要な staff API は **ADR-0038** が
設計する（現状 hand logger 制御 API は存在しないため、ハンドロガータブは ADR-0038 完了まで
read-only から始める）。

### 5. 画面 = 1 アプリ・卓単位のタブ統合

現 desktop の「機能ごと別ウィンドウ」を、**1 アプリ・session（卓）選択 → タブ統合**に畳む。

```
StaffApp (Expo/RN, staff-only, touch)
├─ Login            … staff token 入力（保存。失敗=401/403）
├─ SessionList      … session 一覧 + 新規作成/close          [一部 要 ADR-0038]
└─ TableView(session)  … 卓ごとに以下をタブで統合
    ├─ 会計 (Ledger)   … 台帳追加 / buy-in プリセット / ポイント / 精算確定 / paid-unpaid   ✅ staff API 済
    ├─ 注文 (Orders)   … pending 一覧 → 確定/却下（件数バッジ）                            ✅ staff API 済
    ├─ 座席 (Seating)  … seat→player 割当 / player 登録・選択                          [要 ADR-0038]
    └─ ハンド (Hands)  … hand 履歴 read ✅ / 新ハンド・ウィナー等の遠隔操作              [要 ADR-0038]
```

#### 画面 / 遷移仕様（draft）

| 画面 | 主な要素 | アクション | 使う API（既存/新規） |
|------|---------|-----------|----------------------|
| **Login** | staff token 入力欄、接続先（API URL） | ログイン（token 検証 = 任意の staff GET を叩く） | 既存 `GET /api/staff/...`（401/403 判定） |
| **SessionList** | session 一覧（label / status / 卓状況）、「新規セッション」 | session 選択 → TableView / session 作成 / close | read: `GET /api/players` 他 / **新規**: session create・close（ADR-0038） |
| **TableView → 会計** | player 選択、種別[buy_in/...]・cash・point・メモ、buy-in プリセットボタン、ポイント付与、中間集計、精算確定、支払状態(paid/unpaid/partial)+受領額 | エントリ追加 / 取消(reversal) / ポイント付与 / 精算 commit / 支払額記録 | 既存: `POST .../ledger-entries`, `.../settlement/commit`, `PUT .../payment(-status)`, `GET .../settlement`, `GET /api/staff/buyin-presets`。reversal/grant は **新規**（ADR-0038） |
| **TableView → 注文** | pending 注文一覧（player / item × qty / 単価 prefill）、件数バッジ | 確定（単価入力→order entry 作成）/ 却下 | 既存: `GET .../order-requests`, `POST .../confirm`, `.../reject`, `GET /api/menu` |
| **TableView → 座席** | 席ごとの player 割当、player 新規登録、carry-forward | seat→player 設定 / player 作成 / リネーム / merge | **新規**: assign_seat / player create・rename（ADR-0038）。merge は既存 `POST /api/staff/players/merge` |
| **TableView → ハンド** | hand 履歴（actions / board / pot / needs_review）。将来: 新ハンド/ウィナー/リバイ操作 | read（v1）→ 遠隔制御（後続） | read: 既存 `GET .../hands` / `.../hands/{hid}`。制御は **新規**（ADR-0038） |

遷移は player `mobile/App.tsx` と同じ **依存を増やさない useState スタック**を踏襲（Login →
SessionList → TableView、TableView 内はタブ state）。

### 6. contract-first / repository 注入

UI は `staff/src/api/repository.ts` の `StaffRepository` interface のみに依存する:

| 実装 | 用途 |
|------|------|
| `MockStaffRepository` | 契約 fixtures 相当の in-memory（既定。API なしで画面開発） |
| `HttpStaffRepository` | staff API（`--ledger` + `viewer_api.enabled` + `staff_token`）を fetch |

`EXPO_PUBLIC_API_URL` + token で `HttpStaffRepository` に切替（`mobile/` と同形）。validation /
業務ルールは **core が source of truth**、UI 側で再実装しない（CLAUDE.md WS 原則）。

## Alternatives Considered

- **player `mobile/` に staff モードを足す（同居）** — コード資産は最大再利用だが、認可境界
  （無認証 read vs token write）と画面の関心（自分の情報 vs 全卓 write）が混ざり、誤操作・情報露出の
  risk が上がる。配布も「客に配る web」と「店内端末」を同一ビルドにするのは運用上危険。→ 別アプリ。
- **customtkinter GUI を統一ランチャー化して延命** — PC 運用の改善にはなるが、**iPad で動かない**
  という本要件を満たせない。デスクトップ GUI は rollback / PC 運用として残すに留める。→ 不採用。
- **新規ネイティブ（Swift / iPad 専用）** — タッチ最適化は最良だが、`docs/contracts/` 型共有・web
  export・既存 `mobile/` の知見をすべて捨てる。少人数運用に対しコスト過大。→ Expo/RN。
- **Flutter** — 同上（Dart は contract 型共有から外れる）。→ Expo/RN。
- **iPad を録音主体にする（音声/RFID を iPad で取得）** — マイク/USB CCID は物理的に PC 接続が前提
  （ADR-0015 の PN5180+ESP32-S3 USB CCID）。iPad に移すとハードウェア前提が崩れる。→ 録音は PC、
  iPad は制御（§4）。

## Consequences

- Positive: 店舗操作が **1 つのタッチアプリ**に統合され、iPad 運用が可能になる。会計・注文は
  staff API が揃っているため **最短で動くものを出せる**。player 用と関心分離され、認可も token で明確。
- Negative / trade-offs: 新 front-end の保守対象が 1 つ増える（`mobile/` と二本立て）。座席管理・
  ハンドロガー遠隔制御は **staff API の追加（ADR-0038）が前提**で、それまで該当タブは限定機能。
  認可は shared token（per-staff 監査不可、ADR-0021 を継承）。LAN 限定前提を維持。
- Neutral: デスクトップ GUI（`gui/`）は残置・並存。CLAUDE.md / parallel dev plan に WS4 を追加。

## Validation / Follow-up

本 ADR は設計を確定し、**会計/注文の scaffold は実装着手済**（2026-06-16）。DoD:

- [x] `staff/` scaffold（Expo/RN）+ `StaffRepository` interface + `MockStaffRepository` で
      Login → SessionList → TableView（会計/注文タブ）が mock で動く。
      typecheck + 7 mock tests + web export green（`staff/`）。
- [x] `HttpStaffRepository` が `--ledger`（`viewer_api.enabled` + `staff_token`）の staff API
      （会計/注文 endpoint）に対応（`EXPO_PUBLIC_API_URL` 切替, `mobile/` と同基準）。
      `listSessions`（`GET /api/staff/sessions`）のみ未実装で `not_implemented`（ADR-0038 §B 待ち）。
- [x] 座席タブは ADR-0038 §B 実装後に有効化済。ハンドロガータブは ADR-0038 §C 待ち。
- [x] 不足 staff API の設計 = **ADR-0038**、open question / risk = **ISSUE-0020**。
- [x] UI テスト = **ブラウザ E2E（Playwright）**: web export + MockRepository をヘッドレス Chromium で
      開き、ログイン→会計（追加/取消）→注文確定→座席割当→session 作成を検証（`staff/e2e/staff.spec.ts`,
      5 tests）。iPad 配布（web）経路に最も近い自動テスト。**実機タッチ/レイアウトは手動 QA**（Linux CI に
      iOS シミュレータ無し）。browser 取得（`playwright install`）はネットワーク要。

## Related Files

- `staff/`（新規, planned — Expo/RN staff app）
- `api/server.py`（既存 `/api/staff/...` を再利用 + ADR-0038 で追加）
- `mobile/`（型・repository パターンの参照元、改変しない）
- `docs/contracts/viewer-api.md`（staff 節 / 追加は ADR-0038）
- `CLAUDE.md`（§ Parallel development plan に WS4 を追加）

## Related Tests

- 実装時に追加（`staff/` の mock 契約テスト = `mobile/src/api/mockRepository.test.ts` に倣う）

## Related Commits

- 本 ADR（設計のみ）と同じ commit

## Supersedes / Superseded by

- Supersedes: —（関連: ADR-0017 / ADR-0018 / ADR-0020 / ADR-0021 / ADR-0026。不足 API = ADR-0038、
  open question = ISSUE-0020）
- Superseded by: —
