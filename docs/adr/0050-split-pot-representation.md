# ADR-0050: split pot（チョップ）の表現（S7）

- Status: Accepted
- Date: 2026-08-19
- 関連: ADR-0009 / ADR-0047 / hand schema 1.1 / `docs/contracts/hand-reconstruction.md`

## Context

`HandSummary.winner_seat` は単一 int で、チョップ（split pot）が表現できなかった。実運用では
board プレイやランナー同点で普通に発生し、従来は片方の席だけに全ポットが push され
スタック・result が現実とズレた。

## Decision

1. **語彙**: 「チョップ」「スプリット」を winner キーワードに追加。読み上げ形は
   「シート3 シート5 チョップ」（席を複数読み上げ）。`_extract_all_seat_nos` が出現順で全席を拾う。
2. **engine API（additive）**: `PokerEngine.end_hand_split(winner_seats) -> dict[seat, amount]`。
   pot 総額（pokerkit は開始スタック合計 − 現スタック合計、legacy は `_pot`）を勝者間で等分し、
   **端数チップは読み上げ順の先頭勝者**に寄せる（店ごとの odd-chip ルールの差異は追わず、決定的な
   単純規則に固定して監査可能にする。額の訂正は B4 ハンド訂正で可能）。
3. **HandSummary（hand schema 1.0 → 1.1, additive）**: `pot_awards: [{"seat", "amount"}]` を
   optional 追加。**単独勝者ハンドでは absent**（後方互換）。`winner_seat` は読み上げ先頭の勝者 =
   従来 read 側（viewer / replay UI / PHH）は無改修で壊れない。
4. **チョップは必ず review_required=True**（等分・端数規則が実卓の合意と一致するかを人が確認する）。

## Consequences

- チョップ後のスタック・result が現実と一致する（`split-pot-chop` golden fixture で回帰ロック）。
- side pot ごとの個別勝者指定（all-in 3 者で main/side の勝者が異なる等）は**未対応**（読み上げ
  1 文では表現が曖昧なため）。当面は chop + B4 訂正で運用し、必要になったら staff iPad の
  ハンドタブに award 編集 UI を足す（future scope）。
- viewer / replay UI での pot_awards 表示は additive の後続タスク（表示しなくても壊れない）。
