#!/usr/bin/env python3
"""tools/probe_pcsc.py

実機 RFID（canonical PC/SC 経路: ESP32-S3 + PN5180 USB CCID, ADR-0015/0034/0041）を
`docs/contracts/rfid-usb-ccid.md` v1.2 の MUST 項目に対して検査する bring-up 診断 CLI。

`tools/simulate_rfid.py`（HTTP 模擬・実機なし）と対になる「実機側」ツール。pyscard / OS の
PC/SC スタック越しに、production と同じ `rfid.bridge.PCSCBridge` / `rfid.reader_thread.RFIDThread`
を使って次を確認する:

  - §3-4 reader_name 列挙と `config.rfid.pcsc_readers` の一致（前方一致でなく等値）。
         **CCID slot = 1**（Windows 制限, ADR-0041）なので reader_name は 1 つで、物理リーダーは
         config の `reader`（Get UID の P2）で選ぶ。突き合わせは `(name, reader)` 単位。
  - §4   config の lint（role/seat/reader・重複・キー欠落・board の廃止フィールド/台数）
  - §5   各 reader への connect 成功（host は ATR 非依存。connect が通れば OS PC/SC が ATR 受理）
  - §6-7 Get UID（FF CA 00 <k> 00）応答の UID を 4/7/8 バイト長非依存で正規化。16/24/32B は
         重ね置き（8B UID × 枚数の連結, v1.1）として分割。`FF CA 00 FF 00` で台数 N を問い合わせ
  - §8   デバウンス / hot-plug（タップ→離す→再タップで再発火）を実イベントで観察

要 `pip install ".[pcsc]"`（pyscard）。Linux は `pcscd` 稼働が前提。reader_name は OS 依存なので
`list` で実値を確認し `config.rfid.pcsc_readers[].name` に等値で入れる（契約 §8）。

サブコマンド:
  list    接続中の reader_name を列挙（+ firmware の物理 reader 台数）、config との一致
          （matched/missing/unconfigured）を `(name, reader)` 単位で表示
  check   config を lint し、各 reader に connect + Get UID を 1 回送って PASS/FAIL を表示
          （カード不要。SW=9000/6A81 は PASS、6A86 = `reader` が firmware の範囲外で FAIL）
  watch   実 RFIDThread を起動し、タップごとに RFIDEvent（role/seat/board_index/card/UID 長）を表示
  raw     pyscard を直接叩く低レベル診断: OS が見ているスロット状態（PRESENT/EMPTY/MUTE…）と、
          connect → Get UID の結果 or 例外（hresult 付き）を時系列表示。`watch` が 0 件のとき
          「OS がカードを認識していない」「power-on で失敗」「ATR/プロトコル」を切り分ける

使用例:
  python tools/probe_pcsc.py list
  python tools/probe_pcsc.py check
  python tools/probe_pcsc.py watch --seconds 30
  python tools/probe_pcsc.py raw --seconds 20 --reader 3
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# repo ルートを import パスに追加（スクリプト直接実行のため）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rfid.bridge import (  # noqa: E402
    MAX_READER_INDEX,
    PCSCBridge,
    call_bridge_factory,
    get_uid_apdu,
    list_readers,
    query_reader_count,
    split_uid_response,
)
from rfid.card_master import CardMaster, normalize_tag_id  # noqa: E402

# 契約 §7: host が想定する UID バイト長（4=Mifare Classic / 7=Type A / 8=ISO 15693）。
CONTRACT_UID_LENGTHS = (4, 7, 8)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_JSON = _REPO_ROOT / "config.json"
_CONFIG_DEFAULT_JSON = _REPO_ROOT / "config_default.json"

# 型エイリアス: (reader_name[, reader_index]) -> bridge（DI 用。既定は PCSCBridge）。
# 旧 1 引数 factory も `call_bridge_factory` 経由で受け付ける。
BridgeFactory = Callable[..., object]
ReadersLister = Callable[[], "list[str]"]
ReaderCounter = Callable[[str], "Optional[int]"]


def cfg_reader_index(cfg: dict) -> int:
    """config 要素の物理 reader index（Get UID の P2, 契約 v1.2 §6）。既定 0・不正は 0。"""
    value = cfg.get("reader", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_READER_INDEX:
        return 0
    return value


# ――― config ロード（read-only, copy 副作用なし） ―――

def load_rfid_config(config_path: str | Path | None = None) -> dict:
    """`rfid` セクションを返す。

    `core.config.load_config` と違い **config.json を生成しない**（診断は非破壊）。
    明示パス > config.json > config_default.json の順に最初に存在するものを読む。
    """
    if config_path is not None:
        target = Path(config_path)
    elif _CONFIG_JSON.exists():
        target = _CONFIG_JSON
    else:
        target = _CONFIG_DEFAULT_JSON
    data = json.loads(Path(target).read_text(encoding="utf-8"))
    return data.get("rfid", {})


def get_pcsc_readers(rfid_cfg: dict) -> list[dict]:
    """canonical PC/SC 経路の reader 設定（list）を取り出す。

    `main.py` と同じく `pcsc_readers`（list）を優先し、HTTP 用 `readers`（dict）は採用しない
    （dict なら list でないので空にフォールバック, ADR-0034 / rfid-usb-ccid.md §4）。
    """
    pcsc = rfid_cfg.get("pcsc_readers", rfid_cfg.get("readers", []))
    return pcsc if isinstance(pcsc, list) else []


# ――― 純粋ロジック（テスト可能なシーム） ―――

@dataclass
class ReaderMatch:
    """present な reader_name と config の突き合わせ結果（契約 §3-4）。"""

    matched: list[dict] = field(default_factory=list)       # config にあり、かつ接続中
    missing: list[dict] = field(default_factory=list)       # config にあるが未接続
    unconfigured: list[str] = field(default_factory=list)   # 接続中だが config 未記載


def match_readers(present_names: list[str], pcsc_readers: list[dict]) -> ReaderMatch:
    """接続中 reader_name と `pcsc_readers` を **等値**で突き合わせる（前方一致しない, §4）。

    v1.2 では複数の config 要素が同じ reader_name を共有する（物理リーダーは `reader` で選ぶ）ため、
    matched/missing は **config 要素（= `(name, reader)` の組）単位**で返る。
    """
    present = set(present_names)
    configured_names = {c.get("name", "") for c in pcsc_readers}
    report = ReaderMatch()
    for cfg in pcsc_readers:
        if cfg.get("name", "") in present:
            report.matched.append(cfg)
        else:
            report.missing.append(cfg)
    report.unconfigured = [n for n in present_names if n not in configured_names]
    return report


def lint_pcsc_readers(pcsc_readers: list[dict]) -> list[str]:
    """`pcsc_readers` の静的検査。問題メッセージのリストを返す（空なら OK）。"""
    problems: list[str] = []
    if not pcsc_readers:
        return ["pcsc_readers が空です（config.rfid.pcsc_readers に reader を列挙してください）。"]

    seen_targets: dict[tuple[str, int], str] = {}   # (name, reader) → 使っている label
    seen_seats: dict[int, str] = {}
    board_labels: list[str] = []          # role=board の label（台数チェック用, 契約 v1.3 §4）
    for i, cfg in enumerate(pcsc_readers):
        label = f"pcsc_readers[{i}]"
        name = cfg.get("name")
        if not name or not isinstance(name, str):
            problems.append(f"{label}: name が無い/文字列でない（OS の実 reader_name を等値で指定）。")

        # `reader`（任意・既定 0）= 物理リーダー index（Get UID の P2, 契約 v1.2 §6）。
        # v1.2 では reader 名は 1 つなので **name の重複は正常**。`(name, reader)` の組で一意。
        reader_idx = cfg.get("reader")
        if reader_idx is not None and (
            isinstance(reader_idx, bool) or not isinstance(reader_idx, int)
            or not 0 <= reader_idx <= MAX_READER_INDEX
        ):
            problems.append(
                f"{label}: reader は 0..{MAX_READER_INDEX} の整数（任意・既定 0, §4）"
                f"（実際: {reader_idx!r}）。"
            )
            reader_idx = None
        if isinstance(name, str) and name:
            target = (name, reader_idx if isinstance(reader_idx, int) else 0)
            if target in seen_targets:
                problems.append(
                    f"{label}: (name {name!r}, reader {target[1]}) が {seen_targets[target]} と重複"
                    f"（物理リーダーごとに一意のはず, §4）。"
                )
            else:
                seen_targets[target] = label

        role = cfg.get("role")
        if role not in ("seat", "board"):
            problems.append(f"{label}: role は 'seat' | 'board'（実際: {role!r}）。")
        elif role == "seat":
            seat = cfg.get("seat")
            if not isinstance(seat, int) or not 1 <= seat <= 9:
                problems.append(f"{label}: role=seat には seat 1..9 が必要（実際: {seat!r}）。")
            elif seat in seen_seats:
                problems.append(
                    f"{label}: seat {seat} が {seen_seats[seat]} と重複。"
                )
            else:
                seen_seats[seat] = label
        elif role == "board":
            problems.extend(_lint_board_obsolete_fields(cfg, label))
            board_labels.append(label)

    problems.extend(_lint_board_group(board_labels))
    return problems


def _lint_board_obsolete_fields(cfg: dict, label: str) -> list[str]:
    """board reader に残った旧フィールド（`index` / `cards`）を指摘する（契約 v1.3 §4）。

    v1.1/v1.2 は「1 台 = 1 ストリート専用（flop は 1 台に 3 枚重ね）」前提で位置を config に
    書かせていたが、実機は board reader が並んでいるだけで前提が成立しない
    （ISSUE-0024 / ADR-0042）。現在は全台を 1 つの論理ボードとして検出順に 1..5 を振るため、
    両フィールドは無視される = 残っていると「設定したのに効かない」誤解のもとになる。
    """
    stale = [k for k in ("index", "cards") if k in cfg]
    if not stale:
        return []
    return [
        f"{label}: role=board の {' / '.join(stale)} は廃止（無視されます, ADR-0042）。"
        "ボード位置は board reader 全台を通した検出順で決まるので、config から削除してください。"
    ]


def _lint_board_group(board_labels: list[str]) -> list[str]:
    """board reader 群そのものの妥当性（台数）を検査する（契約 v1.3 §4）。

    全台で 1 つの論理ボード（最大 5 枚）を共有するので、位置の重なりという概念は無い。
    代わりに「5 枚を物理的に載せられるか」が問題になる: 1 台あたり 3 枚程度が実用上限なので、
    board reader が 1 台だけだと 5 枚は載らない。
    """
    if not board_labels:
        return []
    if len(board_labels) == 1:
        return [
            f"{board_labels[0]}: board reader が 1 台だけです。コミュニティカードは 5 枚なので "
            "1 台に 5 枚を重ねることになり、給電不足で読めない可能性が高い（推奨 3 台以上）。"
        ]
    return []


@dataclass
class UidInfo:
    """Get UID 応答の解析（契約 §7）。"""

    raw: Optional[str]
    normalized: str
    byte_length: int
    contract_length: bool  # 4/7/8 バイトか


def analyze_uid(uid: Optional[str]) -> UidInfo:
    """UID 文字列を正規化し、バイト長と契約適合（4/7/8B）を判定する。"""
    if not uid:
        return UidInfo(raw=uid, normalized="", byte_length=0, contract_length=False)
    norm = normalize_tag_id(uid)
    byte_length = len(norm.split(":")) if norm else 0
    return UidInfo(
        raw=uid,
        normalized=norm,
        byte_length=byte_length,
        contract_length=byte_length in CONTRACT_UID_LENGTHS,
    )


def reader_label(cfg: dict) -> str:
    """config 1 件を human-readable な役割ラベルにする（例 'seat 1' / 'board 3' / 'board 1-3'）。

    `cards`>1（重ね置き, 契約 v1.1 §4）の board reader は占有範囲を `board 1-3` の形で示す。
    `reader`（物理リーダー index, 契約 v1.2 §6）が **指定されているときだけ** `[r3]` を添える。
    """
    base = _role_label(cfg)
    idx = cfg.get("reader")
    if isinstance(idx, int) and not isinstance(idx, bool):
        return f"{base} [r{idx}]"
    return base


def _role_label(cfg: dict) -> str:
    role = cfg.get("role", "?")
    if role == "seat":
        return f"seat {cfg.get('seat', '?')}"
    if role == "board":
        # `index` は **RFIDEvent の board_index**（実際に割り当てられた位置）が入る経路でのみ来る。
        # config 由来の board reader は位置を持たない（位置は検出順で決まる, 契約 v1.3 §4）ので
        # ラベルは役割だけになる。
        idx = cfg.get("index")
        return "board" if idx is None else f"board {idx}"
    return role


def format_uid_payload(data: "list[int] | bytes") -> str:
    """Get UID 応答のデータ部を raw 表示用に整形する（契約 v1.1 §6）。

    16/24/32 バイト（8B UID × 2..4 枚 = 重ね置き）は `UID×k = A, B` の形で分割表示、
    それ以外は従来どおり空白区切り 16 進をそのまま出す。
    """
    raw = bytes(bytearray(data))
    uids = split_uid_response(raw)
    if len(uids) > 1:
        return f"UID×{len(uids)} = {', '.join(uids)}"
    return " ".join(f"{b:02X}" for b in raw)


@dataclass
class ConnectResult:
    """1 reader への connect + Get UID 検査結果（契約 §5-6）。"""

    cfg: dict
    connected: bool
    sw: Optional[tuple[int, int]] = None   # Get UID の SW（取れなければ None）


def probe_connect(
    pcsc_readers: list[dict],
    *,
    bridge_factory: BridgeFactory = PCSCBridge,
) -> list[ConnectResult]:
    """各 reader に connect し、`FF CA 00 <k> 00` を 1 回送って SW を取る（§5-6, カード不要）。

    `probe()` を持たない bridge（mock 等）は SW=None（connect 成立性のみ）。
    """
    results: list[ConnectResult] = []
    for cfg in pcsc_readers:
        bridge = call_bridge_factory(
            bridge_factory, cfg.get("name", ""), cfg_reader_index(cfg),
        )
        ok = False
        sw: Optional[tuple[int, int]] = None
        try:
            ok = bool(bridge.connect())
            probe = getattr(bridge, "probe", None)
            if ok and callable(probe):
                sw = probe()
        finally:
            close = getattr(bridge, "close", None)
            if callable(close):
                close()
        results.append(ConnectResult(cfg=cfg, connected=ok, sw=sw))
    return results


def check_verdict(result: ConnectResult) -> tuple[bool, str]:
    """connect + Get UID の結果を PASS/FAIL と説明にする（契約 §5-6）。

    - connect 不可 → FAIL（reader_name 不一致 / 未接続 / 占有が最多）
    - SW 不明（probe を持たない bridge）→ connect 成立で PASS
    - `90 00`（カードあり）/ `6A 81`（カード無し）→ PASS
    - `6A 86` → FAIL: `reader` が firmware の台数を超えている（契約 v1.2 §6）
    """
    if not result.connected:
        return False, "connect 不可（reader_name 不一致 / 未接続 / 他プロセスが占有）"
    if result.sw is None:
        return True, ""
    sw1, sw2 = result.sw
    if (sw1, sw2) == (0x90, 0x00):
        return True, "Get UID OK（カードあり）"
    if (sw1, sw2) == (0x6A, 0x81):
        return True, "Get UID OK（カード無し）"
    if (sw1, sw2) == (0x6A, 0x86):
        return False, (
            f"reader {cfg_reader_index(result.cfg)} は firmware の範囲外 (SW=6A86) — "
            "config の reader か firmware の台数を確認"
        )
    return False, f"Get UID SW={sw1:02X}{sw2:02X}（想定外）"


def format_event(ev, card_master: CardMaster, reader_index: Optional[int] = None) -> str:
    """RFIDEvent 1 件を watch 表示用の 1 行にする（UID 長・card 解決・契約適合を含む）。

    `reader_index` を渡すと役割ラベルに `[r3]`（物理リーダー index, 契約 v1.2 §6）を添える。
    """
    info = analyze_uid(ev.tag_id)
    card = ev.card or card_master.lookup(ev.tag_id)
    card_str = card if card else "(未登録)"
    role = reader_label({
        "role": ev.role, "seat": ev.seat, "index": ev.board_index, "reader": reader_index,
    })
    len_flag = "" if info.contract_length else "  ⚠ 非契約長(4/7/8B 期待)"
    return (
        f"  {role:<14} UID={info.normalized:<23} ({info.byte_length}B) "
        f"card={card_str}{len_flag}"
    )


# ――― pyscard 可用性 ―――

def pyscard_available() -> bool:
    """pyscard（smartcard）が import 可能かを返す。"""
    try:
        import smartcard.System  # noqa: F401
        return True
    except Exception:
        return False


def _require_pyscard() -> bool:
    if pyscard_available():
        return True
    print(
        "[error] pyscard が見つかりません。実機 PC/SC 経路には pyscard が必要です:\n"
        "        pip install \".[pcsc]\"   （Linux は pcscd の稼働も確認）",
        file=sys.stderr,
    )
    return False


# ――― サブコマンド ―――

def _cmd_list(
    args: argparse.Namespace,
    *,
    lister: ReadersLister = list_readers,
    counter: ReaderCounter = query_reader_count,
) -> int:
    if not _require_pyscard():
        return 2
    present = lister()
    print(f"接続中の PC/SC reader: {len(present)} 件"
          f"（v1.2: CCID slot は 1 つだけ = reader 名も 1 つ, ADR-0041）")
    counts: dict[str, Optional[int]] = {}
    for name in present:
        n = counter(name)
        counts[name] = n
        detail = (f"physical readers: {n}" if isinstance(n, int)
                  else "(v1.1 firmware: 台数問い合わせ非対応)")
        print(f"  - {name!r}  {detail}")
    if not present:
        print("  （0 件。pcscd / USB 接続 / ドライバを確認。ESP32-S3 が CCID class で見えているか）")

    rfid_cfg = load_rfid_config(args.config)
    pcsc_readers = get_pcsc_readers(rfid_cfg)
    print(f"\nconfig.rfid.pcsc_readers: {len(pcsc_readers)} 件 (transport={rfid_cfg.get('transport')!r})")
    report = match_readers(present, pcsc_readers)
    over = 0
    for cfg in report.matched:
        k = cfg_reader_index(cfg)
        n = counts.get(cfg.get("name", ""))
        warn = ""
        if isinstance(n, int) and k >= n:
            warn = f"  ⚠ reader {k} は firmware の台数 {n} を超えている"
            over += 1
        print(f"  [matched]      {reader_label(cfg):<14} ← {cfg.get('name')!r}{warn}")
    for cfg in report.missing:
        print(f"  [MISSING]      {reader_label(cfg):<14} ← {cfg.get('name')!r}  （config にあるが未接続）")
    for name in report.unconfigured:
        print(f"  [unconfigured] {name!r}  （接続中だが config 未記載）")
    if report.missing:
        print("\nヒント: MISSING は name の不一致が最多。上の reader 一覧の文字列を等値でコピーする（§4/§8）。")
    if over:
        print("\nヒント: reader は Get UID の P2（物理リーダー index, 0 起点）。firmware の台数に"
              "合わせて config.rfid.pcsc_readers[].reader を直す（§6）。")
    return 0


def _cmd_check(args: argparse.Namespace, *, bridge_factory: BridgeFactory = PCSCBridge) -> int:
    if not _require_pyscard():
        return 2
    rfid_cfg = load_rfid_config(args.config)
    pcsc_readers = get_pcsc_readers(rfid_cfg)

    print("== config lint (§4) ==")
    problems = lint_pcsc_readers(pcsc_readers)
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
    else:
        print(f"  ✓ pcsc_readers {len(pcsc_readers)} 件 OK")

    print("\n== connect + Get UID 検査 (§5-6: カード不要) ==")
    results = probe_connect(pcsc_readers, bridge_factory=bridge_factory)
    all_ok = bool(results)
    for r in results:
        ok, detail = check_verdict(r)
        mark = "✓ PASS" if ok else "✗ FAIL"
        suffix = f"  {detail}" if detail else ""
        print(f"  {mark}  {reader_label(r.cfg):<14} {r.cfg.get('name')!r}{suffix}")
        all_ok = all_ok and ok
    if not results:
        print("  （対象 reader なし）")

    ok = all_ok and not problems
    print(f"\n結果: {'PASS ✅' if ok else 'FAIL ❌'}  "
          f"（UID/card/hot-plug の実動作は `watch` で実カードを使って確認）")
    return 0 if ok else 1


def run_watch(
    pcsc_readers: list[dict],
    card_master: CardMaster,
    *,
    seconds: float,
    poll_interval_ms: int = 100,
    bridge_factory: BridgeFactory = PCSCBridge,
    sink: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """実 RFIDThread を起動し、タップごとに RFIDEvent を整形表示する（§6-8）。

    production の `RFIDThread` をそのまま使うため、role/seat/board_index/デバウンスの挙動は
    hand logger 実行時と同一。`seconds` 経過で停止する。観測した新規タッチ件数を返す。
    """
    from core.event_queue import make_rfid_queue
    from rfid.reader_thread import RFIDThread

    rfid_q = make_rfid_queue()
    stop = threading.Event()
    thread = RFIDThread(
        rfid_queue=rfid_q,
        card_master=card_master,
        reader_configs=pcsc_readers,
        poll_interval_ms=poll_interval_ms,
        stop_event=stop,
        bridge_factory=bridge_factory,
    )
    thread.start()

    # RFIDEvent.reader_id（config の並び順 = "reader_{i}"）→ 物理 reader index（表示用）。
    reader_indexes = {f"reader_{i}": cfg_reader_index(cfg) for i, cfg in enumerate(pcsc_readers)}

    seen = 0
    deadline = clock() + seconds
    try:
        while clock() < deadline:
            try:
                ev = rfid_q.get(timeout=0.2)
            except queue.Empty:
                continue
            seen += 1
            sink(format_event(ev, card_master, reader_indexes.get(ev.reader_id)))
    finally:
        stop.set()
        thread.join(timeout=2)
    return seen


def _cmd_watch(args: argparse.Namespace, *, bridge_factory: BridgeFactory = PCSCBridge) -> int:
    if not _require_pyscard():
        return 2
    rfid_cfg = load_rfid_config(args.config)
    pcsc_readers = get_pcsc_readers(rfid_cfg)
    if not pcsc_readers:
        print("[error] config.rfid.pcsc_readers が空です。先に `list` で reader を確認し config に記入。",
              file=sys.stderr)
        return 2
    card_master = CardMaster(rfid_cfg.get("card_master_file", "./rfid_cards.json"))
    poll = rfid_cfg.get("poll_interval_ms", 100)

    print(f"watch 開始: {args.seconds:.0f} 秒間、各 reader を順にタップしてください "
          f"(poll={poll}ms, Ctrl-C で中断)")
    print("各リーダーにカードを置く→離す→再度置く で、role/seat と hot-plug 再発火を確認できます。")
    print("ラベル末尾の `[r3]` が物理リーダー index（config の reader = Get UID の P2, §6）。")
    print("重ね置き（席 2 枚 / flop 3 枚）は枚数ぶん行が出ます（board は index+offset の位置）。\n")
    try:
        seen = run_watch(
            pcsc_readers, card_master,
            seconds=float(args.seconds),
            poll_interval_ms=poll,
            bridge_factory=bridge_factory,
        )
    except KeyboardInterrupt:
        print("\n中断しました。")
        return 0
    print(f"\n観測した新規タッチ: {seen} 件。"
          f"{' （0 件: カード/配線/正規化を確認）' if seen == 0 else ''}")
    return 0


# ――― raw 診断（pyscard 直叩き）―――

# 契約 §6 の Get UID pseudo-APDU（P2 = 物理 reader index）。`rfid/bridge.py:get_uid_apdu` と同値。
GET_UID_APDU = get_uid_apdu(0)

# SCardGetStatusChange の dwEventState ビット名。値は pyscard の smartcard.scard 定数から取る
# （scard_state_table）。テストは明示テーブルを渡すので pyscard 不要。
_SCARD_STATE_NAMES = (
    "PRESENT", "EMPTY", "MUTE", "UNPOWERED", "INUSE", "EXCLUSIVE",
    "UNAVAILABLE", "ATRMATCH", "UNKNOWN", "IGNORE",
)


def scard_state_table() -> list[tuple[str, int]]:
    """pyscard の SCARD_STATE_* 定数から (名前, ビット) テーブルを作る（無い定数は飛ばす）。"""
    try:
        from smartcard import scard
    except Exception:
        return []
    table: list[tuple[str, int]] = []
    for name in _SCARD_STATE_NAMES:
        val = getattr(scard, f"SCARD_STATE_{name}", None)
        if isinstance(val, int) and val:
            table.append((name, val))
    return table


def decode_reader_state(event_state: int, table: list[tuple[str, int]]) -> str:
    """dwEventState をフラグ名の '|' 連結にする（該当なしなら 16 進）。CHANGED は呼び側で落とす。"""
    names = [name for name, bit in table if event_state & bit]
    return "|".join(names) if names else f"0x{event_state:x}"


def format_scard_error(exc: BaseException) -> str:
    """pyscard 例外を 'クラス名: メッセージ [hresult=0x........]' に整形する。

    hresult の代表値（Windows/pcsc-lite 共通）:
      0x8010000C SCARD_E_NO_SMARTCARD      … OS がスロットを空と判断（firmware の挿抜通知/状態が届いていない）
      0x80100066 SCARD_W_UNRESPONSIVE_CARD … power-on(IccPowerOn) したが応答なし
      0x80100067 SCARD_W_UNPOWERED_CARD    … カードが通電されていない
      0x8010000F SCARD_E_PROTO_MISMATCH    … ATR のプロトコルと要求が不一致
      0x8010000B SCARD_E_SHARING_VIOLATION … 他プロセスが reader を占有
    """
    hr = getattr(exc, "hresult", None)
    text = f"{type(exc).__name__}: {exc}"
    if isinstance(hr, int) and hr != -1:
        text += f" hresult=0x{hr & 0xFFFFFFFF:08x}"
    return text


def _cmd_raw(args: argparse.Namespace) -> int:
    """pyscard を直接叩き、OS のスロット状態と connect/Get UID の結果・例外を時系列で表示する。

    RFIDThread / PCSCBridge を経由しない（PCSCBridge.read_uid は例外を debug ログに落として
    None を返すため、watch が 0 件のときの「理由」が見えない）。ここでは:
      - SCardGetStatusChange(timeout=0) で OS（WinSCard/pcscd）が見ているスロット状態を表示。
        PRESENT なら OS はカードを認識している。EMPTY のままなら firmware の挿抜通知/状態が
        OS に届いていない（→ firmware 側の interrupt / GetSlotStatus を疑う）。
      - createConnection().connect() → FF CA 00 00 00 を試し、例外はクラス・メッセージ・hresult
        を表示（意味は format_scard_error の docstring）。
    状態や結果が変わった行だけ出す（スパム回避）。
    """
    if not _require_pyscard():
        return 2
    from smartcard import scard
    from smartcard.System import readers as sc_readers
    from smartcard.util import toHexString

    present = [str(r) for r in sc_readers()]
    if not present:
        print("[error] PC/SC reader が 0 件です（`list` を確認）", file=sys.stderr)
        return 1
    name = args.name
    if not name:
        configured = [c.get("name", "") for c in get_pcsc_readers(load_rfid_config(args.config))]
        name = next((n for n in configured if n in present), present[0])
    reader_obj = next((r for r in sc_readers() if str(r) == name), None)
    if reader_obj is None:
        print(f"[error] reader {name!r} が見つかりません。接続中: {present}", file=sys.stderr)
        return 1
    reader_index = args.reader or 0
    apdu = get_uid_apdu(reader_index)

    table = scard_state_table()
    changed_bit = getattr(scard, "SCARD_STATE_CHANGED", 0)
    hresult, hcontext = scard.SCardEstablishContext(scard.SCARD_SCOPE_USER)
    if hresult != scard.SCARD_S_SUCCESS:
        print(f"[error] SCardEstablishContext: 0x{hresult & 0xFFFFFFFF:08x} "
              f"{scard.SCardGetErrorMessage(hresult)}", file=sys.stderr)
        return 1

    print(f"raw 開始: reader={name!r} の物理リーダー {reader_index} を {args.seconds:.0f} 秒間 "
          f"{args.interval:.1f}s 間隔で診断。カードを 1 枚置いて数秒キープしてください（Ctrl-C で中断）。")
    print("  [OS状態]  = SCardGetStatusChange が返すスロット状態（OS がカードを認識しているか）")
    print(f"  [connect] = SCardConnect → Get UID({' '.join(f'{b:02X}' for b in apdu)}) の結果 or 例外\n")
    last_state: Optional[str] = None
    last_result: Optional[str] = None
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds:
            t = time.monotonic() - t0
            hr, states = scard.SCardGetStatusChange(
                hcontext, 0, [(name, scard.SCARD_STATE_UNAWARE)])
            if hr == scard.SCARD_S_SUCCESS and states:
                _, ev, atr = states[0]
                st = decode_reader_state(ev & ~changed_bit, table)
                if atr:
                    st += f" ATR={toHexString(list(atr))}"
            else:
                st = (f"SCardGetStatusChange 失敗 0x{hr & 0xFFFFFFFF:08x} "
                      f"{scard.SCardGetErrorMessage(hr)}")
            if st != last_state:
                print(f"[{t:5.1f}s] [OS状態]  {st}")
                last_state = st

            try:
                conn = reader_obj.createConnection()
                conn.connect()
                atr_s = toHexString(conn.getATR())
                data, sw1, sw2 = conn.transmit(list(apdu))
                conn.disconnect()
                res = (f"connect OK ATR={atr_s} → Get UID: {format_uid_payload(data)} "
                       f"SW={sw1:02X}{sw2:02X}")
            except KeyboardInterrupt:
                raise
            except Exception as e:  # 例外の種類そのものが診断対象
                res = format_scard_error(e)
                hr_e = getattr(e, "hresult", None)
                if isinstance(hr_e, int) and hr_e != -1:
                    res += f" ({scard.SCardGetErrorMessage(hr_e)})"
            if res != last_result:
                print(f"[{t:5.1f}s] [connect] {res}")
                last_result = res
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n中断しました。")
    finally:
        scard.SCardReleaseContext(hcontext)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="実機 RFID（PC/SC canonical, ADR-0015/0034/0041, 契約 v1.2）の bring-up 診断",
    )
    parser.add_argument(
        "--config", default=None,
        help="config パス（既定: config.json があればそれ、無ければ config_default.json）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser(
        "list", help="reader_name（1 個）と物理リーダー台数を列挙し config との一致を表示")
    p_list.set_defaults(func=_cmd_list)

    p_check = sub.add_parser(
        "check", help="config lint + 各物理リーダーへの connect + Get UID 検査（カード不要）")
    p_check.set_defaults(func=_cmd_check)

    p_watch = sub.add_parser("watch", help="実 RFIDThread でタップを待ち受け表示（hot-plug 確認）")
    p_watch.add_argument("--seconds", type=float, default=30.0, help="待ち受け秒数（既定 30）")
    p_watch.set_defaults(func=_cmd_watch)

    p_raw = sub.add_parser(
        "raw", help="pyscard 直叩き診断: OS のスロット状態 + connect/Get UID の結果・例外（watch 0 件の切り分け）")
    p_raw.add_argument("--seconds", type=float, default=20.0, help="診断秒数（既定 20）")
    p_raw.add_argument("--interval", type=float, default=0.3, help="試行間隔 秒（既定 0.3）")
    p_raw.add_argument("--reader", type=int, default=0,
                       help="物理リーダー index = Get UID の P2（既定 0, 契約 v1.2 §6）")
    p_raw.add_argument("--name", default=None,
                       help="対象 reader_name（既定: config の先頭で接続中のもの、無ければ最初の reader）")
    p_raw.set_defaults(func=_cmd_raw)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
