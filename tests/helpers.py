from __future__ import annotations

import copy
import random

from atd.model import Depot, Network, Route, Stop
from atd.planner import Plan
from atd.rules import stop_locations


def net(routes: dict[str, list[str]], depots=(), inactive=()) -> Network:
    """net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"])"""
    return Network(
        [Route(rid, [Stop(p) for p in ports], rid not in inactive, rid) for rid, ports in routes.items()],
        [Depot(p) for p in depots],
    )


def apply(network: Network, plan: Plan) -> Network:
    """Simulate the player applying the advisor's changes in game."""
    out = copy.deepcopy(network)
    for change in plan.remove:
        _rules_at(out, change.loc).remove(change.rule)
    for change in plan.add:
        _rules_at(out, change.loc).append(change.rule)
    return out


def _rules_at(network: Network, loc) -> list:
    route = network.route(loc[0])
    if loc[1] is None:
        return route.global_rules
    for l, stop in zip(stop_locations(route), route.stops):
        if l == loc:
            return stop.rules
    raise KeyError(loc)


def random_network(seed: int, n_ports=14, n_routes=6, n_depots=4) -> Network:
    rng = random.Random(seed)
    ports = [f"p{i:02d}" for i in range(n_ports)]
    routes = {}
    for i in range(n_routes):
        stops = rng.sample(ports, rng.randint(2, 5))
        if rng.random() < 0.3:  # out-and-back loop that revisits ports
            stops = stops + stops[-2:0:-1]
        routes[f"r{i}"] = stops
    inactive = [r for r in routes if rng.random() < 0.15]
    return net(routes, rng.sample(ports, n_depots), inactive)
