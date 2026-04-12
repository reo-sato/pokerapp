"""main.py — ポーカーハンドロガー（spec.md Phase 4）
  python main.py       → GUI (DashboardWindow)
  python main.py --cli → CLI デバッグモード
"""
from __future__ import annotations
import argparse, logging, queue, signal, threading
from datetime import datetime
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _session_dialog(cfg: dict) -> dict:
    """CLIでセッション設定（席・スタック・ブラインド・ディーラー）を入力する。"""
    s = cfg.get("session", {}); bl = s.get("blinds", {"sb": 100, "bb": 200})
    print("=== ポーカーハンドロガー セッション設定 ===")
    def _i(p, d):
        try: return int(input(p).strip() or d)
        except (ValueError, EOFError): return d
    def _nm(i):
        try: return input(f"席{i}名前[省略=Player{i}]: ").strip() or f"Player{i}"
        except EOFError: return f"Player{i}"
    n = max(2, min(9, _i(f"席数[2〜9, 省略={s.get('num_seats', 6)}]: ", s.get("num_seats", 6))))
    ps = [{"seat": i, "name": _nm(i), "stack": _i(f"席{i}スタック: ", 10000)} for i in range(1, n+1)]
    btn = s.get("button_seat_initial", 1)
    return {"players": ps, "sb": _i(f"SB[省略={bl['sb']}]: ", bl["sb"]),
            "bb": _i(f"BB[省略={bl['bb']}]: ", bl["bb"]),
            "button_seat": _i(f"ディーラー席[省略={btn}]: ", btn),
            "log_dir": s.get("log_dir", "./logs")}


def _start_rfid_audio(cfg: dict, stop: threading.Event):
    """① RFIDThread → ② AudioThread の順で起動。(rfid_t, audio_t, audio_q, rfid_q, cm) を返す。"""
    from core.event_queue import make_audio_queue; from audio.recorder import AudioThread
    from rfid.card_master import CardMaster
    rc = cfg.get("rfid", {}); ac = cfg.get("audio", {})
    cm = CardMaster(rc.get("card_master_file", "./rfid_cards.json"))
    rfid_q = rfid_t = None
    if rc.get("enabled", False):
        from core.event_queue import make_rfid_queue; rfid_q = make_rfid_queue()
        kw = dict(rfid_queue=rfid_q, card_master=cm, stop_event=stop)
        if rc.get("transport", "pcsc") == "http":
            from rfid.http_receiver import RFIDHTTPReceiver
            rfid_t = RFIDHTTPReceiver(**kw, reader_configs=rc.get("readers", {}),
                bind_host=rc.get("bind_host", "0.0.0.0"), bind_port=rc.get("bind_port", 8787))
        else:
            from rfid.reader_thread import RFIDThread
            rfid_t = RFIDThread(**kw, reader_configs=rc.get("readers", []),
                poll_interval_ms=rc.get("poll_interval_ms", 100))
        rfid_t.start()
    audio_q = make_audio_queue()
    audio_t = AudioThread(audio_queue=audio_q, device_id=ac.get("device_id", 0),
        sample_rate=ac.get("sample_rate", 16000), model_size=ac.get("whisper_model", "medium"),
        language=ac.get("language", "ja"), stop_event=stop)
    audio_t.start()
    return rfid_t, audio_t, audio_q, rfid_q, cm


def _join_all(threads, stop: threading.Event) -> None:
    """stop_event を set してから全スレッドを join(timeout=3) で待つ。タイムアウト時は警告。"""
    stop.set()
    for t in threads:
        if t is None: continue
        t.join(timeout=3)
        if t.is_alive(): logger.warning("スレッド %s がタイムアウトしました", t.name)


def _run_cli(gs, audio_q, stop: threading.Event) -> None:
    """CLIループ: q=終了  n=新ハンド  w <席>=ウィナー"""
    from core.events import AudioEvent; import time
    print("\nコマンド: q=終了  n=新ハンド  w <席>=ウィナー\n")
    try:
        while not stop.is_set():
            try: parts = input("> ").split()
            except (EOFError, KeyboardInterrupt): break
            if not parts: continue
            if parts[0] == "q": break
            elif parts[0] == "n": gs.new_hand(); print(f"新ハンド: #{gs.hand_id}")
            elif parts[0] == "w" and len(parts) >= 2:
                audio_q.put(AudioEvent(action="winner", amount=0, timestamp=time.time(),
                    raw_text=f"シート{parts[1]} ウィナー"))
    except KeyboardInterrupt: pass


def main() -> None:
    ap = argparse.ArgumentParser(description="ポーカーハンドロガー")
    ap.add_argument("--cli", action="store_true", help="CLIモードで起動")
    args = ap.parse_args()
    from core.config import load_config; from core.game_state import GameState, PlayerState
    from integration.engine import IntegrationThread; from output.json_writer import JsonWriter
    cfg = load_config(); sess = _session_dialog(cfg)
    gs = GameState(
        players=[PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"]) for p in sess["players"]],
        sb=sess["sb"], bb=sess["bb"], button_seat=sess["button_seat"])
    gs.new_hand()
    writer = JsonWriter(log_dir=sess["log_dir"],
        session_id=datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_session")
    stop = threading.Event()
    def _sig(s, f): stop.set()
    signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)
    rfid_t, audio_t, audio_q, rfid_q, cm = _start_rfid_audio(cfg, stop)
    if args.cli:
        on_act = lambda r: print(f"  [{r.street}] 席{r.seat} {r.action} {r.amount}"
                                 + (" [要確認]" if r.needs_review else ""))
        it = IntegrationThread(audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, on_action=on_act, stop_event=stop)
        it.start(); _run_cli(gs, audio_q, stop)
    else:
        from gui.dashboard import DashboardWindow
        win = DashboardWindow(update_queue=queue.Queue(), audio_queue=audio_q,
            game_state=gs, json_writer=writer, stop_event=stop, card_master=cm)
        it = IntegrationThread(audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, on_action=win.on_action, on_rfid_card=win.on_rfid_card,
            stop_event=stop)
        it.start(); win.run()  # mainloop — ウィンドウを閉じるまでブロック
    _join_all([rfid_t, audio_t, it], stop)
    logger.info("セッション終了。ログ: %s", writer.path)


if __name__ == "__main__":
    main()
