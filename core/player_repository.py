"""core/player_repository.py

Phase S1: player の永続化と CRUD（create / list / rename）を担うリポジトリ。

永続化は JsonWriter と同じスタイルの単一 JSON ファイル + アトミックリネーム。
形式:
    {"players": [{"player_id": "...", "display_name": "...", "created_at": "..."}]}

業務ルール（validation）はこのリポジトリが source of truth として保持する:
  - 空文字・前後空白のみの display_name は不可
  - 完全一致（前後空白除去後）の display_name 重複は不可
  - rename 時も同じ validation を適用（自分自身との一致は許容）

現時点では player 削除 / merge は scope 外（CLAUDE.md § Future Scope 参照）。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from core.player import Player

logger = logging.getLogger(__name__)

_DEFAULT_PLAYER_DB = Path(__file__).parent.parent / "players.json"


class PlayerValidationError(Exception):
    """display_name の validation 失敗の基底例外。"""


class EmptyDisplayNameError(PlayerValidationError):
    """display_name が空、または前後空白のみ。"""


class DuplicateDisplayNameError(PlayerValidationError):
    """display_name が既存 player と完全一致。"""


class PlayerNotFoundError(PlayerValidationError):
    """指定された player_id が存在しない。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PlayerRepository:
    """player の永続ストア。アプリ再起動を跨いで player_id が安定する。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_PLAYER_DB
        self._players: dict[str, Player] = {}
        self._load()

    # ――― 永続化 ―――

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load player DB (%s), starting empty.", e)
            return
        for raw in data.get("players", []):
            try:
                player = Player.from_dict(raw)
            except (KeyError, TypeError):
                logger.warning("Skipping malformed player record: %r", raw)
                continue
            self._players[player.player_id] = player

    def _flush(self) -> None:
        """JsonWriter と同じくアトミックリネームで書き込む。失敗してもクラッシュしない。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        data = {"players": [p.to_dict() for p in self._players.values()]}
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            logger.exception("Failed to write player DB: %s", self._path)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    # ――― validation ―――

    def _validate_name(self, raw: str | None, exclude_id: str | None) -> str:
        """display_name を検証し、正規化（前後空白除去）した文字列を返す。

        exclude_id を指定すると、その player 自身との重複は許容する（rename 用）。
        """
        if raw is None:
            raise EmptyDisplayNameError("display_name が空です。")
        name = raw.strip()
        if not name:
            raise EmptyDisplayNameError("display_name が空です。")
        for player in self._players.values():
            if player.player_id == exclude_id:
                continue
            if player.display_name == name:
                raise DuplicateDisplayNameError(f"「{name}」は既に存在します。")
        return name

    # ――― CRUD ―――

    def list_players(self) -> list[Player]:
        """全 player を作成時刻 → display_name 順で返す。"""
        return sorted(
            self._players.values(),
            key=lambda p: (p.created_at, p.display_name),
        )

    def get(self, player_id: str) -> Player:
        if player_id not in self._players:
            raise PlayerNotFoundError(f"player_id={player_id} は存在しません。")
        return self._players[player_id]

    def create_player(self, display_name: str) -> Player:
        name = self._validate_name(display_name, exclude_id=None)
        player_id = uuid.uuid4().hex
        player = Player(player_id=player_id, display_name=name, created_at=_now_iso())
        self._players[player_id] = player
        self._flush()
        logger.info("Created player %s (%s)", player_id, name)
        return player

    def rename_player(self, player_id: str, new_display_name: str) -> Player:
        if player_id not in self._players:
            raise PlayerNotFoundError(f"player_id={player_id} は存在しません。")
        name = self._validate_name(new_display_name, exclude_id=player_id)
        player = self._players[player_id]
        player.display_name = name
        self._flush()
        logger.info("Renamed player %s to %s", player_id, name)
        return player
