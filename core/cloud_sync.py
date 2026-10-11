"""core/cloud_sync.py — 店舗 PC → クラウドの送り手（ADR-0067 D2, フェーズ 2b）。

店舗サーバー（`main.py --serve`, ADR-0065）の中で 60 秒ごとに動き、店舗 PC のデータのうち
**クラウドが受け取ったと答えたものから変わったレコードだけ**を送る。クラウドからは何も受け取らない（一方向, D1）。

- 送るもの: players / sessions / ledger（明細・ポイント・精算）/ 注文 / ハンド（sessions.json に載っているセッションだけ。
  店舗 PC が見せる形 = ライブの記録 ⊕ 推定 = `core/hand_estimate.load_record`）/ ハンドの訂正 / 店舗の設定。
- 送る前に落とすもの: ハンドの聞き取った文・監査用の項目（`core/cloud_payload.strip_hand`）。
- 覚えておくもの（`cloud_sync_state.json`）: レコードごとの「受け取ってもらった指紋」・送り手の番号（seq）・
  最後に成功した時刻・最後のエラー。消えても全部を送り直すだけ（クラウドは同じものを 2 回受けても変わらない）。
- 端末トークンとクラウドの URL は `cloud_credentials.json`（git 管理外。`tools/cloud_link.py` が書く）。
- 失敗したら間を空ける（60 秒 → 倍々 → 最大 15 分）。店内の運用には何も影響しない。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from core.atomic_io import atomic_write_json
from core.cloud_payload import (
    HOLE_CARD_VISIBILITY,
    KINDS,
    PROTOCOL,
    RecordKeyError,
    canonical_json,
    fingerprint,
    record_key,
    strip_hand,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "cloud_sync_state.json"
CREDENTIALS_PATH = ROOT / "cloud_credentials.json"

DEFAULT_INTERVAL_SEC = 60.0
MAX_INTERVAL_SEC = 900.0
BATCH_MAX_RECORDS = 1000
BATCH_MAX_BYTES = 3_000_000          # クラウドの上限（既定 4 MB）より小さく
SETTINGS_ID = "store_settings:settings"
PUSH_PATH = "/api/cloud/v1/sync/push"
STATUS_PATH = "/api/cloud/v1/sync/status"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    """読めなければ空（読み直しは次の回。送り手はファイルを動かさない）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ――― 送る元 ―――


@dataclass
class SourcePaths:
    players: Path = ROOT / "players.json"
    sessions: Path = ROOT / "sessions.json"
    ledger: Path = ROOT / "ledger.json"
    orders: Path = ROOT / "order_requests.json"
    corrections: Path = ROOT / "hand_corrections.json"
    log_dir: Path = ROOT / "logs"

    @classmethod
    def under(cls, root: Path, log_dir: Optional[Path] = None) -> "SourcePaths":
        root = Path(root)
        return cls(root / "players.json", root / "sessions.json", root / "ledger.json",
                   root / "order_requests.json", root / "hand_corrections.json", Path(log_dir or root / "logs"))


def collect(paths: SourcePaths) -> dict[str, list[dict]]:
    """店舗 PC のいまのデータを種類ごとに（送る形に整えて）読む。"""
    from core.hand_estimate import load_record

    sessions = list(_read_json(paths.sessions).get("sessions", []))
    ledger = _read_json(paths.ledger)
    out: dict[str, list[dict]] = {
        "players": list(_read_json(paths.players).get("players", [])),
        "sessions": sessions,
        "ledger_entries": list(ledger.get("ledger_entries", [])),
        "point_entries": list(ledger.get("point_ledger_entries", [])),
        "settlements": list(ledger.get("settlements", [])),
        "order_requests": list(_read_json(paths.orders).get("requests", [])),
        "hands": [],
        "corrections": list(_read_json(paths.corrections).get("corrections", [])),
    }
    # ハンドは sessions.json に載っているセッションだけ（席とお客さんが結び付いていない記録は店外で見せる先が無い）
    for session in sessions:
        sid = session.get("session_id")
        if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid or sid.startswith("."):
            continue
        record = load_record(paths.log_dir, sid)
        for hand in (record or {}).get("hands", []) or []:
            if isinstance(hand, dict):
                out["hands"].append(strip_hand(hand))
    return out


def store_settings(cfg: dict) -> dict:
    """クラウドに送る店舗の設定（ADR-0067 D6）。"""
    hole = (cfg.get("viewer_api") or {}).get("hole_cards", "all")
    return {"hole_cards": hole if hole in HOLE_CARD_VISIBILITY else "all"}


# ――― 端末トークン ―――


def load_credentials(path: Path = CREDENTIALS_PATH) -> Optional[dict]:
    data = _read_json(path)
    if not data.get("url") or not data.get("device_token"):
        return None
    return data


def save_credentials(data: dict, path: Path = CREDENTIALS_PATH) -> None:
    atomic_write_json(path, data)


def mask(token: Optional[str]) -> str:
    if not token:
        return "（なし）"
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "…"


# ――― 覚えておくこと ―――


@dataclass
class SyncState:
    acked: dict[str, str] = field(default_factory=dict)       # "種類:キー" → 受け取ってもらった指紋
    rejected: dict[str, str] = field(default_factory=dict)    # "種類:キー" → 断られた理由
    last_seq: int = 0
    last_ok_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_error: Optional[str] = None
    failures: int = 0
    pending: int = 0

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        data = _read_json(path)
        state = cls()
        for name in ("acked", "rejected"):
            value = data.get(name)
            if isinstance(value, dict):
                setattr(state, name, {str(k): str(v) for k, v in value.items()})
        for name in ("last_seq", "failures", "pending"):
            if isinstance(data.get(name), int):
                setattr(state, name, data[name])
        for name in ("last_ok_at", "last_attempt_at", "last_error"):
            if isinstance(data.get(name), str):
                setattr(state, name, data[name])
        return state

    def save(self, path: Path) -> None:
        atomic_write_json(path, {
            "acked": self.acked, "rejected": self.rejected, "last_seq": self.last_seq,
            "last_ok_at": self.last_ok_at, "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error, "failures": self.failures, "pending": self.pending,
        })


# ――― クラウドとのやりとり ―――


class TransportError(Exception):
    """つながらない（ネットが切れている・名前が引けない・時間切れ）。"""


class HttpTransport:
    """httpx で POST / GET する。返り値は (HTTP の状態, JSON)。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def request(self, method: str, url: str, token: str, body: Optional[dict] = None) -> tuple[int, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {token}"}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = canonical_json(body).encode("utf-8")
        try:
            res = httpx.request(method, url, headers=headers, content=content, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from None
        try:
            payload = res.json()
        except ValueError:
            payload = None
        return res.status_code, payload


_ERROR_HINTS = {
    401: "端末トークンが違うか取り消されています（tools/cloud_link.py でつなぎ直してください）",
    403: "クラウドでこの店舗が使えない状態です（運営に確認してください）",
    409: "クラウドとこの PC の版が合いません（どちらかを更新してください）",
}


@dataclass
class _Item:
    ident: str      # "種類:キー"
    kind: str
    record: dict
    fp: str
    size: int


class CloudSyncer:
    def __init__(self, url: str, token: str, *, paths: Optional[SourcePaths] = None,
                 settings_fn: Callable[[], dict] = lambda: {"hole_cards": "all"},
                 state_path: Path = STATE_PATH, transport: Any = None,
                 interval_sec: float = DEFAULT_INTERVAL_SEC, max_interval_sec: float = MAX_INTERVAL_SEC,
                 store_pc: Optional[dict] = None,
                 now_us: Callable[[], int] = lambda: time.time_ns() // 1000) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.paths = paths or SourcePaths()
        self.settings_fn = settings_fn
        self.state_path = Path(state_path)
        self.transport = transport or HttpTransport()
        self.interval_sec = float(interval_sec)
        self.max_interval_sec = float(max_interval_sec)
        self.store_pc = store_pc or {}
        self.now_us = now_us
        self.state = SyncState.load(self.state_path)
        self._lock = threading.Lock()
        self._logged_rejections: set[tuple[str, str]] = set()
        self._seq_checked = False

    # 送るもの

    def _items(self) -> list[_Item]:
        data = collect(self.paths)
        items: list[_Item] = []
        for kind in KINDS:
            for rec in data[kind]:
                try:
                    ident = f"{kind}:{record_key(kind, rec)}"
                except RecordKeyError as exc:
                    logger.warning("クラウドに送れないレコード（%s）: %s", kind, exc)
                    continue
                text = canonical_json(rec)
                items.append(_Item(ident, kind, rec, fingerprint(rec), len(text.encode("utf-8"))))
        settings = self.settings_fn()
        items.append(_Item(SETTINGS_ID, "store_settings", settings, fingerprint(settings),
                           len(canonical_json(settings))))
        return items

    def pending_items(self) -> list[_Item]:
        return [it for it in self._items() if self.state.acked.get(it.ident) != it.fp]

    def _batches(self, items: list[_Item]) -> list[list[_Item]]:
        batches: list[list[_Item]] = []
        current: list[_Item] = []
        size = 0
        for it in items:
            if current and (len(current) >= BATCH_MAX_RECORDS or size + it.size > BATCH_MAX_BYTES):
                batches.append(current)
                current, size = [], 0
            current.append(it)
            size += it.size
        if current:
            batches.append(current)
        return batches

    def _sync_seq(self) -> None:
        """起動して最初の送信の前に、クラウドが持っている最大の番号に合わせる。

        `cloud_sync_state.json` が消えた・PC の時計が戻ったときに、古い番号で送ってハンドの変更が
        「古い」と捨てられるのを防ぐ。
        """
        status, body = self.transport.request("GET", self.url + STATUS_PATH, self.token)
        if status != 200 or not isinstance(body, dict):
            code = body.get("code") if isinstance(body, dict) else None
            raise _Failure(_ERROR_HINTS.get(status) or f"クラウドが {status} を返しました（{code or '理由不明'}）")
        self.state.last_seq = max(self.state.last_seq, int(body.get("max_seq") or 0))
        self._seq_checked = True

    def _next_seq(self) -> int:
        return max(self.state.last_seq + 1, self.now_us())

    def _payload(self, batch: list[_Item], seq: int) -> dict:
        records: dict[str, list[dict]] = {}
        payload: dict = {"protocol": PROTOCOL, "seq": seq, "store_pc": self.store_pc, "records": records}
        for it in batch:
            if it.kind == "store_settings":
                payload["store_settings"] = it.record
            else:
                records.setdefault(it.kind, []).append(it.record)
        return payload

    def _send(self, batch: list[_Item]) -> None:
        """1 つの塊を送る。413 なら半分に分けて送り直す。失敗は例外（_Failure）。"""
        seq = self._next_seq()
        status, body = self.transport.request("POST", self.url + PUSH_PATH, self.token, self._payload(batch, seq))
        if status == 413 and len(batch) > 1:
            half = len(batch) // 2
            self._send(batch[:half])
            self._send(batch[half:])
            return
        if status == 413:
            # 1 件で上限を超える: 送り直しても通らないので、変わるまで送らない
            self._mark_rejected(batch[0], "大きすぎてクラウドに送れません")
            return
        if status != 200 or not isinstance(body, dict):
            code = body.get("code") if isinstance(body, dict) else None
            raise _Failure(_ERROR_HINTS.get(status) or f"クラウドが {status} を返しました（{code or '理由不明'}）")
        rejected = {f"{r.get('kind')}:{r.get('key')}": str(r.get("reason")) for r in body.get("rejected", [])
                    if isinstance(r, dict)}
        # クラウドがもっと新しい番号のものを持っていた（古い番号で送った）種類は、受け取ってもらえていないので次に送り直す
        stale_kinds = {kind for kind, counts in (body.get("results") or {}).items()
                       if isinstance(counts, dict) and counts.get("stale")}
        if stale_kinds:
            logger.warning("クラウドの方が新しい番号でした（%s）。次の回に送り直します。", sorted(stale_kinds))
        for it in batch:
            reason = rejected.get(it.ident)
            if reason is not None:
                self._mark_rejected(it, reason)
            elif it.kind in stale_kinds:
                continue
            else:
                self.state.acked[it.ident] = it.fp
                self.state.rejected.pop(it.ident, None)
        # キーの取れないレコードは "kind:None" で返る（送る前にキーを確かめているので通常は無い）
        self.state.last_seq = max(seq, int(body.get("max_seq") or 0))
        self.state.save(self.state_path)

    def _mark_rejected(self, it: _Item, reason: str) -> None:
        self.state.acked[it.ident] = it.fp            # 変わるまで送らない
        self.state.rejected[it.ident] = reason
        if (it.ident, it.fp) not in self._logged_rejections:
            self._logged_rejections.add((it.ident, it.fp))
            logger.warning("クラウドが受け付けなかったレコード %s: %s", it.ident, reason)

    def run_once(self) -> dict:
        """変わったレコードを送る。結果（送った件数・塊の数・成功したか・エラー）を返す。"""
        with self._lock:
            self.state.last_attempt_at = _now_iso()
            try:
                items = self.pending_items()
            except Exception as exc:  # noqa: BLE001 — 読めないファイルがあっても止まらない
                return self._fail(f"送るデータを読めませんでした: {exc}", pending=self.state.pending)
            if not self._seq_checked and items:
                try:
                    self._sync_seq()
                except _Failure as exc:
                    return self._fail(str(exc), pending=len(items))
                except TransportError as exc:
                    return self._fail(f"クラウドにつながりません（{exc}）", pending=len(items))
            sent = 0
            batches = self._batches(items)
            try:
                for batch in batches:
                    self._send(batch)
                    sent += len(batch)
            except _Failure as exc:
                return self._fail(str(exc), pending=len(items) - sent, sent=sent)
            except TransportError as exc:
                return self._fail(f"クラウドにつながりません（{exc}）", pending=len(items) - sent, sent=sent)
            if sent:
                logger.info("クラウドに送りました: %d 件（%d 回）", sent, len(batches))
            if self.state.failures:
                logger.info("クラウドへの送信が戻りました。")
            self.state.failures = 0
            self.state.last_error = None
            self.state.last_ok_at = _now_iso()
            self.state.pending = 0
            self.state.save(self.state_path)
            return {"ok": True, "sent": sent, "batches": len(batches), "error": None}

    def _fail(self, message: str, *, pending: int, sent: int = 0) -> dict:
        if self.state.failures == 0 or message != self.state.last_error:
            logger.warning("クラウドに送れませんでした: %s", message)
        self.state.failures += 1
        self.state.last_error = message
        self.state.pending = pending
        try:
            self.state.save(self.state_path)
        except OSError:
            logger.exception("cloud_sync_state.json を書けませんでした")
        return {"ok": False, "sent": sent, "batches": 0, "error": message}

    def next_delay(self) -> float:
        if self.state.failures <= 0:
            return self.interval_sec
        return min(self.interval_sec * (2 ** self.state.failures), self.max_interval_sec)

    def status(self) -> dict:
        """`/api/health` の `cloud`（店内の LAN に出る。トークンは出さない）。"""
        return {"enabled": True, "last_ok_at": self.state.last_ok_at,
                "last_attempt_at": self.state.last_attempt_at, "last_error": self.state.last_error,
                "pending": self.state.pending, "rejected": len(self.state.rejected)}

    def cloud_status(self) -> tuple[int, Any]:
        """クラウドの側の件数（`tools/cloud_link.py status`）。"""
        return self.transport.request("GET", self.url + STATUS_PATH, self.token)

    # ――― 閲覧リンク（ADR-0067 D5, 2c）―――

    def _link_call(self, path: str, player_id: str) -> tuple[int, Any]:
        try:
            return self.transport.request("POST", self.url + path, self.token, {"player_id": player_id})
        except TransportError as exc:
            raise CloudLinkError("cloud_unavailable", f"クラウドにつながりません（{exc}）") from None

    def issue_viewer_link(self, player_id: str) -> dict:
        """その人の閲覧リンクを発行する（前のものは使えなくなる）。まだ送っていない人なら先に送る。"""
        if f"players:{player_id}" not in self.state.acked:
            result = self.run_once()
            if not result["ok"]:
                raise CloudLinkError("cloud_unavailable", result["error"] or "クラウドに送れませんでした")
        status, body = self._link_call(VIEWER_LINKS_PATH, player_id)
        if status == 201 and isinstance(body, dict) and body.get("url"):
            return body
        code = body.get("code") if isinstance(body, dict) else None
        if code == "unknown_player":
            raise CloudLinkError("unknown_player", "この人はまだクラウドに届いていません（少し待ってからもう一度）", 409)
        raise CloudLinkError("cloud_error", _ERROR_HINTS.get(status) or f"クラウドが {status} を返しました（{code}）")

    def revoke_viewer_link(self, player_id: str) -> int:
        status, body = self._link_call(VIEWER_LINKS_PATH + "/revoke", player_id)
        if status == 200 and isinstance(body, dict):
            return int(body.get("revoked") or 0)
        code = body.get("code") if isinstance(body, dict) else None
        raise CloudLinkError("cloud_error", _ERROR_HINTS.get(status) or f"クラウドが {status} を返しました（{code}）")

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_once()
            if stop.wait(self.next_delay()):
                return


class _Failure(Exception):
    pass


class CloudLinkError(Exception):
    """閲覧リンクの発行・取り消しができない（スタッフ API が {code, message} で返す）。"""

    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


VIEWER_LINKS_PATH = "/api/cloud/v1/viewer-links"


def syncer_from_config(cfg: dict, *, credentials_path: Path = CREDENTIALS_PATH,
                       state_path: Path = STATE_PATH, transport: Any = None,
                       paths: Optional[SourcePaths] = None) -> Optional[CloudSyncer]:
    """config の `cloud.enabled` と `cloud_credentials.json` から送り手を作る（使わないなら None）。"""
    cloud = cfg.get("cloud") or {}
    if not cloud.get("enabled"):
        return None
    creds = load_credentials(credentials_path)
    if creds is None:
        logger.warning("cloud.enabled ですが %s がありません（tools/cloud_link.py でつないでください）。"
                       "クラウドへは送りません。", credentials_path.name)
        return None
    from core.version import version_info

    info = version_info()
    log_dir = Path(cfg.get("session", {}).get("log_dir", "./logs"))
    if not log_dir.is_absolute():
        log_dir = ROOT / log_dir
    return CloudSyncer(
        creds["url"], creds["device_token"],
        paths=paths or SourcePaths(log_dir=log_dir),
        settings_fn=lambda: store_settings(cfg),
        state_path=state_path, transport=transport,
        interval_sec=float(cloud.get("interval_sec") or DEFAULT_INTERVAL_SEC),
        store_pc={"branch": info.get("branch"), "commit": info.get("commit"), "version": info.get("version")},
    )
