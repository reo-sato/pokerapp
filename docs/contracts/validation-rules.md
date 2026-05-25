# Validation rules contract

JSON Schema は **構造**（型・required・形式）を検査するが、業務 validation
（重複・残高・cross-field・正規化）は表現しきれない。これらは **core が source of truth** で
あり、本 doc に明文化する。front-end は再実装せず、この規則を参照して表示する。

## 役割分担

| 層 | 担当する検査 |
|----|------------|
| JSON Schema（`schemas/*.schema.json`） | 型 / required / 文字列形式 / enum / additionalProperties |
| core validation（`core/*`） | 重複・一意性・残高・cross-field・正規化・状態遷移 |
| front-end | **検査しない**。core / repository の error（`error-shapes.md`）を表示するだけ |

## player（S1, 実装済 — `core/player_repository.py` が source of truth）

- **display_name は前後空白を除去（strip）して保持する**。schema の `minLength: 1` は「空文字」を
  弾くが、「前後空白のみ（例 `"   "`）」は strip 後に空になるため **core が弾く**（`empty_display_name`）。
- **完全一致重複の禁止**: strip 後の display_name が既存 player と **完全一致** したら拒否
  （`duplicate_display_name`）。
- **大文字小文字・全半角の同一視は scope 外**（"Alice" と "alice" は別名）。将来拡張は ISSUE-0002。
- **rename も同じ規則**。ただし自分自身との一致は許容（no-op rename 可）。
- **player_id は不変**。rename で player_id は変わらない。
- 読み込み時の leniency: 永続ファイル読込で `created_at` 欠損は許容（空文字で補完）。これは
  **ロード堅牢性の実装詳細**であり、正規の永続形（schema canonical）では `created_at` は required。

## 将来 model（planned）

対応 phase の freeze 時に本 doc へ追記する。代表例:

- **ledger_entry / point_ledger_entry（S3）**:
  - entry fee は **cash only**（point 不可）。
  - buy-in / rebuy / add-on / order は cash + point 併用可。
  - point 不足分は cash で補完（残高 < 必要点数の差分を cash 計上）。
  - 残高の source of truth は **未確定**（ISSUE-0001）。確定するまで残高 API 契約は freeze しない。
- **session_settlement（S4）**:
  - settlement は常に「player → 店」の 1 方向。player 間精算は扱わない。
  - payment_status は `paid` / `unpaid`（partial は現状扱わない）。

## schema で表現する / しないの境界（指針）

- schema で表現する: 型、必須性、文字列パターン（ID 形式等）、enum、構造の入れ子。
- schema で表現しない（core に置く）: 一意性・重複、他レコード参照の整合、残高・合計の計算、
  正規化（strip / casefold 等）、状態遷移（settled 済みは再確定不可 等）。
