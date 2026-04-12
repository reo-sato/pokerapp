"""tests/simulator.py

simulate_speech(text) を呼ぶと AudioStreamBuffer を通じて AudioEvent を生成し、
IntegrationThread に注入する。生成された ActionRecord を JSON Lines で標準出力に出力する。

使い方:
    python -c "from tests.simulator import simulate_speech; simulate_speech('レイズ 600')"
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Optional

from audio.stream_buffer import AudioStreamBuffer
from core.event_queue import make_audio_queue
from core.game_state import GameState
from core.hand_log import ActionRecord, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def simulate_speech(
    text: str,
    timeout: float = 3.0,
    *,
    sb: int = 100,
    bb: int = 200,
    num_seats: int = 6,
) -> Optional[ActionRecord]:
    """テキストを音声認識結果として IntegrationThread に注入し、ActionRecord を返す。

    Args:
        text: 認識されたテキスト（例: 'レイズ 600'）
        timeout: ActionRecord が届くまでの最大待機秒数
        sb: スモールブラインド額
        bb: ビッグブラインド額
        num_seats: 席数

    Returns:
        生成された ActionRecord、または timeout 時に None
    """
    players = [
        PlayerState(seat=i, name=f"Player{i}", stack=10000)
        for i in range(1, num_seats + 1)
    ]
    gs = GameState(players=players, sb=sb, bb=bb, button_seat=1)
    gs.new_hand()

    audio_q = make_audio_queue()
    record_q: queue.Queue[ActionRecord] = queue.Queue()
    stop = threading.Event()

    writer = JsonWriter(log_dir="/tmp", session_id="simulator_session")
    it = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        on_action=record_q.put,
        stop_event=stop,
    )
    it.start()

    # AudioStreamBuffer でテキストを AudioEvent に変換してキューへ投入
    buf = AudioStreamBuffer()
    events = buf.process_utterance(text)
    for ev in events:
        audio_q.put(ev)

    # ActionRecord が届くまで待機
    record: Optional[ActionRecord] = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = record_q.get(timeout=0.1)
            break
        except queue.Empty:
            pass

    stop.set()
    it.join(timeout=2)
    return record


if __name__ == "__main__":
    import sys

    text = sys.argv[1] if len(sys.argv) > 1 else "レイズ 600"
    result = simulate_speech(text)
    if result is None:
        print(f"タイムアウト: '{text}' に対するアクションが生成されませんでした", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result.to_dict(), ensure_ascii=False))
