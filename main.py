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


def _prompt_seat_player(seat: int, player_repo, assigned: dict[int, str]):
    """席 1 つ分の registry player 選択 (E3, ISSUE-0006)。Player か None を返す。

    入力: 番号 = registry から選択 / "n" = その場で新規登録 / 空 Enter = 割当なし。
    同一 player の重複割当はここで弾く（SessionRepository の player_already_seated と同じ規則）。
    """
    from core.player_repository import (
        DuplicateDisplayNameError,
        EmptyDisplayNameError,
    )

    while True:
        raw = input(f"席{seat} player (番号 / n=新規登録 / 空Enterで割当なし): ").strip()
        if not raw:
            return None
        if raw.lower() == "n":
            new_name = input("  新規 player の表示名: ").strip()
            try:
                player = player_repo.create_player(new_name)
            except (EmptyDisplayNameError, DuplicateDisplayNameError) as e:
                print(f"  登録できません: {e}")
                continue
            print(f"  登録しました: {player.display_name}")
            return player
        try:
            idx = int(raw)
            player = player_repo.list_players()[idx - 1]
        except (ValueError, IndexError):
            print("  一覧の番号 / n / 空Enter のいずれかを入力してください。")
            continue
        if player.player_id in assigned.values():
            print(f"  {player.display_name} は既に別の席に割り当て済みです。")
            continue
        return player


def _prompt_session_config(player_repo=None) -> dict:
    """CLIで席数・プレイヤー名・スタック・ブラインドを入力する。

    player_repo を渡すと (config.session_layer.enabled, E3) 各席の名前入力の代わりに
    registry の player 選択を行い、戻り値に seat_no → player_id の "seat_players" を含める。
    """
    print("=== ポーカーハンドロガー セッション設定 ===")

    while True:
        try:
            num_seats = int(input("席数 (2〜9): ").strip())
            if 2 <= num_seats <= 9:
                break
        except ValueError:
            pass
        print("2〜9 の整数を入力してください。")

    seat_players: dict[int, str] = {}
    if player_repo is not None:
        registered = player_repo.list_players()
        print("--- player registry ---")
        if registered:
            for i, p in enumerate(registered, start=1):
                print(f"  {i}: {p.display_name}")
        else:
            print("  (未登録。各席で n を入力すると新規登録できます)")

    players = []
    for i in range(1, num_seats + 1):
        if player_repo is not None:
            player = _prompt_seat_player(i, player_repo, seat_players)
            if player is not None:
                seat_players[i] = player.player_id
                name = player.display_name
            else:
                name = input(f"席{i} プレイヤー名: ").strip() or f"Player{i}"
        else:
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

    return {
        "players": players, "sb": sb, "bb": bb, "log_dir": log_dir,
        "seat_players": seat_players,
    }


def _make_session_layer(cfg: dict):
    """config.session_layer.enabled が true なら (player_repo, session_repo) を返す (E3, ADR-0008)。

    既定 false = (None, None) で従来動作（timestamp session_id・player_id なし, rollback path）。
    """
    if not cfg.get("session_layer", {}).get("enabled", False):
        return None, None
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

    player_repo = PlayerRepository()
    return player_repo, SessionRepository(player_repo=player_repo)


def _create_layer_session(session_repo, sb: int, bb: int) -> str:
    """session レイヤに session を作成し UUID session_id を返す（ADR-0008 §2: canonical 採番）。"""
    label = input("セッションラベル (空Enterでなし): ").strip() or None
    session = session_repo.create_session(label=label, blinds={"sb": sb, "bb": bb})
    print(f"セッション作成: {session.session_id}" + (f" ({label})" if label else ""))
    return session.session_id


def _maybe_close_session(session_repo, session_id: str) -> None:
    """セッション終了時に close するか確認する（close すると viewer の status に反映）。"""
    if session_repo is None:
        return
    from core.session_repository import SessionError

    try:
        ans = input("セッションを終了 (close) しますか? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if ans == "y":
        try:
            session_repo.close_session(session_id)
            print("セッションを close しました。")
        except SessionError as e:
            print(f"close できませんでした: {e}")


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
    player_repo, session_repo = _make_session_layer(cfg)
    session_cfg = _prompt_session_config(player_repo=player_repo)

    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in session_cfg["players"]
    ]
    game_state = _make_game_state(cfg, players, session_cfg["sb"], session_cfg["bb"])

    # E3 (ADR-0008 §2): session レイヤ有効時は UUID session_id を canonical にする
    if session_repo is not None:
        session_id = _create_layer_session(session_repo, session_cfg["sb"], session_cfg["bb"])
    else:
        session_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_session1"
    json_writer = JsonWriter(log_dir=session_cfg["log_dir"], session_id=session_id)

    audio_q = make_audio_queue()
    stop_event = threading.Event()
    camera_q = None

    def on_action(record):
        print(
            f"  [{record.street}] 席{record.seat}({record.player_name}) "
            f"{record.action} {record.amount or ''}"
            f"  pot={record.pot_after}"
            + (" [要確認]" if record.needs_review else "")
        )

    audio_cfg = cfg.get("audio", {})
    cam_cfg = cfg.get("camera", {})

    audio_thread = AudioThread(
        audio_queue=audio_q,
        device_id=audio_cfg.get("device_id", 0),
        sample_rate=audio_cfg.get("sample_rate", 16000),
        model_size=audio_cfg.get("whisper_model", "medium"),
        language=audio_cfg.get("language", "ja"),
        stop_event=stop_event,
    )
    # Phase 2/3: カメラが設定済みの場合のみ CameraThread を起動する
    # Phase 3: camera_q を IntegrationThread に渡すことで ±2秒マッチングが有効になる
    camera_thread = None
    if cam_cfg.get("roi"):
        from core.event_queue import make_camera_queue
        from vision.camera import CameraThread
        camera_q = make_camera_queue()
        camera_thread = CameraThread(
            camera_queue=camera_q,
            device_id=cam_cfg.get("device_id", 0),
            roi_config=cam_cfg.get("roi", {}),
            fps=cam_cfg.get("fps", 20),
            motion_threshold=cam_cfg.get("motion_threshold", 2000),
            stop_event=stop_event,
        )
        camera_thread.start()
        print("カメラスレッド起動（動体検出 + ±2秒マッチング有効）。")

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
                bind_host=rfid_cfg.get("bind_host", "127.0.0.1"),
                bind_port=rfid_cfg.get("bind_port", 8787),
                stop_event=stop_event,
            )
            print(f"RFID HTTP受信スレッド起動 ({rfid_cfg.get('bind_host','127.0.0.1')}:{rfid_cfg.get('bind_port',8787)})。")
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
        camera_queue=camera_q,
        rfid_queue=rfid_q if rfid_cfg.get("enabled", False) else None,
        on_action=on_action,
        stop_event=stop_event,
        event_recorder=event_recorder,
        session_repo=session_repo,
        seat_player_map=session_cfg.get("seat_players"),
    )
    audio_thread.start()
    integration_thread.start()

    print(f"\nセッション開始。ログ: {json_writer.path}")
    print("コマンド: [q]=終了  [n]=新ハンド  [w <席>]=ウィナー  [r <席> <金額>]=リバイ")
    print("ディーラーがアナウンスすると自動検出されます。\n")

    # GameStateManager はロックを持たないため、状態変更コマンド (n/w/r) はすべて
    # audio_q 経由で IntegrationThread に処理させる（直接呼ぶと apply_action とレースする）。
    from core.events import AudioEvent
    import time as _time

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
                audio_q.put(AudioEvent(
                    action="new_hand", amount=0, timestamp=_time.time(), raw_text="",
                ))
                print("新ハンド開始を送信しました。")
            elif cmd == "w" and len(parts) >= 2:
                try:
                    seat = int(parts[1])
                    audio_q.put(AudioEvent(
                        action="winner",
                        amount=0,
                        timestamp=_time.time(),
                        raw_text=f"シート{seat} ウィナー",
                    ))
                except ValueError:
                    print("使い方: w <席番号>")
            elif cmd == "r" and len(parts) >= 3:
                try:
                    seat = int(parts[1])
                    amount = int(parts[2])
                    audio_q.put(AudioEvent(
                        action="rebuy", amount=amount, timestamp=_time.time(),
                        raw_text=f"シート{seat} リバイ {amount}", seat=seat,
                    ))
                    print(f"リバイを送信しました: 席{seat} +{amount}（反映はアクション表示で確認）")
                except ValueError as e:
                    print(f"エラー: {e}")
            else:
                print("不明なコマンドです。q / n / w <席> / r <席> <金額>")

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop_event.set()
        audio_thread.join(timeout=3)
        integration_thread.join(timeout=3)
        if camera_thread is not None:
            camera_thread.join(timeout=3)
        if rfid_thread is not None:
            rfid_thread.join(timeout=3)
        print(f"\nセッション終了。ログ保存先: {json_writer.path}")
        _maybe_close_session(session_repo, session_id)


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
    player_repo, session_repo = _make_session_layer(cfg)
    session_cfg = _prompt_session_config(player_repo=player_repo)

    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in session_cfg["players"]
    ]
    game_state = _make_game_state(cfg, players, session_cfg["sb"], session_cfg["bb"])

    # E3 (ADR-0008 §2): session レイヤ有効時は UUID session_id を canonical にする
    if session_repo is not None:
        session_id = _create_layer_session(session_repo, session_cfg["sb"], session_cfg["bb"])
    else:
        session_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_session1"
    json_writer = JsonWriter(log_dir=session_cfg["log_dir"], session_id=session_id)

    audio_q = make_audio_queue()
    stop_event = threading.Event()
    camera_q = None
    rfid_q = None

    audio_cfg = cfg.get("audio", {})
    cam_cfg = cfg.get("camera", {})
    rfid_cfg = cfg.get("rfid", {})

    dash = GUIDashboard(
        game_state=game_state,
        json_writer=json_writer,
        audio_queue=audio_q,
        camera_queue=camera_q,
        stop_event=stop_event,
        rfid_receiver=None,  # rfid_thread 確定後に設定
        player_repo=player_repo,
        seat_player_map=session_cfg.get("seat_players"),
    )

    audio_thread = AudioThread(
        audio_queue=audio_q,
        device_id=audio_cfg.get("device_id", 0),
        sample_rate=audio_cfg.get("sample_rate", 16000),
        model_size=audio_cfg.get("whisper_model", "medium"),
        language=audio_cfg.get("language", "ja"),
        stop_event=stop_event,
    )

    camera_thread = None
    if cam_cfg.get("roi"):
        from core.event_queue import make_camera_queue
        from vision.camera import CameraThread
        camera_q = make_camera_queue()
        dash._camera_queue = camera_q
        camera_thread = CameraThread(
            camera_queue=camera_q,
            device_id=cam_cfg.get("device_id", 0),
            roi_config=cam_cfg.get("roi", {}),
            fps=cam_cfg.get("fps", 20),
            motion_threshold=cam_cfg.get("motion_threshold", 2000),
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
                bind_host=rfid_cfg.get("bind_host", "127.0.0.1"),
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
        camera_queue=camera_q,
        rfid_queue=rfid_q,
        on_action=dash.on_action,
        on_rfid_card=dash.on_rfid_card,
        stop_event=stop_event,
        event_recorder=event_recorder,
        session_repo=session_repo,
        seat_player_map=session_cfg.get("seat_players"),
    )

    dash.start_threads(
        audio_thread=audio_thread,
        integration_thread=integration_thread,
        camera_thread=camera_thread,
        rfid_thread=rfid_thread,
    )
    dash.run()

    # mainloop 終了後のクリーンアップ
    stop_event.set()
    audio_thread.join(timeout=3)
    integration_thread.join(timeout=3)
    if camera_thread is not None:
        camera_thread.join(timeout=3)
    print(f"\nセッション終了。ログ保存先: {json_writer.path}")
    _maybe_close_session(session_repo, session_id)


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


def run_ledger() -> None:
    """Phase S3a (M4, ADR-0014): スタッフ用 会計入力画面を起動する（hand logger とは別画面）。"""
    from core.ledger_repository import LedgerRepository
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository
    from gui.ledger_entry import LedgerEntryWindow

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print("customtkinter が見つかりません。pip install customtkinter でインストールしてください。")
        sys.exit(1)

    player_repo = PlayerRepository()
    session_repo = SessionRepository(player_repo=player_repo)
    ledger_repo = LedgerRepository(session_repo=session_repo)
    win = LedgerEntryWindow(ledger_repo, session_repo, player_repo)
    win.run()


def run_viewer_api() -> None:
    """Phase M1: player 向け読み取り専用 viewer API を起動する (ADR-0013)。"""
    from core.config import load_config

    try:
        from api.server import run_server
    except ImportError:
        print("fastapi / uvicorn が見つかりません。pip install \".[api]\" でインストールしてください。")
        sys.exit(1)

    cfg = load_config()
    api_cfg = cfg.get("viewer_api", {})
    print(
        f"Viewer API を起動します: http://{api_cfg.get('bind_host', '127.0.0.1')}:"
        f"{api_cfg.get('bind_port', 8788)}/api/health (Ctrl+C で終了)"
    )
    run_server(cfg)


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
        help="CLIモードで起動（音声認識のみ、カメラなし）",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="ROIキャリブレーションモードで起動（Phase 2）",
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
        "--viewer-api",
        action="store_true",
        help="player 向け読み取り専用 viewer API を起動する（Phase M1, 要 pip install '.[api]'）",
    )
    parser.add_argument(
        "--ledger",
        action="store_true",
        help="スタッフ用 会計入力画面を起動する（Phase M4/S3a, hand logger とは別画面）",
    )
    args = parser.parse_args()

    if args.players:
        run_player_registry()
        sys.exit(0)

    if args.ledger:
        run_ledger()
        sys.exit(0)

    if args.viewer_api:
        run_viewer_api()
        sys.exit(0)

    if args.calibrate:
        from core.config import load_config
        from vision.calibration import run_calibration
        cfg = load_config()
        device_id = cfg.get("camera", {}).get("device_id", 0)
        run_calibration(device_id=device_id)
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
