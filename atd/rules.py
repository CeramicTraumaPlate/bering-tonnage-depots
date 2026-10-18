"""Turn routing decisions into game rules, and evaluate rules back into sets.

A location is (route_id, port, visit): port None means the route's Global
Rules; otherwise it is the `visit`-th stop at `port` in the route's loop
(1-based). Visit numbers survive inserting other ports into the route.

The evaluator is the mod's own model of the game's filters. Rules are treated
as a union per action; the game's first-match ordering is not modelled.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .model import (COLLECT, LOAD, ONWARDS_PORTS, PORT, ROUTE_PORTS, STORE, THIS_ROUTE, WAIT,
                    Network, Route, Rule)
from .routing import RoutingPlan

Loc = tuple  # (route_id, port | None, visit)


def stop_locations(route: Route) -> list[Loc]:
    """One location per stop, in loop order."""
    seen: dict[str, int] = defaultdict(int)
    out = []
    for s in route.stops:
        seen[s.port] += 1
        out.append((route.id, s.port, seen[s.port]))
    return out


def existing_rules(network: Network) -> dict[Loc, list[Rule]]:
    """Every rule currently on the network, by location."""
    out: dict[Loc, list[Rule]] = {}
    for r in network.routes:
        if r.global_rules:
            out[(r.id, None, 0)] = list(r.global_rules)
        for loc, stop in zip(stop_locations(r), r.stops):
            if stop.rules:
                out[loc] = list(stop.rules)
    return out


# --------------------------------------------------------------------------- generation

def managed_routes(plan: RoutingPlan, network: Network) -> list[str]:
    """Routes that touch a transfer depot. All others are left entirely to the player."""
    return [rid for rid in plan.routes if network.route(rid).port_set & plan.transfer_depots.keys()]


def generate(plan: RoutingPlan, network: Network) -> dict[Loc, list[Rule]]:
    """The managed rules that express `plan`, before player rules and limits are considered."""
    out: dict[Loc, list[Rule]] = {}
    for rid in managed_routes(plan, network):
        route = network.route(rid)
        locs = stop_locations(route)
        ports = route.port_set

        # Global: own ports, then one Load rule per transfer destination with Except At.
        glob = [Rule(LOAD, ROUTE_PORTS, THIS_ROUTE)]
        transfer_dests = sorted({d for (r, p), ds in plan.load.items() if r == rid for d in ds} - ports)
        for d in transfer_dests:
            skip = tuple(sorted({p for p in ports if d not in plan.load.get((rid, p), ())}))
            glob.append(Rule(LOAD, PORT, d, except_at=skip))
        out[(rid, None, 0)] = glob

        # Depot stops: Unload, Store, Collect, on every visit.
        for loc in locs:
            h = loc[1]
            if h not in plan.transfer_depots:
                continue
            rules = []
            for d in sorted(plan.store.get((rid, h), ())):
                rules.append(Rule(STORE, PORT, d))
            coll = plan.collect.get((rid, h), frozenset())
            if coll & ports:
                rules.append(Rule(COLLECT, ROUTE_PORTS, THIS_ROUTE))
            for d in sorted(coll - ports):
                rules.append(Rule(COLLECT, PORT, d))
            if rules:
                out[loc] = rules
    return out


def generate_compact(plan: RoutingPlan, network: Network,
                     extra_collectors: dict[str, set[str]] | None = None,
                     existing: dict[Loc, list[Rule]] | None = None,
                     future_proof: bool = True,
                     extra_drops: dict[str, set[str]] | None = None,
                     no_onwards_collect: frozenset[str] | set[str] = frozenset()) -> dict[Loc, list[Rule]]:
    """Few, readable rules. Use with a plan made with direct_first=False.

    Cargo may be handed over at ANY depot one step closer to its destination
    (plan.store_ok); whichever the ship reaches first takes it. That lets one
    rule say "everything for JP N goes off at Tokyo" even when some JP N ports
    could also go via another hub. Cargo still always moves one step closer, so
    it can't loop.

    Every broad rule (`Route Ports | <route>`, `Onwards Ports | <hub>`, either one
    with `Except: Route Ports | This Route`) is only used where everything it
    matches is allowed at that place; what's left gets one Port rule each.

    extra_collectors: routes with player-written Collect rules per port, active or
    not. The game counts them in Onwards Ports, so the generator must too.

    existing: the rules in the game now. Any that are still valid are kept and
    only what's missing is added, so a network change doesn't reshuffle rules
    that already work.

    future_proof: plan as if a new feeder already joined every hub (a stand-in route to a
    stand-in port). Only self-updating rules (`Onwards Ports | <hub>`) can match a port
    that doesn't exist, so wherever the game's filters allow it, today's rules already
    hand on, store and collect cargo for routes added at any hub later. The stand-ins
    never appear in the output.

    no_onwards_collect: routes that must not get a Collect that uses Onwards Ports (as its filter
    or its Except). A route with Recursive Onwards Ports on that collects Onwards Ports cargo at
    one of its depots for another depot it calls at sends the game into an infinite loop (the
    developers' bug, confirmed on Discord). Such a Collect is written as Route Ports/Port rules.
    """
    existing = existing or {}
    if future_proof:
        net2, plan2 = with_future_feeders(plan, network)
        rules = _generate(plan2, net2, extra_collectors, existing, extra_drops, no_onwards_collect)
        return {loc: rs for loc, rs in rules.items() if not is_stand_in(loc[0])}
    return _generate(plan, network, extra_collectors, existing, extra_drops, no_onwards_collect)


STAND_IN = "~"  # prefix of the stand-in routes and ports used by future_proof


def is_stand_in(id_: str) -> bool:
    return id_.startswith(STAND_IN)


def with_future_feeders(plan: RoutingPlan, network: Network) -> tuple[Network, RoutingPlan]:
    """The network plus one stand-in feeder per transfer depot, and its routing plan."""
    from .model import Stop
    from .routing import plan_routes
    extra = [Route(f"{STAND_IN}feeder_{h}", [Stop(h), Stop(f"{STAND_IN}port_{h}")], True, f"{STAND_IN}{h}")
             for h in sorted(plan.transfer_depots)]
    net2 = Network(list(network.routes) + extra, list(network.depots))
    params = dict(plan.params or {})
    params["direct_first"] = False
    return net2, plan_routes(net2, **params)


def _generate(plan, network, extra_collectors, existing, extra_drops=None, no_onwards_collect=frozenset()):
    # Collect rules depend on where cargo is actually dropped off, which depends on the
    # Store rules, which can use Onwards Ports, which depends on who collects. Start from
    # "anything that could be dropped there" (always valid) and narrow it to what is
    # actually dropped there until nothing changes.
    waiting = in_depots(plan, network)
    for h, ds in (extra_drops or {}).items():  # dropped by the player's own Stores
        if h in plan.transfer_depots:
            waiting.setdefault(h, set()).update(d for d in ds if d != h and (h, d) in plan.depot_hops)
    ctx = compact_context(plan, network, extra_collectors, waiting=waiting)
    first, actual = _generate_once(plan, network, existing, ctx, no_onwards_collect)
    out = first
    for _ in range(4):
        for h, ds in waiting.items():  # already in the depot, whatever rules put it there
            actual[h] |= ds
        narrowed = collect_wants(plan, ctx.ports, actual)
        if narrowed == ctx.collect_want:
            return out
        ctx = compact_context(plan, network, extra_collectors, narrowed, waiting=waiting)
        out, actual = _generate_once(plan, network, existing, ctx, no_onwards_collect)
    return first  # didn't settle: the broad version is always valid


def _generate_once(plan, network, existing, ctx, no_onwards_collect=frozenset()):
    out: dict[Loc, list[Rule]] = {}
    actual: dict[str, set[str]] = defaultdict(set)  # depot -> dests actually dropped off there
    # A stand-in route can't be named, and a stand-in port can't get a Port rule.
    route_cands = [((ROUTE_PORTS, rid, None), ctx.ports[rid]) for rid in plan.routes if not is_stand_in(rid)]
    onward_cands = [((ONWARDS_PORTS, h, None), m) for h, m in sorted(ctx.onwards.items())]

    for rid in managed_routes(plan, network):
        route = network.route(rid)
        own = ctx.ports[rid]
        transfer = {d for (r, d) in plan.store_at if r == rid}
        carried = set(own) | transfer
        depots = [h for h in sorted(own) if h in plan.transfer_depots]
        others = [c for c in route_cands if c[0][1] != rid]
        plain = onward_cands + others  # self-updating rules first: new routes at a hub join them
        # The same rules with one Except: this route's ports, another route's, or another hub's.
        excepts = ([((ROUTE_PORTS, THIS_ROUTE), own)] + [(k[:2], m) for k, m in onward_cands]
                   + [(k[:2], m) for k, m in others])
        excepted = [((f, t, ek), m - em) for (f, t, _), m in plain for ek, em in excepts
                    if ek != (f, t) and m & em and m - em]
        # On equal coverage, self-updating beats naming a route: it keeps working when routes
        # join a hub. So Onwards Ports first, and an Except on This Route or a hub before one
        # naming a route.
        store_cands = sorted(plain + excepted, key=_self_updating_rank)

        # Store: fewest rules, over all this route's hubs, that hand every transfer destination on.
        options = []
        for h in depots:
            ok = plan.store_ok.get((rid, h), frozenset())
            harmless = {h} | (ctx.universe - carried)
            for key, m in store_cands:
                if (m - harmless) <= ok and m & ok:
                    options.append((h, key, m & ok))
        remaining, chosen = set(transfer), []
        for loc in stop_locations(route):
            h = loc[1]
            if h not in depots:
                continue
            ok = plan.store_ok.get((rid, h), frozenset())
            harmless = {h} | (ctx.universe - carried)
            for rule in existing.get(loc, ()):
                key = (rule.filter, rule.target, rule.except_cargo)
                m = _key_ports(key, own, ctx) if rule.action == STORE and rule.parsed else None
                if (m is not None and (m - harmless) <= ok and m & ok & remaining
                        and not any(c[0] == h and c[1] == key for c in chosen)):
                    chosen.append((h, key, m & ok))
                    remaining -= m & ok
        while _real(remaining):
            best = max(options, key=lambda o: _gain(o[2], remaining), default=None)
            if best is None or not _real(best[2] & remaining):
                break
            chosen.append(best)
            remaining -= best[2]
        for d in sorted(remaining):
            if is_stand_in(d) and not is_stand_in(rid):
                continue  # only a self-updating rule could hand it on
            h = next(h for h in depots if d in plan.store_ok.get((rid, h), ()))
            chosen.append((h, (PORT, d, None), {d}))
        dropped_at: dict[str, set[str]] = defaultdict(set)  # dest -> depots where it goes off
        for h, _, cov in chosen:
            if not is_stand_in(rid):  # a future route's cargo doesn't earn anyone a Collect today
                actual[h] |= cov
            for d in cov:
                dropped_at[d].add(h)
        # Cargo for a ship-less route's ports may be loaded only if a Store here drops it at a
        # depot to wait; left aboard it would ride round and round (the day-1804 Naha cargo).
        idle_ok = ctx.idle & set().union(*[_key_ports(key, own, ctx) or set() for _, key, _ in chosen])

        # Load: own ports, then anything it can hand on, not at the first depot it goes off at.
        # A rule excepted at H may also load cargo that goes off at H among other depots:
        # it isn't loaded at H, and anywhere else it's simply carried to its next depot.
        glob = [Rule(LOAD, ROUTE_PORTS, THIS_ROUTE)]
        by_first: dict[str, set[str]] = defaultdict(set)
        for d in transfer:
            if dropped_at[d]:
                by_first[min(dropped_at[d])].add(d)
        for h in sorted(by_first):
            want = set(by_first[h])
            allowed = want | own | {d for d in transfer if h in dropped_at[d]} | idle_ok
            keep = [((r.filter, r.target, r.except_cargo), _key_ports((r.filter, r.target, r.except_cargo), own, ctx))
                    for r in existing.get((rid, None, 0), ())
                    if r.action == LOAD and r.parsed and r.except_at == (h,)]
            for f, t, exc in _cover(want, store_cands, allowed=allowed, preferred=keep, no_port=is_stand_in):
                glob.append(Rule(LOAD, f, t, except_at=(h,), except_cargo=exc))
        out[(rid, None, 0)] = glob

        # Depot stops: Store, then Collect, on every visit.
        for loc in stop_locations(route):
            h = loc[1]
            if h not in plan.transfer_depots:
                continue
            rules = []
            for hh, (f, t, exc), _ in chosen:
                if hh == h:
                    rules.append(Rule(STORE, f, t, except_cargo=exc))
            want = ctx.collect_want.get((rid, h), set())
            harmless = ctx.universe - ctx.potential.get(h, set())  # can never be in this depot
            cands = [((ROUTE_PORTS, THIS_ROUTE, None), own)] + sorted(plain + excepted, key=_self_updating_rank)
            keep = [((x.filter, x.target, x.except_cargo), _key_ports((x.filter, x.target, x.except_cargo), own, ctx))
                    for x in existing.get(loc, ()) if x.action == COLLECT and x.parsed]
            if rid in no_onwards_collect:
                cands = [c for c in cands if not _uses_onwards(c[0])]
                keep = [k for k in keep if not _uses_onwards(k[0])]
            # Collecting anything this route is exactly one step on for is also safe, so broad
            # (self-updating) rules can cover what's wanted here.
            closer = {d for d in ctx.potential.get(h, ()) if d != h and plan.depot_hops.get((h, d)) is not None
                      and plan.hops.get((rid, d)) == plan.depot_hops[(h, d)]}
            for f, t, exc in _cover(want, cands, allowed=want | harmless | closer, preferred=keep,
                                    no_port=is_stand_in):
                rules.append(Rule(COLLECT, f, t, except_cargo=exc))
            if rules:
                out[loc] = rules
    return out, actual


@dataclass
class CompactContext:
    ports: dict[str, frozenset[str]]               # every route's ports, with or without a ship
    universe: set[str]
    potential: dict[str, set[str]]                 # depot -> dests that may ever be stored there
    collect_want: dict[tuple[str, str], set[str]]  # (route, depot) -> dests it must collect there
    onwards: dict[str, set[str]]                   # depot -> what Onwards Ports | depot matches
    # Ports only routes without a ship visit. Loading cargo for them is fine: it waits at the
    # hub until that route gets a ship. Without this, a ship-less route at a hub would make
    # every self-updating Load there swap to a narrower rule, and back once it has a ship.
    idle: set[str] = field(default_factory=set)


def in_depots(plan: RoutingPlan, network: Network) -> dict[str, set[str]]:
    """Transfer depot -> destinations of cargo sitting in it now that some route can move on."""
    out: dict[str, set[str]] = {}
    for dep in network.depots:
        if dep.port in plan.transfer_depots:
            ds = {d for d, teu in dep.stored_by_destination.items()
                  if teu and d != dep.port and (dep.port, d) in plan.depot_hops}
            if ds:
                out[dep.port] = ds
    return out


def collect_wants(plan: RoutingPlan, ports: dict[str, frozenset[str]],
                  dropped: dict[str, set[str]]) -> dict[tuple[str, str], set[str]]:
    """Who collects what at each hub, given what may be dropped off there.

    Only a route exactly one step on collects. Cargo for a route's own ports: every
    route that goes there collects it. Cargo passing through to a further hub: one
    collector is enough (lowest route id), plus its twins (same ports), so twins keep
    identical rules and can be set up with the game's Copy All.
    """
    want: dict[tuple[str, str], set[str]] = {}
    for h, members in plan.transfer_depots.items():
        for d in sorted(dropped.get(h, ())):
            if d == h:
                continue
            step = plan.depot_hops.get((h, d))
            able = [rid for rid in members if plan.hops.get((rid, d)) is not None and plan.hops[(rid, d)] == step]
            if step and able:
                able = [rid for rid in able if ports[rid] == ports[able[0]]]
            for rid in able:
                want.setdefault((rid, h), set()).add(d)
    return want


def compact_context(plan: RoutingPlan, network: Network,
                    extra_collectors: dict[str, set[str]] | None = None,
                    collect_want: dict[tuple[str, str], set[str]] | None = None,
                    waiting: dict[str, set[str]] | None = None) -> CompactContext:
    ports = {r.id: r.port_set for r in network.routes}
    universe = set(plan.destinations).union(*ports.values()) if ports else set(plan.destinations)
    potential: dict[str, set[str]] = defaultdict(set)
    for (rid, h), ds in plan.store_ok.items():
        potential[h] |= ds
    for h, ds in (in_depots(plan, network) if waiting is None else waiting).items():
        potential[h] |= ds
    if collect_want is None:
        collect_want = collect_wants(plan, ports, potential)
    collectors: dict[str, set[str]] = defaultdict(set)
    for (rid, h) in collect_want:
        collectors[h].add(rid)
    for h, rids in (extra_collectors or {}).items():
        collectors[h] |= {r for r in rids if r in ports}
    onwards = {h: set().union(*(ports[r] for r in rids)) - {h}
               for h, rids in collectors.items() if h in plan.transfer_depots and rids}
    return CompactContext(ports, universe, dict(potential), collect_want, onwards, idle_ports(plan, network))


def idle_ports(plan: RoutingPlan, network: Network) -> set[str]:
    """Ports that only routes without a ship (or switched off) visit."""
    active = set(plan.routes)
    served = {p for r in network.routes if r.id in active for p in r.port_set}
    return {p for r in network.routes if r.id not in active for p in r.port_set} - served


def verify_compact(plan: RoutingPlan, network: Network, rules: dict[Loc, list[Rule]],
                   extra_collectors: dict[str, set[str]] | None = None) -> list[str]:
    """Check compact rules move every box closer to its destination and none get stuck.

    For every managed route, evaluating `rules` with the model of the filters:
      Load    only cargo it can deliver, and at a non-depot stop all of it;
      Store   only cargo for which that depot is one step closer;
      every transfer destination is stored at some depot on the route;
      Collect  everything any route stores at a depot is collected there by some route
               exactly one step closer, and no route collects stored cargo it isn't one step
               closer for (that would carry it away).
    """
    problems = []
    ctx = compact_context(plan, network, extra_collectors)
    collectors = collectors_of(rules)
    for h, rids in (extra_collectors or {}).items():
        collectors.setdefault(h, set()).update(rids)
    stored: dict[str, set[str]] = defaultdict(set)              # depot -> dests stored there
    for h, ds in in_depots(plan, network).items():                # including what's there already
        stored[h] |= ds
    collected: dict[str, dict[str, set[str]]] = defaultdict(dict)  # depot -> route -> dests it collects
    for rid in managed_routes(plan, network):
        route = network.route(rid)
        transfer = {d for (r, d) in plan.store_at if r == rid}
        deliverable = {d for d in plan.destinations if (rid, d) in plan.hops}
        carried = set(route.port_set) | transfer
        stored_somewhere: set[str] = set()
        # Where this route drops each destination off, to spot full-loop detours below.
        drops_at: dict[str, set[str]] = defaultdict(set)
        stores_any: set[str] = set()
        for loc in stop_locations(route):
            eff = evaluate_stop(route, loc, rules, network, collectors)
            for d in (eff.sets[STORE] & carried) - {loc[1]}:
                drops_at[d].add(loc[1])
            if loc[1] in plan.transfer_depots:
                stores_any |= eff.sets[STORE]
        for loc in stop_locations(route):
            p = loc[1]
            eff = evaluate_stop(route, loc, rules, network, collectors)
            if eff.unknown:
                problems.append(f"{rid} @ {p}#{loc[2]}: cannot evaluate {len(eff.unknown)} rule(s)")
            got = eff.sets[LOAD] - {p}
            may_skip = set(plan.store_ok.get((rid, p), ()))
            waits = ctx.idle & stores_any  # for a ship-less route's ports, dropped at a hub to wait
            if not got <= deliverable | waits:
                problems.append(f"{rid} @ {p}#{loc[2]} Load: can't deliver {sorted(got - deliverable - waits)}")
            detour = {d for d in got if drops_at.get(d) == {p}}
            if detour:
                problems.append(f"{rid} @ {p}#{loc[2]} Load: picks up {sorted(detour)} where it's dropped off, "
                                f"so it rides a whole loop first")
            if not (deliverable - {p} - may_skip) <= got:
                problems.append(f"{rid} @ {p}#{loc[2]} Load: misses {sorted(deliverable - {p} - may_skip - got)}")
            got = (eff.sets[STORE] & carried) - {p}
            ok = set(plan.store_ok.get((rid, p), ()))
            if not got <= ok:
                problems.append(f"{rid} @ {p}#{loc[2]} Store: not a step closer {sorted(got - ok)}")
            stored_somewhere |= got
            stored[p] |= got
            if p in plan.transfer_depots:
                collected[p][rid] = collected[p].get(rid, set()) | eff.sets[COLLECT]
        if not transfer <= stored_somewhere:
            problems.append(f"{rid}: never hands on {sorted(transfer - stored_somewhere)}")
    for h, ds in stored.items():
        for d in sorted(ds):
            step = plan.depot_hops.get((h, d))
            takers = [rid for rid, got in collected[h].items() if d in got]
            wrong = [rid for rid in takers if plan.hops.get((rid, d)) != step]
            if wrong:
                problems.append(f"{h} Collect: {sorted(wrong)} would carry {d} away from it")
            if not [rid for rid in takers if plan.hops.get((rid, d)) == step]:
                problems.append(f"{h} Collect: nobody picks up {d}")
    return problems


def _uses_onwards(key) -> bool:
    """True if a (filter, target, except_cargo) key uses Onwards Ports as its filter or its Except."""
    f, _, exc = key
    return f == ONWARDS_PORTS or bool(exc and exc[0] == ONWARDS_PORTS)


def _self_updating_rank(cand) -> tuple[int, int]:
    """Sort key: Onwards Ports before Route Ports; no Except, then This Route, a hub, a route."""
    (f, _, exc), _ = cand
    exc_rank = 0 if not exc else 1 if exc[1] == THIS_ROUTE else 2 if exc[0] == ONWARDS_PORTS else 3
    return (f != ONWARDS_PORTS, exc_rank)


def _carried(plan: RoutingPlan, rid: str, own) -> set[str]:
    """Destinations this route can ever have aboard: its own ports, what it loads, what it stores."""
    loads = {d for (r, _), ds in plan.load.items() if r == rid for d in ds}
    stores = {d for (r, d) in plan.store_at if r == rid}
    return set(own) | loads | stores


def _key_ports(key, own, ctx) -> set[str] | None:
    """What a rule (filter, target, except_cargo) matches in the compact model, or None if unknown."""
    def match(f, t):
        if f == PORT:
            return {t}
        if f == ROUTE_PORTS:
            return set(own) if t == THIS_ROUTE else (set(ctx.ports[t]) if t in ctx.ports else None)
        if f == ONWARDS_PORTS:
            return set(ctx.onwards.get(t, ()))
        return None
    f, t, exc = key
    m = match(f, t)
    if m is None or not exc:
        return m
    ex = match(*exc)
    return None if ex is None else m - ex


def _cover(want: set[str], candidates, allowed: set[str], preferred=(),
           no_port=lambda d: False) -> list[tuple[str, str, tuple | None]]:
    """Greedy set cover: broad rules whose matches are all allowed, then Port rules.

    Candidates are ((filter, target, except_cargo), ports). `preferred` (rules
    already in the game) are taken first wherever they're valid and still useful.
    Stand-in ports (future-proofing) never earn a rule of their own: they only break
    ties between rules that cover the same real ports. On equal coverage the earlier
    candidate wins."""
    remaining = set(want)
    chosen = []
    for key, m in preferred:
        if m is not None and set(m) <= allowed and set(m) & remaining and key not in chosen:
            chosen.append(key)
            remaining -= set(m)
    valid = [(key, set(m)) for key, m in candidates if set(m) <= allowed and set(m) & want]
    while _real(remaining):
        best = max(valid, key=lambda km: _gain(km[1], remaining), default=None)
        if best is None or not _real(best[1] & remaining):
            break
        chosen.append(best[0])
        remaining -= best[1]
    chosen = [(k[0], k[1], k[2] if len(k) > 2 else None) for k in chosen]
    return chosen + [(PORT, d, None) for d in sorted(remaining) if not no_port(d)]


def _real(ports) -> set[str]:
    return {d for d in ports if not is_stand_in(d)}


def _gain(m, remaining) -> tuple[int, int]:
    """Real ports a rule still covers, then stand-in ports (future routes) as the tie-break."""
    got = m & remaining
    real = _real(got)
    return (len(real), len(got) - len(real))


# --------------------------------------------------------------------------- evaluation

@dataclass
class Effective:
    """What the rules at one stop do. `unknown` lists rules the model cannot evaluate."""

    sets: dict[str, set[str]]
    unknown: list[Rule]


def evaluate_stop(route: Route, loc: Loc, rules: dict[Loc, list[Rule]], network: Network,
                  collectors: dict[str, set[str]] | None = None) -> Effective:
    """Union of the stop's own rules and the route's global rules that apply at this port.

    `collectors` (who collects at each depot, for Onwards Ports) defaults to the
    Collect rules in `rules`; pass the whole network's when `rules` is a subset.
    """
    port = loc[1]
    sets: dict[str, set[str]] = defaultdict(set)
    unknown = []
    candidates = list(rules.get(loc, ())) + [
        g for g in rules.get((route.id, None, 0), ()) if port not in g.except_at
    ]
    if collectors is None:
        collectors = collectors_of(rules)
    for rule in candidates:
        if rule.action == WAIT:  # holds the ship, moves no cargo
            continue
        dests = rule_destinations(rule, route, network, collectors)
        if dests is None:
            unknown.append(rule)
        else:
            sets[rule.action] |= dests
    return Effective(sets, unknown)


def rule_destinations(rule: Rule, route: Route, network: Network,
                      collectors: dict[str, set[str]] | None = None) -> set[str] | None:
    """Destinations a rule matches (its filter minus its Except cargo), or None if unknown."""
    if not rule.parsed:
        return None
    dests = filter_destinations(rule.filter, rule.target, route, network, collectors, rule.recursive)
    if dests is None or not rule.except_cargo:
        return dests
    excluded = filter_destinations(*rule.except_cargo, route, network, collectors)
    return None if excluded is None else dests - excluded


def filter_destinations(filt: str, target: str, route: Route, network: Network,
                        collectors: dict[str, set[str]] | None = None,
                        recursive: bool = False) -> set[str] | None:
    """Ports a filter + target matches, or None if the model can't tell."""
    if filt == PORT:
        return {target}
    if filt == ROUTE_PORTS:
        if target == THIS_ROUTE:
            return set(route.port_set)
        try:
            return set(network.route(target).port_set)
        except KeyError:
            return None
    if filt == ONWARDS_PORTS and recursive:
        return onwards_closure(target, network, collectors or {})
    if filt == ONWARDS_PORTS:
        # Ports of routes with a Collect rule at the target's depot, excluding the target (v1.0.2).
        ports: set[str] = set()
        for rid in (collectors or {}).get(target, ()):
            ports |= network.route(rid).port_set
        return ports - {target}
    return None  # Region and anything else: no data yet


def onwards_closure(hub: str, network: Network, collectors: dict[str, set[str]]) -> set[str]:
    """Recursive Onwards Ports | hub: ports of routes collecting at the hub, then of routes
    collecting at any depot among those ports, and so on until the network is exhausted
    (the developer's description, v1.0.4). The hub itself is left out."""
    depots = {d.port for d in network.depots if d.enabled}
    seen, queue, ports = {hub}, [hub], set()
    while queue:
        h = queue.pop()
        for rid in sorted(collectors.get(h, ())):
            try:
                ps = network.route(rid).port_set
            except KeyError:
                continue
            ports |= ps
            for p in ps:
                if p in depots and p not in seen:
                    seen.add(p)
                    queue.append(p)
    return ports - {hub}


def collectors_of(rules: dict[Loc, list[Rule]]) -> dict[str, set[str]]:
    """Port -> routes with a Collect rule on a stop at that port."""
    out: dict[str, set[str]] = defaultdict(set)
    for (rid, port, _), rs in rules.items():
        if port is not None and any(r.action == COLLECT for r in rs):
            out[port].add(rid)
    return out


def merge(*rule_maps: dict[Loc, list[Rule]]) -> dict[Loc, list[Rule]]:
    out: dict[Loc, list[Rule]] = defaultdict(list)
    for m in rule_maps:
        for loc, rs in m.items():
            out[loc].extend(rs)
    return dict(out)


def verify(plan: RoutingPlan, network: Network, rules: dict[Loc, list[Rule]]) -> list[str]:
    """Acceptance test 8: evaluating `rules` gives the plan's load, store and collect sets.

    Load sets ignore cargo for the stop's own port. Collect sets are compared
    only on destinations some route stores at that depot; collecting other
    destinations is harmless because they never arrive there.
    """
    problems = []
    for rid in managed_routes(plan, network):
        route = network.route(rid)
        for loc in stop_locations(route):
            p = loc[1]
            eff = evaluate_stop(route, loc, rules, network)
            if eff.unknown:
                problems.append(f"{rid} @ {p}#{loc[2]}: cannot evaluate {len(eff.unknown)} rule(s)")
            want_load = set(plan.load.get((rid, p), ()))
            got_load = eff.sets[LOAD] - {p}
            if got_load != want_load:
                problems.append(_diff(rid, loc, LOAD, want_load, got_load))
            # Store: cargo for this port is delivered on arrival, and cargo this
            # route never carries can't be stored, so compare only what it carries.
            carried = _carried(plan, rid, route.port_set)
            want_store = set(plan.store.get((rid, p), ()))
            got_store = (eff.sets[STORE] & carried) - {p}
            if got_store != want_store:
                problems.append(_diff(rid, loc, STORE, want_store, got_store))
            relevant = set(plan.stored_here.get(p, ()))
            want_coll = set(plan.collect.get((rid, p), ()))
            got_coll = eff.sets[COLLECT] & relevant
            if got_coll != want_coll:
                problems.append(_diff(rid, loc, COLLECT, want_coll, got_coll))
    return problems


def _diff(rid, loc, action, want, got) -> str:
    return (f"{rid} @ {loc[1]}#{loc[2]} {action}: missing {sorted(want - got)}, "
            f"extra {sorted(got - want)}")


# --------------------------------------------------------------------------- display

def _route_name(network: Network, rid: str) -> str:
    try:
        return network.route(rid).name or rid
    except KeyError:
        return rid


def render(rule: Rule, network: Network | None = None) -> str:
    """Rule as the player enters it in the UI: Action | Amount | Filter | Target [| Except At ...]."""
    if not rule.parsed:
        return rule.raw or rule.action
    name = (lambda p: network.name(p)) if network else (lambda p: p)
    if rule.filter in (PORT, ONWARDS_PORTS):
        target = name(rule.target)
    elif rule.filter == ROUTE_PORTS and rule.target != THIS_ROUTE and network:
        target = _route_name(network, rule.target)
    else:
        target = rule.target
    text = f"{rule.action} | {rule.amount} | {rule.filter} | {target}"
    if rule.except_at:
        text += " | Except At: " + ", ".join(name(p) for p in rule.except_at)
    if rule.except_cargo:
        f, t = rule.except_cargo
        if f in (PORT, ONWARDS_PORTS):
            t = name(t)
        elif f == ROUTE_PORTS and t != THIS_ROUTE and network:
            t = _route_name(network, t)
        text += f" | Except: {f} | {t}"
    return text
