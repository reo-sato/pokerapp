#!/usr/bin/env python3
"""tools/register_cards.py

実機カード（ICODE SLIX トランプ）の UID を `rfid_cards.json`（tag_id → card_code）に登録する
**タップ駆動**の CLI。canonical PC/SC 経路（`rfid/bridge.py:PCSCBridge`）で reader を polling し、
「次に置くべきカード」を表示 → 置く → 登録 → 離すのを待つ → 次、を繰り返す。

- 1 枚ごとに `CardMaster.register`（validation + アトミック保存）。Ctrl-C で中断しても登録済み分は残る。
- **2 デッキ対応・再開可能**: `--deck N` は「その card_code に紐づく UID が N 枚未満なら未登録」と
  判定する。JSON 形式（tag_id → card_code）は変えない（2 デッキ目の 'Ah' は別 UID → 同じ 'Ah'）。
  途中で止めても同じコマンドで続きから再開できる。
- 誤タップ防御: 別 code で登録済みの UID は警告して登録しない（直したければ `unregister`）。
  期待 code で登録済みの UID は「済」として次へ（冪等）。置きっぱなしでは次に進まない（離すまで待つ）。
  **複数枚が重なって載っている間は登録しない**（`⚠ N 枚検出` を出して待つ。契約 v1.1 §6 で 1 slot に
  複数 UID が載りうるため、登録は必ず 1 枚ずつ）。

サブコマンド:
  run         タップ駆動で登録（既定: deck 1 / suit-rank 順 / ジョーカー 2 枚）
  list        登録状況（deck ごとの進捗と不足 code、多重登録の警告）
  unregister  UID の登録を解除

使用例:
  python tools/register_cards.py run --deck 1
  python tools/register_cards.py run --deck 2 --order rank-suit
  python tools/register_cards.py run --deck 1 --only Ah,Kd,Qs      # 抜けた分だけ
  python tools/register_cards.py run --deck 1 --start-at Ah         # 途中の code から
  python tools/register_cards.py run --reader "seat 1"              # 使う物理リーダーを選ぶ
  python tools/register_cards.py list --deck 2
  python tools/register_cards.py unregister E0:04:01:53:1C:2A:B2:6C

要 `pip install ".[pcsc]"`（pyscard）。**登録に使う物理リーダーは `--reader` で config の
`pcsc_readers` の要素を選ぶ**（index か `seat 1` / `board 1` のラベル。既定は先頭要素）。
契約 v1.2（ADR-0041）では PC/SC の reader 名は 1 つだけで、物理リーダー N 台は Get UID の
P2（config の `reader`）で選ぶため、reader_name だけでは 1 台を指定できない。
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# repo ルートを import パスに追加（スクリプト直接実行のため）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rfid.bridge import PCSCBridge, bridge_read_uids, call_bridge_factory  # noqa: E402
from rfid.card_master import VALID_CARDS, CardMaster, normalize_tag_id  # noqa: E402
from tools.probe_pcsc import (  # noqa: E402
    cfg_reader_index,
    get_pcsc_readers,
    load_rfid_config,
    pyscard_available,
    reader_label,
)

# 新品デッキの並び（A→K）。スート順は ♠♥♦♣。
RANKS = "A23456789TJQK"
SUITS = "shdc"
JOKERS = ("Jk", "JK")   # CardMaster.VALID_CARDS のジョーカー 2 種
ORDER_PRESETS = ("suit-rank", "rank-suit")

DEFAULT_CARDS_FILE = "./rfid_cards.json"


# ――― 純粋ロジック ―――

def deck_order(preset: str = "suit-rank", jokers: int = 2) -> list[str]:
    """登録順の card_code 列を返す（52 枚 + ジョーカー 0..2 枚）。"""
    if preset == "suit-rank":
        codes = [r + s for s in SUITS for r in RANKS]
    elif preset == "rank-suit":
        codes = [r + s for r in RANKS for s in SUITS]
    else:
        raise ValueError(f"unknown order preset: {preset!r} (choose from {ORDER_PRESETS})")
    if not 0 <= jokers <= len(JOKERS):
        raise ValueError(f"jokers must be 0..{len(JOKERS)}")
    return codes + list(JOKERS[:jokers])


def code_counts(entries: dict[str, str]) -> dict[str, int]:
    """tag_id → code の対応表から、code ごとの UID 数を数える。"""
    counts: dict[str, int] = {}
    for code in entries.values():
        counts[code] = counts.get(code, 0) + 1
    return counts


def pending_codes(entries: dict[str, str], order: list[str], deck: int) -> list[str]:
    """deck 枚目として未登録（= その code の UID 数 < deck）の code を order 順に返す。"""
    if deck < 1:
        raise ValueError("deck must be >= 1")
    counts = code_counts(entries)
    return [c for c in order if counts.get(c, 0) < deck]


def parse_codes(csv: str) -> list[str]:
    """`--only Ah,Kd` の文字列を検証済みの code リストにする（順序維持・重複除去）。"""
    out: list[str] = []
    for raw in csv.split(","):
        code = raw.strip()
        if not code:
            continue
        if code not in VALID_CARDS:
            raise ValueError(f"invalid card code: {code!r} (例: Ah, Kd, 2c, Jk)")
        if code not in out:
            out.append(code)
    return out


def format_status(entries: dict[str, str], order: list[str], deck: int) -> list[str]:
    """`list` 用の表示行。deck 1..deck の進捗と不足 code、多重登録・順序外 code の警告。"""
    counts = code_counts(entries)
    lines = [f"登録 UID 数: {len(entries)}（対象 code {len(order)} 種 × deck {deck}）"]
    for d in range(1, deck + 1):
        missing = [c for c in order if counts.get(c, 0) < d]
        done = len(order) - len(missing)
        tail = "  ✅ 完了" if not missing else f"  不足: {' '.join(missing)}"
        lines.append(f"  deck {d}: {done}/{len(order)} 済{tail}")
    extra = {c: n for c, n in counts.items() if c in order and n > deck}
    if extra:
        lines.append("  ⚠ deck 数より多い UID が付いた code: "
                     + ", ".join(f"{c}×{n}" for c, n in sorted(extra.items())))
    outside = sorted(c for c in counts if c not in order)
    if outside:
        lines.append(f"  （順序外の code も登録あり: {' '.join(outside)}）")
    return lines


def reader_choices(pcsc_readers: list[dict]) -> list[str]:
    """`--reader` に指定できる候補の表示行（index / ラベル / reader_name）。"""
    return [
        f"{i}: {reader_label(cfg):<14} ← {cfg.get('name')!r}"
        for i, cfg in enumerate(pcsc_readers)
    ]


def select_reader(pcsc_readers: list[dict], selector: Optional[str] = None) -> dict:
    """`--reader` の指定から config 要素を 1 つ選ぶ（契約 v1.2 §4 / ADR-0041）。

    selector は **config の index**（"0"）か **役割ラベル**（"seat 1" / "board 1" / "board 1-3"、
    `[r3]` 付きも可・大小/空白ゆるめ）。None / 空なら先頭要素。選べなければ ValueError。
    """
    if not pcsc_readers:
        raise ValueError("config.rfid.pcsc_readers が空です（`probe_pcsc list` で reader を確認）")
    if selector is None or not str(selector).strip():
        return pcsc_readers[0]

    raw = str(selector).strip()
    if raw.isdigit():
        i = int(raw)
        if not 0 <= i < len(pcsc_readers):
            raise ValueError(
                f"--reader {raw} は範囲外（0..{len(pcsc_readers) - 1}）:\n  "
                + "\n  ".join(reader_choices(pcsc_readers))
            )
        return pcsc_readers[i]

    want = " ".join(raw.lower().split())
    for cfg in pcsc_readers:
        labels = {reader_label(cfg).lower(), reader_label({**cfg, "reader": None}).lower()}
        if cfg.get("role") == "board" and cfg.get("index") is not None:
            labels.add(f"board {cfg['index']}")     # cards>1 の "board 1-3" を "board 1" でも選べる
        if want in labels:
            return cfg
    raise ValueError(
        f"--reader {selector!r} に一致する pcsc_readers 要素がありません:\n  "
        + "\n  ".join(reader_choices(pcsc_readers))
    )


@dataclass
class RegisterResult:
    registered: int = 0        # 新規登録
    skipped_done: int = 0      # 期待 code で登録済みだった（冪等スキップ）
    rejected: int = 0          # 別 code で登録済みの UID が置かれた（登録せず）
    remaining: list[str] = field(default_factory=list)  # 未登録のまま残った code


def run_registration(
    bridge: object,
    master: CardMaster,
    codes: list[str],
    *,
    deck: int,
    poll_interval: float = 0.1,
    sink: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: Optional[int] = None,
) -> RegisterResult:
    """codes の順に「置く→登録→離す」を繰り返す（テスト可能な純粋ループ）。

    bridge は connect 済みの PCSCBridge 互換（`read_uids() -> list[str]`。旧 `read_uid()` のみでも可）。
    置きっぱなしのカードは 1 回だけ処理し、カード無し（離脱）を読むまで次を受け付けない。
    **複数枚が重なっている間は登録しない**（どの UID を登録すべきか決められないため, 契約 v1.1 §6）。
    max_polls は上限（テスト/安全弁）。None なら codes が尽きるまで。
    """
    result = RegisterResult()
    queue = list(codes)
    total = len(codes)
    polls = 0
    need_release = False
    multi_warned = 0   # 直近に警告した枚数（状態が変わったときだけ出す）

    def prompt() -> None:
        if queue:
            n = total - len(queue) + 1
            sink(f"→ 次: [{n:>2}/{total}] {queue[0]}（deck {deck}）— カードをリーダーに置いてください")

    prompt()
    while queue:
        if max_polls is not None and polls >= max_polls:
            break
        polls += 1
        uids = bridge_read_uids(bridge)
        if len(uids) > 1:
            # 重ね置き: 登録対象が一意に決まらないので、1 枚になるまで待つ
            if multi_warned != len(uids):
                sink(f"  ⚠ {len(uids)} 枚検出 — 1 枚だけ置いてください")
                multi_warned = len(uids)
            sleep(poll_interval)
            continue
        multi_warned = 0
        uid = uids[0] if uids else None
        if uid is None:
            if need_release:
                need_release = False
                prompt()
            sleep(poll_interval)
            continue
        if need_release:
            # まだ乗っている（または離さずに別カードに替えた）→ 一度離すまで待つ
            sleep(poll_interval)
            continue

        expected = queue[0]
        existing = master.lookup(uid)
        n = total - len(queue) + 1
        if existing == expected:
            sink(f"  = [{n:>2}/{total}] {expected}: {uid} は登録済み → 次へ")
            result.skipped_done += 1
            queue.pop(0)
        elif existing:
            sink(f"  ⚠ {uid} は既に {existing!r} として登録済み（{expected} ではない）。"
                 "別のカードを置いてください（上書きは `unregister` してから）")
            result.rejected += 1
        else:
            master.register(uid, expected)
            result.registered += 1
            queue.pop(0)
            sink(f"  ✓ [{n:>2}/{total}] {expected} ← {uid}")
        need_release = True
        if queue:
            sink("    カードを離してください")
        sleep(poll_interval)

    result.remaining = queue
    return result


# ――― CLI ―――

def _require_pyscard() -> bool:
    if pyscard_available():
        return True
    print("[error] pyscard が見つかりません: pip install \".[pcsc]\"", file=sys.stderr)
    return False


def _cards_file(args: argparse.Namespace) -> Path:
    if args.cards_file:
        return Path(args.cards_file)
    rfid_cfg = load_rfid_config(args.config)
    return Path(rfid_cfg.get("card_master_file", DEFAULT_CARDS_FILE))


def _resolve_order(args: argparse.Namespace) -> list[str]:
    if args.only:
        return parse_codes(args.only)
    order = deck_order(args.order, args.jokers)
    if args.start_at:
        if args.start_at not in order:
            raise ValueError(f"--start-at {args.start_at!r} は順序に含まれていません")
        order = order[order.index(args.start_at):]
    return order


def _cmd_run(args: argparse.Namespace, *, bridge_factory: Callable[..., object] = PCSCBridge) -> int:
    if not _require_pyscard():
        return 2
    try:
        order = _resolve_order(args)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    rfid_cfg = load_rfid_config(args.config)
    pcsc_readers = get_pcsc_readers(rfid_cfg)
    try:
        target = select_reader(pcsc_readers, args.reader)
    except ValueError as e:
        # config が空なら接続中の reader を 1 台だけ拾う（bring-up 直後の救済）。
        from rfid.bridge import list_readers
        present = sorted(list_readers())
        if pcsc_readers or not present:
            print(f"[error] {e}", file=sys.stderr)
            return 2
        target = {"name": present[0], "reader": 0}
    reader_name = target.get("name", "")
    reader_index = cfg_reader_index(target)
    if not reader_name:
        print("[error] reader が見つかりません（`probe_pcsc list` で確認）", file=sys.stderr)
        return 1

    master = CardMaster(_cards_file(args))
    codes = pending_codes(master.all_entries(), order, args.deck)
    print(f"reader: {reader_label(target)} ← {reader_name!r} (物理リーダー {reader_index}) / "
          f"cards: {master._path} / deck {args.deck} / 順序 {args.order}"
          f"{' (--only)' if args.only else ''}")
    if not codes:
        print(f"deck {args.deck} は対象 {len(order)} 種すべて登録済みです。`list` で確認できます。")
        return 0
    print(f"未登録 {len(codes)} 枚: {' '.join(codes[:8])}{' …' if len(codes) > 8 else ''}")
    print("1 枚ずつ置く→登録→離す。Ctrl-C で中断（登録済み分は保存済み。同じコマンドで再開）。\n")

    bridge = call_bridge_factory(bridge_factory, reader_name, reader_index)
    if not bridge.connect():
        print(f"[error] reader {reader_name!r} (物理リーダー {reader_index}) に接続できません",
              file=sys.stderr)
        return 1
    try:
        result = run_registration(bridge, master, codes, deck=args.deck,
                                  poll_interval=args.poll_interval)
    except KeyboardInterrupt:
        print("\n中断しました（ここまでの登録は保存済み）。")
        result = RegisterResult(remaining=[])  # 集計は list で
    finally:
        close = getattr(bridge, "close", None)
        if callable(close):
            close()
    print(f"\n登録 {result.registered} 枚 / 登録済みスキップ {result.skipped_done} / 拒否 {result.rejected}")
    for line in format_status(master.all_entries(), order, args.deck):
        print(line)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        order = _resolve_order(args)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    master = CardMaster(_cards_file(args))
    print(f"cards: {master._path}")
    for line in format_status(master.all_entries(), order, args.deck):
        print(line)
    return 0


def _cmd_unregister(args: argparse.Namespace) -> int:
    master = CardMaster(_cards_file(args))
    norm = normalize_tag_id(args.uid)
    before = master.lookup(norm)
    if master.unregister(norm):
        print(f"解除: {norm}（{before}）")
        return 0
    print(f"[error] {norm} は登録されていません", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="実機カード UID を rfid_cards.json に登録（タップ駆動）")
    parser.add_argument("--config", default=None, help="config パス（既定: config.json → config_default.json）")
    parser.add_argument("--cards-file", default=None,
                        help=f"rfid_cards.json のパス（既定: config の card_master_file か {DEFAULT_CARDS_FILE}）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_order_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--deck", type=int, default=1, help="何デッキ目として登録/判定するか（既定 1）")
        p.add_argument("--order", choices=ORDER_PRESETS, default="suit-rank",
                       help="登録順: suit-rank=♠A..K ♥ ♦ ♣ / rank-suit=A♠♥♦♣ 2… （既定 suit-rank）")
        p.add_argument("--jokers", type=int, default=2, help="ジョーカー枚数 0..2（既定 2: Jk, JK）")
        p.add_argument("--only", default=None, help="この code だけ（カンマ区切り, 順序もこの通り）")
        p.add_argument("--start-at", default=None, help="順序のこの code から始める")

    p_run = sub.add_parser("run", help="タップ駆動で登録")
    add_order_args(p_run)
    p_run.add_argument(
        "--reader", default=None,
        help="使う物理リーダー: config.rfid.pcsc_readers の index（例 0）か役割ラベル"
             "（例 'seat 1' / 'board 1'）。既定は先頭要素（契約 v1.2 §4）",
    )
    p_run.add_argument("--poll-interval", type=float, default=0.1, help="polling 間隔 秒（既定 0.1）")
    p_run.set_defaults(func=_cmd_run)

    p_list = sub.add_parser("list", help="登録状況（deck ごとの不足 code）")
    add_order_args(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_unreg = sub.add_parser("unregister", help="UID の登録を解除")
    p_unreg.add_argument("uid", help="tag_id（例 E0:04:01:53:1C:2A:B2:6C。区切り/大小は正規化）")
    p_unreg.set_defaults(func=_cmd_unregister)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
