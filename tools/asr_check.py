"""tools/asr_check.py — 書き起こしの API（OpenAI, 既定 gpt-transcribe, ADR-0072）を店舗テストで使う前の確かめ。

店舗 PC で（`asr_check.cmd` をダブルクリック = check）:

    python tools/asr_check.py set-key      # 鍵を入れる（入力は画面に出ない・PowerShell の履歴にも残らない）→ そのまま check
    python tools/asr_check.py check        # 鍵・設定・API に届くか（保存した発話を 3 つ送り、かかった秒数と文を出す）
    python tools/asr_check.py use openai   # 書き起こしを API に（local = 店舗 PC の Whisper に戻す）
    python tools/asr_check.py remove-key   # 鍵を消す

鍵は Windows のユーザーの環境変数 `POKERAPP_OPENAI_API_KEY` に置く（`setx` と同じ所。リポジトリ・config・ログには
置かない）。ロガーは環境変数に無ければ登録を直接読むので、入れたあとに新しく起動したロガーから使える。
鍵の値はこの道具も表示しない。
"""
from __future__ import annotations

import argparse
import getpass
import os
import statistics
import sys
import time
import wave
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import remote_asr  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEY_NAME = remote_asr.KEY_ENV[0]
SAMPLE_COUNT = 3
MIN_SAMPLE_SEC = 0.6


def _load_config(path: Optional[str]) -> dict:
    from core.config import load_config

    return load_config(Path(path) if path else None)


def describe_source(source: Optional[str]) -> str:
    if source == "env":
        return "あります（この窓の環境変数）"
    if source == "registry":
        return "あります（Windows に登録済み。新しく起動したロガーが読みます）"
    return "ありません"


def explain_error(error: Exception) -> str:
    """API の失敗を、店舗で次にやることの分かる文に（鍵の値は出さない）。"""
    text = remote_asr.redact(str(error))
    if "HTTP 401" in text:
        return f"鍵が違うか、無効になっています。OpenAI の管理画面で確かめて set-key で入れ直してください（{text}）"
    if "HTTP 429" in text:
        return f"使用の上限・残高・回数の制限です。OpenAI の管理画面の Billing / Limits を見てください（{text}）"
    if "HTTP 404" in text or ("HTTP 400" in text and "model" in text.lower()):
        return f"モデルの名前が使えません（audio.remote_model）。消せば既定の {remote_asr.DEFAULT_MODEL} です（{text}）"
    if "つながりません" in text:
        return f"API につながりません。Wi-Fi・インターネットを確かめてください（{text}）"
    return text


def wav_seconds(data: bytes) -> float:
    import io

    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, EOFError):
        return 0.0


def find_samples(log_dir: Path, count: int = SAMPLE_COUNT) -> list[Path]:
    """送って確かめる発話: いちばん新しいセッションから、プレー中に保存した発話（聞き流した off_ は除く）を新しい順に。"""
    audio_root = log_dir / "audio"
    if not audio_root.is_dir():
        return []
    picked: list[Path] = []
    for session in sorted((d for d in audio_root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
        files = sorted((f for f in session.glob("*.wav") if not f.name.startswith("off_")),
                       key=lambda f: f.name, reverse=True)
        for f in files:
            try:
                if wav_seconds(f.read_bytes()) >= MIN_SAMPLE_SEC:
                    picked.append(f)
            except OSError:
                continue
            if len(picked) >= count:
                return picked
    return picked


def quiet_sample(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """保存した発話が無いときに送る音（無音）。文は空になるが、鍵とモデルが通るかは分かる。"""
    return remote_asr.wav_bytes(b"\x00\x00" * int(seconds * sample_rate), sample_rate)


def make_transcriber(audio_cfg: dict):
    """設定どおりの書き起こしの相手（cloud ならクラウドの中継、それ以外は OpenAI を直接）。使えなければ (None, 理由)。"""
    mode = str(audio_cfg.get("transcriber", "local")).lower()
    if mode == "cloud":
        from core.cloud_sync import load_credentials

        try:
            creds = load_credentials()
        except Exception:  # noqa: BLE001
            creds = None
        if not creds:
            return None, "クラウドにつないでいません（python tools/cloud_link.py link）"
        return remote_asr.CloudTranscriber(creds.get("url", ""), creds.get("device_token", "")), None
    key = remote_asr.api_key()
    if not key:
        return None, "鍵がありません（asr_check.cmd set-key）"
    model = str(audio_cfg.get("remote_model") or remote_asr.DEFAULT_MODEL)
    return remote_asr.OpenAITranscriber(model=model, key=key, language=audio_cfg.get("language", "ja")), None


def run_check(cfg: dict, log_dir: Path, wav: Optional[Path] = None, out: Callable[[str], None] = print,
              transcriber=None) -> int:
    audio_cfg = cfg.get("audio", {}) or {}
    mode = str(audio_cfg.get("transcriber", "local")).lower()
    model = str(audio_cfg.get("remote_model") or remote_asr.DEFAULT_MODEL)
    out(f"書き起こしの方式（audio.transcriber）: {mode}"
        + {"local": "（店舗 PC の Whisper）", "openai": f"（OpenAI の API・{model}）",
           "cloud": "（クラウドの中継）"}.get(mode, "（知らない値）"))
    if mode != "cloud":
        out(f"鍵（{KEY_NAME}）: {describe_source(remote_asr.key_source())}")
    if not audio_cfg.get("enabled", True):
        out("● 音声入力が無効です（audio.enabled=false）。ロガーは声を聞きません")
    if transcriber is None:
        transcriber, why = make_transcriber(audio_cfg)
        if transcriber is None:
            out(f"✗ API を試せません: {why}")
            return 1
    samples: list[tuple[str, bytes]] = []
    if wav is not None:
        samples.append((wav.name, wav.read_bytes()))
    else:
        samples = [(f"{p.parent.name[:24]}/{p.name}", p.read_bytes()) for p in find_samples(log_dir)]
    if not samples:
        out("保存した発話が無いので、1 秒の無音を送ります（文は空になります。鍵とモデルが通るかだけ分かります）")
        samples = [("無音 1 秒", quiet_sample())]
    out(f"API に {len(samples)} つ送ります（{transcriber.asr_name}）…")
    took: list[float] = []
    for name, data in samples:
        started = time.monotonic()
        try:
            text = transcriber._request(data)  # noqa: SLF001 — 声かどうかの判定を通さず、そのまま送る
        except remote_asr.RemoteAsrError as e:
            out(f"✗ {name}: {explain_error(e)}")
            return 1
        took.append(time.monotonic() - started)
        out(f"  {took[-1]:.2f} 秒  {wav_seconds(data):.1f} 秒の音声  「{text}」")
    out(f"✓ API に届きました（{transcriber.asr_name}。1 発話 {statistics.median(took):.2f} 秒）")
    per_min = remote_asr.USD_PER_MIN.get(model)
    if mode != "cloud" and per_min:
        out(f"  費用の目安: 声 1 分あたり ${per_min}（公式の価格表で確かめる）。1 セッションの実績は OpenAI の使用量の画面で")
    if mode == "local":
        out("● いまのロガーは店舗 PC の Whisper で書き起こします。API にするには: asr_check.cmd use openai")
    else:
        out("ロガーを起動すると、この方式で書き起こします（動いているロガーは起動し直してください）")
    return 0


def _cmd_check(args) -> int:
    log_dir = Path(args.log_dir) if args.log_dir else ROOT / "logs"
    return run_check(_load_config(args.config), log_dir, Path(args.wav) if args.wav else None)


def store_key(value: str, out: Callable[[str], None] = print) -> bool:
    value = value.strip()
    if not value:
        out("何も入力されませんでした")
        return False
    if not value.startswith("sk-") or any(c.isspace() for c in value):
        out("OpenAI の API の鍵は sk- で始まり、空白を含みません。貼り付けを確かめてください")
        return False
    if os.name != "nt":
        out(f"Windows 以外では、シェルの設定で環境変数 {KEY_NAME} を設定してください")
        return False
    remote_asr.write_user_env(KEY_NAME, value)
    os.environ[KEY_NAME] = value            # 続けて確かめる（この窓のこのプロセスだけ）
    out(f"鍵を Windows に登録しました（{KEY_NAME}。値は表示しません）")
    return True


def _cmd_set_key(args) -> int:
    value = getpass.getpass("OpenAI の API の鍵を貼り付けて Enter（入力は表示されません）: ")
    if not store_key(value):
        return 1
    if args.no_check:
        return 0
    print()
    return _cmd_check(args)


def _cmd_remove_key(args) -> int:
    if os.name != "nt":
        print(f"Windows 以外では、シェルの設定から {KEY_NAME} を消してください")
        return 1
    remote_asr.write_user_env(KEY_NAME, None)
    print(f"鍵（{KEY_NAME}）を消しました。動いているロガーは起動し直すと店舗 PC の Whisper に戻ります"
          "（audio.transcriber が openai のままでも、鍵が無ければ Whisper です）")
    return 0


def _cmd_use(args) -> int:
    from core.atomic_io import atomic_write_json
    from core.config import _CONFIG_PATH

    path = Path(args.config) if args.config else _CONFIG_PATH
    cfg = _load_config(str(path))
    cfg.setdefault("audio", {})["transcriber"] = args.mode
    atomic_write_json(path, cfg)
    label = {"openai": "OpenAI の API", "local": "店舗 PC の Whisper", "cloud": "クラウドの中継"}[args.mode]
    print(f"書き起こしを {label} にしました（audio.transcriber={args.mode}）。ロガーを起動し直すと変わります")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="書き起こしの API（OpenAI）を使う前の確かめ（ADR-0072）")
    parser.add_argument("--config", default=None, help="config パス（既定: アプリの config.json）")
    parser.add_argument("--log-dir", default=None, help="logs フォルダ（既定: アプリの logs/）")
    sub = parser.add_subparsers(dest="command")
    p_check = sub.add_parser("check", help="鍵・設定・API に届くか（保存した発話を 3 つ送る）")
    p_check.add_argument("--wav", default=None, help="送る WAV（既定: いちばん新しいセッションの保存した発話）")
    p_set = sub.add_parser("set-key", help="鍵を入れる（入力は表示されない）→ そのまま check")
    p_set.add_argument("--no-check", action="store_true", help="入れたあとに確かめない")
    sub.add_parser("remove-key", help="鍵を消す")
    p_use = sub.add_parser("use", help="書き起こしの方式を変える（openai / local / cloud）")
    p_use.add_argument("mode", choices=["openai", "local", "cloud"])
    p_check.set_defaults(func=_cmd_check)
    p_set.set_defaults(func=_cmd_set_key, wav=None)
    sub.choices["remove-key"].set_defaults(func=_cmd_remove_key)
    p_use.set_defaults(func=_cmd_use)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):           # 引数なし（ダブルクリック）= check
        args = build_parser().parse_args([*(sys.argv[1:] if argv is None else argv), "check"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
