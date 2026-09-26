from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 全角 ASCII（U+FF01..U+FF5E）→ 半角 + 全角スペース → 半角スペース。日本語 IME のまま
# `ｎ` と打ってもコマンドとして通るようにする（ISSUE-0030）。読み上げ文の経路は通さない
# （`raw_text` は入力そのまま。全角数字は `parse_amount` が既に解釈する）。
_FULLWIDTH_TO_ASCII = str.maketrans(
    {chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)} | {"　": " "}
)


# 引数がくっついたコマンド（`w1` / `r1 500`）を分ける。ログが入力行に割り込む環境では
# 空白を打ち損ねやすく、実機で `w1` が 2 回弾かれた（ISSUE-0034）。引数を取る w/r のみ。
_GLUED_COMMAND = re.compile(r"^([wr])(\d+)$")


def _normalize_cli_command(line: str) -> str:
    """CLI コマンド照合用に全角英数字・全角スペースを半角へ寄せ、`w1` を `w 1` に分ける。"""
    normalized = line.translate(_FULLWIDTH_TO_ASCII)
    parts = normalized.split()
    if parts:
        m = _GLUED_COMMAND.match(parts[0].lower())
        if m:
            parts[0:1] = [m.group(1), m.group(2)]
            return " ".join(parts)
    return normalized


def _parse_seat_list(raw: str) -> "list[int] | None":
    """使う席の入力を席番号の昇順リストにする。不正なら None。

    - 数字 1 つ（2〜9）= 人数。1 番から順に使う（従来どおり。例 `6` → 1〜6 番）。
    - 数字 2 つ以上 = 使う席番号（1〜9、重複なし。例 `2 4 5 8`）。卓の空いている席を飛ばして座るとき。
      全角の数字・空白、「、」「,」区切りも可。
    """
    text = raw.translate(_FULLWIDTH_TO_ASCII).replace("、", " ").replace(",", " ")
    try:
        numbers = [int(tok) for tok in text.split()]
    except ValueError:
        return None
    if len(numbers) == 1:
        return list(range(1, numbers[0] + 1)) if 2 <= numbers[0] <= 9 else None
    if len(numbers) < 2 or len(set(numbers)) != len(numbers) or not all(1 <= n <= 9 for n in numbers):
        return None
    return sorted(numbers)


def _prompt_session_config() -> dict:
    """CLIで使う席・プレイヤー名・スタック・ブラインドを入力する。"""
    print("=== ポーカーハンドロガー セッション設定 ===")

    while True:
        seats = _parse_seat_list(input("席（人数か席番号。例: 6 = 1〜6 番 / 2 4 5 8 = その番号の席）: "))
        if seats is not None:
            break
        print("人数（2〜9）か、使う席番号を 2 つ以上（1〜9、空白区切り）入力してください。")

    players = []
    for seat in seats:
        typed = input(f"席{seat} プレイヤー名: ").strip()
        name = typed or f"Player{seat}"
        while True:
            try:
                stack = int(input(f"席{seat} 初期スタック: ").strip())
                if stack > 0:
                    break
            except ValueError:
                pass
            print("正の整数を入力してください。")
        # named = 名前を入力した席。空 Enter の席（PlayerN）はお客さんに結び付けない（ADR-0059）。
        players.append({"seat": seat, "name": name, "stack": stack, "named": bool(typed)})

    while True:
        try:
            sb = int(input("SB金額: ").strip())
            bb = int(input("BB金額: ").strip())
            if sb > 0 and bb > 0:
                break
        except ValueError:
            pass
        print("正の整数を入力してください。")

    # 1 ハンド目のボタン席（FR-05b / ISSUE-0032）。以降はハンドごとに 1 つずつ回る。
    # 内部表現は「次の new_hand で 1 つ進める前の席」なので、入力された席の **1 つ手前**を渡す。
    seat_list = "/".join(str(n) for n in seats)
    while True:
        raw = input(f"1ハンド目のボタン席 ({seat_list}, 空Enterで{seats[-1]}): ").strip()
        if not raw:
            first_button = seats[-1]
            break
        try:
            first_button = int(_normalize_cli_command(raw))
            if first_button in seats:
                break
        except ValueError:
            pass
        print(f"使う席（{seat_list}）のどれかを入力してください。")
    button_seat = seats[(seats.index(first_button) - 1) % len(seats)]

    log_dir = input("ログ保存先 (空Enterで ./logs): ").strip() or "./logs"

    return {
        "players": players, "sb": sb, "bb": bb,
        "button_seat": button_seat, "log_dir": log_dir,
    }


def _make_event_recorder(cfg: dict, log_dir: str, session_id: str):
    """config.recording.enabled が true なら EventRecorder を返す (R1, ADR-0010)。

    デフォルト false = 記録しない (挙動不変)。生イベントを
    logs/{session_id}.events.jsonl に append-only で記録する sidecar。
    """
    if not cfg.get("recording", {}).get("enabled", False):
        return None
    from output.event_recorder import EventRecorder

    return EventRecorder(Path(log_dir) / f"{session_id}.events.jsonl")


def _make_table_state_writer(cfg: dict, log_dir: str, session_id: str):
    """config.table_state.enabled（既定 true）なら TableStateWriter を返す。

    RFID だけから導く卓状態（カード / 有効席 / ストリート）を
    `logs/{session}.table_state.json` に publish する。`tools/table_monitor.py` が読む。
    """
    if not cfg.get("table_state", {}).get("enabled", True):
        return None
    from output.table_state_writer import TableStateWriter

    return TableStateWriter(
        log_dir, session_id,
        history=cfg.get("table_state", {}).get("history", True),
    )


def _make_card_correction_hook(rfid_thread):
    """ミスディール訂正で RFID 側の割り当て・デバウンスも落とすフック（ADR-0054）。

    engine は `("board", 位置)` / `("seat", 席)` で呼ぶ。RFID を使っていない構成では None
    （engine 内の記録だけを取り消す = 訂正自体は成立する）。
    """
    if rfid_thread is None:
        return None
    forget_board = getattr(rfid_thread, "forget_board_position", None)
    forget_seat = getattr(rfid_thread, "forget_seat_cards", None)
    if forget_board is None or forget_seat is None:
        return None

    def hook(kind: str, key: int) -> None:
        if kind == "board":
            forget_board(key)
        elif kind == "seat":
            forget_seat(key)

    return hook


def _rfid_tracking_kwargs(rfid_cfg: dict) -> dict:
    """卓の流れに合わせた RFID の解釈（ADR-0058）の設定。config に無ければ本番の既定値。

    店舗 PC の config.json はこの設定が入る前に作られていることがあるので、キーが無いときも
    既定で有効にする（フォールドした札がボードに入る / 配り直しが入力なしで反映されない、を防ぐ）。
    `release_sec: null` で自動の差し替えを止め、`commit_sec: 0` で最初に見えた瞬間に確定する。
    `redeal_window_sec: null` でボードの 1 枚だけの差し直しを扱わない（flop 全体と 6 枚目の詰め直しだけ）。
    `redeal_confirm_sec` はボードの差し直しで前の札が見えないことを確かめる秒数（null = `release_sec`）。
    """
    from rfid.reader_thread import (
        DEFAULT_COMMIT_SEC,
        DEFAULT_GAP_SEC,
        DEFAULT_REDEAL_CONFIRM_SEC,
        DEFAULT_REDEAL_WINDOW_SEC,
        DEFAULT_RELEASE_SEC,
    )
    release = rfid_cfg.get("release_sec", DEFAULT_RELEASE_SEC)
    window = rfid_cfg.get("redeal_window_sec", DEFAULT_REDEAL_WINDOW_SEC)
    confirm = rfid_cfg.get("redeal_confirm_sec", DEFAULT_REDEAL_CONFIRM_SEC)
    return {
        "commit_sec": float(rfid_cfg.get("commit_sec", DEFAULT_COMMIT_SEC)),
        "gap_sec": float(rfid_cfg.get("gap_sec", DEFAULT_GAP_SEC)),
        "release_sec": None if release is None else float(release),
        "redeal_window_sec": None if window is None else float(window),
        "redeal_confirm_sec": None if confirm is None else float(confirm),
    }


def _make_audio_thread(cfg: dict, audio_queue, stop_event, on_transcript=None, listen_gate=None,
                       audio_dir=None):
    """config.audio.enabled が true（既定）なら AudioThread を返す。false なら None。

    false はマイクを繋がない実機テスト（RFID のカード読み取りだけを見る / ダミーアクションを
    キーボードで投入する）用。アクションは CLI の入力ループが読み上げ文を `parse_action` に
    通して AudioEvent にするので、音声と同じ語彙・同じ経路で進行できる。
    `on_transcript` は聞き取った文ごとに呼ばれる（CLI の表示, ADR-0060）。
    `listen_gate` はプレー中だけ set される Event（ハンドの間の発話は認識に回さない, ADR-0063）。
    `audio_dir` は発話の音声を WAV で保存するフォルダ（`audio.save_audio` のとき）。
    """
    audio_cfg = cfg.get("audio", {})
    if not audio_cfg.get("enabled", True):
        return None
    from audio.recorder import _MIN_BUFFER_SECONDS, _SILENCE_RMS_THRESHOLD, AudioThread

    return AudioThread(
        audio_queue=audio_queue,
        device_id=audio_cfg.get("device_id", 0),
        sample_rate=audio_cfg.get("sample_rate", 16000),
        model_size=audio_cfg.get("whisper_model", "medium"),
        language=audio_cfg.get("language", "ja"),
        stop_event=stop_event,
        on_transcript=on_transcript,
        min_speech_sec=float(audio_cfg.get("min_speech_sec", _MIN_BUFFER_SECONDS)),
        listen_gate=listen_gate,
        speech_rms=float(audio_cfg.get("speech_rms", _SILENCE_RMS_THRESHOLD)),
        beam_size=int(audio_cfg.get("beam_size", 5)),
        temperature_fallback=bool(audio_cfg.get("temperature_fallback", False)),
        audio_dir=audio_dir,
        vad_threshold=float(audio_cfg.get("vad_threshold", 0.5)),
    )


def _make_listen_gate(cfg: dict):
    """手札の配布でハンドを始める運用（RFID + `engine.auto_new_hand`）なら、プレー中だけ音声を認識に
    回すための Event を返す（ADR-0063）。それ以外（RFID なし等）は None = 常に聞く
    （配る前の「ハンド開始」で始める運用を止めない）。"""
    if not cfg.get("rfid", {}).get("enabled", False):
        return None
    if not cfg.get("engine", {}).get("auto_new_hand", True):
        return None
    return threading.Event()


# 雑音（聞き違い）として捨てた文は、CLI では頭だけ出す（幻聴のループは数百文字になる。全文は transcripts.jsonl）
_NOISE_SHOWN_CHARS = 20


def _print_transcript(transcript) -> None:
    """聞き取った文とアクションとしての読みを CLI に出す（音声テスト用, ADR-0060）。

    声ではない音（VAD で Whisper にかけなかった音）は出さない（記録 transcripts.jsonl にだけ残る）。
    """
    from audio.recorder import describe_events

    if getattr(transcript, "no_speech", False):
        return
    text = transcript.text
    if getattr(transcript, "noise", False):
        heard = "雑音（聞き違い）として無視"
        if len(text) > _NOISE_SHOWN_CHARS:
            text = text[:_NOISE_SHOWN_CHARS] + "…"
    else:
        heard = describe_events(transcript.events)
    lag = ""
    if transcript.utterance_start_ts is not None:
        delay = transcript.heard_at - transcript.utterance_start_ts
        if delay >= 10:
            lag = f"  （{delay:.0f} 秒前の発話）"
    print(f"  [聞き取り] 「{text}」→ {heard}{lag}", flush=True)


def _wait_for_backlog(audio_thread, timeout_sec: float = 60.0) -> None:
    """打った入力が、先に言われてまだ認識中の発話を追い越さないよう待つ（ADR-0061）。

    認識は発話より遅れることがある（遅れても全部処理する）。`n` / `w` などを先に積むと、
    前のハンドのアクションが次のハンドに入ってしまうので、認識待ちが無くなるまで待つ。
    """
    import time as _time

    if audio_thread is None or audio_thread.backlog() == 0:
        return
    print(f"  （聞き取った発話を先に反映しています… 残り {audio_thread.backlog()} 件）", flush=True)
    deadline = _time.time() + timeout_sec
    while audio_thread.backlog() > 0 and _time.time() < deadline:
        _time.sleep(0.1)
    if audio_thread.backlog() > 0:
        print(f"  （{timeout_sec:.0f} 秒待っても残り {audio_thread.backlog()} 件。入力を先に反映します）",
              flush=True)


def _report_audio_start(audio_thread, device_id: int, wait_sec: float = 3.0) -> None:
    """マイクを開けたかを CLI に出す（ログはファイルに行くので、失敗に気づけるように）。"""
    import time as _time

    deadline = _time.time() + wait_sec
    while audio_thread.health.get("state") == "starting" and _time.time() < deadline:
        _time.sleep(0.05)
    health = audio_thread.health
    state = health.get("state")
    if state == "running":
        print(f"音声入力: マイク 番号 {device_id}（{health.get('device_name') or '?'}）で聞き取っています。"
              "聞き取った文は [聞き取り] と表示します。")
    elif state == "unavailable":
        print("音声入力: PyAudio が無いため使えません（キーボードの読み上げ文で進行できます）。")
    elif state == "error":
        print(f"音声入力: マイク（番号 {device_id}）を開けませんでした — {health.get('error', '')}。"
              r"番号は tools\audio_check.py list で確かめてください（音声なしで続けます）。")
    else:
        print(f"音声入力: マイクの状態を確認できません（{state}）。")


def _open_session_layer(cfg: dict, session_cfg: dict):
    """`session_layer.enabled` なら session を作り、起動時に入力した名前を席に結び付ける（ADR-0059）。

    戻り値は (session_repo, player_repo, session_id, 席 → player_id)。無効なら None（従来どおり
    timestamp の session_id で、席とお客さんの対応を記録しない）。名前は同じ表示名の player に
    結び付き、無ければ作る。名前を入れなかった席（PlayerN）は結び付けない。
    """
    if not cfg.get("session_layer", {}).get("enabled", False):
        return None
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

    player_repo = PlayerRepository()
    session_repo = SessionRepository(player_repo=player_repo)
    session = session_repo.create_session(
        label=datetime.now().strftime("%Y-%m-%d_%H%M%S"),
        blinds={"sb": session_cfg["sb"], "bb": session_cfg["bb"]},
    )
    seat_player_map = {
        p["seat"]: player_repo.find_or_create(p["name"]).player_id
        for p in session_cfg["players"] if p.get("named", True)
    }
    return session_repo, player_repo, session.session_id, seat_player_map


def _close_session_layer(session_repo, session_id: str) -> None:
    """hand logger の終了で session を閉じる（お客さんの画面で「進行中」のまま残さない, ADR-0059）。"""
    from core.session_repository import SessionAlreadyClosedError

    try:
        session_repo.close_session(session_id)
    except SessionAlreadyClosedError:
        pass
    except Exception:
        logger.exception("session を閉じられませんでした: %s", session_id)


def _parse_name_command(parts: list[str]) -> "tuple[int, str | None] | None":
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


def _make_game_state(cfg: dict, players: list, sb: int, bb: int, button_seat=None):
    """config.engine.backend で game-state 実装を選ぶ (R2, ADR-0009)。

    既定 "legacy" = 従来の `GameStateManager`（挙動不変）。"pokerkit" は preview backend
    （要 pokerkit, default-off）。`button_seat` は **1 ハンド目のボタンの 1 つ手前**の席
    （省略時は最大の席番号 = ボタン導入前と同じ並び, ISSUE-0032）。legacy は無視する。
    """
    from core.poker_engine import create_game_state

    backend = cfg.get("engine", {}).get("backend", "legacy")
    return create_game_state(backend, players, sb, bb, button_seat=button_seat)


def _auto_hand_kwargs(cfg: dict, audio_thread) -> dict:
    """手札が配られたら新しいハンド / 勝者の自動判定（ADR-0062）の設定。

    `engine.auto_new_hand` / `engine.auto_winner` は既定で有効（config に無い既存の店舗 PC も有効）。
    認識待ちの発話の数を渡し、配る前に話された発話を前のハンドへ先に反映させる。
    """
    engine_cfg = cfg.get("engine", {})
    return {
        "auto_new_hand": bool(engine_cfg.get("auto_new_hand", True)),
        "auto_winner": bool(engine_cfg.get("auto_winner", True)),
        "speech_backlog": audio_thread.backlog if audio_thread is not None else None,
        "speech_pending_since": audio_thread.oldest_pending_start if audio_thread is not None else None,
        "voice_heard_at": (lambda: audio_thread.last_voice_at) if audio_thread is not None else None,
        "fold_absent_sec": float(engine_cfg.get("fold_absent_sec", 3.0)),
    }


def _rfid_folds_enabled(cfg: dict, seat_presence) -> bool:
    """フォールドを席の札の離脱で決めるか（オーナー決定 2026-09-25, `engine.rfid_folds` 既定 true）。

    席の在否が取れる RFID（PC/SC の RFIDThread）があるときだけ。無ければ従来どおり音声の「フォールド」を
    次の手番の人に付ける（キーボードだけの進行も止めない）。
    """
    return bool(cfg.get("engine", {}).get("rfid_folds", True)) and seat_presence is not None


def _print_notice(message: str) -> None:
    """ハンドの開始・勝者・判定待ちのお知らせ（integration スレッドから呼ばれる）。"""
    print(f"  ● {message}", flush=True)


def _print_cards(message: str) -> None:
    """RFID で読んだ札（手札・ボード・確定時のまとめ）。integration スレッドから呼ばれる。"""
    print(f"  [カード] {message}", flush=True)


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
    game_state = _make_game_state(
        cfg, players, session_cfg["sb"], session_cfg["bb"],
        button_seat=session_cfg.get("button_seat"),
    )

    # 席とお客さんの対応（ADR-0059）。お客さん向け画面はこの記録からハンドを探す。
    session_layer = _open_session_layer(cfg, session_cfg)
    if session_layer is not None:
        session_repo, player_repo, session_id, seat_player_map = session_layer
    else:
        session_repo = player_repo = None
        seat_player_map = {}
        session_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_session1"
    json_writer = JsonWriter(log_dir=session_cfg["log_dir"], session_id=session_id)

    audio_q = make_audio_queue()
    stop_event = threading.Event()
    camera_q = None

    def on_action(record):
        if getattr(record, "actor_source", None) == "unresolved":
            # 状態に適用できなかった入力（ハンド外 / 低信頼の制御語 など, ADR-0047 B2）。
            # 黙って捨てず、何が保留されたかを見せる（ゲーム状態は変わっていない）。
            print(f"  [未適用] {record.action} ({record.reason})  ← 状態は変わっていません")
            return
        # どこから決めたか（札の離脱 / 聞き取れなかったとみて補った）
        origin = {
            "rfid_departure": "  ← 札が離れた",
            "rfid_muck": "  ← 札が中央を通過",
            "implied": "  ← 聞き取れなかったとみて補完",
        }.get(getattr(record, "actor_source", None), "")
        if getattr(record, "actor_source", None) == "rfid_departure" and record.action == "check":
            origin = "  ← 札を前に出した（ショーダウン）"
        print(
            f"  [{record.street}] 席{record.seat}({record.player_name}) "
            f"{record.action} {record.amount or ''}"
            f"  pot={record.pot_after}"
            + (" [要確認]" if record.needs_review else "")
            + origin
        )

    cam_cfg = cfg.get("camera", {})

    audio_cfg = cfg.get("audio", {})
    if audio_cfg.get("enabled", True):
        # 初回は音声認識モデル（medium ≈ 1.5 GB）をダウンロードする。ログはファイルに行くので
        # 何も出ないと固まったように見える（ADR-0060）。
        import time as _time

        print(f"音声認識モデル（{audio_cfg.get('whisper_model', 'medium')}）を読み込んでいます…"
              "（初回はダウンロードで数分かかります）", flush=True)
        loading_since = _time.time()
    listen_gate = _make_listen_gate(cfg)
    # 聞き取った発話はすべて記録する（読めなかったもの・雑音も）。音声は audio.save_audio のときだけ保存。
    from output.transcript_log import TranscriptLog

    transcript_log = TranscriptLog(Path(session_cfg["log_dir"]) / f"{session_id}.transcripts.jsonl")

    def on_transcript(transcript):
        _print_transcript(transcript)
        transcript_log.write(transcript)

    audio_dir = (Path(session_cfg["log_dir"]) / "audio" / session_id
                 if audio_cfg.get("save_audio", False) else None)
    audio_thread = _make_audio_thread(cfg, audio_q, stop_event, on_transcript=on_transcript,
                                      listen_gate=listen_gate, audio_dir=audio_dir)
    if audio_thread is None:
        print("音声入力は無効です (audio.enabled=false)。"
              "アクションはキーボードから読み上げ文で投入してください。")
    elif not audio_thread.asr_ready:
        error = getattr(audio_thread._transcriber, "load_error", None)  # noqa: SLF001
        print(f"音声認識モデルを読み込めませんでした（{error}）。音声は使わずに続けます"
              "（キーボードの読み上げ文で進行できます）。")
        audio_thread = None
    else:
        print(f"音声認識モデルの読み込み完了（{_time.time() - loading_since:.0f} 秒）。")
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
                **_rfid_tracking_kwargs(rfid_cfg),
            )
            print("RFID pyscardスレッド起動。")
        rfid_thread.start()

    event_recorder = _make_event_recorder(cfg, session_cfg["log_dir"], session_id)
    # 新ハンドで RFID の board 位置もリセットする（engine の board と同じ同期点。
    # 片方だけが番号を振り直すと同じ札が 2 か所に出る, ISSUE-0026）。
    # 新ハンドは board 位置 + マック観測をまとめてリセットする（ADR-0026 / ADR-0055）。
    on_new_hand = (
        getattr(rfid_thread, "reset_for_new_hand", None)
        or getattr(rfid_thread, "reset_board_positions", None)
    ) if rfid_thread else None
    on_card_correction = _make_card_correction_hook(rfid_thread)
    seat_absent_since = getattr(rfid_thread, "seat_cards_absent_since", None) if rfid_thread else None
    seat_presence = getattr(rfid_thread, "presence_snapshot", None) if rfid_thread else None
    board_presence = getattr(rfid_thread, "board_presence", None) if rfid_thread else None
    table_state_writer = _make_table_state_writer(cfg, session_cfg["log_dir"], session_id)

    integration_thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=game_state,
        json_writer=json_writer,
        camera_queue=camera_q,
        rfid_queue=rfid_q if rfid_cfg.get("enabled", False) else None,
        on_action=on_action,
        stop_event=stop_event,
        event_recorder=event_recorder,
        on_new_hand=on_new_hand,
        on_card_correction=on_card_correction,
        seat_cards_absent_since=seat_absent_since,
        seat_presence=seat_presence,
        rfid_folds=_rfid_folds_enabled(cfg, seat_presence),
        board_presence=board_presence,
        table_state_writer=table_state_writer,
        control_conf_threshold=cfg.get("engine", {}).get("control_conf_threshold", 0.0),
        session_repo=session_repo,
        seat_player_map=seat_player_map,
        on_notice=_print_notice,
        on_cards=_print_cards,
        listen_gate=listen_gate,
        **_auto_hand_kwargs(cfg, audio_thread),
    )
    if audio_thread is not None:
        audio_thread.start()
    integration_thread.start()
    if audio_thread is not None:
        _report_audio_start(audio_thread, audio_cfg.get("device_id", 0))

    print(f"\nセッション開始。ログ: {json_writer.path}")
    if session_repo is not None:
        seated = ", ".join(
            f"席{p['seat']}={p['name']}" for p in session_cfg["players"] if p.get("named", True)
        )
        print(f"お客さんの記録: {seated or 'なし（名前を入力した席がありません）'}")
    auto = _auto_hand_kwargs(cfg, None)
    if auto["auto_new_hand"] and rfid_thread is not None:
        print("手札を配ると新しいハンドが始まります（2 席以上に 2 枚ずつ置いたとき。n は不要）。"
              "音声はプレー中だけ聞き取ります。")
    if _rfid_folds_enabled(cfg, seat_presence):
        print(f"フォールドは席の札で決めます（{auto['fold_absent_sec']:.0f} 秒離れたまま / 卓の中央を通過）。"
              "札が戻れば取り消します。音声の「フォールド」は次の手番の人には付けません。")
    if audio_dir is not None:
        print(f"発話の音声を保存しています: {audio_dir}")
    if auto["auto_winner"]:
        print("勝者: ほかが全員フォールドしたら自動。ショーダウンは、見せずにマックしたら「フォールド」"
              "（アウトオブポジションから順）、全員見せたら「ハンド終了」か次の手札で手札から判定。"
              "決まらないときは w <席>。")
    print("コマンド: [q]=終了  [n]=新ハンド  [w <席>]=ウィナー  [r <席> <金額>]=リバイ")
    print("ミスディール訂正: [cb <位置>]=ボードの N 枚目を取り消し  [cs <席>]=その席の札を読み直し")
    print("席: [name <席> <名前>]=その席の人（参加）  [name <席> -]=空席（次のハンドから配られない）"
          "  スタック 0 の席は r で買い足すまで配られません")
    print("ブラインド: [blinds <SB> <BB>]=次のハンドからブラインドを変える（例: blinds 200 400）")
    print("上記以外の入力は読み上げ文として解釈します"
          "（例: チェック / シート3 コール / ベット 500）。マイクが無くてもこれで進行できます。")
    print("ディーラーがアナウンスすると自動検出されます。\n")

    # GameStateManager はロックを持たないため、状態変更コマンド (n/w/r) はすべて
    # audio_q 経由で IntegrationThread に処理させる（直接呼ぶと apply_action とレースする）。
    from audio.recognizer import parse_actions
    from audio.recorder import describe_event
    from core.events import AudioEvent
    import time as _time

    try:
        while True:
            line = input("> ").strip()
            if not line:
                continue
            # 先に言われた発話がまだ認識中なら、その反映を待ってから入力を積む（順序を保つ）
            _wait_for_backlog(audio_thread)
            parts = _normalize_cli_command(line).split()
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
            elif cmd == "name":
                parsed = _parse_name_command(line.translate(_FULLWIDTH_TO_ASCII).split())
                if parsed is None:
                    print("使い方: name <席> <名前> / name <席> -（空席）")
                else:
                    seat, name = parsed
                    if seat not in {p["seat"] for p in session_cfg["players"]}:
                        print(f"席{seat} はこの卓にありません")
                        continue
                    vacate = name is None
                    if session_repo is None:
                        print("（席とお客さんの記録は無効です: config の session_layer.enabled=true で有効。"
                              "席の参加・休みだけ反映します）")
                    else:
                        new_map = dict(seat_player_map)
                        if vacate:
                            new_map.pop(seat, None)
                        else:
                            new_map[seat] = player_repo.find_or_create(name).player_id
                        seat_player_map = new_map
                        # 席 → player は次のハンドの開始時に書かれる。名前はゲーム状態を持つ
                        # integration スレッドで変える（ハンドの途中なら次のハンドから）。
                        integration_thread.set_seat_player_map(seat_player_map)
                    shown = f"Player{seat}" if vacate else name
                    audio_q.put(AudioEvent(
                        action="rename_seat", amount=0, timestamp=_time.time(),
                        raw_text=shown, seat=seat,
                    ))
                    # 空席は次のハンドから配られない（休み）。名前を付けた席は参加（休みなら戻す）。
                    audio_q.put(AudioEvent(
                        action="sit_out" if vacate else "sit_in", amount=0, timestamp=_time.time(),
                        raw_text=f"シート{seat} {'休み' if vacate else '参加'}", seat=seat,
                    ))
                    if vacate:
                        print(f"席{seat} を空席にしました（次のハンドから配られません）")
                    else:
                        print(f"席{seat} を {shown} にしました（次のハンドから）")
            elif cmd == "blinds":
                numbers = re.findall(r"\d+", " ".join(parts[1:]))    # `blinds 200 400` / `blinds 200/400`
                if len(numbers) != 2 or int(numbers[0]) <= 0 or int(numbers[0]) > int(numbers[1]):
                    print("使い方: blinds <SB> <BB>（例: blinds 200 400）")
                    continue
                sb, bb = int(numbers[0]), int(numbers[1])
                audio_q.put(AudioEvent(
                    action="set_blinds", amount=bb, timestamp=_time.time(), raw_text=f"{sb}/{bb}",
                ))
                print(f"ブラインド {sb}/{bb} を送信しました（次のハンドから）")
            elif cmd in ("cb", "cs") and len(parts) >= 2:
                # ミスディール訂正（ADR-0054）。状態変更は他と同じく queue 経由。
                try:
                    key = int(parts[1])
                except ValueError:
                    print(f"使い方: {cmd} <{'位置' if cmd == 'cb' else '席番号'}>")
                    continue
                if cmd == "cb":
                    audio_q.put(AudioEvent(
                        action="correct_board", amount=key, timestamp=_time.time(),
                        raw_text=f"ボード{key} 訂正",
                    ))
                    print(f"ボード {key} 枚目の取り消しを送信しました（正しいカードを置いてください）")
                else:
                    audio_q.put(AudioEvent(
                        action="correct_seat", amount=0, timestamp=_time.time(),
                        raw_text=f"シート{key} 訂正", seat=key,
                    ))
                    print(f"席 {key} の札の読み直しを送信しました（正しいカードを置き直してください）")
            else:
                # 上のコマンド以外は **ディーラーのアナウンスとして解釈**する（マイク無しで
                # アクションを投入する経路。音声と同じ `parse_actions` を通すので語彙は共通 =
                # 二重管理にならない）。例: 「チェック」「シート3 コール」「ベット 500」。
                # キーボード入力は ASR ではなく操作者の意図的な入力なので信頼度 1.0 を明示する
                # （None は「Whisper 欠測」の意味で保守的既定 0.5 = 要レビューに倒れる, ADR-0047 B3）。
                # 続けて打った複数のアクション（「フォールド フォールド コール」）は順に分ける（ADR-0061）
                events = parse_actions(line, confidence=1.0)
                if not events:
                    print("認識できません。コマンド: q / n / w <席> / r <席> <金額>、"
                          "または読み上げ文（例: チェック / シート3 コール / ベット 500 / 600）")
                for ev in events:
                    audio_q.put(ev)
                    print(f"  → {describe_event(ev)}")

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
        if session_repo is not None:
            _close_session_layer(session_repo, session_id)
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
    game_state = _make_game_state(
        cfg, players, session_cfg["sb"], session_cfg["bb"],
        button_seat=session_cfg.get("button_seat"),
    )

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

    listen_gate = _make_listen_gate(cfg)
    audio_thread = _make_audio_thread(cfg, audio_q, stop_event, listen_gate=listen_gate)
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
                **_rfid_tracking_kwargs(rfid_cfg),
            )

    # HTTP transport の場合、rfid_receiver を GUI に渡してステータス表示する
    if rfid_thread is not None and rfid_cfg.get("transport") == "http":
        dash._rfid_receiver = rfid_thread

    event_recorder = _make_event_recorder(cfg, session_cfg["log_dir"], session_id)
    # 新ハンドで RFID の board 位置もリセットする（engine の board と同じ同期点。
    # 片方だけが番号を振り直すと同じ札が 2 か所に出る, ISSUE-0026）。
    # 新ハンドは board 位置 + マック観測をまとめてリセットする（ADR-0026 / ADR-0055）。
    on_new_hand = (
        getattr(rfid_thread, "reset_for_new_hand", None)
        or getattr(rfid_thread, "reset_board_positions", None)
    ) if rfid_thread else None
    on_card_correction = _make_card_correction_hook(rfid_thread)
    seat_absent_since = getattr(rfid_thread, "seat_cards_absent_since", None) if rfid_thread else None
    seat_presence = getattr(rfid_thread, "presence_snapshot", None) if rfid_thread else None
    board_presence = getattr(rfid_thread, "board_presence", None) if rfid_thread else None
    table_state_writer = _make_table_state_writer(cfg, session_cfg["log_dir"], session_id)

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
        on_new_hand=on_new_hand,
        on_card_correction=on_card_correction,
        seat_cards_absent_since=seat_absent_since,
        seat_presence=seat_presence,
        rfid_folds=_rfid_folds_enabled(cfg, seat_presence),
        board_presence=board_presence,
        table_state_writer=table_state_writer,
        control_conf_threshold=cfg.get("engine", {}).get("control_conf_threshold", 0.0),
        listen_gate=listen_gate,
        **_auto_hand_kwargs(cfg, audio_thread),
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
            from api.server import PLAYER_WEB_DIR, auth_kwargs_from_config, create_app
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
                player_web_dir=PLAYER_WEB_DIR,  # お客さん向け画面も同じポートで（ADR-0059）
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


def run_viewer_api(host: str | None = None, port: int | None = None) -> None:
    """Phase M1: player 向け読み取り専用 viewer API を起動する (ADR-0017)。

    お客さん向け画面（mobile/ の web 版）も同じポートの `/` で配信する（ADR-0059）。
    `host` / `port` は config の `viewer_api.bind_host` / `bind_port` より優先する。
    """
    from core.config import load_config

    try:
        from api.server import run_server
    except ImportError:
        print("fastapi / uvicorn が見つかりません。pip install \".[api]\" でインストールしてください。")
        sys.exit(1)

    cfg = load_config()
    api_cfg = cfg.get("viewer_api", {})
    shown_host = host or api_cfg.get("bind_host", "127.0.0.1")
    shown_port = port or api_cfg.get("bind_port", 8788)
    print(f"Viewer API を起動します: http://{shown_host}:{shown_port}/api/health (Ctrl+C で終了)")
    if shown_host == "0.0.0.0":
        print(f"お客さんのスマホからは http://<この PC の IP アドレス>:{shown_port}/ を開きます")
    run_server(cfg, host=host, port=port)


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


def _route_logs_to_file(path: str) -> None:
    """ログの出力先を端末からファイルへ切り替える（ISSUE-0034）。

    `--cli` は 1 つの端末で「入力プロンプト」と「別スレッドのログ」を共有しているため、
    RFID の検出ログがタイプ中の行に割り込むとコマンドが壊れる（実機で `w 1` が通らなかった）。
    卓の状態は `tools/table_monitor.py` で見られるので、ログは落としてしまってよい。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.FileHandler(p, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s"
    ))
    root.addHandler(handler)
    print(f"ログは {p} に出します（端末には出ません）。")


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
    parser.add_argument(
        "--host",
        help="--viewer-api の待ち受けアドレス（config の viewer_api.bind_host より優先。"
             "店舗の LAN に出すなら 0.0.0.0, ADR-0059）",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="--viewer-api の待ち受けポート（config の viewer_api.bind_port より優先）",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        nargs="?",
        const="logs/pokerapp.log",
        help="ログを端末ではなくファイルへ出す（既定 logs/pokerapp.log）。"
             "--cli では RFID の検出ログが入力行に割り込んでコマンドが壊れるため、"
             "実機テスト時はこれを付ける（卓の状態は tools/table_monitor.py で見る, ISSUE-0034）",
    )
    args = parser.parse_args()

    if args.log_file:
        _route_logs_to_file(args.log_file)

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
        run_viewer_api(host=args.host, port=args.port)
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
