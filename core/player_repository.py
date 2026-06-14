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


class PlayerMergeError(PlayerValidationError):
    """player merge の不正（自己 merge / サイクル等, ADR-0030）。code=invalid_merge。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PlayerRepository:
    """player の永続ストア。アプリ再起動を跨いで player_id が安定する。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_PLAYER_DB
        self._players: dict[str, Player] = {}
        self._load()

    @property
    def path(self) -> Path:
        """この repository の永続ファイルパス（sync が snapshot/merge 対象を特定する用）。"""
        return self._path

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

    def reload(self) -> None:
        """ディスクから再読込する（read-only viewer が外部更新を取り込む用）。

        別プロセス（hand logger 等）が `players.json` を更新した場合に最新状態を取り込む。
        validation などの業務ルールには影響しない、純粋な再読込のみ。
        """
        self._players.clear()
        self._load()

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

    def list_players(self, include_merged: bool = False) -> list[Player]:
        """player を作成時刻 → display_name 順で返す。

        既定では merge で吸収された tombstone（`merged_into!=None`）を除外する（ADR-0030）。
        `include_merged=True` で tombstone も含める（merge 管理 UI 用）。
        """
        players = self._players.values()
        if not include_merged:
            players = [p for p in players if p.merged_into is None]
        return sorted(players, key=lambda p: (p.created_at, p.display_name))

    def get(self, player_id: str) -> Player:
        if player_id not in self._players:
            raise PlayerNotFoundError(f"player_id={player_id} は存在しません。")
        return self._players[player_id]

    # ――― player merge（ADR-0030: alias / tombstone + read-time canonicalization）―――

    def resolve_canonical(self, player_id: str) -> str:
        """merge チェーンを辿って survivor の player_id を返す（ADR-0030）。

        未 merge / 未知 ID はそのまま返す（read は lenient）。サイクル・過大深度はガードして
        現時点の解決先を返す（クラッシュさせない）。
        """
        seen: set[str] = set()
        current = player_id
        for _ in range(64):
            p = self._players.get(current)
            if p is None or p.merged_into is None:
                return current
            if current in seen:
                logger.warning("merge cycle detected resolving %s", player_id)
                return current
            seen.add(current)
            current = p.merged_into
        logger.warning("merge chain too deep resolving %s", player_id)
        return current

    def equivalence_class(self, player_id: str) -> set[str]:
        """`player_id` と同一 canonical に解決される全 player_id 集合（survivor + 全 absorbed）。

        歴史的レコード（seat_assignment / ledger 等）が absorbed の旧 ID で残っていても、read 側が
        この集合で突合すれば survivor の視点で漏れなく拾える（ADR-0030 D2）。
        """
        canonical = self.resolve_canonical(player_id)
        cls = {canonical, player_id}
        for pid in self._players:
            if self.resolve_canonical(pid) == canonical:
                cls.add(pid)
        return cls

    def merge_players(self, survivor_id: str, absorbed_id: str) -> Player:
        """`absorbed_id` を `survivor_id` に統合する（alias/tombstone, ADR-0030）。

        履歴は書き換えず、absorbed に `merged_into=survivor` を付けるのみ（read で canonicalize）。
        survivor 自身が tombstone のときはその canonical を実 survivor にする。自己 merge /
        サイクルは PlayerMergeError。同一 survivor への再 merge は冪等。
        """
        if survivor_id not in self._players:
            raise PlayerNotFoundError(f"survivor player_id={survivor_id} は存在しません。")
        if absorbed_id not in self._players:
            raise PlayerNotFoundError(f"absorbed player_id={absorbed_id} は存在しません。")
        if survivor_id == absorbed_id:
            raise PlayerMergeError("survivor と absorbed が同一 player_id です。")
        canonical_survivor = self.resolve_canonical(survivor_id)
        if canonical_survivor == absorbed_id:
            raise PlayerMergeError(
                "absorbed が survivor の canonical です（merge でサイクルになります）。"
            )
        absorbed = self._players[absorbed_id]
        if absorbed.merged_into == canonical_survivor:
            return absorbed  # 冪等（既に同一 survivor へ統合済み）
        absorbed.merged_into = canonical_survivor
        absorbed.merged_at = _now_iso()
        self._flush()
        logger.info("Merged player %s into %s", absorbed_id, canonical_survivor)
        return absorbed

    def unmerge(self, player_id: str) -> Player:
        """merge を取り消す（`merged_into` を除去, ADR-0030 D4。破壊していないので可逆）。"""
        if player_id not in self._players:
            raise PlayerNotFoundError(f"player_id={player_id} は存在しません。")
        player = self._players[player_id]
        player.merged_into = None
        player.merged_at = None
        self._flush()
        logger.info("Unmerged player %s", player_id)
        return player

    def create_player(self, display_name: str) -> Player:
        name = self._validate_name(display_name, exclude_id=None)
        player_id = uuid.uuid4().hex
        now = _now_iso()
        player = Player(player_id=player_id, display_name=name,
                        created_at=now, updated_at=now)
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
        # rename を sync 伝播させるため updated_at を更新する（LWW, ADR-0032）。
        player.updated_at = _now_iso()
        self._flush()
        logger.info("Renamed player %s to %s", player_id, name)
        return player
