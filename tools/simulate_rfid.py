#!/usr/bin/env python3
"""tools/simulate_rfid.py

実機（ESP32 + PN532）が無くても RFID 経路をローカル検証するための injector CLI。

`rfid/http_receiver.py` の `RFIDHTTPReceiver` は ESP32 から
`POST /rfid {"reader_id","tag_id","timestamp"}` を受ける。本ツールは同じ POST を送るだけなので、
アプリを `rfid.enabled=true, transport="http"`（既定 `bind_port=8787`）で起動しておけば、
席カード / ボードカードのタッチを手元から再現できる。

カード対応:
  受信側は `tag_id` を `rfid_cards.json`（CardMaster）で card 文字列に解決する。物理タグが無いので
  本ツールは「カード文字列 → 決定的な合成タグ」を使う（`demo_tag_for_card`）。`register-demo` で 52 枚分の
  合成タグを `rfid_cards.json` に登録しておけば、`--card Ah` のように指定したカードがログに現れる。
  実際に登録済みの自前タグを送りたい場合は `--tag <UID>` を使う（card 解決は受信側 / 既存マスタに従う）。

使用例:
  python tools/simulate_rfid.py register-demo                 # 合成デッキを rfid_cards.json に登録
  python tools/simulate_rfid.py status                        # GET /status
  python tools/simulate_rfid.py board Ah Kd Qs                # フロップ (board_1..3)
  python tools/simulate_rfid.py board Ah Kd Qs 2c 7h          # フロップ+ターン+リバー (board_1..5)
  python tools/simulate_rfid.py seat 3 As Ks                  # seat_3 にホールカード 2 枚
  python tools/simulate_rfid.py send --reader board_4 --card 9d
  python tools/simulate_rfid.py send --reader seat_1 --tag 04AABBCC   # 生タグ（既存マスタで解決）
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# repo ルートを import パスに追加（スクリプト直接実行のため）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rfid.card_master import CardMaster, normalize_tag_id  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CARDS_FILE = "./rfid_cards.json"

# CardMaster.VALID_CARDS と同じ並び（合成タグの決定的な採番に使う）。
_RANKS = "23456789TJQKA"
_SUITS = "cdhs"


def demo_tag_for_card(card: str) -> str:
    """カード文字列（"Ah" など）→ 決定的な合成タグ UID。

    物理タグの代わりに使う。プレフィクス "DEAD" は明らかに合成と分かる 16 進。
    受信側 / CardMaster が `normalize_tag_id` で正規化するので生の hex 文字列で返す。
    """
    card = card.strip()
    if len(card) != 2 or card[0] not in _RANKS or card[1] not in _SUITS:
        raise ValueError(f"invalid card {card!r} (expected like 'Ah', 'Td', '2c')")
    r = _RANKS.index(card[0])
    s = _SUITS.index(card[1])
    return f"DEAD{r:02X}{s:02X}"


def register_demo_deck(cards_file: str | Path = DEFAULT_CARDS_FILE) -> int:
    """合成デッキ（52 枚）の tag→card を `cards_file` に登録し、登録枚数を返す。

    既存エントリは保持しつつ追記する（CardMaster.register が validation + アトミック保存）。
    """
    cm = CardMaster(cards_file)
    count = 0
    for r in _RANKS:
        for s in _SUITS:
            card = r + s
            cm.register(demo_tag_for_card(card), card)
            count += 1
    return count


def post_event(
    host: str,
    port: int,
    reader_id: str,
    tag_id: str,
    timestamp: str = "",
    *,
    timeout: float = 5.0,
) -> tuple[int, dict]:
    """`POST /rfid` を 1 件送り (status_code, response_dict) を返す。"""
    url = f"http://{host}:{port}/rfid"
    body = json.dumps(
        {"reader_id": reader_id, "tag_id": tag_id, "timestamp": timestamp}
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def get_status(host: str, port: int, *, timeout: float = 5.0) -> tuple[int, dict]:
    """`GET /status` を取得して (status_code, response_dict) を返す。"""
    url = f"http://{host}:{port}/status"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def _resolve_tag(*, tag: str | None, card: str | None) -> str:
    """--tag / --card のどちらかから送出する tag_id を決める。"""
    if tag and card:
        raise ValueError("--tag と --card は同時指定できません")
    if tag:
        return tag
    if card:
        return demo_tag_for_card(card)
    raise ValueError("--tag か --card のどちらかが必要です")


def _emit(args: argparse.Namespace, reader_id: str, tag_id: str) -> int:
    """1 件 POST して結果を表示。失敗時は終了コード用に 1 を返す。"""
    try:
        status, body = post_event(args.host, args.port, reader_id, tag_id, args.ts)
    except urllib.error.URLError as exc:
        print(
            f"[error] {reader_id} 送信失敗: {exc} "
            f"(アプリが rfid.enabled=true で {args.host}:{args.port} で起動しているか確認)",
            file=sys.stderr,
        )
        return 1
    norm = normalize_tag_id(tag_id)
    print(f"  {reader_id:<8} tag={norm}  -> HTTP {status} {body.get('status')}")
    return 0 if status == 200 else 1


# ――― サブコマンド ―――

def _cmd_status(args: argparse.Namespace) -> int:
    try:
        status, body = get_status(args.host, args.port)
    except urllib.error.URLError as exc:
        print(f"[error] status 取得失敗: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    try:
        tag_id = _resolve_tag(tag=args.tag, card=args.card)
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    return _emit(args, args.reader, tag_id)


def _cmd_seat(args: argparse.Namespace) -> int:
    if not 1 <= args.seat <= 8:
        print("[error] seat は 1..8", file=sys.stderr)
        return 2
    reader_id = f"seat_{args.seat}"
    print(f"seat_{args.seat} へホールカード {len(args.cards)} 枚:")
    rc = 0
    for card in args.cards:
        rc |= _emit(args, reader_id, demo_tag_for_card(card))
    return rc


def _cmd_board(args: argparse.Namespace) -> int:
    if not 1 <= len(args.cards) <= 5:
        print("[error] board は 1..5 枚（flop=3, +turn=4, +river=5）", file=sys.stderr)
        return 2
    print(f"ボード {len(args.cards)} 枚 (board_1..{len(args.cards)}):")
    rc = 0
    for i, card in enumerate(args.cards, start=1):
        rc |= _emit(args, f"board_{i}", demo_tag_for_card(card))
    return rc


def _cmd_register_demo(args: argparse.Namespace) -> int:
    count = register_demo_deck(args.cards_file)
    print(f"合成デッキ {count} 枚を {args.cards_file} に登録しました。")
    print("これで `board Ah Kd Qs` / `seat 3 As Ks` の --card 指定がログに反映されます。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="実機なしで RFID HTTP イベントを注入する（ローカル QA 用）",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"受信ホスト (既定 {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"受信ポート (既定 {DEFAULT_PORT})")
    parser.add_argument("--ts", default="", help="ISO8601 タイムスタンプ（既定: 空=受信側で現在時刻）")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="GET /status を表示").set_defaults(func=_cmd_status)

    p_send = sub.add_parser("send", help="1 件の RFID イベントを送る")
    p_send.add_argument("--reader", required=True, help="reader_id（seat_1..9 / board_1..5）")
    g = p_send.add_mutually_exclusive_group(required=True)
    g.add_argument("--tag", help="生タグ UID（既存マスタで card 解決）")
    g.add_argument("--card", help="カード文字列（合成タグ。register-demo 前提）")
    p_send.set_defaults(func=_cmd_send)

    p_seat = sub.add_parser("seat", help="ある席にホールカードを送る")
    p_seat.add_argument("seat", type=int, help="席番号 1..9")
    p_seat.add_argument("cards", nargs="+", help="カード文字列（通常 2 枚）例: As Ks")
    p_seat.set_defaults(func=_cmd_seat)

    p_board = sub.add_parser("board", help="ボードカードを順に送る（board_1..）")
    p_board.add_argument("cards", nargs="+", help="カード文字列 1..5 枚 例: Ah Kd Qs")
    p_board.set_defaults(func=_cmd_board)

    p_reg = sub.add_parser("register-demo", help="合成デッキを rfid_cards.json に登録")
    p_reg.add_argument("--cards-file", default=DEFAULT_CARDS_FILE, help=f"既定 {DEFAULT_CARDS_FILE}")
    p_reg.set_defaults(func=_cmd_register_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
