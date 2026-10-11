"""Always-on-top overlay: in game, Esc > Export Route Data to Clipboard, and it shows what to change.

    pythonw -m atd.overlay [--save NAME]

Reads only the clipboard, and only text that is a Bering Tonnage route export.
Never touches the game. Your next export confirms which rules you've entered.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:
    # Started as a plain file (double-click, `python overlay.py`): rerun as part of the package.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from atd.overlay import main
    sys.exit(main())

import argparse
import ctypes
import json
import os
import tkinter as tk
from pathlib import Path

from .advisor import load_settings
from . import nudges
from .export_parser import parse_export
from .planner import is_other_game, load_record, make_plan, record_meta, save_record
from .routing import plan_routes
from .model import ONWARDS_PORTS, THIS_ROUTE
from .rules import collectors_of, existing_rules, merge, render, rule_destinations, stop_locations

PROJECT = Path(__file__).resolve().parent.parent
POLL_MS = 700
FOCUS_POLL_MS = 250
STARTUP_SHOW_MS = 3000
GAME_EXE = "bering-tonnage.exe"
EXPORT_MARK = '"Bering Tonnage route data"'

BG, FG, DIM, ACCENT, OK, WARN = "#14202b", "#e6edf3", "#7d8b99", "#5aa9e6", "#5cc98b", "#e6b450"


class Overlay:
    def __init__(self, save: str):
        self.save = save
        self.record_path = PROJECT / "records" / f"{save}.json"
        self.settings = load_settings(PROJECT / "atd.toml")
        self.last_clip = None
        self.last_data = None
        self.adopt_next = False
        self.collapsed = False

        self.root = root = tk.Tk()
        root.title("Auto-Transfer Depots")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.92)
        root.configure(bg=BG)
        w = 440
        root.geometry(f"{w}x320+{root.winfo_screenwidth() - w - 24}+80")

        bar = tk.Frame(root, bg="#0d151d")
        bar.pack(fill="x")
        self.title = tk.Label(bar, text="Depots", bg="#0d151d", fg=FG, font=("Segoe UI Semibold", 10))
        self.title.pack(side="left", padx=8, pady=4)
        self.status = tk.Label(bar, text="waiting for export", bg="#0d151d", fg=DIM, font=("Segoe UI", 9))
        self.status.pack(side="left")
        for txt, cmd in (("×", root.destroy), ("–", self.toggle)):
            b = tk.Label(bar, text=txt, bg="#0d151d", fg=DIM, font=("Segoe UI", 11), cursor="hand2", padx=6)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())
        for widget in (bar, self.title, self.status):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag)

        self.text = tk.Text(root, bg=BG, fg=FG, bd=0, wrap="word", padx=10, pady=6,
                            font=("Segoe UI", 9), cursor="arrow", highlightthickness=0)
        self.text.pack(fill="both", expand=True)
        for tag, opts in {
            "h": {"font": ("Segoe UI Semibold", 9), "foreground": ACCENT, "spacing1": 6},
            "add": {"lmargin1": 12, "lmargin2": 24},
            "remove": {"lmargin1": 12, "lmargin2": 24, "foreground": WARN},
            "done": {"overstrike": True, "foreground": DIM},
            "ok": {"foreground": OK, "font": ("Segoe UI Semibold", 10)},
            "okline": {"foreground": OK},
            "dim": {"foreground": DIM},
            "gloss": {"foreground": DIM, "font": ("Segoe UI", 8)},
            "link": {"foreground": ACCENT, "underline": True},
            "warn": {"foreground": WARN},
        }.items():
            self.text.tag_configure(tag, **opts)
        self.text.tag_bind("item", "<Button-1>", self._tick)
        self.text.tag_bind("link", "<Button-1>", lambda e: self.adopt())
        self.text.tag_bind("link", "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind("link", "<Leave>", lambda e: self.text.config(cursor="arrow"))
        self._show([("Esc → Export Route Data to Clipboard\nto check your depots.", "dim")])

        root.after(200, self.poll)
        if self.settings["overlay_only_with_game"]:
            self.visible = True
            # Stay visible briefly at start so you can see it launched, then follow the game.
            root.after(STARTUP_SHOW_MS, self.follow_game)

    # ------------------------------------------------------------------ visibility

    def follow_game(self):
        """Show only while Bering Tonnage (or this overlay) is the foreground window."""
        want = _foreground_is_game()
        if want and not self.visible:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        elif not want and self.visible:
            self.root.withdraw()
        self.visible = want
        self.root.after(FOCUS_POLL_MS, self.follow_game)

    # ------------------------------------------------------------------ clipboard

    def poll(self):
        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            clip = None  # empty or not text (e.g. a screenshot)
        if clip and clip != self.last_clip and EXPORT_MARK in clip[:400]:
            self.last_clip = clip
            try:
                self.update(json.loads(clip))
            except Exception as e:  # keep running whatever the export holds
                self.status.config(text="couldn't read export", fg=WARN)
                self._show([(f"{type(e).__name__}: {e}", "warn")])
        self.root.after(POLL_MS, self.poll)

    # ------------------------------------------------------------------ planning

    def adopt(self):
        """One-time cleanup: treat every rule in the game as ours (except the default Load rule)."""
        if self.last_data is not None:
            self.adopt_next = True
            self.update(self.last_data)

    def update(self, data: dict):
        self.last_data = data
        adopt, self.adopt_next = self.adopt_next, False
        export = parse_export(data)
        net = export.network
        s = self.settings
        dests = {d for cargo in export.ship_cargo.values() for d in cargo}
        dests |= {d for dep in net.depots for d in dep.stored_by_destination}
        routing = plan_routes(net, s["max_transfers"], s["disabled_routes"], s["disabled_depots"],
                              extra_destinations=dests, direct_first=not s["compact"])
        new_game_note = None
        if is_other_game(record_meta(self.record_path), export.meta):
            old = record_meta(self.record_path)
            kept = self.record_path.with_name(f"{self.save}.day{old.get('game_day', 'x')}.old.json")
            self.record_path.replace(kept)
            new_game_note = (f"New game or older save detected: started a fresh memory file "
                             f"(the old one is kept as records/{kept.name})")
        plan = make_plan(routing, net, load_record(self.record_path), s["auto_transfer"],
                         set(s["disabled_routes"]), s["compact"], adopt=adopt)
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        save_record(self.record_path, plan.record, {"save": self.save, "release": export.release,
                                                     "game_day": export.meta.get("day"),
                                                     "start_region": export.meta.get("start_region")})

        names = {r.id: r.name or r.id for r in net.routes}
        lines: list[tuple[str, str]] = []
        if new_game_note:
            lines.append((new_game_note + "\n", "dim"))
        if not export.tested:
            lines.append((f"Untested game version {export.release}: check results carefully.\n", "warn"))
        hubs = ", ".join(net.name(p).split(" [")[0] for p in routing.transfer_depots) or "none"
        n = len(plan.add) + len(plan.remove)
        if n == 0:
            self.status.config(text=f"day {export.meta.get('day')}", fg=DIM)
            lines.append(("✓ Depots up to date\n", "ok"))
        else:
            self.status.config(text=f"{n} change{'s' * (n != 1)} · day {export.meta.get('day')}", fg=WARN)
            groups: dict[tuple, list] = {}
            for kind, changes in (("remove", plan.remove), ("add", plan.add)):
                for c in changes:
                    groups.setdefault(c.loc, []).append((kind, c.rule))
            # Who collects where once these rules are in: that's what Onwards Ports means.
            collectors = collectors_of(merge(existing_rules(net), plan.desired))
            # Collect rules that pick up depot cargo nobody collects now: these go first.
            frees: dict[tuple, int] = {}  # (loc, rule key) -> TEU freed
            stuck = nudges.uncollected_depot_cargo(net)
            all_rules = merge(existing_rules(net), plan.desired)
            for c in plan.add:
                if c.rule.action == "Collect" and c.loc[1] in stuck:
                    route = net.route(c.loc[0])
                    dests = rule_destinations(c.rule, route, net, collectors) or set()
                    teu = sum(t for d, t in stuck[c.loc[1]].items() if d in dests)
                    if teu:
                        frees[(c.loc, c.rule.key())] = teu
            urgent_routes = {loc[0] for loc, _ in frees}
            current = existing_rules(net)
            twins = _twins(net, groups, names)
            copied = sum(len(items) for loc, items in groups.items() if loc[0] in twins)
            if copied:
                self.status.config(text=f"{n} changes ({copied} by Copy All) · day {export.meta.get('day')}", fg=WARN)
            for loc in sorted((l for l in groups if l[0] not in twins),
                              key=lambda l: (l[0] not in urgent_routes, names.get(l[0], l[0]),
                                             l[1] is not None, _index(net, l))):
                where = "Global Rules" if loc[1] is None else f"stop {_index(net, loc)} {net.name(loc[1]).split(' [')[0]}"
                lines.append((f"{names.get(loc[0], loc[0])} · {where}\n", "h"))
                for kind, rule in groups[loc]:
                    verb = "+ " if kind == "add" else "− "
                    lines.append((verb + _short(render(rule, net)), f"{kind} item"))
                    if kind == "add" and loc[1] is not None:
                        hint = _placement(rule, current.get(loc, ()))
                        if hint:
                            lines.append((f"   {hint}", "gloss item"))
                    if (loc, rule.key()) in frees:
                        lines.append((f"   ← do this first: frees {frees[(loc, rule.key())]:,} TEU waiting in "
                                      f"the {net.name(loc[1]).split(' [')[0]} depot", "warn item"))
                    if rule.filter == ONWARDS_PORTS:
                        via = sorted(names.get(r, r) for r in collectors.get(rule.target, ()))
                        hub = net.name(rule.target).split(" [")[0]
                        own = ""
                        if rule.except_cargo:
                            ef, et = rule.except_cargo
                            if et == THIS_ROUTE:
                                own = ", minus this route's own ports"
                            elif ef == ONWARDS_PORTS:
                                own = f", minus what's onwards from {net.name(et).split(' [')[0]}"
                            else:
                                own = f", minus {names.get(et, et)} ports"
                        lines.append((f"   = cargo for {', '.join(via) or 'no'} ports, not {hub} itself{own}",
                                      "gloss item"))
                    lines.append(("\n", f"{kind} item"))
            # Twins last: they copy routes listed above.
            for rid, original in sorted(twins.items(), key=lambda kv: names.get(kv[0], kv[0])):
                lines.append((f"{names.get(rid, rid)} · same changes as {names.get(original, original)}\n", "h"))
                lines.append(("   Do them on " + names.get(original, original) + ", then in the game use Copy All on "
                              "each changed stop and on Global Rules, and paste into " + names.get(rid, rid) + "\n",
                              "gloss"))
        for w in plan.warnings + export.notes:
            if any(k in w for k in ("could not be read", "wording",
                                    "a player rule loads", "a player rule stores")):
                lines.append((f"⚠ {_short(w.replace('a player rule', 'one of your own rules'))}\n", "warn"))
        if plan.optional_remove:
            names_ = {r.id: r.name or r.id for r in net.routes}
            lines.append((f"Optional tidy-up: {len(plan.optional_remove)} rule(s) no longer needed but harmless\n", "dim"))
            for c in plan.optional_remove:
                where = "Global Rules" if c.loc[1] is None else net.name(c.loc[1]).split(" [")[0]
                lines.append((f"   − {names_.get(c.loc[0], c.loc[0])} · {where}: {_short(render(c.rule, net))}\n", "dim"))
        lines.append(("Network\n", "h"))
        for n in nudges.check_network(net, s, routing, export.ship_cargo, export.ship_capacity, export.slowest_leg):
            icon, tag = {nudges.OK: ("✓", "okline"), nudges.WARN: ("⚠", "warn")}.get(n.level, ("•", "dim"))
            lines.append((f"{icon} {_short(n.text)}\n", tag))
        lines.append((f"Transfer depots: {hubs}\n", "dim"))
        if any(k in w for w in plan.warnings for k in ("a player rule loads", "a player rule stores")):
            lines.append(("\nLeftovers from earlier suggestions? ", "dim"))
            lines.append(("Treat all current rules as suggested", "link"))
            lines.append((" (keeps your default Load rules; anything not needed is listed as −)\n", "dim"))
        self._show(lines)

    # ------------------------------------------------------------------ ui helpers

    def _show(self, lines):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        for text, tags in lines:
            self.text.insert("end", text, tuple(tags.split()))
        self.text.config(state="disabled")

    def _tick(self, event):
        """Click an item to strike it through while you work; the next export is the real check."""
        idx = self.text.index(f"@{event.x},{event.y}")
        start, end = f"{idx} linestart", f"{idx} lineend"
        if "done" in self.text.tag_names(idx):
            self.text.tag_remove("done", start, end)
        else:
            self.text.tag_add("done", start, end)

    def toggle(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.text.pack_forget()
            self.root.geometry(f"{self.root.winfo_width()}x28")
        else:
            self.text.pack(fill="both", expand=True)
            self.root.geometry(f"{self.root.winfo_width()}x320")

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")


def _index(net, loc) -> int:
    if loc[1] is None:
        return 0
    for i, l in enumerate(stop_locations(net.route(loc[0])), 1):
        if l == tuple(loc):
            return i
    return 0


def _short(text: str) -> str:
    """'Store | All | Port | Sendai-Shiogama [port_sendai]' -> without the [ids]."""
    import re
    return re.sub(r" \[[a-z0-9_]+\]", "", text)


def _placement(rule, stop_rules) -> str:
    """Where a new rule goes on a stop: the game adds it at the bottom, so say when to move it up."""
    rank = nudges.ORDER_RANK.get(rule.action)
    if rank is None:
        return ""
    later = [r for r in stop_rules if nudges.ORDER_RANK.get(r.action, -1) > rank]
    if not later:
        return ""
    if rule.action in ("Store", "Unload"):
        return "↑ move to the top of the stop"
    return "↑ move above the stop's Load rule" + ("s" if len(later) > 1 else "")


def _twins(net, groups, names) -> dict[str, str]:
    """Routes with the same stops and exactly the same changes as another route: route -> original."""
    def signature(rid):
        return tuple(sorted((loc[1] or "", loc[2], kind, repr(rule.key()))
                            for loc, items in groups.items() if loc[0] == rid for kind, rule in items))
    seen: dict[tuple, str] = {}
    twins = {}
    for r in sorted(net.routes, key=lambda r: names.get(r.id, r.id)):
        sig = signature(r.id)
        if not sig:
            continue
        k = (tuple(r.ports), sig)
        if k in seen:
            twins[r.id] = seen[k]
        else:
            seen[k] = r.id
    return twins


_exe_by_pid: dict[int, str] = {}


def _foreground_is_game() -> bool:
    """True if the foreground window belongs to the game or to this overlay."""
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return True
    name = _exe_by_pid.get(pid.value)
    if name is None:
        name = ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
            kernel32.CloseHandle(h)
        if len(_exe_by_pid) > 256:
            _exe_by_pid.clear()
        _exe_by_pid[pid.value] = name
    return name == GAME_EXE


def main(argv=None):
    ap = argparse.ArgumentParser(prog="atd-overlay")
    ap.add_argument("--save", default="default", help="one name per save game")
    args = ap.parse_args(argv)
    # One overlay at a time: a second launch quietly exits. The mutex goes away when this process does.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel32.CreateMutexW(None, False, "BeringTonnageDepotsOverlay")
    if mutex and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return 0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    except Exception:
        pass
    Overlay(args.save).root.mainloop()


if __name__ == "__main__":
    main()
