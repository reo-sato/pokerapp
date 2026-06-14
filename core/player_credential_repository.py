"""core/player_credential_repository.py

ADR-0027 (L1): player の PIN credential を node-local に保持するリポジトリ。

PIN は ``players.json`` ではなく専用ファイル ``player_credentials.json`` に分離する
（read API / sync snapshot 非対象。漏洩・伝播・schema 変更を避ける, ADR-0027 D1）。
PIN は PBKDF2-HMAC-SHA256 + per-PIN salt でハッシュ化して保存し、**平文は保持しない**。
低エントロピー（4–6 桁）PIN への総当たりは **per-player lockout** で緩和する
（主防御は LAN 限定 = `viewer_api.bind_host` 既定 127.0.0.1, ADR-0027 D5）。

永続形式（PlayerRepository と同じくアトミックリネーム書き込み）:
    {"credentials": [
      {"player_id": "...", "pin_hash": "pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>",
       "updated_at": "...", "failed_attempts": 0, "locked_until": null}
    ]}
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime
from hashlib import pbkdf2_hmac
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CRED_DB = Path(__file__).parent.parent / "player_credentials.json"
_ALGO = "pbkdf2_sha256"


class CredentialError(Exception):
    """PIN credential 操作の基底例外。"""


class PinTooShortError(CredentialError):
    """PIN が最小長未満（code=pin_too_short）。"""


class PinLockedError(CredentialError):
    """連続失敗で lockout 中（code=pin_locked）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PlayerCredentialRepository:
    """player_id → PIN credential の node-local ストア（read API / sync 非対象）。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        iterations: int = 210_000,
        min_length: int = 4,
        max_attempts: int = 5,
        lockout_sec: int = 300,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_CRED_DB
        self._iterations = int(iterations)
        self._min_length = int(min_length)
        self._max_attempts = int(max_attempts)
        self._lockout_sec = int(lockout_sec)
        self._creds: dict[str, dict] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # ――― 永続化 ―――

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load credential DB (%s), starting empty.", e)
            return
        for raw in data.get("credentials", []):
            pid = raw.get("player_id")
            if pid and raw.get("pin_hash"):
                self._creds[pid] = {
                    "player_id": pid,
                    "pin_hash": raw["pin_hash"],
                    "updated_at": raw.get("updated_at", ""),
                    "failed_attempts": int(raw.get("failed_attempts", 0)),
                    "locked_until": raw.get("locked_until"),
                }

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        data = {"credentials": list(self._creds.values())}
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            logger.exception("Failed to write credential DB: %s", self._path)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def reload(self) -> None:
        self._creds.clear()
        self._load()

    # ――― API ―――

    def has_pin(self, player_id: str) -> bool:
        return player_id in self._creds

    def set_pin(self, player_id: str, pin: str) -> None:
        """PIN を設定/変更する。lockout / 失敗回数はリセットする。"""
        pin = pin or ""
        if len(pin) < self._min_length:
            raise PinTooShortError(f"PIN は {self._min_length} 桁以上にしてください。")
        salt = secrets.token_bytes(16)
        digest = pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, self._iterations)
        self._creds[player_id] = {
            "player_id": player_id,
            "pin_hash": f"{_ALGO}${self._iterations}${salt.hex()}${digest.hex()}",
            "updated_at": _now_iso(),
            "failed_attempts": 0,
            "locked_until": None,
        }
        self._flush()

    def verify_pin(self, player_id: str, pin: str, *, now: float | None = None) -> bool:
        """PIN を検証する。

        - credential 未登録 → False。
        - lockout 中 → PinLockedError。
        - 一致 → True（失敗カウンタ reset）。
        - 不一致 → False（失敗カウンタ +1、上限到達で lockout 設定）。
        """
        rec = self._creds.get(player_id)
        if rec is None:
            return False
        current = now if now is not None else time.time()
        locked_until = rec.get("locked_until")
        if locked_until is not None and current < locked_until:
            raise PinLockedError("PIN がロックされています。しばらく待って再試行してください。")
        if self._check(rec["pin_hash"], pin or ""):
            rec["failed_attempts"] = 0
            rec["locked_until"] = None
            self._flush()
            return True
        rec["failed_attempts"] = int(rec.get("failed_attempts", 0)) + 1
        if rec["failed_attempts"] >= self._max_attempts:
            rec["locked_until"] = current + self._lockout_sec
            rec["failed_attempts"] = 0
        self._flush()
        return False

    @staticmethod
    def _check(stored: str, pin: str) -> bool:
        try:
            algo, iter_s, salt_hex, hash_hex = stored.split("$")
        except ValueError:
            return False
        if algo != _ALGO:
            return False
        digest = pbkdf2_hmac("sha256", pin.encode("utf-8"),
                             bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(digest.hex(), hash_hex)
