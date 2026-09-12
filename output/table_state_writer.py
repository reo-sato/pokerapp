"""output/table_state_writer.py

**RFID だけから導く卓状態**（`core/table_state.TableState`）をディスクへ publish する sidecar。

2 つを書く:

- `logs/{session_id}.table_state.json` — **最新スナップショット**（アトミック上書き）。
  UI（`tools/table_monitor.py`）はこれを読む。別プロセスから reload-on-read で読む前提
  （単一書き手 = hand logger プロセス, ADR-0020 の型）。
- `logs/{session_id}.table_state.jsonl` — **append-only の履歴**。実プレイ環境での検証用で、
  「いつ観測して / いつ書いたか」を残す。後から**反映遅延**と**不在時間の分布**を測れる
  （ADR-0045 D7 の計測 #2/#3 の入力になる）。

書き込み失敗でハンドロガーを止めない（記録の継続を優先。ログに残して続行する）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from core.atomic_io import atomic_write_json
from core.table_state import TableState

logger = logging.getLogger(__name__)


def _material(payload: dict) -> dict:
    """履歴の重複判定に使う「実質的な状態」（時刻と経過秒数を除いたもの）。

    定期 publish では `updated_at` と `away_sec` だけが動くので、それらを落として比較する。
    """
    out = {k: v for k, v in payload.items() if k != "updated_at"}
    out["seats"] = [
        {k: v for k, v in seat.items() if k != "away_sec"} for seat in payload.get("seats", [])
    ]
    return out


class TableStateWriter:
    """卓状態スナップショット + 履歴の書き出し。"""

    def __init__(
        self, log_dir: str | Path, session_id: str, *, history: bool = True
    ) -> None:
        self._dir = Path(log_dir)
        self._session_id = session_id
        self._history = history
        self._snapshot_path = self._dir / f"{session_id}.table_state.json"
        self._history_path = self._dir / f"{session_id}.table_state.jsonl"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._failed = False   # 一度失敗したら警告は 1 回だけ（ログを溢れさせない）
        self._last_material: Optional[dict] = None

    @property
    def snapshot_path(self) -> Path:
        return self._snapshot_path

    @property
    def history_path(self) -> Path:
        return self._history_path

    def publish(self, state: TableState, *, observed_at: Optional[float] = None) -> None:
        """スナップショットを上書きし、履歴に 1 行 append する。

        Args:
            observed_at: この更新の**きっかけになった観測**の時刻（epoch）。反映遅延を
                         後から測るために履歴へ残す（RFID イベントの `timestamp`）。
        """
        payload = state.to_dict()
        material = _material(payload)
        changed = material != self._last_material
        try:
            atomic_write_json(self._snapshot_path, payload)
            # 履歴は**実質的に変わったときだけ** append する。定期 publish のたびに書くと
            # 経過秒数の差分だけで膨らみ、後から遅延や不在時間を読み取りにくくなる。
            if self._history and changed:
                line = dict(payload)
                line["observed_at"] = observed_at
                with self._history_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._last_material = material
            self._failed = False
        except OSError:
            if not self._failed:
                logger.exception(
                    "卓状態の書き出しに失敗しました（%s）— ハンドの記録は続行します",
                    self._snapshot_path,
                )
                self._failed = True
