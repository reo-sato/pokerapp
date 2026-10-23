"""rfid/card_master.py

RFID タグ ID → ポーカーカード文字列 の対応表を管理する。

ファイル形式 (rfid_cards.json):
    {
      "description": "...",
      "cards": {
        "04:AB:CD:EF:12:34": "Ah",
        "04:AB:CD:EF:12:35": "Kd"
      }
    }

タグ ID の正規化: 大文字 + コロン区切り 16 進数。
  例: b"\\x04\\xab\\xcd\\xef\\x12\\x34" → "04:AB:CD:EF:12:34"
      "04abcdef1234"                    → "04:AB:CD:EF:12:34"
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

# 有効なポーカーカード文字列の正規表現
_CARD_RE = re.compile(r"^[2-9TJQKA][cdhs]$")

# 54枚（標準52 + ジョーカー2枚）の有効カードセット
_RANKS = "23456789TJQKA"
_SUITS = "cdhs"
VALID_CARDS: frozenset[str] = frozenset(
    r + s for r in _RANKS for s in _SUITS
) | frozenset(["Jk", "JK"])


class CardMaster:
    """RFID タグ ID → ポーカーカード文字列 の対応表。

    スレッドセーフではない（RFIDThread から単一スレッドで使用するため不要）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._mapping: dict[str, str] = {}
        self._load()

    # ――― 公開 API ―――

    def lookup(self, tag_id: str) -> str:
        """正規化済みタグ ID からカード文字列を返す。未登録時は空文字。"""
        return self._mapping.get(normalize_tag_id(tag_id), "")

    def lookup_bytes(self, uid_bytes: bytes) -> str:
        """bytes 形式のタグ UID からカード文字列を返す。"""
        return self.lookup(bytes_to_tag_id(uid_bytes))

    def register(self, tag_id: str, card: str) -> None:
        """タグとカードを登録してファイルに保存する。

        Raises:
            ValueError: card が無効なカード文字列の場合
        """
        if card not in VALID_CARDS:
            raise ValueError(f"Invalid card string: {card!r}. Must be like 'Ah', 'Kd', '2c'.")
        norm = normalize_tag_id(tag_id)
        self._mapping[norm] = card
        self._save()
        logger.info("Registered tag %s → %s", norm, card)

    def unregister(self, tag_id: str) -> bool:
        """タグの登録を解除する。登録されていた場合 True を返す。"""
        norm = normalize_tag_id(tag_id)
        if norm in self._mapping:
            del self._mapping[norm]
            self._save()
            logger.info("Unregistered tag %s", norm)
            return True
        return False

    def all_entries(self) -> dict[str, str]:
        """登録されている全タグのコピーを返す（tag_id → card）。"""
        return dict(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    # ――― 永続化 ―――

    def _load(self) -> None:
        if not self._path.exists():
            logger.info("Card master not found at %s, starting empty.", self._path)
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("cards", {})
            loaded = 0
            for tag_id, card in raw.items():
                norm = normalize_tag_id(tag_id)
                if card in VALID_CARDS:
                    self._mapping[norm] = card
                    loaded += 1
                else:
                    logger.warning("Skipping invalid card %r for tag %s", card, norm)
            logger.info("CardMaster loaded %d entries from %s", loaded, self._path)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load card master: %s", e)

    def _save(self) -> None:
        data = {
            "description": "RFID tag to poker card mapping. "
                           "Register each physical card with its NFC tag UID "
                           "(python tools/register_cards.py run --deck N).",
            "cards": self._mapping,
        }
        try:
            atomic_write_json(self._path, data)
        except OSError:
            logger.exception("Failed to save card master to %s", self._path)


# ――― タグ ID ユーティリティ ―――

def normalize_tag_id(tag_id: str) -> str:
    """タグ ID を大文字コロン区切り 16 進数に正規化する。

    入力形式の例:
        "04:ab:cd:ef:12:34"  →  "04:AB:CD:EF:12:34"
        "04abcdef1234"       →  "04:AB:CD:EF:12:34"
        "04 AB CD EF 12 34"  →  "04:AB:CD:EF:12:34"
    """
    # 区切り文字を除去して純粋な 16 進数文字列に
    cleaned = re.sub(r"[^0-9a-fA-F]", "", tag_id)
    if len(cleaned) % 2 != 0:
        cleaned = "0" + cleaned  # 奇数桁の補正
    pairs = [cleaned[i:i+2].upper() for i in range(0, len(cleaned), 2)]
    return ":".join(pairs)


def bytes_to_tag_id(uid_bytes: bytes) -> str:
    """bytes 形式の UID を正規化済みタグ ID に変換する。"""
    return ":".join(f"{b:02X}" for b in uid_bytes)
