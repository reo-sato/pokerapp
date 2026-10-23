"""cloud/views.py — 店舗ごとの読み取りの写し（ADR-0067 2c）。

お客さんの読み取りは、店内と**同じ**読み取りの中身（`api/read_models.py`）と repository（会計の集計
`compute_settlement`・merge した player の解決など）で作る。そのために、クラウドに保存したレコードを
店舗 PC と同じ形のファイル（players.json / sessions.json / ledger.json / hand_corrections.json /
logs/<session>.json）に書き出し、そこから repository を作る。

- 写しは店舗ごとに 1 つ。店舗 PC から何か届いて店舗の番号（`stores.last_seq`）が変わったら作り直す。
- ハンドはセッションを開いたときに、そのセッションの分だけ書き出す。
- 置き場は一時ディレクトリ（Cloud Run ではメモリ）。消えても次の要求で作り直すだけ。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional

from cloud.storage import Storage
from core.hand_correction_repository import HandCorrectionRepository
from core.ledger_repository import LedgerRepository
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class StoreView:
    def __init__(self, storage: Storage, store_id: str, seq: Optional[int], root: Path) -> None:
        self.storage, self.store_id, self.seq = storage, store_id, seq
        self.dir = root / store_id / str(seq or 0)
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.log_dir = self.dir / "logs"
        self.log_dir.mkdir(parents=True)
        recs = lambda kind: storage.list_records(store_id, kind)  # noqa: E731
        _write(self.dir / "players.json", {"players": recs("players")})
        _write(self.dir / "sessions.json", {"sessions": recs("sessions")})
        _write(self.dir / "ledger.json", {"ledger_entries": recs("ledger_entries"),
                                          "point_ledger_entries": recs("point_entries"),
                                          "settlements": recs("settlements")})
        _write(self.dir / "hand_corrections.json", {"corrections": recs("corrections")})
        settings = storage.get_record(store_id, "store_settings", "settings") or {}
        self.settings = settings
        self.players = PlayerRepository(self.dir / "players.json")
        self.sessions = SessionRepository(self.dir / "sessions.json", player_repo=self.players)
        self.ledger = LedgerRepository(self.dir / "ledger.json", session_repo=self.sessions,
                                       player_repo=self.players)
        self.corrections = HandCorrectionRepository(self.dir / "hand_corrections.json")
        self._hands_lock = threading.Lock()
        self._hands_written: set[str] = set()

    def ensure_hands(self, session_id: str) -> None:
        """そのセッションのハンドを logs/<session>.json に書き出す（まだなら）。"""
        with self._hands_lock:
            if session_id in self._hands_written:
                return
            hands = self.storage.list_records(self.store_id, "hands", session_id=session_id)
            hands.sort(key=lambda h: h.get("hand_id", 0))
            if hands and "/" not in session_id and "\\" not in session_id and not session_id.startswith("."):
                _write(self.log_dir / f"{session_id}.json", {"session_id": session_id, "hands": hands})
            self._hands_written.add(session_id)

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class ViewCache:
    """店舗ごとの最新の写し。店舗の番号が変わっていれば作り直す。"""

    def __init__(self, storage: Storage, root: Optional[Path] = None) -> None:
        self.storage = storage
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="pokerapp-views-"))
        self._views: dict[str, StoreView] = {}
        self._retired: dict[str, StoreView] = {}   # 1 つ前の写し（読み取りの途中で消さないよう 1 回待ってから消す）
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, store_id: str) -> StoreView:
        store = self.storage.get_store(store_id)
        seq = store.get("last_seq") if store else None
        with self._lock:
            lock = self._locks.setdefault(store_id, threading.Lock())
        with lock:
            view = self._views.get(store_id)
            if view is None or view.seq != seq:
                if view is not None:
                    older = self._retired.pop(store_id, None)
                    if older is not None:
                        older.close()
                    self._retired[store_id] = view
                view = StoreView(self.storage, store_id, seq, self.root)
                self._views[store_id] = view
            return view
