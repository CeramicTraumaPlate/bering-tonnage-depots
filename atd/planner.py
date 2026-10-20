"""Compare desired managed rules with the rules already in the game.

Requirements covered here:
  8  Player rules win: never edit or delete them; add no rule they already cover.
  9  Same input, same output: everything is sorted; a second run changes nothing.
  10 Clean removal: with auto-transfer off, every recorded managed rule is removed.
Plus the rule limit (40 per route, stops and Global Rules together) and the managed-rule record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from dataclasses import replace

from .model import COLLECT, LOAD, ONWARDS_PORTS, PORT, ROUTE_PORTS, STORE, THIS_ROUTE, Network, Rule
from .routing import RoutingPlan
from .rules import (Loc, collectors_of, evaluate_stop, existing_rules, generate, generate_compact, merge,
                    rule_destinations, stop_locations, verify_compact)

ROUTE_LIMIT = 40  # rules per route, all stops and Global Rules together
NEAR_LIMIT = 32

RECORD_VERSION = 2  # 2: rules keep their Except cargo (1 lost it)


@dataclass
class Change:
    loc: Loc
    rule: Rule


@dataclass
class Plan:
    add: list[Change] = field(default_factory=list)
    remove: list[Change] = field(default_factory=list)
    desired: dict[Loc, list[Rule]] = field(default_factory=dict)     # managed rules after this run
    # What to remember as ours: everything wanted, plus our rules still in the game
    # (e.g. waiting to be removed), so they are never mistaken for the player's.
    record: dict[Loc, list[Rule]] = field(default_factory=dict)
    covered_by_player: list[Change] = field(default_factory=list)    # skipped as duplicates
    skipped_for_limit: dict[str, list[str]] = field(default_factory=dict)  # route -> dests
    # Our rules that are no longer needed but do no harm if left in: removing them is optional.
    optional_remove: list[Change] = field(default_factory=list)
    # Player rules that only do what a conflict warning complains about: suggested for removal.
    suggest_remove: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- record

def load_record(path: Path) -> set[tuple]:
    """Managed rules from earlier runs, as (route, port, visit, rule key) tuples."""
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("record_version") not in (1, RECORD_VERSION):
        raise ValueError(f"{path}: unknown record version {data.get('record_version')}")
    return {_entry_from_json(e) for e in data["managed"]}


def record_meta(path: Path) -> dict:
    """The game details saved with a record (game_day, start_region, ...), or {}."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k not in ("managed", "record_version")}


def has_recursive_loop_bug(release: str | None) -> bool:
    """True for game releases before 1.1.1 (and unknown ones), where Recursive Onwards Ports plus an
    Onwards Ports Collect for another depot of the route loops forever."""
    try:
        parts = tuple(int(x) for x in str(release).split("."))
    except ValueError:
        return True
    return parts < (1, 1, 1)


def is_other_game(meta: dict, export_meta: dict) -> bool:
    """True if a record clearly belongs to a different game than this export.

    A game's day only moves forward, and its starting region never changes, so a
    record from a later day or another start region can't be from this game.
    (Loading an older save of the same game looks the same; starting fresh is
    the safe choice then too.)
    """
    day, rec_day = export_meta.get("day"), meta.get("game_day")
    if isinstance(day, int) and isinstance(rec_day, int) and day < rec_day:
        return True
    start, rec_start = export_meta.get("start_region"), meta.get("start_region")
    return bool(start and rec_start and start != rec_start)


def save_record(path: Path, desired: dict[Loc, list[Rule]], meta: dict) -> None:
    entries = sorted(
        ({"route": loc[0], "port": loc[1], "visit": loc[2], "rule": _rule_to_json(r)}
         for loc, rules in desired.items() for r in rules),
        key=lambda e: json.dumps(e, sort_keys=True),
    )
    data = {"record_version": RECORD_VERSION, **meta, "managed": entries}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rule_to_json(r: Rule) -> dict:
    return {"action": r.action, "amount": r.amount, "filter": r.filter, "target": r.target,
            "except_at": sorted(r.except_at), "except_cargo": list(r.except_cargo) if r.except_cargo else None}


def _entry_from_json(e: dict) -> tuple:
    r = e["rule"]
    exc = r.get("except_cargo")
    rule = Rule(r["action"], r["filter"], r["target"], r["amount"], tuple(r["except_at"]),
                tuple(exc) if exc else None)
    return (e["route"], e["port"], e["visit"], rule.key())


# --------------------------------------------------------------------------- planning

def make_plan(
    routing: RoutingPlan,
    network: Network,
    record: set[tuple],
    auto_transfer: bool = True,
    disabled_routes: set[str] = frozenset(),
    compact: bool = False,
    adopt: bool = False,
    future_proof: bool = True,
    recursive_templates: bool | set[str] = False,
    game_release: str | None = None,
) -> Plan:
    """compact=True expects a routing plan made with direct_first=False.

    recursive_templates: True, or the ids of the routes, whose own Onwards Ports rules came
    from a route template with Recursive Onwards Ports on.

    adopt=True: one-time cleanup. Treat every rule in the game as this tool's,
    except the default `Load | All | Route Ports | This Route`, so that rules
    from earlier suggestions are listed for removal instead of warned about.

    game_release: the export's release. Before 1.1.1 a recursive route that collects Onwards Ports
    cargo for another of its depots loops forever in the game, so such Collects are never written;
    1.1.1 fixed it. An unknown release is treated as having the bug.
    """
    plan = Plan()
    current = existing_rules(network)
    if recursive_templates:
        # Your own Onwards Ports rules came from route templates with Recursive Onwards Ports on.
        # The export can't show it, so assume it. Rules this tool suggested are never recursive.
        rec_routes = recursive_templates if isinstance(recursive_templates, (set, frozenset)) else None
        current = {loc: [replace(r, recursive=True)
                         if (r.filter == ONWARDS_PORTS and (rec_routes is None or loc[0] in rec_routes)
                             and (loc[0], loc[1], loc[2], r.key()) not in record) else r
                         for r in rs] for loc, rs in current.items()}

    # Routes with Recursive Onwards Ports on must not Collect Onwards Ports cargo (game bug: infinite loop).
    loop_routes = ({r.id for r in network.routes} if recursive_templates is True
                   else set(recursive_templates) if recursive_templates else set())
    if not has_recursive_loop_bug(game_release):
        loop_routes = set()

    # Split present rules into managed (recorded) and player (everything else).
    managed_present: dict[Loc, list[Rule]] = {}
    player: dict[Loc, list[Rule]] = {}
    for loc, rules in current.items():
        for r in rules:
            is_default = (r.action, r.filter, r.target, r.except_at, r.except_cargo) == (
                LOAD, ROUTE_PORTS, THIS_ROUTE, (), None)
            is_managed = r.parsed and ((loc[0], loc[1], loc[2], r.key()) in record
                                       or (adopt and not is_default))
            (managed_present if is_managed else player).setdefault(loc, []).append(r)

    if not auto_transfer:
        wanted = {}
    elif compact:
        # Every Collect rule in the game (yours or ours, ship or no ship) widens Onwards Ports,
        # so the generator must judge Onwards rules by all of them.
        plain = {loc: [r for r in rs if not r.recursive] for loc, rs in current.items()}
        wanted = generate_compact(routing, network, extra_collectors=collectors_of(current), existing=plain,
                                  future_proof=future_proof,
                                  extra_drops=_player_drops(network, player, collectors_of(current)),
                                  no_onwards_collect=loop_routes)
        # Recursive rules reach through every hub where someone collects, including the
        # Collects suggested here, so plan again with those until nothing changes.
        for _ in range(3 if recursive_templates else 0):
            drops = _player_drops(network, player, collectors_of(merge(current, wanted)))
            again = generate_compact(routing, network, extra_collectors=collectors_of(merge(current, wanted)),
                                     existing=plain, future_proof=future_proof, extra_drops=drops,
                                     no_onwards_collect=loop_routes)
            if again == wanted:
                break
            wanted = again
    else:
        wanted = generate(routing, network)
    collectors = collectors_of(merge(player, wanted))  # who collects where, once all rules are in
    wanted = _apply_limits(wanted, player, routing, network, plan, collectors)
    wanted = _drop_covered(wanted, player, network, plan, collectors)
    plan.desired = wanted
    plan.record = merge(wanted, {loc: [r for r in rs if (loc, r.key()) not in
                                       {(loc, w.key()) for w in wanted.get(loc, ())}]
                                 for loc, rs in managed_present.items()})

    want_keys = {(loc, r.key()) for loc, rs in wanted.items() for r in rs}
    have_keys = {(loc, r.key()) for loc, rs in managed_present.items() for r in rs}
    for loc, rs in sorted(wanted.items(), key=_loc_sort):
        plan.add += [Change(loc, r) for r in rs if (loc, r.key()) not in have_keys]
    for loc, rs in sorted(managed_present.items(), key=_loc_sort):
        if loc[0] in disabled_routes:
            plan.warnings.append(f"{loc[0]} is switched off; its managed rules were left in place")
            continue
        plan.remove += [Change(loc, r) for r in rs if (loc, r.key()) not in want_keys]

    if compact and plan.remove:
        _split_optional_removals(routing, network, player, plan)

    _check_conflicts(routing, network, player, plan, collectors, compact)
    _check_recursive_loops(network, player, plan, loop_routes)
    _check_limits(network, player, wanted, plan)
    plan.warnings = sorted(set(plan.warnings))
    return plan


def _player_drops(network, player, collectors) -> dict[str, set[str]]:
    """Hub -> destinations the player's own Store rules drop off there (e.g. a recursive
    template's Store), so the generator arranges for someone to collect them."""
    out: dict[str, set[str]] = {}
    for loc, rs in player.items():
        if loc[1] is None:
            continue
        route = network.route(loc[0])
        for r in rs:
            if r.action == STORE:
                out.setdefault(loc[1], set()).update(rule_destinations(r, route, network, collectors) or ())
    return out


def _split_optional_removals(routing, network, player, plan) -> None:
    """A rule we no longer need but that breaks nothing if left in isn't a required change.

    Each removal is tried the other way round: keep the rule on top of the plan and
    re-run the self-check. If nothing new fails, the removal is only a tidy-up.
    """
    # Rules left in act together (a Collect widens Onwards Ports for others), so each one
    # is tried on top of every rule already left in, never on its own.
    base = merge(player, plan.desired)
    baseline = len(verify_compact(routing, network, base))
    required, kept = [], {}
    for change in plan.remove:
        trial = merge(base, kept, {change.loc: [change.rule]})
        if len(verify_compact(routing, network, trial)) > baseline:
            required.append(change)
        else:
            kept = merge(kept, {change.loc: [change.rule]})
            plan.optional_remove.append(change)
            plan.desired.setdefault(change.loc, [])  # still ours: it stays in the record
            plan.record.setdefault(change.loc, [])
            if change.rule not in plan.record[change.loc]:
                plan.record[change.loc].append(change.rule)
    plan.remove = required


def _apply_limits(wanted, player, routing, network, plan, collectors) -> dict[Loc, list[Rule]]:
    """Over the route limit: keep the rules that cover the most destinations, drop the rest and name them."""
    out: dict[Loc, list[Rule]] = {loc: list(rs) for loc, rs in wanted.items()}
    dropped_store: dict[str, set[str]] = {}
    for rid in sorted({loc[0] for loc in wanted}):
        route = network.route(rid)
        budget = ROUTE_LIMIT - sum(len(rs) for loc, rs in player.items() if loc[0] == rid)
        mine = [(loc, r) for loc, rs in sorted(wanted.items(), key=_loc_sort) if loc[0] == rid for r in rs]
        if len(mine) <= budget:
            continue
        ranked = sorted(mine, key=lambda lr: (-len(rule_destinations(lr[1], route, network, collectors) or ()),
                                              lr[1].key()))
        drop = ranked[max(budget, 0):]
        for loc, r in drop:
            out[loc] = [x for x in out[loc] if x is not r]
            if r.filter == PORT:
                plan.skipped_for_limit.setdefault(rid, []).append(r.target)
                if r.action == STORE:
                    dropped_store.setdefault(rid, set()).add(r.target)
    out = {loc: rs for loc, rs in out.items() if rs}
    # A destination that can't be stored must not be loaded either, or it rides forever.
    for rid, dests in dropped_store.items():
        g = (rid, None, 0)
        if g in out:
            out[g] = [r for r in out[g] if not (r.action == LOAD and r.filter == PORT and r.target in dests)]
    for rid in plan.skipped_for_limit:
        plan.skipped_for_limit[rid] = sorted(set(plan.skipped_for_limit[rid]))
    return out


def _drop_covered(wanted, player, network, plan, collectors) -> dict[Loc, list[Rule]]:
    """Requirement 8: no managed rule where player rules already do the same thing."""
    out: dict[Loc, list[Rule]] = {}
    for loc, rules in sorted(wanted.items(), key=_loc_sort):
        route = network.route(loc[0])
        if loc[1] is None:
            stops = stop_locations(route)
        else:
            stops = [loc]
        kept = []
        for r in rules:
            dests = rule_destinations(r, route, network, collectors)
            applies = [s for s in stops if loc[1] is not None or s[1] not in r.except_at]
            if dests and applies and all(
                dests - {s[1]} <= evaluate_stop(route, s, player, network, collectors).sets[r.action]
                for s in applies
            ):
                plan.covered_by_player.append(Change(loc, r))
            else:
                kept.append(r)
        if kept:
            out[loc] = kept
    return out


def _check_conflicts(routing, network, player, plan, collectors, compact=False) -> None:
    """Player rules that load or store against the plan: leave them, but say so.

    Compact rules let cargo go off at ANY depot one step closer, so there a player rule
    only conflicts if it loads cargo the route can't deliver, or stores cargo somewhere
    that isn't a step closer. (The one-depot-per-destination plan picks by port id.)
    """
    # Ports only a ship-less route visits: cargo for them waiting at a hub is expected
    # (the nudges say that route has no ship), so it isn't a rule conflict.
    served = {p for r in network.routes if r.active for p in r.port_set}
    waiting = {p for r in network.routes if not r.active for p in r.port_set} - served
    no_recursive = {loc: [r for r in rs if not r.recursive] for loc, rs in player.items()}
    for rid in routing.routes:
        route = network.route(rid)
        deliverable = {d for d in routing.destinations if (rid, d) in routing.hops}
        carried = set(route.port_set) | {d for (r, d) in routing.store_at if r == rid}
        for loc in stop_locations(route):
            p = loc[1]
            eff = evaluate_stop(route, loc, player, network, collectors)
            # A recursive template's rules can't be edited one by one, and the plan already collects
            # what they drop off (its own Collects, via _player_drops): not a conflict to report.
            if any(r.recursive for rs in player.values() for r in rs):
                eff = evaluate_stop(route, loc, no_recursive, network, collectors)
            if compact:
                ok_load = deliverable
                ok_store = set(routing.store_ok.get((rid, p), ())) | (set(eff.sets[STORE]) - carried)
            else:
                ok_load = set(routing.load.get((rid, p), ()))
                ok_store = set(routing.store.get((rid, p), ()))
            extra_load = eff.sets[LOAD] - {p} - ok_load - waiting
            if extra_load:
                plan.warnings.append(
                    f"{route.name or rid} @ {network.name(p)}: a player rule loads "
                    f"{_names(network, extra_load)}, which the plan leaves to another route"
                )
                _suggest_removals(routing, network, player, plan, collectors, route, loc, LOAD,
                                  extra_load | {p}, compact)
            extra_store = eff.sets[STORE] - {p} - ok_store - waiting
            if extra_store:
                plan.warnings.append(
                    f"{route.name or rid} @ {network.name(p)}: a player rule stores "
                    f"{_names(network, extra_store)}, which the plan does not store here"
                )
                _suggest_removals(routing, network, player, plan, collectors, route, loc, STORE,
                                  extra_store | {p}, compact)
            unreadable = eff.unknown
            if unreadable:
                plan.warnings.append(
                    f"{route.name or rid} @ {network.name(p)}: {len(unreadable)} player rule(s) "
                    f"could not be read; this stop may not behave as planned"
                )


def _check_recursive_loops(network, player, plan, loop_routes) -> None:
    """Player Collects that send the game into an infinite loop (a game bug): a route with
    Recursive Onwards Ports on that collects Onwards Ports cargo at one of its depots for
    another depot it calls at. Say so and suggest removing the rule."""
    for loc, rules in sorted(player.items(), key=_loc_sort):
        rid, port = loc[0], loc[1]
        if rid not in loop_routes or port is None:
            continue
        route = network.route(rid)
        for r in rules:
            if (r.action == COLLECT and r.filter == ONWARDS_PORTS and r.target != port
                    and r.target in route.port_set):
                plan.warnings.append(
                    f"{route.name or rid} @ {network.name(port)}: a player rule collects Onwards Ports "
                    f"for {network.name(r.target)} on a route with Recursive Onwards Ports on; this "
                    f"loops forever in the game (a game bug) and can crash it. Remove the rule")
                plan.suggest_remove.append(Change(loc, r))


def _suggest_removals(routing, network, player, plan, collectors, route, loc, action, extra, compact) -> None:
    """Name the player rules behind a conflict warning and suggest removing them.

    When rules can be self-checked (compact), a rule is suggested if taking it out adds no
    failure, even if it also does something wanted. Otherwise only a rule whose every
    destination is in `extra` is suggested: removing one that does more could break the plan.
    """
    rules = list(player.get(loc, ())) + [g for g in player.get((route.id, None, 0), ())
                                          if loc[1] not in g.except_at]
    base = len(verify_compact(routing, network, merge(player, plan.desired))) if compact else 0
    for r in rules:
        if r.action != action:
            continue
        dests = rule_destinations(r, route, network, collectors)
        if not dests or not dests & extra or not (compact or dests <= extra):
            continue
        owner = loc if r in player.get(loc, ()) else (route.id, None, 0)
        if any(c.loc == owner and c.rule is r for c in plan.suggest_remove):
            continue
        if compact:
            trial = {k: [x for x in v if not (k == owner and x is r)] for k, v in player.items()}
            if len(verify_compact(routing, network, merge(trial, plan.desired))) > base:  # breaks more
                continue
        plan.suggest_remove.append(Change(owner, r))


def _check_limits(network, player, wanted, plan) -> None:
    """Routes near the limit, and how many rules the route templates' per-stop Loads use there."""
    for route in network.routes:
        rid = route.id
        n = sum(len(rs) for loc, rs in player.items() if loc[0] == rid)
        n += sum(len(rs) for loc, rs in wanted.items() if loc[0] == rid)
        if n < NEAR_LIMIT:
            continue
        text = f"{route.name or rid} has {n} of {ROUTE_LIMIT} rules"
        # The same Load on every stop is one Global Rule's worth.
        per_stop: dict[tuple, int] = {}
        for loc, rs in player.items():
            if loc[0] == rid and loc[1] is not None:
                for r in rs:
                    if r.action == LOAD and r.parsed and not r.except_at:
                        k = (r.filter, r.target, r.except_cargo)
                        per_stop[k] = per_stop.get(k, 0) + 1
        repeats = {k: c for k, c in per_stop.items() if c >= 3}
        if repeats:
            used = sum(repeats.values())
            text += (f". {used} of them are the same {len(repeats)} Load rule{'s' * (len(repeats) != 1)} "
                     f"repeated on each stop: as Global Rules they'd take {len(repeats)}, freeing {used - len(repeats)}")
        plan.warnings.append(text)


def _names(network: Network, ports) -> str:
    return ", ".join(network.name(p) for p in sorted(ports))


def _loc_sort(item):
    (rid, port, visit) = item[0]
    return (rid, port is not None, port or "", visit)
