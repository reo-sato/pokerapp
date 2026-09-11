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


def _make_event_recorder(cfg: dict, log_dir: str, session_id: str):
    """config.recording.enabled が true なら EventRecorder を返す (R1, ADR-0010)。

    デフォルト false = 記録しない (挙動不変)。生イベントを
    logs/{session_id}.events.jsonl に append-only で記録する sidecar。
    """
    if not cfg.get("recording", {}).get("enabled", False):
        return None
    from output.event_recorder import EventRecorder

    return EventRecorder(Path(log_dir) / f"{session_id}.events.jsonl")


def _make_audio_thread(cfg: dict, audio_queue, stop_event):
    """config.audio.enabled が true（既定）なら AudioThread を返す。false なら None。

    false はマイクを繋がない実機テスト（RFID のカード読み取りだけを見る / ダミーアクションを
    キーボードで投入する）用。アクションは CLI の入力ループが読み上げ文を `parse_action` に
    通して AudioEvent にするので、音声と同じ語彙・同じ経路で進行できる。
    """
    audio_cfg = cfg.get("audio", {})
    if not audio_cfg.get("enabled", True):
        return None
    from audio.recorder import AudioThread

    return AudioThread(
        audio_queue=audio_queue,
        device_id=audio_cfg.get("device_id", 0),
        sample_rate=audio_cfg.get("sample_rate", 16000),
        model_size=audio_cfg.get("whisper_model", "medium"),
        language=audio_cfg.get("language", "ja"),
        stop_event=stop_event,
    )


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
    from integration.engine import IntegrationThread
    from output.json_writer import JsonWriter

    cfg = load_config()
    session_cfg = _prompt_session_config()

    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in session_cfg["players"]
    ]
    game_state = _make_game_state(cfg, players, session_cfg["sb"], session_cfg["bb"])

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

    audio_thread = _make_audio_thread(cfg, audio_q, stop_event)
    if audio_thread is None:
        print("音声入力は無効です (audio.enabled=false)。"
              "アクションはキーボードから読み上げ文で投入してください。")
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
            # canonical PC/SC は pcsc_readers (list) を使う。readers (dict) は HTTP 用なので
            # list でない場合は空にフォールバックする（ADR-0034 / rfid-usb-ccid.md §4）。
            pcsc_readers = rfid_cfg.get("pcsc_readers", rfid_cfg.get("readers", []))
            if not isinstance(pcsc_readers, list):
                pcsc_readers = []
            rfid_thread = RFIDThread(
                rfid_queue=rfid_q,
                card_master=card_master,
                reader_configs=pcsc_readers,
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
    )
    if audio_thread is not None:
        audio_thread.start()
    integration_thread.start()

    print(f"\nセッション開始。ログ: {json_writer.path}")
    print("コマンド: [q]=終了  [n]=新ハンド  [w <席>]=ウィナー  [r <席> <金額>]=リバイ")
    print("上記以外の入力は読み上げ文として解釈します"
          "（例: チェック / シート3 コール / ベット 500）。マイクが無くてもこれで進行できます。")
    print("ディーラーがアナウンスすると自動検出されます。\n")

    # GameStateManager はロックを持たないため、状態変更コマンド (n/w/r) はすべて
    # audio_q 経由で IntegrationThread に処理させる（直接呼ぶと apply_action とレースする）。
    from audio.recognizer import parse_action
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
                # 上のコマンド以外は **ディーラーのアナウンスとして解釈**する（マイク無しで
                # アクションを投入する経路。音声と同じ `parse_action` を通すので語彙は共通 =
                # 二重管理にならない）。例: 「チェック」「シート3 コール」「ベット 500」。
                ev = parse_action(line)
                if ev is None:
                    print("認識できません。コマンド: q / n / w <席> / r <席> <金額>、"
                          "または読み上げ文（例: チェック / シート3 コール / ベット 500）")
                else:
                    audio_q.put(ev)
                    print(f"  → {ev.action}"
                          + (f" {ev.amount}" if ev.amount else "")
                          + (f" (席{ev.seat})" if ev.seat else ""))

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop_event.set()
        if audio_thread is not None:
            audio_thread.join(timeout=3)
        integration_thread.join(timeout=3)
        if camera_thread is not None:
            camera_thread.join(timeout=3)
        if rfid_thread is not None:
            rfid_thread.join(timeout=3)
        print(f"\nセッション終了。ログ保存先: {json_writer.path}")


def run_gui() -> None:
    """Phase 4 GUIモード: customtkinter ダッシュボードを起動する。"""
    from core.config import load_config
    from core.event_queue import make_audio_queue
    from core.game_state import PlayerState
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

    # E3 (ADR-0008 / ISSUE-0006): session レイヤ接続。既定 off では従来どおり timestamp session_id。
    session_layer_enabled = cfg.get("session_layer", {}).get("enabled", False)
    player_repo = None
    session_repo = None
    if session_layer_enabled:
        from core.player_repository import PlayerRepository
        from core.session_repository import SessionRepository
        player_repo = PlayerRepository()
        session_repo = SessionRepository(player_repo=player_repo)
        session = session_repo.create_session(
            label=datetime.now().strftime("%Y-%m-%d_%H%M%S")
        )
        session_id = session.session_id  # session レイヤ採番の UUID4 hex
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
        session_layer_enabled=session_layer_enabled,
    )

    audio_thread = _make_audio_thread(cfg, audio_q, stop_event)
    if audio_thread is None:
        print("音声入力は無効です (audio.enabled=false)。"
              "アクションはキーボードから読み上げ文で投入してください。")

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
            # CLI 経路と同じく canonical PC/SC は pcsc_readers (list) を使う。readers (dict) は
            # HTTP 用なので list でない場合は空にフォールバックする（ADR-0034 / 契約 §4）。
            pcsc_readers = rfid_cfg.get("pcsc_readers", rfid_cfg.get("readers", []))
            if not isinstance(pcsc_readers, list):
                pcsc_readers = []
            rfid_thread = RFIDThread(
                rfid_queue=rfid_q,
                card_master=card_master,
                reader_configs=pcsc_readers,
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
    )

    dash.start_threads(
        audio_thread=audio_thread,
        integration_thread=integration_thread,
        camera_thread=camera_thread,
        rfid_thread=rfid_thread,
    )

    # WS4 §C (ADR-0039): staff iPad からの hand logger 遠隔制御。既定 off で挙動不変。
    # session_layer 有効時のみ（session_id が sessions.json にあり staff が卓を選べる）。
    control_thread = None
    if cfg.get("hand_control", {}).get("enabled", False) and session_layer_enabled:
        from core.control_queue import ControlCommandLog
        from integration.control_consumer import ControlConsumerThread
        control_log = ControlCommandLog(
            Path(session_cfg["log_dir"]) / f"{session_id}.control.jsonl"
        )
        control_thread = ControlConsumerThread(
            control_log=control_log,
            audio_queue=audio_q,
            stop_event=stop_event,
            poll_interval_ms=cfg.get("hand_control", {}).get("poll_interval_ms", 200),
        )
        control_thread.start()
        print(f"hand 遠隔制御を有効化しました: {control_log.path}（staff iPad から操作可）")

    dash.run()

    # mainloop 終了後のクリーンアップ
    stop_event.set()
    if audio_thread is not None:
        audio_thread.join(timeout=3)
    integration_thread.join(timeout=3)
    if camera_thread is not None:
        camera_thread.join(timeout=3)
    if control_thread is not None:
        control_thread.join(timeout=3)
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
    """WS2-α: hand logger とは別画面の read-only Session / Seating Viewer を起動する。"""
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository
    from gui.session_viewer import SessionViewerWindow

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print("customtkinter が見つかりません。pip install customtkinter でインストールしてください。")
        sys.exit(1)

    # name 解決を一貫させるため、同じ PlayerRepository インスタンスを共有する。
    player_repo = PlayerRepository()
    session_repo = SessionRepository(player_repo=player_repo)
    win = SessionViewerWindow(session_repo=session_repo, player_repo=player_repo)
    win.run()


def run_ledger_view() -> None:
    """Phase S3.2: hand logger とは別画面の Ledger Viewer / Editor を起動する。

    `config.viewer_api.enabled = true` なら viewer API を **in-process** で抱えて起動し、
    player スマホからの注文リクエスト受付（write）が有効になる（単一プロセス所有,
    ADR-0018 §3。単独 `--viewer-api` は read-only のまま）。
    """
    from core.config import load_config
    from core.ledger_repository import LedgerRepository
    from core.menu import MenuMaster
    from core.order_request_repository import OrderRequestRepository
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository
    from gui.ledger_view import LedgerViewWindow

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print("customtkinter が見つかりません。pip install customtkinter でインストールしてください。")
        sys.exit(1)

    cfg = load_config()
    players = PlayerRepository()
    sessions = SessionRepository(player_repo=players)
    ledger = LedgerRepository(session_repo=sessions, player_repo=players)
    order_repo = OrderRequestRepository(session_repo=sessions, player_repo=players)
    menu = MenuMaster()
    buyin_presets = cfg.get("ledger", {}).get("buyin_presets", []) or []  # ADR-0026

    api_server = None
    api_thread = None
    api_cfg = cfg.get("viewer_api", {})
    if api_cfg.get("enabled", False):
        try:
            import uvicorn
            from api.server import auth_kwargs_from_config, create_app
        except ImportError:
            print("viewer_api.enabled=true ですが fastapi/uvicorn が未導入のため "
                  "API なしで起動します（pip install \".[api]\"）。")
        else:
            bind_host = api_cfg.get("bind_host", "127.0.0.1")
            bind_port = api_cfg.get("bind_port", 8788)
            app = create_app(
                players, sessions,
                cfg.get("session", {}).get("log_dir", "./logs"),
                ledger_repo=ledger, order_repo=order_repo, menu=menu,
                orders_writable=True,
                staff_token=api_cfg.get("staff_token") or None,
                buyin_presets=buyin_presets,
                **auth_kwargs_from_config(api_cfg),  # L1 PIN, ADR-0027
            )
            api_server = uvicorn.Server(uvicorn.Config(
                app, host=bind_host, port=bind_port, log_level="warning"))
            api_thread = threading.Thread(
                target=api_server.run, daemon=True, name="ViewerAPIThread")
            api_thread.start()
            print(f"viewer API を組み込み起動しました: http://{bind_host}:{bind_port} "
                  "（注文リクエスト受付 有効）")

    win = LedgerViewWindow(
        ledger_repo=ledger, session_repo=sessions, player_repo=players,
        order_repo=order_repo, menu=menu, buyin_presets=buyin_presets,
    )
    win.run()

    if api_server is not None:
        api_server.should_exit = True
        api_thread.join(timeout=3)


def run_viewer_api() -> None:
    """Phase M1: player 向け読み取り専用 viewer API を起動する (ADR-0017)。"""
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


def export_ledger(out_dir: str) -> None:
    """Phase S3.3: 確定済 settlement と cashflow（ledger entry）を CSV にエクスポートする。"""
    from core.ledger_repository import LedgerRepository
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository
    from output.ledger_csv_exporter import LedgerCsvExporter

    players = PlayerRepository()
    sessions = SessionRepository(player_repo=players)
    ledger = LedgerRepository(session_repo=sessions, player_repo=players)
    names = {p.player_id: p.display_name for p in players.list_players()}

    exporter = LedgerCsvExporter()
    out = Path(out_dir)
    settlements = ledger.all_settlements()
    entries = ledger.list_entries()
    s_path = exporter.export_settlements(settlements, out / "settlements.csv", player_names=names)
    c_path = exporter.export_entries(entries, out / "ledger_cashflow.csv", player_names=names)
    print(f"settlement {len(settlements)} 件を出力: {s_path}")
    print(f"cashflow {len(entries)} 件を出力: {c_path}")


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
        "--sessions",
        action="store_true",
        help="Session / Seating Viewer を起動する（WS2-α, read-only, hand logger とは別画面）",
    )
    parser.add_argument(
        "--ledger",
        action="store_true",
        help="Ledger Viewer / Editor 画面を起動する（Phase S3.2, hand logger とは別画面）",
    )
    parser.add_argument(
        "--export-ledger",
        metavar="OUT_DIR",
        nargs="?",
        const="logs/ledger_export",
        help="settlement / cashflow を CSV にエクスポートする（Phase S3.3, 既定 logs/ledger_export）",
    )
    parser.add_argument(
        "--viewer-api",
        action="store_true",
        help="player 向け読み取り専用 viewer API を起動する（Phase M1, ADR-0017, 要 [api] extra）",
    )
    args = parser.parse_args()

    # 起動時バックアップ（B2 / v1.0 ローンチレビュー）。会計データ消失の最大リスク対策。
    # best-effort: 失敗してもアプリ起動は止めない。config.backup.on_startup=false で無効化可。
    try:
        from core.backup import backup_data_files
        from core.config import load_config
        _bcfg = load_config().get("backup", {})
        if _bcfg.get("on_startup", True):
            backup_data_files(dest_dir=_bcfg.get("dir", "./backups"),
                              keep=int(_bcfg.get("keep", 30)))
    except Exception:
        logger.warning("起動時バックアップに失敗しました（続行します）", exc_info=True)

    if args.players:
        run_player_registry()
        sys.exit(0)

    if args.ledger:
        run_ledger_view()
        sys.exit(0)

    if args.export_ledger:
        export_ledger(args.export_ledger)
        sys.exit(0)

    if args.viewer_api:
        run_viewer_api()
        sys.exit(0)

    if args.sessions:
        run_session_viewer()
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
