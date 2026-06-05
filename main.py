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


def _init_session_layer(cfg: dict, session_cfg: dict):
    """Phase 2.2 (ADR-0008): config flag に応じて S2 session レイヤを初期化する。

    config.session_layer.enabled が真なら ``SessionRepository.create_session`` で
    UUID4 hex の session_id を採番し、それを hand logger の canonical session_id に使う。
    偽なら従来の timestamp session_id を採番し、session レイヤには接続しない（rollback path）。

    Returns:
        (session_id, session_repo, player_repo, seating)
          - session_id: JsonWriter / HandSummary が使う canonical な session_id。
          - session_repo: 有効時のみ SessionRepository、無効時 None。
          - player_repo: 有効時のみ PlayerRepository（GUI の seat 選択 UI が参照）、無効時 None。
          - seating: 初期 ``seat_no -> player_id`` マップ。Phase 2.3 では空で開始し、GUI の
            seat selection UX（`gui/seat_assignment.py`）で実行時に設定する。
    """
    if cfg.get("session_layer", {}).get("enabled", False):
        from core.player_repository import PlayerRepository
        from core.session_repository import SessionRepository

        # player_repo を共有: session_repo の unknown_player 判定と GUI の player 候補が
        # 同じ registry を見るようにする。
        player_repo = PlayerRepository()
        session_repo = SessionRepository(player_repo=player_repo)
        session = session_repo.create_session(
            blinds={"sb": session_cfg["sb"], "bb": session_cfg["bb"]},
        )
        logger.info("Session layer enabled: session_id=%s", session.session_id)
        # 初期 seating は空。seat→player_id は GUI の seat selection で hand 開始時に確定する。
        return session.session_id, session_repo, player_repo, {}

    session_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_session1"
    return session_id, None, None, {}


def _make_event_recorder(cfg: dict, log_dir: str, session_id: str):
    """config.recording.enabled が true なら EventRecorder を返す (R1, ADR-0010)。

    デフォルト false = 記録しない (挙動不変)。生イベントを
    logs/{session_id}.events.jsonl に append-only で記録する sidecar。
    """
    if not cfg.get("recording", {}).get("enabled", False):
        return None
    from output.event_recorder import EventRecorder

    return EventRecorder(Path(log_dir) / f"{session_id}.events.jsonl")


def _make_game_state(cfg: dict, players: list, sb: int, bb: int):
    """config.engine.backend で game-state 実装を選ぶ (R2, ADR-0009)。

    既定 "legacy" = 従来の `GameStateManager`（挙動不変）。"pokerkit" は preview backend
    （要 pokerkit, default-off）。
    """
    from core.poker_engine import create_game_state

    backend = cfg.get("engine", {}).get("backend", "legacy")
    return create_game_state(backend, players, sb, bb)


def run_cli() -> None:
    """Phase 1 CLIモード: AudioThread + IntegrationThread を起動してセッションを録音する。"""
    from core.config import load_config
    from core.event_queue import make_audio_queue
    from core.game_state import PlayerState
    from audio.recorder import AudioThread
    from integration.engine import IntegrationThread
    from output.json_writer import JsonWriter

    cfg = load_config()
    session_cfg = _prompt_session_config()

    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in session_cfg["players"]
    ]
    game_state = _make_game_state(cfg, players, session_cfg["sb"], session_cfg["bb"])

    # CLI モードは seat selection UI を持たない（GUI 専用, Phase 2.3）。player_repo は未使用。
    session_id, session_repo, _player_repo, seating = _init_session_layer(cfg, session_cfg)
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

    # Phase 6/7: RFID が有効な場合のみ起動する (transport に応じてスレッドを選択)
    rfid_thread = None
    rfid_cfg = cfg.get("rfid", {})
    if rfid_cfg.get("enabled", False):
        from core.event_queue import make_rfid_queue
        from rfid.card_master import CardMaster
        rfid_q = make_rfid_queue()
        card_master = CardMaster(rfid_cfg.get("card_master_file", "./rfid_cards.json"))
        transport = rfid_cfg.get("transport", "pcsc")
        if transport == "http":
            from rfid.http_receiver import RFIDHTTPReceiver
            rfid_thread = RFIDHTTPReceiver(
                rfid_queue=rfid_q,
                card_master=card_master,
                reader_configs=rfid_cfg.get("readers", {}),
                bind_host=rfid_cfg.get("bind_host", "0.0.0.0"),
                bind_port=rfid_cfg.get("bind_port", 8787),
                stop_event=stop_event,
            )
            print(f"RFID HTTP受信スレッド起動 ({rfid_cfg.get('bind_host','0.0.0.0')}:{rfid_cfg.get('bind_port',8787)})。")
        else:
            from rfid.reader_thread import RFIDThread
            rfid_thread = RFIDThread(
                rfid_queue=rfid_q,
                card_master=card_master,
                reader_configs=rfid_cfg.get("readers", []),
                poll_interval_ms=rfid_cfg.get("poll_interval_ms", 100),
                stop_event=stop_event,
            )
            print("RFID pyscardスレッド起動。")
        rfid_thread.start()

    event_recorder = _make_event_recorder(cfg, session_cfg["log_dir"], session_id)
    integration_thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=game_state,
        json_writer=json_writer,
        rfid_queue=rfid_q if rfid_cfg.get("enabled", False) else None,
        on_action=on_action,
        stop_event=stop_event,
        session_repo=session_repo,
        session_id=session_id if session_repo is not None else None,
        seating=seating,
        event_recorder=event_recorder,
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
        if rfid_thread is not None:
            rfid_thread.join(timeout=3)
        print(f"\nセッション終了。ログ保存先: {json_writer.path}")


def run_gui() -> None:
    """Phase 4 GUIモード: customtkinter ダッシュボードを起動する。"""
    from core.config import load_config
    from core.event_queue import make_audio_queue
    from core.game_state import PlayerState
    from audio.recorder import AudioThread
    from integration.engine import IntegrationThread
    from output.json_writer import JsonWriter
    from gui.dashboard import GUIDashboard

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print("customtkinter が見つかりません。pip install customtkinter でインストールしてください。")
        print("または --cli オプションを使用してください。")
        sys.exit(1)

    cfg = load_config()
    session_cfg = _prompt_session_config()

    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in session_cfg["players"]
    ]
    game_state = _make_game_state(cfg, players, session_cfg["sb"], session_cfg["bb"])

    session_id, session_repo, player_repo, seating = _init_session_layer(cfg, session_cfg)
    json_writer = JsonWriter(log_dir=session_cfg["log_dir"], session_id=session_id)

    audio_q = make_audio_queue()
    stop_event = threading.Event()
    rfid_q = None

    audio_cfg = cfg.get("audio", {})
    rfid_cfg = cfg.get("rfid", {})

    dash = GUIDashboard(
        game_state=game_state,
        json_writer=json_writer,
        audio_queue=audio_q,
        stop_event=stop_event,
        rfid_receiver=None,  # rfid_thread 確定後に設定
        player_repo=player_repo,
        session_layer_enabled=session_repo is not None,
        session_repo=session_repo,
    )

    audio_thread = AudioThread(
        audio_queue=audio_q,
        device_id=audio_cfg.get("device_id", 0),
        sample_rate=audio_cfg.get("sample_rate", 16000),
        model_size=audio_cfg.get("whisper_model", "medium"),
        language=audio_cfg.get("language", "ja"),
        stop_event=stop_event,
    )

    rfid_thread = None
    if rfid_cfg.get("enabled", False):
        from core.event_queue import make_rfid_queue
        from rfid.card_master import CardMaster
        rfid_q = make_rfid_queue()
        card_master = CardMaster(rfid_cfg.get("card_master_file", "./rfid_cards.json"))
        transport = rfid_cfg.get("transport", "pcsc")
        if transport == "http":
            from rfid.http_receiver import RFIDHTTPReceiver
            rfid_thread = RFIDHTTPReceiver(
                rfid_queue=rfid_q,
                card_master=card_master,
                reader_configs=rfid_cfg.get("readers", {}),
                bind_host=rfid_cfg.get("bind_host", "0.0.0.0"),
                bind_port=rfid_cfg.get("bind_port", 8787),
                stop_event=stop_event,
            )
        else:
            from rfid.reader_thread import RFIDThread
            rfid_thread = RFIDThread(
                rfid_queue=rfid_q,
                card_master=card_master,
                reader_configs=rfid_cfg.get("readers", []),
                poll_interval_ms=rfid_cfg.get("poll_interval_ms", 100),
                stop_event=stop_event,
            )

    # HTTP transport の場合、rfid_receiver を GUI に渡してステータス表示する
    if rfid_thread is not None and rfid_cfg.get("transport") == "http":
        dash._rfid_receiver = rfid_thread

    event_recorder = _make_event_recorder(cfg, session_cfg["log_dir"], session_id)
    integration_thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=game_state,
        json_writer=json_writer,
        rfid_queue=rfid_q,
        on_action=dash.on_action,
        on_rfid_card=dash.on_rfid_card,
        stop_event=stop_event,
        session_repo=session_repo,
        session_id=session_id if session_repo is not None else None,
        seating=seating,
        event_recorder=event_recorder,
    )

    dash.start_threads(
        audio_thread=audio_thread,
        integration_thread=integration_thread,
        rfid_thread=rfid_thread,
    )
    dash.run()

    # mainloop 終了後のクリーンアップ
    stop_event.set()
    audio_thread.join(timeout=3)
    integration_thread.join(timeout=3)
    print(f"\nセッション終了。ログ保存先: {json_writer.path}")


def run_player_registry() -> None:
    """Phase S1: hand logger とは別画面の Player Registry を起動する。"""
    from core.player_repository import PlayerRepository
    from gui.player_registry import PlayerRegistryWindow

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print("customtkinter が見つかりません。pip install customtkinter でインストールしてください。")
        sys.exit(1)

    repo = PlayerRepository()
    win = PlayerRegistryWindow(repository=repo)
    win.run()


def run_session_viewer() -> None:
    """WS2-α: hand logger とは別画面の Session / Seating Viewer（read-only）を起動する。"""
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository
    from gui.session_viewer import SessionViewerWindow

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print("customtkinter が見つかりません。pip install customtkinter でインストールしてください。")
        sys.exit(1)

    player_repo = PlayerRepository()
    session_repo = SessionRepository(player_repo=player_repo)
    win = SessionViewerWindow(session_repo=session_repo, player_repo=player_repo)
    win.run()


def export_phh(json_path: str) -> None:
    """JSON セッションログを PHH ファイル群にエクスポートする。"""
    import json
    from core.hand_log import ActionRecord, HandSummary
    from output.phh_exporter import PHHExporter

    src = Path(json_path)
    if not src.exists():
        print(f"ファイルが見つかりません: {json_path}")
        sys.exit(1)

    data = json.loads(src.read_text(encoding="utf-8"))
    hands_raw = data.get("hands", [])
    if not hands_raw:
        print("ハンドデータがありません。")
        sys.exit(0)

    summaries: list[HandSummary] = []
    for h in hands_raw:
        actions = [
            ActionRecord(
                hand_id=a["hand_id"],
                timestamp=a["timestamp"],
                street=a["street"],
                seat=a["seat"],
                player_name=a["player_name"],
                action=a["action"],
                amount=a["amount"],
                pot_after=a["pot_after"],
                stack_after=a["stack_after"],
                source=a.get("source", {}),
                needs_review=a.get("needs_review", False),
                confidence=a.get("confidence", 0.0),
            )
            for a in h.get("actions", [])
        ]
        summary = HandSummary(
            hand_id=h["hand_id"],
            session_id=h["session_id"],
            started_at=h["started_at"],
            ended_at=h["ended_at"],
            blinds=h["blinds"],
            board=h.get("board", []),
            board_source=h.get("board_source", ""),
            players=h.get("players", []),
            pot_total=h["pot_total"],
            winner_seat=h["winner_seat"],
            actions=actions,
            review_required=h.get("review_required", False),
        )
        summaries.append(summary)

    out_dir = src.parent / (src.stem + "_phh")
    exporter = PHHExporter()
    paths = exporter.write_session(summaries, out_dir)
    print(f"{len(paths)} 件のハンドを {out_dir} に出力しました。")
    for p in paths:
        print(f"  {p}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ポーカーハンドロガー")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="CLIモードで起動（音声認識 + RFID、GUI なし）",
    )
    parser.add_argument(
        "--export-phh",
        metavar="SESSION_JSON",
        help="JSON セッションログを PHH ファイル群に変換する（Phase 5）",
    )
    parser.add_argument(
        "--players",
        action="store_true",
        help="Player Registry 画面を起動する（Phase S1, hand logger とは別画面）",
    )
    parser.add_argument(
        "--sessions-viewer",
        action="store_true",
        help="Session / Seating Viewer（read-only）を起動する（WS2-α, hand logger とは別画面）",
    )
    args = parser.parse_args()

    if args.players:
        run_player_registry()
        sys.exit(0)

    if args.sessions_viewer:
        run_session_viewer()
        sys.exit(0)

    if args.export_phh:
        export_phh(args.export_phh)
        sys.exit(0)

    if args.cli:
        run_cli()
    else:
        run_gui()


if __name__ == "__main__":
    main()
