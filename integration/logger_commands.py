"""integration/logger_commands.py — ロガーの操作（端末の 1 行 / iPad の制御）を 1 か所で扱う（ADR-0073 D4）。

前は端末（`main.py` の入力のループ）だけがこの処理を持ち、iPad の制御（`core/control_queue.py`）は一部の操作だけを
別の翻訳で AudioEvent にしていた。端末と制御で events.jsonl の行がずれる（例: 勝者）うえ、名前（お客さんの記録に書く）・
読み取り装置の再起動（RFID のスレッドを呼ぶ）など、queue の外の副作用を持つ操作は iPad から送れなかった。

ここでは「1 つの操作 → ロガーの中の副作用 + AudioEvent の列」を作る。端末の入力も iPad の制御（同じ意味の端末の 1 行に
直してから）もこれを通すので、同じ操作なら同じ記録になる（`tests/test_logger_commands.py`）。ゲーム状態は変えない
（AudioEvent を IntegrationThread が処理する, ISSUE-0012）。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

FULLWIDTH_TO_ASCII = str.maketrans(
    {chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)} | {"　": " "}
)

# 引数がくっついたコマンド（`w1` / `r1 500`）を分ける。ログが入力行に割り込む環境では
# 空白を打ち損ねやすく、実機で `w1` が 2 回弾かれた（ISSUE-0034）。引数を取る w/r/d のみ。
_GLUED_COMMAND = re.compile(r"^([wrd])(\d+)$")

USAGE = ("認識できません。コマンド: q / n / w <席> / r <席> <金額> / d <席> <金額> / ante on|off|<額> / clock、"
         "または読み上げ文（例: チェック / シート3 コール / ベット 500 / 600）")


def normalize_command(line: str) -> str:
    """コマンド照合用に全角英数字・全角スペースを半角へ寄せ、`w1` を `w 1` に分ける。"""
    normalized = line.translate(FULLWIDTH_TO_ASCII)
    parts = normalized.split()
    if parts:
        m = _GLUED_COMMAND.match(parts[0].lower())
        if m:
            parts[0:1] = [m.group(1), m.group(2)]
            return " ".join(parts)
    return normalized


def parse_name_command(parts: list[str]) -> "tuple[int, str | None] | None":
    """`name <席> <名前>` / `name <席> -`（空席にする）を (席, 名前 or None) にする。不正なら None。

    `seat` にしないのは、`seat 3 call` が読み上げ文（英語の席表現）として既に通るため。
    """
    if len(parts) < 3:
        return None
    try:
        seat = int(parts[1])
    except ValueError:
        return None
    name = " ".join(parts[2:]).strip()
    if name == "-":
        return seat, None
    return (seat, name) if name else None


@dataclass
class CommandResult:
    """1 つの操作の結果: queue に積む AudioEvent・端末 / iPad に出す文・使えたか・終了か。"""

    events: list = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    ok: bool = True          # False = 使い方の誤り・この卓ではできない（iPad では「未適用」）
    stop: bool = False       # q（終了）。終わり方はロガーの側が持つ


class LoggerCommands:
    """端末の 1 行と iPad の制御を、副作用 + AudioEvent の列にする。端末と制御の読み手（別のスレッド）から呼ばれるので
    ロックで順番にする。"""

    def __init__(self, *, session_cfg: dict, game_state: Any, integration_thread: Any,
                 rfid_thread: Any = None, session_repo: Any = None, player_repo: Any = None,
                 seat_player_map: Optional[dict] = None, clock: Callable[[], float] = time.time) -> None:
        self.session_cfg = session_cfg
        self.game_state = game_state
        self.integration_thread = integration_thread
        self.rfid_thread = rfid_thread
        self.session_repo = session_repo
        self.player_repo = player_repo
        self.seat_player_map = dict(seat_player_map or {})
        self._clock = clock
        self._lock = threading.RLock()

    def _event(self, action: str, amount: int = 0, raw_text: str = "", seat: Optional[int] = None):
        from core.events import AudioEvent

        kwargs = {"seat": seat} if seat is not None else {}
        return AudioEvent(action=action, amount=amount, timestamp=self._clock(), raw_text=raw_text, **kwargs)

    # ――― 入口 ―――

    def run_line(self, line: str) -> CommandResult:
        """端末の 1 行（コマンドか読み上げ文）。"""
        with self._lock:
            return self._run_line(line)

    def run_text(self, text: str) -> CommandResult:
        """読み上げ文だけ（iPad の「打ち込み」。`n` などのコマンドとしては読まない）。"""
        with self._lock:
            return self._text(text)

    # ――― 中身 ―――

    def _run_line(self, line: str) -> CommandResult:
        line = line.strip()
        if not line:
            return CommandResult()
        parts = normalize_command(line).split()
        cmd = parts[0].lower()
        if cmd == "q":
            return CommandResult(stop=True)
        if cmd == "n":
            return CommandResult([self._event("new_hand")], ["新ハンド開始を送信しました。"])
        if cmd == "w" and len(parts) >= 2:
            try:
                seats = [int(p) for p in parts[1:]]
            except ValueError:
                return CommandResult(messages=["使い方: w <席番号>（分けたときは w <席> <席>）"], ok=False)
            # 2 席以上 = 分けた（チョップ。engine が席をすべて拾って等分する）
            raw = (" ".join(f"シート{s}" for s in seats) + " チョップ" if len(seats) > 1
                   else f"シート{seats[0]} ウィナー")
            return CommandResult([self._event("winner", raw_text=raw)])
        if cmd == "r" and len(parts) >= 3:
            try:
                seat, amount = int(parts[1]), int(parts[2])
            except ValueError as e:
                return CommandResult(messages=[f"エラー: {e}"], ok=False)
            return CommandResult([self._event("rebuy", amount, f"シート{seat} リバイ {amount}", seat)],
                                 [f"リバイを送信しました: 席{seat} +{amount}（反映はアクション表示で確認）"])
        if cmd == "d" and len(parts) >= 3:
            # スタックを減らす（0 より下にはしない。ハンドに入っている席はそのハンドのあとに, 2026-10-19）
            try:
                seat, amount = int(parts[1]), int(parts[2])
                if amount <= 0:
                    raise ValueError("金額は 1 以上")
            except ValueError as e:
                return CommandResult(messages=[f"使い方: d <席> <金額>（{e}）"], ok=False)
            return CommandResult([self._event("reduce_stack", amount, f"シート{seat} スタック減 {amount}", seat)])
        if cmd == "name":
            return self._name(line)
        if cmd == "dealer":
            # 配っているディーラー（ADR-0074）。`dealer -` = 名前を消す。ハンドの途中なら次のハンドから
            name = line.translate(FULLWIDTH_TO_ASCII).strip()[len("dealer"):].strip()
            if not name or len(name) > 40:
                return CommandResult(messages=["使い方: dealer <名前>（40 文字まで）/ dealer -（消す）"], ok=False)
            return CommandResult([self._event("set_dealer", 0, name)],
                                 ["ディーラーの名前を消しました" if name == "-" else f"ディーラー {name} を送信しました"])
        if cmd in ("button", "btn") and len(parts) >= 2:
            try:
                seat = int(parts[1])
            except ValueError:
                return CommandResult(messages=["使い方: button <席番号>"], ok=False)
            return CommandResult([self._event("set_button", 0, f"シート{seat} ボタン", seat)],
                                 [f"ボタン 席{seat} を送信しました"])
        if cmd == "blinds":
            numbers = re.findall(r"\d+", " ".join(parts[1:]))    # `blinds 200 400` / `blinds 200/400`
            if len(numbers) != 2 or int(numbers[0]) <= 0 or int(numbers[0]) > int(numbers[1]):
                return CommandResult(messages=["使い方: blinds <SB> <BB>（例: blinds 200 400）"], ok=False)
            sb, bb = int(numbers[0]), int(numbers[1])
            return CommandResult([self._event("set_blinds", bb, f"{sb}/{bb}")],
                                 [f"ブラインド {sb}/{bb} を送信しました（次のハンドから）"])
        if cmd == "ante":
            # BB アンティ。on = BB と同額（BB を変えればアンティも変わる）/ 額 = その額（ADR-0071）/ off = 外す
            choice = parts[1].lower() if len(parts) >= 2 else ""
            if choice not in ("on", "off") and not choice.isdigit():
                return CommandResult(messages=["使い方: ante on（BB と同額）/ ante <額> / ante off（外す）"], ok=False)
            return CommandResult([self._event("set_bb_ante", 0, choice)])
        if cmd == "clock":
            # トーナメントの時計（ADR-0071）。状態の変更はロガーのスレッドで（queue 経由）
            return CommandResult([self._event("clock", 0, " ".join(parts[1:]) or "status")])
        if cmd == "rr":
            # 読み取り装置の再起動（契約 v1.11 §6）。卓に札が無くなったら RFID のスレッドが送る（結果は ● で出る）
            if self.rfid_thread is None or not hasattr(self.rfid_thread, "request_restart"):
                return CommandResult(messages=["RFID のリーダー（PC/SC）を使っていないので再起動できません"], ok=False)
            self.rfid_thread.request_restart()
            return CommandResult(messages=["読み取り装置の再起動を頼みました（卓に札が無いときに送ります）"])
        if cmd in ("cb", "cs") and len(parts) >= 2:
            # ミスディール訂正（ADR-0054）。状態変更は他と同じく queue 経由。
            try:
                key = int(parts[1])
            except ValueError:
                return CommandResult(messages=[f"使い方: {cmd} <{'位置' if cmd == 'cb' else '席番号'}>"], ok=False)
            if cmd == "cb":
                return CommandResult([self._event("correct_board", key, f"ボード{key} 訂正")],
                                     [f"ボード {key} 枚目の取り消しを送信しました（正しいカードを置いてください）"])
            return CommandResult([self._event("correct_seat", 0, f"シート{key} 訂正", key)],
                                 [f"席 {key} の札の読み直しを送信しました（正しいカードを置き直してください）"])
        return self._text(line)

    def _text(self, text: str) -> CommandResult:
        # コマンド以外は **ディーラーのアナウンスとして解釈**する（マイク無しでアクションを投入する経路。音声と同じ
        # `parse_actions` を通すので語彙は共通）。キーボード入力は ASR ではなく操作者の意図的な入力なので信頼度 1.0
        # （None は「Whisper 欠測」の意味で保守的既定 0.5 = 要レビューに倒れる, ADR-0047 B3）。続けて打った複数の
        # アクション（「フォールド フォールド コール」）は順に分ける（ADR-0061）
        from audio.recognizer import parse_actions
        from audio.recorder import describe_event

        events = parse_actions(text, confidence=1.0)
        if not events:
            return CommandResult(messages=[USAGE], ok=False)
        return CommandResult(events, [f"→ {describe_event(ev)}" for ev in events])

    def _name(self, line: str) -> CommandResult:
        parsed = parse_name_command(line.translate(FULLWIDTH_TO_ASCII).split())
        if parsed is None:
            return CommandResult(messages=["使い方: name <席> <名前> / name <席> -（空席）"], ok=False)
        seat, name = parsed
        vacate = name is None
        out = CommandResult()
        adding = seat not in {p["seat"] for p in self.session_cfg["players"]}
        if adding:
            # 途中から座った人（次のハンドから席を足す, 2026-10-26。pokerkit の卓だけ = legacy は席を足せない）
            if vacate or not 1 <= seat <= 9 or not hasattr(self.game_state, "add_seat"):
                return CommandResult(messages=[f"席{seat} はこの卓にありません"], ok=False)
            self.session_cfg["players"].append({"seat": seat, "name": name, "stack": 0, "named": True})
            out.messages.append(f"席{seat} を足します（次のハンドから。持ち点を数えるなら r {seat} <金額> で買い込み）")
            # 先に席を足す（名前の変更が席を見つけられるように）
            out.events.append(self._event("sit_in", 0, f"シート{seat} 参加", seat))
        if self.session_repo is None:
            out.messages.append("（席とお客さんの記録は無効です: config の session_layer.enabled=true で有効。"
                                "席の参加・休みだけ反映します）")
        else:
            new_map = dict(self.seat_player_map)
            if vacate:
                new_map.pop(seat, None)
            else:
                new_map[seat] = self.player_repo.find_or_create(name).player_id
            self.seat_player_map = new_map
            # 席 → player は次のハンドの開始時に書かれる。名前はゲーム状態を持つ integration スレッドで変える
            # （ハンドの途中なら次のハンドから）。
            self.integration_thread.set_seat_player_map(self.seat_player_map)
        shown = f"Player{seat}" if vacate else name
        out.events.append(self._event("rename_seat", 0, shown, seat))
        # 空席は次のハンドから配られない（休み）。名前を付けた席は参加（休みなら戻す）。
        if not adding:
            out.events.append(self._event("sit_out" if vacate else "sit_in", 0,
                                          f"シート{seat} {'休み' if vacate else '参加'}", seat))
        out.messages.append(f"席{seat} を空席にしました（次のハンドから配られません）" if vacate
                            else f"席{seat} を {shown} にしました（次のハンドから）")
        return out


# ───────────────────────── iPad の制御 → 端末の 1 行 ─────────────────────────


def control_line(command_type: str, args: dict) -> Optional[str]:
    """iPad の制御を、同じ意味の端末の 1 行にする（同じ処理を通して同じ記録にするため）。行の形が無い制御は None
    （席の休み / 参加・台本・真のアクションの持ち点 = `core/control_queue.command_to_audio_event` のまま）。

    引数が使えなければ ValueError（API と読み手で同じ検査）。
    """
    def seat(key: str = "seat") -> int:
        v = args.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 9:
            raise ValueError(f"{command_type} には {key}（1〜9）が要ります")
        return v

    def positive(key: str) -> int:
        v = args.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"{command_type} には正の {key} が要ります")
        return v

    t = command_type
    if t == "new_hand":
        return "n"
    if t == "winner":
        seats = args.get("seats")
        if isinstance(seats, list) and len(seats) >= 2:
            if not all(isinstance(s, int) and not isinstance(s, bool) and 1 <= s <= 9 for s in seats):
                raise ValueError("winner の seats は 1〜9 の席の並び")
            return "w " + " ".join(str(s) for s in seats)
        return f"w {seat()}"
    if t == "rebuy":
        return f"r {seat()} {positive('amount')}"
    if t == "reduce_stack":
        return f"d {seat()} {positive('amount')}"
    if t == "correct_board":
        index = args.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= 5:
            raise ValueError("correct_board には index（1〜5）が要ります")
        return f"cb {index}"
    if t == "correct_seat":
        return f"cs {seat()}"
    if t == "set_blinds":
        sb, bb = positive("sb"), positive("bb")
        if sb > bb:
            raise ValueError("set_blinds は sb <= bb")
        return f"blinds {sb} {bb}"
    if t == "set_button":
        return f"button {seat()}"
    if t == "seat_player":
        name = args.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if len(name) > 40 or "\n" in name:
            raise ValueError("名前は 40 文字まで")
        return f"name {seat()} {name or '-'}"
    if t == "set_ante":
        value = str(args.get("value", "")).strip().lower()
        if value not in ("on", "off") and not value.isdigit():
            raise ValueError("set_ante の value は on / off / 額")
        return f"ante {value}"
    if t == "clock":
        op = str(args.get("op", "status")).strip().lower()
        if not re.fullmatch(r"status|pause|resume|next|prev|set \d{1,3}( \d{1,4})?", op):
            raise ValueError("clock の op は status / pause / resume / next / prev / set <レベル> [経過分]")
        return f"clock {op}"
    if t == "reader_restart":
        return "rr"
    if t == "set_dealer":
        name = args.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if len(name) > 40 or "\n" in name:
            raise ValueError("名前は 40 文字まで")
        return f"dealer {name or '-'}"
    return None
