"""Approach C: read a route export, print the rules to add or remove.

    python -m atd.advisor path/to/export.json [--save NAME] [--commit] [--dry-run]

Read-only toward the game. The only file it writes is the managed-rule record
(records/<save>.json), which lets later runs tell managed rules from player rules.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

from . import __version__
from .export_parser import load_export
from .model import COLLECT, Network
from .planner import Plan, load_record, make_plan, save_record
from .routing import RoutingPlan, plan_routes
from .rules import (collectors_of, evaluate_stop, existing_rules, generate, generate_compact, merge, render,
                    stop_locations, verify, verify_compact)

DEFAULTS = {
    "auto_transfer": True,
    "max_transfers": 3,
    "disabled_depots": [],
    "disabled_routes": [],
    "dry_run": False,
    "compact": True,  # short Route Ports rules; false = one rule per destination, direct routes first
    "overlay_only_with_game": True,  # hide the overlay unless Bering Tonnage is the active window
    # Routes made with a template's Recursive Onwards Ports option: false, true (all) or a list of
    # route names. The export doesn't show it, so the overlay has to be told.
    "recursive_templates": False,
}
RETIRED = {"future_proof"}  # always on now; ignored if an old atd.toml still has it


def load_settings(path: Path | None) -> dict:
    """Settings from `path`, then from atd.local.toml next to it (your own, never uploaded)."""
    settings = dict(DEFAULTS)
    files = [path, path.with_name("atd.local.toml")] if path else []
    for f_path in files:
        if f_path.exists():
            with open(f_path, "rb") as f:
                settings.update({k: v for k, v in tomllib.load(f).get("settings", {}).items() if k not in RETIRED})
    unknown = set(settings) - set(DEFAULTS)
    if unknown:
        raise SystemExit(f"Unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
    return settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atd-advisor", description=__doc__.split("\n\n")[0])
    ap.add_argument("export", type=Path, help="JSON from Esc > Export Route Data to File")
    ap.add_argument("--save", default="default", help="name used to key the managed-rule record")
    ap.add_argument("--config", type=Path, default=Path("atd.toml"))
    ap.add_argument("--records", type=Path, default=Path("records"))
    ap.add_argument("--max-transfers", type=int)
    ap.add_argument("--off", action="store_true", help="auto-transfer off: plan removal of all managed rules")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    ap.add_argument("--commit", action="store_true", help="record the plan even on the first run")
    args = ap.parse_args(argv)

    s = load_settings(args.config)
    if args.max_transfers is not None:
        s["max_transfers"] = args.max_transfers
    if args.off:
        s["auto_transfer"] = False
    if args.dry_run:
        s["dry_run"] = True

    export = load_export(args.export)
    net = export.network
    record_path = args.records / f"{args.save}.json"
    record = load_record(record_path)

    routing = plan_routes(net, s["max_transfers"], s["disabled_routes"], s["disabled_depots"],
                          extra_destinations=_cargo_destinations(export), direct_first=not s["compact"])
    plan = make_plan(routing, net, record, s["auto_transfer"], set(s["disabled_routes"]), s["compact"],
                     recursive_templates=recursive_routes(net, s["recursive_templates"]))

    # Acceptance test 8 on live data: the generator's rules reproduce the routing decisions.
    if s["compact"]:
        player_collect = collectors_of(existing_rules(net))
        problems = verify_compact(routing, net, generate_compact(routing, net, player_collect),
                                  player_collect)
    else:
        problems = verify(routing, net, generate(routing, net))

    read_only_reason = None
    if not export.tested:
        read_only_reason = (f"untested game version (release {export.release}, "
                            f"export_version {export.export_version})")
    elif s["dry_run"]:
        read_only_reason = "dry run"
    elif not record_path.exists() and not args.commit:
        read_only_reason = "first run on this save is always a dry run; rerun with --commit to record"

    print(report(export, routing, plan, problems, s, read_only_reason))

    if read_only_reason is None:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        save_record(record_path, plan.record, {"save": args.save, "release": export.release,
                                                "game_day": export.meta.get("day")})
        print(f"\nRecorded {sum(map(len, plan.desired.values()))} managed rule(s) in {record_path}")
    return 1 if problems else 0


def recursive_routes(net: Network, setting) -> bool | set[str]:
    """The `recursive_templates` setting as make_plan wants it: False, True or route ids."""
    if isinstance(setting, bool):
        return setting
    wanted = {str(x).strip().lower() for x in (setting or [])}
    return {r.id for r in net.routes if (r.name or "").strip().lower() in wanted or r.id in wanted}


def _cargo_destinations(export) -> set[str]:
    dests = {d for cargo in export.ship_cargo.values() for d in cargo}
    for dep in export.network.depots:
        dests |= set(dep.stored_by_destination)
    return dests


def _player_rules(net: Network, record: set[tuple]):
    return {loc: [r for r in rs if not (r.parsed and (loc[0], loc[1], loc[2], r.key()) in record)]
            for loc, rs in existing_rules(net).items()}


# --------------------------------------------------------------------------- report

def report(export, routing: RoutingPlan, plan: Plan, problems: list[str], settings: dict,
           read_only_reason: str | None) -> str:
    net = export.network
    route_name = {r.id: r.name or r.id for r in net.routes}
    out = [f"Auto-Transfer Depots advisor {__version__}",
           f"Export: release {export.release}, export_version {export.export_version}, "
           f"game day {export.meta.get('day')} ({export.meta.get('date')})",
           f"Settings: auto-transfer {'on' if settings['auto_transfer'] else 'OFF'}, "
           f"max transfers {settings['max_transfers']}"]
    if read_only_reason:
        out.append(f"Read-only: {read_only_reason}")
    inactive = [r for r in net.routes if not r.active]
    if inactive:
        out.append("Inactive routes (ignored): " + ", ".join(route_name[r.id] for r in inactive))

    out += ["", "TRANSFER DEPOTS"]
    if routing.transfer_depots:
        for port, rids in routing.transfer_depots.items():
            out.append(f"  {net.name(port)}: " + ", ".join(route_name[r] for r in rids))
    else:
        out.append("  None. A transfer depot needs an owned depot where two or more active routes stop.")

    out += ["", "CHANGES"]
    if not plan.add and not plan.remove:
        out.append("  None. The game already matches the plan.")
    for title, changes in (("Add", plan.add), ("Remove", plan.remove)):
        by_where = defaultdict(list)
        for c in changes:
            by_where[c.loc].append(c.rule)
        for loc in sorted(by_where, key=lambda l: (route_name.get(l[0], l[0]), l[1] is not None,
                                                   _stop_index(net, l))):
            out.append(f"  {title} on {route_name.get(loc[0], loc[0])}, {_where(net, loc)}:")
            out += [f"    {render(r, net)}" for r in by_where[loc]]
    if plan.covered_by_player:
        out.append(f"  ({len(plan.covered_by_player)} rule(s) not needed: your own rules already do the same)")

    out += ["", "UNREACHABLE"]
    if not routing.unreachable:
        out.append("  None.")
    for d in routing.unreachable:
        origins = routing.unreachable[d]
        out.append(f"  {net.name(d)} from: " + ", ".join(net.name(o) for o in origins))

    out += ["", "STRANDED"]
    stranded = _stranded(net, routing, plan, export)
    out += [f"  {s}" for s in stranded] or ["  None."]

    out += ["", "WARNINGS"]
    warnings = list(plan.warnings) + export.notes
    for rid, dests in plan.skipped_for_limit.items():
        warnings.append(f"{route_name[rid]}: rule limit reached; not routed: "
                        + ", ".join(net.name(d) for d in dests))
    for dep in net.depots:
        if dep.capacity_teu and dep.stored_teu is not None and dep.stored_teu >= dep.capacity_teu:
            warnings.append(f"Depot {net.name(dep.port)} is full ({dep.stored_teu}/{dep.capacity_teu} TEU)")
    warnings += [f"Self-check failed: {p}" for p in problems]
    warnings.append("Known gap: flag, licence and coastwise restrictions are not checked "
                    "(the export doesn't list them per lane)")
    out += [f"  {w}" for w in warnings]

    hints = _hints(net, routing)
    if hints:
        out += ["", "HINTS"] + [f"  {h}" for h in hints]
    return "\n".join(out)


def _where(net: Network, loc) -> str:
    if loc[1] is None:
        return "Global Rules"
    idx = _stop_index(net, loc)
    return f"stop {idx} {net.name(loc[1])}" if idx else net.name(loc[1])


def _stop_index(net: Network, loc) -> int:
    if loc[1] is None:
        return 0
    for i, l in enumerate(stop_locations(net.route(loc[0])), 1):
        if l == tuple(loc):
            return i
    return 0


def _stranded(net, routing, plan, export) -> list[str]:
    rules = merge(_player_rules(net, set()), plan.desired)
    out = []
    for dep in net.depots:
        if not dep.stored_by_destination:
            continue
        collected: set[str] = set()
        for r in net.routes:
            if r.active and dep.port in r.port_set:
                for loc in stop_locations(r):
                    if loc[1] == dep.port:
                        collected |= evaluate_stop(r, loc, rules, net).sets[COLLECT]
        for d, teu in sorted(dep.stored_by_destination.items()):
            if teu and d not in collected:
                out.append(f"{teu} TEU in {net.name(dep.port)} bound for {net.name(d)}: no route collects it")
    return out


def _hints(net: Network, routing: RoutingPlan) -> list[str]:
    """Ports where active routes already meet but there is no depot."""
    depots = {d.port for d in net.depots}
    meet = defaultdict(list)
    for r in net.routes:
        if r.active:
            for p in r.port_set:
                meet[p].append(r.name or r.id)
    return [f"{net.name(p)}: {', '.join(sorted(rs))} meet here; a depot would make it a transfer point"
            for p, rs in sorted(meet.items()) if len(rs) >= 2 and p not in depots]


if __name__ == "__main__":
    sys.exit(main())
