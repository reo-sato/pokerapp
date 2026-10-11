"""tools/cloud_link.py — 店舗 PC をクラウドにつなぐ（ADR-0067 D3, フェーズ 2b）。

店舗 PC で 1 回だけ実行する。管理トークン（運営だけが持つ。Secret Manager に置いたもの）は伏せ字で入力し、
**保存しない**。保存するのは、クラウドが発行した**端末トークン**とクラウドの URL だけ（`cloud_credentials.json`,
git 管理外・更新で保持）。

使い方（リポジトリの直下で）:
    python tools/cloud_link.py link --url https://<クラウド> --name "店名"   # 店舗を作ってつなぐ（管理トークンを入力）
    python tools/cloud_link.py link --url https://<クラウド> --token           # 発行済みの端末トークンでつなぐ（伏せ字で入力）
    python tools/cloud_link.py reissue        # 端末トークンを発行し直す（前のものは使えなくなる。管理トークンを入力）
    python tools/cloud_link.py status         # つながっているか・クラウドの件数・送れていないもの
    python tools/cloud_link.py push           # いますぐ 1 回送る（店舗サーバーを待たずに確かめたいとき）
    python tools/cloud_link.py unlink         # この PC のつながりを外す（クラウドの記録は消えない）
つないだあとは店舗サーバー（--serve）を起動し直すと、60 秒ごとに送り始める。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.atomic_io import atomic_write_json  # noqa: E402
from core.cloud_sync import (  # noqa: E402
    CREDENTIALS_PATH,
    STATE_PATH,
    HttpTransport,
    SyncState,
    TransportError,
    load_credentials,
    mask,
    save_credentials,
    syncer_from_config,
)
from core.config import _CONFIG_PATH, load_config  # noqa: E402

ADMIN_STORES = "/api/cloud/v1/admin/stores"


class LinkError(Exception):
    pass


def _check_url(url: str) -> str:
    parsed = urlparse(url.strip())
    local = parsed.hostname in ("localhost", "127.0.0.1")
    if parsed.scheme not in ("https", "http") or not parsed.hostname or (parsed.scheme == "http" and not local):
        raise LinkError("URL は https://… で指定してください（手元の確認だけ http://localhost を使えます）")
    return f"{parsed.scheme}://{parsed.netloc}"


def _set_enabled(enabled: bool, config_path: Path) -> None:
    cfg = load_config(config_path)
    cloud = cfg.get("cloud") if isinstance(cfg.get("cloud"), dict) else {}
    cloud["enabled"] = enabled
    cfg["cloud"] = cloud
    atomic_write_json(config_path, cfg)


def _call(transport: Any, method: str, url: str, token: str, body: Optional[dict] = None) -> dict:
    try:
        status, payload = transport.request(method, url, token, body)
    except TransportError as exc:
        raise LinkError(f"クラウドにつながりません: {exc}") from None
    if status not in (200, 201) or not isinstance(payload, dict):
        message = payload.get("message") if isinstance(payload, dict) else None
        raise LinkError(f"クラウドが {status} を返しました: {message or '理由不明'}")
    return payload


def link_new_store(url: str, name: str, admin_token: str, *, transport: Any, credentials_path: Path,
                   config_path: Path) -> dict:
    url = _check_url(url)
    if load_credentials(credentials_path):
        raise LinkError("この PC は既につながっています（別の店舗としてつなぎ直すなら、先に unlink）。")
    body = _call(transport, "POST", url + ADMIN_STORES, admin_token, {"name": name})
    creds = {"url": url, "device_token": body["device_token"], "store_id": body["store"]["store_id"],
             "store_name": body["store"]["name"], "device_id": body["device"]["device_id"]}
    save_credentials(creds, credentials_path)
    _set_enabled(True, config_path)
    return creds


def link_with_token(url: str, device_token: str, *, transport: Any, credentials_path: Path,
                    config_path: Path) -> dict:
    url = _check_url(url)
    body = _call(transport, "GET", url + "/api/cloud/v1/sync/status", device_token)
    creds = {"url": url, "device_token": device_token, "store_id": body.get("store_id"),
             "store_name": body.get("name")}
    save_credentials(creds, credentials_path)
    _set_enabled(True, config_path)
    return creds


def reissue(admin_token: str, *, transport: Any, credentials_path: Path) -> dict:
    creds = load_credentials(credentials_path)
    if not creds or not creds.get("store_id"):
        raise LinkError("この PC はまだつながっていません（link を先に）。")
    body = _call(transport, "POST", f"{creds['url']}{ADMIN_STORES}/{creds['store_id']}/devices", admin_token,
                 {"revoke_others": True})
    creds = {**creds, "device_token": body["device_token"], "device_id": body["device"]["device_id"]}
    save_credentials(creds, credentials_path)
    return creds


def unlink(*, credentials_path: Path, state_path: Path, config_path: Path) -> None:
    for path in (credentials_path, state_path):
        if path.exists():
            path.unlink()
    _set_enabled(False, config_path)


def status_lines(*, transport: Any, credentials_path: Path, state_path: Path) -> list[str]:
    creds = load_credentials(credentials_path)
    if not creds:
        return ["この PC はクラウドにつながっていません（python tools/cloud_link.py link …）。"]
    lines = [f"クラウド: {creds['url']}", f"店舗: {creds.get('store_name') or '?'}（{creds.get('store_id') or '?'}）",
             f"端末トークン: {mask(creds['device_token'])}"]
    state = SyncState.load(state_path)
    lines.append(f"最後に送れた時刻: {state.last_ok_at or 'まだ'} / 送れていない件数: {state.pending}"
                 f" / 受け付けられなかった件数: {len(state.rejected)}")
    if state.last_error:
        lines.append(f"最後のエラー: {state.last_error}")
    try:
        body = _call(transport, "GET", creds["url"] + "/api/cloud/v1/sync/status", creds["device_token"])
        counts = ", ".join(f"{k} {v}" for k, v in sorted((body.get("counts") or {}).items())) or "まだ何もありません"
        lines.append(f"クラウドにあるもの: {counts}")
    except LinkError as exc:
        lines.append(f"クラウドの確認に失敗: {exc}")
    return lines


def main(argv: Optional[list[str]] = None, *, transport: Any = None,
         credentials_path: Path = CREDENTIALS_PATH, state_path: Path = STATE_PATH,
         config_path: Path = _CONFIG_PATH, ask_secret: Callable[[str], str] = getpass.getpass) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("link", help="クラウドにつなぐ")
    p.add_argument("--url", required=True, help="クラウドの URL（https://…）")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="店名（新しく店舗を作る。管理トークンが要る）")
    group.add_argument("--token", action="store_true", help="発行済みの端末トークンでつなぐ（伏せ字で入力）")
    sub.add_parser("reissue", help="端末トークンを発行し直す（管理トークンが要る）")
    sub.add_parser("status", help="つながりの状態")
    sub.add_parser("push", help="いますぐ 1 回送る")
    sub.add_parser("unlink", help="この PC のつながりを外す")
    args = ap.parse_args(argv)
    transport = transport or HttpTransport()
    try:
        if args.cmd == "link":
            if args.name:
                admin = ask_secret("管理トークン（表示されません）: ").strip()
                creds = link_new_store(args.url, args.name.strip(), admin, transport=transport,
                                       credentials_path=credentials_path, config_path=config_path)
            else:
                token = ask_secret("端末トークン（表示されません）: ").strip()
                creds = link_with_token(args.url, token, transport=transport,
                                        credentials_path=credentials_path, config_path=config_path)
            print(f"つなぎました: {creds.get('store_name')}（{creds['url']}）。端末トークン {mask(creds['device_token'])} を"
                  f" {credentials_path.name} に保存しました。")
            print("店舗サーバーを起動し直すと送り始めます（いますぐ確かめるなら: python tools/cloud_link.py push）。")
        elif args.cmd == "reissue":
            admin = ask_secret("管理トークン（表示されません）: ").strip()
            creds = reissue(admin, transport=transport, credentials_path=credentials_path)
            print(f"発行し直しました（前の端末トークンは使えません）: {mask(creds['device_token'])}。"
                  "店舗サーバーを起動し直してください。")
        elif args.cmd == "status":
            for line in status_lines(transport=transport, credentials_path=credentials_path, state_path=state_path):
                print(line)
        elif args.cmd == "push":
            syncer = syncer_from_config(load_config(config_path), credentials_path=credentials_path,
                                        state_path=state_path, transport=transport)
            if syncer is None:
                raise LinkError("クラウドにつながっていません（link を先に。config の cloud.enabled も確認）。")
            result = syncer.run_once()
            if not result["ok"]:
                raise LinkError(result["error"])
            print(f"送りました: {result['sent']} 件（変わったものだけ）。")
        elif args.cmd == "unlink":
            unlink(credentials_path=credentials_path, state_path=state_path, config_path=config_path)
            print("この PC のつながりを外しました（クラウドの記録は残っています）。")
    except LinkError as exc:
        print(f"[cloud_link] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
