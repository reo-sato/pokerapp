"""output/replay_hand.py

Phase 2-C: EvidenceLog (logs/evidence_<session>.jsonl) を読み戻して、
ハンド単位のイベント窓 (hand window) に分割するユーティリティ群。

ユースケース:
  - オフラインでハンドを再生して、retrospective inference (Phase 3+) の入力に使う
  - テストや診断で「あのハンドに含まれていた events」を取り出す
  - GUI で「過去ハンドを再生」する機能の足場

EvidenceLog は M1 で導入された append-only JSONL で、1 行 1 観測。
本モジュールは:
  1. JSONL を ``list[EvidenceRecord]`` にデシリアライズ
  2. ``HandBoundaryDetector`` を頭から流して boundary を再構築
  3. hand_id ごとに events を窓化して返す
の 3 段階を提供する。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from core.events import ASRAlternative, AudioEvent, CameraEvent, RFIDEvent, WordTiming
from core.hand_boundary import HandBoundaryDetector

logger = logging.getLogger(__name__)


EventPayload = Union[AudioEvent, RFIDEvent, CameraEvent, None]


@dataclass
class EvidenceRecord:
    """1 観測を JSONL から復元したレコード。

    ``event`` は型付き dataclass。再構築不能な未知 kind は ``None`` で保持し、
    ``payload`` 経由で raw dict にアクセスできる (forward compat 用)。
    """

    timestamp: float
    kind: str
    event: EventPayload = None
    payload: dict = field(default_factory=dict)


def _build_audio_event(payload: dict) -> AudioEvent:
    """JSONL から AudioEvent を再構築する。"""
    raw_alts = payload.get("alternatives") or []
    alternatives = []
    for alt in raw_alts:
        if not isinstance(alt, dict):
            continue
        words = [
            WordTiming(
                word=str(w.get("word", "")),
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
                confidence=float(w.get("conf", w.get("confidence", 0.0)) or 0.0),
            )
            for w in (alt.get("words") or [])
            if isinstance(w, dict)
        ]
        alternatives.append(ASRAlternative(
            text=str(alt.get("text", "")),
            confidence=float(alt.get("confidence", 0.0) or 0.0),
            words=words,
        ))
    word_ts = [
        WordTiming(
            word=str(w.get("word", "")),
            start=float(w.get("start", 0.0)),
            end=float(w.get("end", 0.0)),
            confidence=float(w.get("conf", w.get("confidence", 0.0)) or 0.0),
        )
        for w in (payload.get("word_timestamps") or [])
        if isinstance(w, dict)
    ]
    t_end = payload.get("t_end")
    return AudioEvent(
        action=str(payload.get("action", "")),
        amount=int(payload.get("amount", 0) or 0),
        timestamp=float(payload.get("ts", 0.0)),
        raw_text=str(payload.get("raw_text", "")),
        alternatives=alternatives,
        word_timestamps=word_ts,
        t_end=float(t_end) if t_end is not None else None,
    )


def _build_rfid_event(payload: dict) -> RFIDEvent:
    t_end = payload.get("t_end")
    return RFIDEvent(
        tag_id=str(payload.get("tag_id", "")),
        card=str(payload.get("card", "")),
        reader_id=str(payload.get("reader_id", "")),
        role=str(payload.get("role", "")),
        seat=payload.get("seat") if payload.get("seat") is not None else None,
        timestamp=float(payload.get("ts", 0.0)),
        raw_tag_id=str(payload.get("raw_tag_id", payload.get("tag_id", ""))),
        board_index=payload.get("board_index"),
        t_end=float(t_end) if t_end is not None else None,
    )


def _build_camera_event(payload: dict) -> CameraEvent:
    return CameraEvent(
        seat=int(payload.get("seat", 0)),
        timestamp=float(payload.get("ts", 0.0)),
        frame=None,
    )


def load_evidence_log(log_path: Union[str, Path]) -> list[EvidenceRecord]:
    """JSONL を読み込み EvidenceRecord のリストにする。

    "kind" が ``audio`` / ``rfid`` / ``camera`` のものを型付き event に再構築する。
    それ以外 (例: ``beam_snapshot``) は ``event=None`` のままで raw payload を残す。

    壊れた行 (不正 JSON) はログに warning を出して skip する。
    """
    path = Path(log_path)
    if not path.exists():
        logger.warning("evidence log not found: %s", path)
        return []

    records: list[EvidenceRecord] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("evidence log line %d skipped: %s", line_no, e)
                continue
            kind = str(payload.get("kind", ""))
            ts = float(payload.get("ts", 0.0))
            event: EventPayload = None
            try:
                if kind == "audio":
                    event = _build_audio_event(payload)
                elif kind == "rfid":
                    event = _build_rfid_event(payload)
                elif kind == "camera":
                    event = _build_camera_event(payload)
            except (TypeError, ValueError):
                logger.exception("evidence log line %d rebuild failed", line_no)
                event = None
            records.append(EvidenceRecord(
                timestamp=ts, kind=kind, event=event, payload=payload,
            ))
    return records


def extract_hand_windows(
    records: list[EvidenceRecord],
    detector: Optional[HandBoundaryDetector] = None,
) -> dict[int, list[EvidenceRecord]]:
    """EvidenceRecord 列を hand_id ごとの window にグルーピングする。

    detector を頭から流し、start / end boundary を観測しながらレコードを振り分ける。

    挙動:
      - start で current buffer は新規 hand 用にリセットされる (trigger event が
        新 hand の最初の record になる)
      - end でその hand の window が closed されて返り値 dict に確定される
      - ``end → start`` (同イベントが両方を発行) の場合は前 hand を閉じてから
        新 hand を開く (trigger event は新 hand 側に属する)
      - end のみの場合 (winner / board_cleared)、trigger event は閉じる hand 側
      - hand 境界が一度も観測されなかった records は返り値 dict に含まれない
        (= 暗黙の hand 0 / -1 などには入れず捨てる。テストで明示シナリオを通すこと)

    **「end 未観測 hand を除外」の仕様**:
      現在の実装は、start が観測されても対応する end が観測されなければその hand を
      返り値 dict に含めない。これは「open window は不完全であり、settlement が
      確定していない」という意味で安全側に倒した仕様。

      TODO (Phase 3+ 拡張余地):
        - 「end の無い hand を **provisional window** として返す」モードを追加
          (例: ``include_open_hands=True`` フラグ、または別 dict ``open_hands``)
        - これによりセッション最後の未完了 hand や、replay 時の進行中 hand を
          診断的に取り出せるようにする
        - その際の hand_id は detector の current_hand_id をそのまま使い、
          消費側 (HandReconstructor 等) で ``resolution_status="provisional"`` の
          HandSummary を生成する設計が自然
    """
    if detector is None:
        detector = HandBoundaryDetector()

    windows: dict[int, list[EvidenceRecord]] = {}
    current_events: list[EvidenceRecord] = []

    for rec in records:
        boundaries: list = []
        if rec.kind == "audio" and isinstance(rec.event, AudioEvent):
            boundaries = detector.observe_audio_event(rec.event)
        elif rec.kind == "rfid" and isinstance(rec.event, RFIDEvent):
            boundaries = detector.observe_rfid_event(rec.event)
        elif rec.kind == "camera" and isinstance(rec.event, CameraEvent):
            boundaries = detector.observe_camera_event(rec.event)
        # 不明 kind / 再構築失敗 record は素通り (boundary 影響しない)

        has_end = any(b.kind == "end" for b in boundaries)
        has_start = any(b.kind == "start" for b in boundaries)

        if has_end and has_start:
            end_b = next(b for b in boundaries if b.kind == "end")
            windows[end_b.hand_id] = list(current_events)
            current_events = [rec]
        elif has_end:
            end_b = next(b for b in boundaries if b.kind == "end")
            current_events.append(rec)
            windows[end_b.hand_id] = list(current_events)
            current_events = []
        elif has_start:
            current_events = [rec]
        else:
            current_events.append(rec)

    # ハング中の hand (end 未観測) は windows に入れない
    return windows
