"""core/control_queue.py

hand logger 遠隔制御の append-only control-command queue（ADR-0037）。

staff API（別プロセス）が `{log_dir}/{session_id}.control.jsonl` に 1 行 1 コマンドを **append** し、
hand logger プロセスの consumer がそれを tail して `AudioEvent` に翻訳し `audio_queue` に積む。
状態変更経路は従来の IntegrationThread に一元化する（新しい mutate 経路を作らない, ISSUE-0012）。

不変条件:
  - append-only（コマンドは mutate/delete しない）。
  - 1 行 = 1 JSON コマンド `{command_id, type, args, created_at}`。
  - reader は **byte offset** を進めながら新規行のみ読む。consumer は起動時に末尾へシークし、
    過去コマンドを再実行しない（hand logger 再起動での誤再生を防ぐ）。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# staff から受け付ける制御コマンド種別（GUI/CLI の new_hand / winner / rebuy に対応）。
VALID_CONTROL_TYPES = ("new_hand", "winner", "rebuy")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ControlCommand:
    """1 件の制御コマンド（append-only ログの 1 行）。"""

    command_id: str
    type: str
    args: dict
    created_at: str

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "type": self.type,
            "args": dict(self.args),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ControlCommand":
        return cls(
            command_id=d["command_id"],
            type=d["type"],
            args=dict(d.get("args") or {}),
            created_at=d.get("created_at", ""),
        )


class ControlCommandLog:
    """control-command queue の append-only I/O（session ごとに 1 ファイル）。

    writer（staff API）と reader（hand logger consumer）は別プロセスだが、append-only + byte
    offset 読みなので lock 不要（POSIX の追記書き込みは行単位で観測される前提。reader は
    完全な行＝末尾が改行のものだけを消費する）。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(
        self, type: str, args: Optional[dict] = None, clock: Callable[[], str] = _now_iso
    ) -> ControlCommand:
        """コマンドを 1 行 append して返す。type は VALID_CONTROL_TYPES のみ。"""
        if type not in VALID_CONTROL_TYPES:
            raise ValueError(f"unknown control type: {type!r}")
        command = ControlCommand(
            command_id=uuid.uuid4().hex,
            type=type,
            args=dict(args or {}),
            created_at=clock(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(command.to_dict(), ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        return command

    def end_offset(self) -> int:
        """現在のファイル末尾の byte offset（未作成は 0）。consumer の開始位置に使う。"""
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def read_from(self, offset: int) -> tuple[list[ControlCommand], int]:
        """``offset`` から完全な行（末尾が改行）のみ読み、(commands, new_offset) を返す。

        途中まで書かれた最終行は次回に持ち越す（new_offset を最後の改行直後までに留める）。
        壊れた行はスキップ（log warning）して読み進める。
        """
        try:
            with self.path.open("rb") as f:
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            return [], offset

        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return [], offset  # 完全な行がまだ無い
        complete = data[: last_nl + 1]
        new_offset = offset + len(complete)
        commands: list[ControlCommand] = []
        for raw in complete.decode("utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                commands.append(ControlCommand.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                logger.warning("skip malformed control line: %r", line)
        return commands, new_offset


def command_to_audio_event(command: ControlCommand, clock: Callable[[], float]):
    """ControlCommand を AudioEvent に翻訳する（GUI/CLI の構築と同一形）。

    未対応 type / 不正 args は None を返す（consumer はスキップ）。状態変更は積んだ AudioEvent を
    IntegrationThread が処理する（ここでは GameState を一切触らない）。
    """
    from core.events import AudioEvent

    t = command.type
    args = command.args or {}
    if t == "new_hand":
        return AudioEvent(action="new_hand", amount=0, timestamp=clock(), raw_text="")
    if t == "winner":
        seat = args.get("seat")
        if not isinstance(seat, int):
            logger.warning("control winner without int seat: %r", args)
            return None
        return AudioEvent(
            action="winner", amount=0, timestamp=clock(),
            raw_text=f"シート{seat} ウィナー", seat=seat,
        )
    if t == "rebuy":
        seat = args.get("seat")
        amount = args.get("amount")
        if not isinstance(seat, int) or not isinstance(amount, int) or amount <= 0:
            logger.warning("control rebuy with invalid args: %r", args)
            return None
        return AudioEvent(
            action="rebuy", amount=amount, timestamp=clock(),
            raw_text=f"シート{seat} リバイ {amount}", seat=seat,
        )
    logger.warning("unknown control type: %r", t)
    return None
