"""Network health: can every port reach every other port, and if not, what would fix it?

Every suggested fix is tried on a copy of the network first; only fixes that
actually connect more ports are suggested, best first.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .model import Depot, Network
from .model import COLLECT
from .routing import RoutingPlan, plan_routes
from .rules import collectors_of, evaluate_stop, existing_rules, stop_locations

OK, INFO, WARN = "ok", "info", "warn"


@dataclass
class Nudge:
    level: str  # OK, INFO or WARN
    text: str


def check_network(net: Network, settings: dict, routing: RoutingPlan | None = None,
                  ship_cargo: dict[str, dict[str, int]] | None = None,
                  ship_capacity: dict[str, int] | None = None,
                  slowest_leg: dict[str, int] | None = None) -> list[Nudge]:
    """ship_cargo: route id -> destination -> TEU aboard its ship (from the export).
    ship_capacity / slowest_leg: route id -> TEU of its ship(s) / lowest leg speed in %."""
    plan = routing or _plan(net, settings)
    extra = set(plan.destinations)
    mt = settings["max_transfers"]
    out: list[Nudge] = []
    short = lambda p: net.name(p).split(" [")[0]  # noqa: E731
    active = [r for r in net.routes if r.active and r.id not in settings["disabled_routes"]]
    ports = sorted({p for r in active for p in r.port_set})
    if not active:
        return [Nudge(INFO, "No active routes yet.")]

    # Ports no active route visits can't be fixed with depots: say why instead.
    served = set(ports)
    orphans = sorted(d for d in plan.unreachable if d not in served)
    by_route: dict[str, list[str]] = {}
    for d in orphans:
        owners = [r.name or r.id for r in net.routes if d in r.port_set]
        key = ", ".join(owners) if owners else ""
        by_route.setdefault(key, []).append(d)
    for owners, ds in sorted(by_route.items()):
        names = ", ".join(short(d) for d in ds)
        if owners:
            out.append(Nudge(WARN, f"{names}: only on {owners}, which has no ship, so nothing delivers there"))
        else:
            out.append(Nudge(WARN, f"{names}: cargo is headed there, but no route of yours goes there"))

    broken = {pair for pair in _pairs(plan) if pair[1] in served}
    if not broken:
        out.insert(0, Nudge(OK, f"All {len(ports)} ports can reach each other"))
    else:
        out.insert(0, Nudge(WARN, f"{len(broken)} port pairs can't reach each other"))
        for d, origins in sorted(((d, o) for d, o in plan.unreachable.items() if d in served),
                                 key=lambda kv: -len(kv[1]))[:4]:
            out.append(Nudge(WARN, f"{short(d)} unreachable from {_list(short, origins)}"))
        out += _fixes(net, settings, broken, short, extra)

    # Isolated routes and routes without a ship.
    for r in active:
        if not any(r.port_set & o.port_set for o in active if o.id != r.id) and len(active) > 1:
            out.append(Nudge(WARN, f"{r.name or r.id} shares no port with any other route"))
    for r in net.routes:
        if not r.active and r.stops:
            out.append(Nudge(INFO, f"{r.name or r.id} has no ship, so it isn't used"))

    # Paths at the transfer limit: one more route beyond them won't connect.
    longest = [(n, rid, d) for (rid, d), n in plan.hops.items() if n == mt and mt > 0]
    if longest and not broken:
        n, rid, d = min(longest, key=lambda x: (net.route(x[1]).name or x[1], x[2]))
        out.append(Nudge(INFO, f"Longest trips use {mt} of {mt} transfers "
                               f"(e.g. {net.route(rid).name or rid} → {short(d)}). "
                               f"Routes added beyond them need a higher limit or a shortcut depot."))

    out += _stuck_cargo(net, plan, ship_cargo or {}, short)
    out += rules_out_of_order(net, short)
    out += _collect_without_drop(net, short)
    out += _backlog(net, ship_capacity or {}, slowest_leg or {}, short)

    # Depots.
    for dep in net.depots:
        if dep.capacity_teu and dep.stored_teu is not None and dep.stored_teu >= dep.capacity_teu:
            out.append(Nudge(WARN, f"Depot {short(dep.port)} is full ({dep.stored_teu}/{dep.capacity_teu} TEU): "
                                   f"nothing more can be stored there until it empties or you upgrade it"))
        elif dep.capacity_teu and dep.stored_teu and dep.stored_teu >= 0.8 * dep.capacity_teu:
            out.append(Nudge(INFO, f"Depot {short(dep.port)} is {dep.stored_teu * 100 // dep.capacity_teu}% full "
                                   f"({dep.stored_teu:,}/{dep.capacity_teu:,} TEU)"))
    return out


def uncollected_depot_cargo(net: Network) -> dict[str, dict[str, int]]:
    """Depot port -> {destination: TEU} that no route collects there under the rules in the game now."""
    out: dict[str, dict[str, int]] = {}
    rules = existing_rules(net)
    collectors = collectors_of(rules)
    for dep in net.depots:
        collected: set[str] = set()
        for r in net.routes:
            if r.active and dep.port in r.port_set:
                for loc in stop_locations(r):
                    if loc[1] == dep.port:
                        collected |= evaluate_stop(r, loc, rules, net, collectors).sets[COLLECT]
        stuck = {d: teu for d, teu in dep.stored_by_destination.items() if teu and d not in collected}
        if stuck:
            out[dep.port] = stuck
    return out


# Order on a stop (community guide, O1/O2): Stores and Unloads first (free space), then
# Collects (clear the depot), then Loads. Global Rules always run after the stop's own.
ORDER_RANK = {"Store": 0, "Unload": 0, "Collect": 1, "Load": 2}


def rules_out_of_order(net: Network, short=None) -> list[Nudge]:
    """Stops where a Collect or Load sits above a Store, or a Load above a Collect."""
    short = short or (lambda p: net.name(p).split(" [")[0])
    bad = []
    for r in net.routes:
        if not r.active:
            continue
        for loc, stop in zip(stop_locations(r), r.stops):
            ranks = [ORDER_RANK[x.action] for x in stop.rules if x.action in ORDER_RANK]
            if any(a > b for a, b in zip(ranks, ranks[1:])):
                bad.append(f"{r.name or r.id} stop {_stop_no(r, loc)} {short(loc[1])}")
    if not bad:
        return []
    return [Nudge(WARN, f"Rules out of order on {len(bad)} stop{'s' * (len(bad) != 1)} ({'; '.join(bad)}). "
                        f"On each, move Stores to the top, then Collects, then Loads")]


def _stop_no(route, loc) -> int:
    return next(i for i, l in enumerate(stop_locations(route), 1) if l == loc)


def _collect_without_drop(net: Network, short) -> list[Nudge]:
    """Community rule S5: a ship that collects cargo it never stores or delivers slowly fills up.

    Only cargo that can actually be in that depot counts (something on the network stores it there).
    """
    rules = existing_rules(net)
    collectors = collectors_of(rules)
    stored_at: dict[str, set[str]] = {}
    sets = {}
    for r in net.routes:
        if not r.active:
            continue
        for loc in stop_locations(r):
            eff = evaluate_stop(r, loc, rules, net, collectors)
            sets[(r.id, loc)] = eff
            stored_at.setdefault(loc[1], set()).update(eff.sets["Store"] - {loc[1]})
    out = []
    for r in net.routes:
        if not r.active:
            continue
        drops = set(r.port_set)
        collected: dict[str, set[str]] = {}
        for loc in stop_locations(r):
            eff = sets[(r.id, loc)]
            drops |= eff.sets["Store"] | eff.sets["Unload"]
            got = eff.sets["Collect"] & stored_at.get(loc[1], set())
            if got:
                collected.setdefault(loc[1], set()).update(got)
        for hub, got in sorted(collected.items()):
            never = sorted(got - drops)
            if never:
                out.append(Nudge(WARN, f"{r.name or r.id} collects cargo for {', '.join(short(d) for d in never)} "
                                       f"at {short(hub)} but never drops it off or delivers it, so it builds up on "
                                       f"the ship. Add a Store for it, or remove that Collect"))
    return out


BACKLOG_SHIPLOADS = 3  # warn when cargo waiting for a route is more than this many shiploads
SLOW_LEG = 50          # mention legs slower than this %


def _backlog(net: Network, capacity: dict[str, int], slowest: dict[str, int], short) -> list[Nudge]:
    """Routes that can't keep up: far more cargo waiting for them in depots than one ship carries."""
    rules = existing_rules(net)
    collectors = collectors_of(rules)
    waiting: dict[str, dict[str, int]] = {}  # route -> depot -> TEU only it collects (shared: split evenly)
    for dep in net.depots:
        takers: dict[str, set[str]] = {}
        for r in net.routes:
            if r.active and dep.port in r.port_set:
                got: set[str] = set()
                for loc in stop_locations(r):
                    if loc[1] == dep.port:
                        got |= evaluate_stop(r, loc, rules, net, collectors).sets[COLLECT]
                takers[r.id] = got
        for d, teu in dep.stored_by_destination.items():
            who = [rid for rid, got in takers.items() if d in got]
            for rid in who:
                waiting.setdefault(rid, {}).setdefault(dep.port, 0)
                waiting[rid][dep.port] += teu // len(who)
    out = []
    for rid, by_depot in sorted(waiting.items()):
        cap = capacity.get(rid)
        total = sum(by_depot.values())
        if not cap or total <= BACKLOG_SHIPLOADS * cap:
            continue
        route = net.route(rid)
        where = ", ".join(f"{short(p)} {t:,}" for p, t in sorted(by_depot.items(), key=lambda kv: -kv[1]) if t)
        text = (f"{route.name or rid} can't keep up: {total:,} TEU waiting for it ({where}), "
                f"about {total // cap} loads for its {cap:,} TEU ship")
        if rid in slowest and slowest[rid] < SLOW_LEG:
            text += f". Its slowest leg runs at {slowest[rid]}% speed"
        text += ". Faster legs, another ship or a second route would help"
        out.append(Nudge(WARN, text))
    return out


def _stuck_cargo(net: Network, plan: RoutingPlan, ship_cargo: dict, short) -> list[Nudge]:
    """Cargo the game is holding right now that nothing will move, judged by the rules in the game."""
    out = []
    served = {p for r in net.routes if r.active for p in r.port_set}
    for port, stuck in sorted(uncollected_depot_cargo(net).items()):
        for d, teu in sorted(stuck.items(), key=lambda kv: -kv[1]):
            idle = [r.name or r.id for r in net.routes if not r.active and d in r.port_set]
            if idle and d not in served:  # expected: it goes on once that route has a ship
                out.append(Nudge(INFO, f"{short(port)} depot: {teu:,} TEU for {short(d)} waiting "
                                       f"for {' or '.join(idle)} to get a ship"))
                continue
            out.append(Nudge(WARN, f"{short(port)} depot: {teu:,} TEU for {short(d)} waiting, "
                                   f"and no route collects it there yet"))
    for rid, cargo in sorted(ship_cargo.items()):
        try:
            route = net.route(rid)
        except KeyError:
            continue
        stuck = [(d, teu) for d, teu in sorted(cargo.items(), key=lambda kv: -kv[1])
                 if teu and d not in route.port_set and (rid, d) not in plan.hops]
        if stuck:
            total = sum(teu for _, teu in stuck)
            parts = ", ".join(f"{short(d)} {teu:,}" for d, teu in stuck)
            out.append(Nudge(WARN, f"{route.name or rid}'s ship carries {total:,} TEU no route can deliver "
                                   f"({parts}); it stays aboard until you unload it"))
    return out


def _fixes(net: Network, settings: dict, broken: set, short, extra: set[str]) -> list[Nudge]:
    """Try each possible fix on a copy of the network; suggest the ones that help."""
    tries = []
    owned = {d.port for d in net.depots}
    meet: dict[str, list[str]] = {}
    for r in net.routes:
        if r.active and r.id not in settings["disabled_routes"]:
            for p in r.port_set:
                meet.setdefault(p, []).append(r.name or r.id)
    for port, routes in sorted(meet.items()):
        if len(routes) < 2:
            continue
        trial = copy.deepcopy(net)
        if port in owned:  # owned but switched off in settings
            if port not in settings["disabled_depots"]:
                continue
            s2 = dict(settings, disabled_depots=[p for p in settings["disabled_depots"] if p != port])
            fixed = len(broken - _pairs(_plan(trial, s2, extra)))
            tries.append((fixed, f"Switch the {short(port)} depot back on in atd.toml"))
            continue
        trial.depots.append(Depot(port))
        fixed = len(broken - _pairs(_plan(trial, settings, extra)))
        tries.append((fixed, f"Buy a depot at {short(port)} ({' and '.join(sorted(routes))} meet there)"))
    for bump in (1, 2, 3):
        s2 = dict(settings, max_transfers=settings["max_transfers"] + bump)
        fixed = len(broken - _pairs(_plan(net, s2, extra)))
        if fixed:
            tries.append((fixed, f"Raise max_transfers to {s2['max_transfers']} in atd.toml"))
            break
    out = []
    for fixed, text in sorted((t for t in tries if t[0]), key=lambda t: (-t[0], t[1]))[:3]:
        what = "connects everything" if fixed == len(broken) else f"connects {fixed} of {len(broken)} pairs"
        out.append(Nudge(INFO, f"Fix: {text}: {what}"))
    if not out:
        out.append(Nudge(INFO, "Fix: the unreachable ports' routes don't meet the rest of the network; "
                               "add a stop at one of your hub ports"))
    return out


def _plan(net: Network, settings: dict, extra: set[str] = frozenset()) -> RoutingPlan:
    return plan_routes(net, settings["max_transfers"], settings["disabled_routes"], settings["disabled_depots"],
                       extra_destinations=extra, direct_first=not settings.get("compact", True))


def _pairs(plan: RoutingPlan) -> set[tuple[str, str]]:
    return {(o, d) for d, origins in plan.unreachable.items() for o in origins}


def _list(short, ports, limit=4) -> str:
    names = [short(p) for p in ports]
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")
