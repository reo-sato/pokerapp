"""audio/speech_normalizer.py

音声認識後処理の正規化モジュール。

生の ASR テキストを受け取り、action_aliases / seat_aliases を用いて
正規化テキスト・アクション候補・席番号を抽出する。

使い方:
    # アプリ起動時 (main.py など) に一度だけ初期化する
    from audio.speech_normalizer import init_normalizer
    init_normalizer("./speech_normalization.json")

    # 認識ループ内（parse_action 経由で自動適用される）
    from audio.speech_normalizer import normalize
    result = normalize(raw_text)

辞書ファイルの追記は再起動なしで反映されない（現仕様）。
将来的に hot-reload が必要になった場合は mtime 監視を追加すること。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class NormalizedResult:
    """speech_normalizer.normalize() の返り値。"""

    raw_text: str
    normalized_text: str
    action: Optional[str]          # 大文字正規形: "BET" / "RAISE" / "CALL" / "CHECK" / "ALLIN" / None
    seat: Optional[int]            # 正規化で抽出できた席番号、またはNone
    tokens: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)  # e.g. ["ベッド->BET"]


class SpeechNormalizer:
    """speech_normalization.json を読み込んで ASR テキストを正規化するクラス。

    正規化の順序:
      1. seat_aliases でシート番号表現を統一 ("二番" → "シート2")
      2. action_aliases でアクション表現を統一 ("ベッド" → "BET")
      3. 将来: number_aliases / phrase_patterns / context validation を追加予定

    マッチングはすべてサブストリング・最長一致（左優先）で行う。
    辞書にない語はそのまま残す。
    """

    def __init__(self, normalization_path: str) -> None:
        self._path = normalization_path
        self._action_aliases: dict[str, str] = {}
        self._seat_aliases: dict[str, int] = {}
        self._action_keys: list[str] = []   # 長さ降順ソート済み
        self._seat_keys: list[str] = []     # 長さ降順ソート済み
        self._load()

    def _load(self) -> None:
        """JSON ファイルを読み込む。失敗した場合は空辞書で継続する。"""
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.warning(
                "Speech normalization file not found: %r (continuing with empty table)",
                self._path,
            )
            return
        except Exception:
            logger.exception(
                "Failed to load speech normalization file: %r (continuing with empty table)",
                self._path,
            )
            return

        raw_action: dict = data.get("action_aliases", {})
        raw_seat: dict = data.get("seat_aliases", {})

        # 型チェック: 壊れた辞書のエントリを除外して起動を継続する
        self._action_aliases = {
            k: v for k, v in raw_action.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        self._seat_aliases = {
            k: int(v) for k, v in raw_seat.items()
            if isinstance(k, str) and str(v).lstrip("-").isdigit()
        }

        # 長い順に並べることで部分マッチの衝突を防ぐ
        self._action_keys = sorted(self._action_aliases, key=len, reverse=True)
        self._seat_keys = sorted(self._seat_aliases, key=len, reverse=True)

        logger.info(
            "SpeechNormalizer: loaded %d action_aliases, %d seat_aliases from %r",
            len(self._action_aliases), len(self._seat_aliases), self._path,
        )

    def normalize(self, raw_text: str) -> NormalizedResult:
        """raw_text を正規化して NormalizedResult を返す。

        - 辞書にない語はそのまま残す
        - 複数のアクション候補が見つかった場合は最左優先で採用し、WARNING を出す
        - マッチしたルールは matched_rules に記録する
        """
        text = raw_text
        matched_rules: list[str] = []
        seat: Optional[int] = None
        action_candidates: list[tuple[int, str]] = []  # (position, action_name)

        # ── 1. seat_aliases ──────────────────────────────────────────────────
        for key in self._seat_keys:
            pos = text.find(key)
            if pos != -1:
                seat_val = self._seat_aliases[key]
                seat = seat_val
                replacement = f"シート{seat_val}"
                text = text[:pos] + replacement + text[pos + len(key):]
                matched_rules.append(f"{key}->seat:{seat_val}")
                break  # 1発話に席番号は1つと仮定

        # ── 2. action_aliases ────────────────────────────────────────────────
        # 元テキスト上での最左位置を記録し、後でアクション候補を決定する
        # 置換は記録後に一括で行う（複数キーが同テキストにマッチする場合に位置がずれないよう注意）
        pending_replacements: list[tuple[int, int, str, str]] = []  # (start, end, key, alias)
        for key in self._action_keys:
            pos = text.find(key)
            if pos != -1:
                alias = self._action_aliases[key]
                action_candidates.append((pos, alias))
                pending_replacements.append((pos, pos + len(key), key, alias))

        # 位置の重複を排除しながら左から右に置換する
        pending_replacements.sort(key=lambda x: x[0])
        result_chars: list[str] = []
        cursor = 0
        used_positions: set[int] = set()
        for start, end, key, alias in pending_replacements:
            if start in used_positions or any(start <= p < end for p in used_positions):
                continue
            result_chars.append(text[cursor:start])
            result_chars.append(alias)
            cursor = end
            matched_rules.append(f"{key}->{alias}")
            for p in range(start, end):
                used_positions.add(p)
        result_chars.append(text[cursor:])
        text = "".join(result_chars)

        # 最左位置のアクションを採用
        action: Optional[str] = None
        if action_candidates:
            action_candidates.sort(key=lambda x: x[0])
            unique_actions = list(dict.fromkeys(a for _, a in action_candidates))
            if len(unique_actions) > 1:
                logger.warning(
                    "SpeechNormalizer: multiple action candidates in %r: %s (using leftmost: %s)",
                    raw_text, unique_actions, unique_actions[0],
                )
            action = unique_actions[0]

        result = NormalizedResult(
            raw_text=raw_text,
            normalized_text=text,
            action=action,
            seat=seat,
            tokens=text.split(),
            matched_rules=matched_rules,
        )

        if matched_rules:
            logger.info(
                "Audio normalize: raw=%r normalized=%r action=%s seat=%s rules=%s",
                raw_text, text, action, seat, matched_rules,
            )
        else:
            logger.debug("Audio normalize: raw=%r (no rules matched)", raw_text)

        return result

    # ── 将来拡張用スタブ ──────────────────────────────────────────────────────

    def normalize_with_context(self, raw_text: str, game_state=None) -> NormalizedResult:
        """コンテキスト付き正規化（将来: ゲームステートと組み合わせる）。

        現時点では normalize() へのパススルー。
        game_state が渡された場合、現在有効なアクションのみを候補として絞り込む拡張を
        ここに追加することを想定している。
        """
        return self.normalize(raw_text)


# ── モジュールレベルシングルトン ────────────────────────────────────────────────

_normalizer: Optional[SpeechNormalizer] = None


def init_normalizer(path: str) -> None:
    """アプリ起動時に一度だけ呼び出してモジュールレベルノーマライザを初期化する。"""
    global _normalizer
    _normalizer = SpeechNormalizer(path)


def normalize(text: str) -> NormalizedResult:
    """モジュールレベルのノーマライザで text を正規化する。

    init_normalizer() が呼ばれていない場合は no-op の結果を返す。
    """
    if _normalizer is not None:
        return _normalizer.normalize(text)
    return NormalizedResult(
        raw_text=text,
        normalized_text=text,
        action=None,
        seat=None,
        tokens=text.split(),
        matched_rules=[],
    )
