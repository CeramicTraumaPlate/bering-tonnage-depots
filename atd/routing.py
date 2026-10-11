"""Routing logic from the spec ("Routing logic" section). No game dependency.

For each route R and destination D, hops(R, D) is how many transfers R is from
D. Cargo always moves to something with a lower number, so it cannot loop.
Every iteration is over sorted ids, so input order never changes the output.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .model import Network, Route

INF = None  # hops value for "no chain within the transfer limit"

# Optional tie-break: remaining distance in km from a depot port to a destination.
DistanceFn = Callable[[str, str], "float | None"]


@dataclass
class RoutingPlan:
    """All decisions for one network. Keys use route ids and port ids."""

    routes: list[str]
    destinations: list[str]
    transfer_depots: dict[str, list[str]]           # depot port -> route ids meeting there
    hops: dict[tuple[str, str], int]                 # (route, dest) -> hops; missing = INF
    depot_hops: dict[tuple[str, str], int]           # (depot, dest) -> hops; missing = INF
    load: dict[tuple[str, str], frozenset[str]]      # (route, stop port) -> dests to load
    store: dict[tuple[str, str], frozenset[str]]     # (route, depot port) -> dests to store
    collect: dict[tuple[str, str], frozenset[str]]   # (route, depot port) -> dests to collect
    store_at: dict[tuple[str, str], str]             # (route, dest) -> chosen depot
    stored_here: dict[str, frozenset[str]]           # depot port -> dests any route stores there
    unreachable: dict[str, list[str]] = field(default_factory=dict)  # dest -> origin ports
    # (route, depot) -> every dest this route may store there: any depot one step closer.
    # Compact rules use this; whichever such depot the ship reaches first takes the cargo.
    store_ok: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    params: dict = field(default_factory=dict)  # plan_routes arguments, to re-plan a variant network

    def route_hops(self, route_id: str, dest: str) -> int | None:
        return self.hops.get((route_id, dest), INF)


def find_transfer_depots(routes: list[Route], depot_ports: Iterable[str]) -> dict[str, list[str]]:
    """Decision 1: an enabled depot where at least two active routes stop."""
    out = {}
    for port in sorted(set(depot_ports)):
        meeting = sorted(r.id for r in routes if port in r.port_set)
        if len(meeting) >= 2:
            out[port] = meeting
    return out


def plan_routes(
    network: Network,
    max_transfers: int = 3,
    disabled_routes: Iterable[str] = (),
    disabled_depots: Iterable[str] = (),
    extra_destinations: Iterable[str] = (),
    distance: DistanceFn | None = None,
    direct_first: bool = True,
) -> RoutingPlan:
    """direct_first=False drops decision 3: every route loads anything it can deliver.
    Cargo may then take a transfer when a direct ship would also come, but it still
    arrives, and the rules get much shorter (see rules.generate_compact)."""
    disabled_routes = set(disabled_routes)
    disabled_depots = set(disabled_depots)
    routes = sorted(
        (r for r in network.routes if r.active and r.id not in disabled_routes),
        key=lambda r: r.id,
    )
    route_by_id = {r.id: r for r in routes}
    depot_ports = [d.port for d in network.depots if d.enabled and d.port not in disabled_depots]
    transfer = find_transfer_depots(routes, depot_ports)

    routes_at: dict[str, list[str]] = defaultdict(list)
    for r in routes:
        for p in sorted(r.port_set):
            routes_at[p].append(r.id)
    depots_on = {r.id: sorted(p for p in r.port_set if p in transfer) for r in routes}

    destinations = sorted(set(routes_at) | set(extra_destinations))

    # Decision 2: breadth-first search outward from the routes that visit D.
    hops: dict[tuple[str, str], int] = {}
    depot_hops: dict[tuple[str, str], int] = {}
    for d in destinations:
        level = sorted(routes_at.get(d, []))
        for rid in level:
            hops[(rid, d)] = 0
        k = 0
        while level and k < max_transfers:
            next_level = set()
            for rid in level:
                for h in depots_on[rid]:
                    if (h, d) in depot_hops:
                        continue
                    depot_hops[(h, d)] = k
                    for other in transfer[h]:
                        if (other, d) not in hops:
                            hops[(other, d)] = k + 1
                            next_level.add(other)
            level = sorted(next_level)
            k += 1
        # Routes on the last level still define depot_hops for their depots.
        for rid in level:
            for h in depots_on[rid]:
                depot_hops.setdefault((h, d), k)

    # Decision 4 + 6: one store depot per (route, destination), chosen the same way every time.
    store_at: dict[tuple[str, str], str] = {}
    store_ok: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in routes:
        for d in destinations:
            n = hops.get((r.id, d))
            if not n:  # INF or 0: nothing to store
                continue
            candidates = [h for h in depots_on[r.id] if depot_hops.get((h, d)) == n - 1]
            store_at[(r.id, d)] = min(candidates, key=lambda h: _tie_key(h, d, distance))
            for h in candidates:
                store_ok[(r.id, h)].add(d)

    store: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (rid, d), h in store_at.items():
        store[(rid, h)].add(d)
    stored_here: dict[str, set[str]] = defaultdict(set)
    for (rid, h), ds in store.items():
        stored_here[h] |= ds

    # Decision 5: collect at H what this route is exactly depot_hops(H, D) away from.
    collect: dict[tuple[str, str], set[str]] = defaultdict(set)
    for h, members in transfer.items():
        for rid in members:
            for d in sorted(stored_here[h]):
                n = hops.get((rid, d))
                if d != h and n is not None and n == depot_hops.get((h, d)):
                    collect[(rid, h)].add(d)

    # Decision 3: at port P, only the routes with the lowest finite hops load D.
    load: dict[tuple[str, str], set[str]] = defaultdict(set)
    for p, members in routes_at.items():
        for d in destinations:
            if d == p:
                continue
            finite = [(hops[(rid, d)], rid) for rid in members if (rid, d) in hops]
            if not finite:
                continue
            best = min(n for n, _ in finite)
            for n, rid in finite:
                # Requirement 6: never load at the depot where this cargo is stored.
                if (n == best or not direct_first) and store_at.get((rid, d)) != p:
                    load[(rid, p)].add(d)

    # Requirement 4: report origins with no path.
    unreachable: dict[str, list[str]] = {}
    for d in destinations:
        origins = sorted(
            p for p, members in routes_at.items()
            if p != d and not any((rid, d) in hops for rid in members)
        )
        if origins:
            unreachable[d] = origins

    return RoutingPlan(
        routes=[r.id for r in routes],
        destinations=destinations,
        transfer_depots=transfer,
        hops=hops,
        depot_hops=depot_hops,
        load=_freeze(load),
        store=_freeze(store),
        collect=_freeze(collect),
        store_at=store_at,
        stored_here=_freeze(stored_here),
        unreachable=unreachable,
        store_ok=_freeze(store_ok),
        params=dict(max_transfers=max_transfers, disabled_routes=sorted(disabled_routes),
                    disabled_depots=sorted(disabled_depots), extra_destinations=sorted(set(extra_destinations)),
                    distance=distance, direct_first=direct_first),
    )


def _tie_key(depot: str, dest: str, distance: DistanceFn | None):
    km = distance(depot, dest) if distance else None
    return (km is None, km or 0.0, depot)


def _freeze(d: dict) -> dict:
    return {k: frozenset(v) for k, v in sorted(d.items()) if v}
