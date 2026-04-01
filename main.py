from __future__ import annotations

import argparse
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _prompt_session_config() -> dict:
    """CLIで席数・プレイヤー名・スタック・ブラインドを入力する。"""
    print("=== ポーカーハンドロガー セッション設定 ===")

    while True:
        try:
            num_seats = int(input("席数 (2〜9): ").strip())
            if 2 <= num_seats <= 9:
                break
        except ValueError:
            pass
        print("2〜9 の整数を入力してください。")

    players = []
    for i in range(1, num_seats + 1):
        name = input(f"席{i} プレイヤー名: ").strip() or f"Player{i}"
        while True:
            try:
                stack = int(input(f"席{i} 初期スタック: ").strip())
                if stack > 0:
                    break
            except ValueError:
                pass
            print("正の整数を入力してください。")
        players.append({"seat": i, "name": name, "stack": stack})

    while True:
        try:
            sb = int(input("SB金額: ").strip())
            bb = int(input("BB金額: ").strip())
            if sb > 0 and bb > 0:
                break
        except ValueError:
            pass
        print("正の整数を入力してください。")

    log_dir = input("ログ保存先 (空Enterで ./logs): ").strip() or "./logs"

    return {"players": players, "sb": sb, "bb": bb, "log_dir": log_dir}


def run_cli() -> None:
    """Phase 1 CLIモード: AudioThread + IntegrationThread を起動してセッションを録音する。"""
    from core.config import load_config
    from core.event_queue import make_audio_queue
    from core.game_state import GameStateManager, PlayerState
    from audio.recorder import AudioThread
    from integration.engine import IntegrationThread
    from output.json_writer import JsonWriter

    cfg = load_config()
    session_cfg = _prompt_session_config()

    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in session_cfg["players"]
    ]
    game_state = GameStateManager(
        players=players,
        sb=session_cfg["sb"],
        bb=session_cfg["bb"],
    )

    session_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_session1"
    json_writer = JsonWriter(log_dir=session_cfg["log_dir"], session_id=session_id)

    audio_q = make_audio_queue()
    stop_event = threading.Event()

    def on_action(record):
        print(
            f"  [{record.street}] 席{record.seat}({record.player_name}) "
            f"{record.action} {record.amount or ''}"
            f"  pot={record.pot_after}"
            + (" [要確認]" if record.needs_review else "")
        )

    audio_cfg = cfg.get("audio", {})
    audio_thread = AudioThread(
        audio_queue=audio_q,
        device_id=audio_cfg.get("device_id", 0),
        sample_rate=audio_cfg.get("sample_rate", 16000),
        model_size=audio_cfg.get("whisper_model", "medium"),
        language=audio_cfg.get("language", "ja"),
        stop_event=stop_event,
    )
    integration_thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=game_state,
        json_writer=json_writer,
        on_action=on_action,
        stop_event=stop_event,
    )

    audio_thread.start()
    integration_thread.start()

    print(f"\nセッション開始。ログ: {json_writer.path}")
    print("コマンド: [q]=終了  [n]=新ハンド  [w <席>]=ウィナー  [r <席> <金額>]=リバイ")
    print("ディーラーがアナウンスすると自動検出されます。\n")

    try:
        while True:
            line = input("> ").strip()
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()

            if cmd == "q":
                break
            elif cmd == "n":
                game_state.new_hand()
                print(f"新ハンド開始: hand_id={game_state.hand_id}")
            elif cmd == "w" and len(parts) >= 2:
                try:
                    seat = int(parts[1])
                    from core.events import AudioEvent
                    import time
                    audio_q.put(AudioEvent(
                        action="winner",
                        amount=0,
                        timestamp=time.time(),
                        raw_text=f"シート{seat} ウィナー",
                    ))
                except ValueError:
                    print("使い方: w <席番号>")
            elif cmd == "r" and len(parts) >= 3:
                try:
                    seat = int(parts[1])
                    amount = int(parts[2])
                    game_state.rebuy(seat, amount)
                    print(f"リバイ: 席{seat} +{amount} → スタック {game_state.get_stack(seat)}")
                except (ValueError, Exception) as e:
                    print(f"エラー: {e}")
            else:
                print("不明なコマンドです。q / n / w <席> / r <席> <金額>")

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop_event.set()
        audio_thread.join(timeout=3)
        integration_thread.join(timeout=3)
        print(f"\nセッション終了。ログ保存先: {json_writer.path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ポーカーハンドロガー")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="CLIモードで起動（Phase 1: 音声認識のみ、カメラなし）",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="ROIキャリブレーションモードで起動（Phase 2）",
    )
    args = parser.parse_args()

    if args.calibrate:
        print("キャリブレーションモードは Phase 2 で実装予定です。")
        sys.exit(0)

    if args.cli:
        run_cli()
    else:
        # デフォルトはGUIモード（Phase 4 で実装）
        print("GUIモードは Phase 4 で実装予定です。--cli オプションを使用してください。")
        sys.exit(0)


if __name__ == "__main__":
    main()
