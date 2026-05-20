"""gui/tournament_display.py

会場掲示用の Tournament ディスプレイ (別 Toplevel)。

レイアウト:
  ┌──────────────────────────────────────────────────────┐
  │  LEVEL 3              Blinds  300 / 600  (ante 75)   │
  │                                                       │
  │              ┌──────────────────────┐                 │
  │              │      19 : 42         │  ← 残り時間 大  │
  │              └──────────────────────┘                 │
  │                                                       │
  │  NEXT  Level 4: 400 / 800   |   NEXT BREAK in 19:42  │
  │                                                       │
  │  Entries 64   |  Remaining 41  |  Addons 7            │
  │  Avg Stack 31,219  |  Total Chips 1,280,000           │
  │  Late Reg: 39:42 left                                 │
  └──────────────────────────────────────────────────────┘

再描画ポリシー:
  - 残り時間ラベル (大カウントダウン + Late Reg + NEXT BREAK) は 100ms ごとに
    ``tick_time()`` で更新
  - 他のラベル (level 名 / Blinds / Next / Entries / Avg Stack 等) は
    ``update_state()`` を呼んだときだけ、かつ「直前の表示と diff があるとき」
    に限り configure する
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.tournament_state import TournamentState
    from core.tournament_timer import TournamentStructure, TournamentTimer


def format_mmss(sec: float) -> str:
    if sec is None:
        return "--:--"
    sec = max(0, int(sec + 0.5))
    m, s = divmod(sec, 60)
    if m >= 100:
        h, m2 = divmod(m, 60)
        return f"{h:d}:{m2:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_blinds(sb: Optional[int], bb: Optional[int], ante: int) -> str:
    if sb is None or bb is None:
        return "—"
    base = f"{sb:,} / {bb:,}"
    if ante > 0:
        base += f"  (ante {ante:,})"
    return base


class TournamentDisplayWindow:
    """customtkinter.CTkToplevel をホストする会場ディスプレイ。

    Args:
      parent: 親 root (CTk)
      ctk:    customtkinter モジュール (テストで mock するため注入式)
    """

    def __init__(self, parent: object, ctk: object) -> None:
        self._ctk = ctk
        self._cache: dict[str, str] = {}
        self._fullscreen: bool = False

        self._toplevel = ctk.CTkToplevel(parent)
        self._toplevel.title("Tournament Display")
        self._toplevel.geometry("1100x650")
        self._toplevel.protocol("WM_DELETE_WINDOW", self._on_close)
        self._toplevel.bind("<F11>", self._toggle_fullscreen)
        self._toplevel.bind("<Escape>", self._exit_fullscreen)
        self._closed: bool = False

        self._build_ui()

    # ───────── UI 構築 ─────────

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._toplevel
        try:
            root.grid_columnconfigure(0, weight=1)
            root.grid_rowconfigure(1, weight=1)
        except Exception:  # noqa: BLE001
            pass

        # Top bar: LEVEL  /  Blinds
        top = ctk.CTkFrame(root)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 10))
        try:
            top.grid_columnconfigure((0, 1), weight=1)
        except Exception:  # noqa: BLE001
            pass

        self._lbl_level = ctk.CTkLabel(top, text="LEVEL —", font=("", 42, "bold"))
        self._lbl_level.grid(row=0, column=0, sticky="w", padx=12, pady=8)

        self._lbl_blinds = ctk.CTkLabel(top, text="Blinds —", font=("", 32, "bold"))
        self._lbl_blinds.grid(row=0, column=1, sticky="e", padx=12, pady=8)

        # Center: huge countdown
        center = ctk.CTkFrame(root, fg_color="#101010", corner_radius=20)
        center.grid(row=1, column=0, sticky="nsew", padx=40, pady=10)
        try:
            center.grid_columnconfigure(0, weight=1)
            center.grid_rowconfigure(0, weight=1)
        except Exception:  # noqa: BLE001
            pass
        self._lbl_countdown = ctk.CTkLabel(
            center, text="--:--", font=("", 160, "bold"), text_color="#FFD500",
        )
        self._lbl_countdown.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)

        # Next + Next break
        sub = ctk.CTkFrame(root)
        sub.grid(row=2, column=0, sticky="ew", padx=20, pady=6)
        try:
            sub.grid_columnconfigure((0, 1), weight=1)
        except Exception:  # noqa: BLE001
            pass
        self._lbl_next = ctk.CTkLabel(sub, text="NEXT —", font=("", 22))
        self._lbl_next.grid(row=0, column=0, sticky="w", padx=12, pady=6)
        self._lbl_next_break = ctk.CTkLabel(sub, text="NEXT BREAK —", font=("", 22))
        self._lbl_next_break.grid(row=0, column=1, sticky="e", padx=12, pady=6)

        # Stats
        stats = ctk.CTkFrame(root)
        stats.grid(row=3, column=0, sticky="ew", padx=20, pady=(6, 16))
        try:
            stats.grid_columnconfigure((0, 1, 2), weight=1)
        except Exception:  # noqa: BLE001
            pass
        self._lbl_entries = ctk.CTkLabel(stats, text="Entries —", font=("", 22))
        self._lbl_entries.grid(row=0, column=0, sticky="w", padx=12, pady=4)
        self._lbl_remaining = ctk.CTkLabel(stats, text="Remaining —", font=("", 22))
        self._lbl_remaining.grid(row=0, column=1, padx=12, pady=4)
        self._lbl_addons = ctk.CTkLabel(stats, text="Addons —", font=("", 22))
        self._lbl_addons.grid(row=0, column=2, sticky="e", padx=12, pady=4)

        self._lbl_avg = ctk.CTkLabel(stats, text="Avg Stack —", font=("", 22))
        self._lbl_avg.grid(row=1, column=0, sticky="w", padx=12, pady=4)
        self._lbl_total = ctk.CTkLabel(stats, text="Total Chips —", font=("", 22))
        self._lbl_total.grid(row=1, column=1, padx=12, pady=4)
        self._lbl_late_reg = ctk.CTkLabel(stats, text="Late Reg —", font=("", 22))
        self._lbl_late_reg.grid(row=1, column=2, sticky="e", padx=12, pady=4)

    # ───────── 描画 API ─────────

    def tick_time(self, timer: "TournamentTimer") -> None:
        """100ms ごとに残り時間系のみ更新する。"""
        if self._closed:
            return
        self._set_if_changed(self._lbl_countdown, "countdown",
                             format_mmss(timer.remaining_sec()))

        nb = timer.time_to_next_break_sec()
        if nb is None:
            nb_text = "NEXT BREAK: —"
        elif timer.current_level().is_break:
            nb_text = "ON BREAK"
        else:
            nb_text = f"NEXT BREAK in {format_mmss(nb)}"
        self._set_if_changed(self._lbl_next_break, "next_break", nb_text)

        lr = timer.time_to_late_reg_close_sec()
        if lr is None:
            lr_text = "Late Reg: closed"
        else:
            lr_text = f"Late Reg: {format_mmss(lr)} left"
        self._set_if_changed(self._lbl_late_reg, "late_reg", lr_text)

    def update_state(
        self,
        timer: "TournamentTimer",
        state: "TournamentState",
        structure: "TournamentStructure",
    ) -> None:
        """値変化系を diff 描画する (= 1 秒に 1 回 + イベント時のみ呼ぶ)。"""
        if self._closed:
            return
        from core.tournament_state import compute_avg_stack, compute_total_chips

        cur = timer.current_level()
        nxt = timer.next_level()

        # Level label & blinds line
        if cur.is_break:
            level_text = "BREAK"
            # 直前 level の blinds を併記する (= 「再開時の blinds」)
            prev_blinds = self._last_active_blinds_before(timer)
            if prev_blinds is None:
                blinds_text = "BREAK"
            else:
                psb, pbb, pan = prev_blinds
                blinds_text = f"BREAK (blinds remain {format_blinds(psb, pbb, pan)})"
        else:
            level_text = f"LEVEL {cur.level}" if cur.level is not None else cur.label.upper()
            blinds_text = f"Blinds  {format_blinds(cur.sb, cur.bb, cur.ante)}"
        self._set_if_changed(self._lbl_level, "level", level_text)
        self._set_if_changed(self._lbl_blinds, "blinds", blinds_text)

        # Next
        if nxt is None:
            next_text = "NEXT —"
        elif nxt.is_break:
            next_text = f"NEXT  {nxt.label}"
        else:
            next_text = f"NEXT  Level {nxt.level}: {format_blinds(nxt.sb, nxt.bb, nxt.ante)}"
        self._set_if_changed(self._lbl_next, "next", next_text)

        # Stats
        self._set_if_changed(self._lbl_entries,   "entries",   f"Entries {state.entries:,}")
        self._set_if_changed(self._lbl_remaining, "remaining", f"Remaining {state.players_remaining:,}")
        self._set_if_changed(self._lbl_addons,    "addons",    f"Addons {state.addons:,}")
        self._set_if_changed(self._lbl_avg,       "avg",       f"Avg Stack {compute_avg_stack(state, structure):,}")
        self._set_if_changed(self._lbl_total,     "total",     f"Total Chips {compute_total_chips(state, structure):,}")

    # ───────── helpers ─────────

    def _set_if_changed(self, label: object, key: str, text: str) -> None:
        if self._cache.get(key) == text:
            return
        self._cache[key] = text
        try:
            label.configure(text=text)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _last_active_blinds_before(timer: "TournamentTimer"):
        levels = timer.structure.levels
        idx = levels.index(timer.current_level())
        for i in range(idx - 1, -1, -1):
            lv = levels[i]
            if not lv.is_break and lv.sb is not None and lv.bb is not None:
                return lv.sb, lv.bb, lv.ante
        return None

    def _toggle_fullscreen(self, _event: object = None) -> str | None:
        self._fullscreen = not self._fullscreen
        try:
            self._toplevel.attributes("-fullscreen", self._fullscreen)
        except Exception:  # noqa: BLE001
            pass
        return "break"

    def _exit_fullscreen(self, _event: object = None) -> str | None:
        if self._fullscreen:
            self._fullscreen = False
            try:
                self._toplevel.attributes("-fullscreen", False)
            except Exception:  # noqa: BLE001
                pass
        return "break"

    def _on_close(self) -> None:
        self._closed = True
        try:
            self._toplevel.destroy()
        except Exception:  # noqa: BLE001
            pass

    @property
    def is_closed(self) -> bool:
        return self._closed
