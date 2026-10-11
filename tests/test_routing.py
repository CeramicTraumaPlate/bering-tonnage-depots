"""Library acceptance tests 1-8 from the spec."""

import random
import unittest

from atd.model import COLLECT, LOAD, ONWARDS_PORTS, PORT, ROUTE_PORTS, STORE, THIS_ROUTE
from atd.routing import plan_routes
from atd.rules import generate, generate_compact, verify, verify_compact

from helpers import net, random_network

WORKED = {
    "jp_north": ["tokyo", "sendai", "tomakomai"],
    "sea_of_japan": ["tomakomai", "niigata", "hakata"],
    "pacific": ["tokyo", "oakland"],
}


class LibraryAcceptance(unittest.TestCase):
    def test_1_two_routes_share_a_depot(self):
        n = net({"A": ["a1", "a2", "h"], "B": ["h", "b1", "b2"]}, depots=["h"])
        p = plan_routes(n)
        self.assertEqual(p.transfer_depots, {"h": ["A", "B"]})
        for own, other in (("A", "b"), ("B", "a")):
            for port in n.route(own).ports:
                self.assertEqual(p.route_hops(own, port), 0)
            self.assertEqual(p.route_hops(own, f"{other}1"), 1)
            self.assertEqual(p.route_hops(own, f"{other}2"), 1)
        self.assertEqual(p.store[("A", "h")], {"b1", "b2"})
        self.assertEqual(p.store[("B", "h")], {"a1", "a2"})
        self.assertEqual(p.collect[("B", "h")], p.store[("A", "h")])
        self.assertEqual(p.collect[("A", "h")], p.store[("B", "h")])

    def test_2_worked_example(self):
        p = plan_routes(net(WORKED, depots=["tokyo", "tomakomai"]))
        self.assertEqual(p.route_hops("pacific", "oakland"), 0)
        self.assertEqual(p.route_hops("jp_north", "oakland"), 1)
        self.assertEqual(p.route_hops("sea_of_japan", "oakland"), 2)
        # Sea of Japan loads at Niigata and stores at Tomakomai.
        self.assertIn("oakland", p.load[("sea_of_japan", "niigata")])
        self.assertEqual(p.store_at[("sea_of_japan", "oakland")], "tomakomai")
        # JP North collects at Tomakomai and stores at Tokyo.
        self.assertIn("oakland", p.collect[("jp_north", "tomakomai")])
        self.assertEqual(p.store_at[("jp_north", "oakland")], "tokyo")
        # Pacific collects at Tokyo and delivers.
        self.assertIn("oakland", p.collect[("pacific", "tokyo")])
        self.assertNotIn(("pacific", "oakland"), p.store_at)

    def test_3_no_shared_depot(self):
        n = net({"A": ["a1", "h1"], "B": ["h2", "b1"]}, depots=["h1", "h2"])
        p = plan_routes(n)
        self.assertEqual(p.transfer_depots, {})
        self.assertIsNone(p.route_hops("A", "b1"))
        self.assertIsNone(p.route_hops("B", "a1"))
        self.assertEqual(generate(p, n), {})

    def test_4_shared_port_without_depot(self):
        p = plan_routes(net({"A": ["a1", "x"], "B": ["x", "b1"]}))
        self.assertEqual(p.transfer_depots, {})
        self.assertIsNone(p.route_hops("A", "b1"))
        self.assertEqual(p.store, {})
        self.assertIn("b1", p.unreachable)

    def test_5_direct_route_wins(self):
        n = net({"via": ["p", "h", "x"], "spoke": ["h", "d"], "direct": ["p", "d"]}, depots=["h"])
        p = plan_routes(n)
        self.assertEqual(p.route_hops("via", "d"), 1)
        self.assertEqual(p.route_hops("direct", "d"), 0)
        self.assertIn("d", p.load[("direct", "p")])
        self.assertNotIn("d", p.load.get(("via", "p"), set()))
        # `via` still carries d-bound cargo it picks up at x.
        self.assertIn("d", p.load[("via", "x")])

    def test_6_route_order_does_not_matter(self):
        for seed in range(20):
            n = random_network(seed)
            shuffled = random_network(seed)
            random.Random(seed).shuffle(shuffled.routes)
            random.Random(seed + 1).shuffle(shuffled.depots)
            a, b = plan_routes(n), plan_routes(shuffled)
            self.assertEqual(a, b)
            self.assertEqual(generate(a, n), generate(b, shuffled))

    def test_7_max_transfers(self):
        p = plan_routes(net(WORKED, depots=["tokyo", "tomakomai"]), max_transfers=1)
        self.assertEqual(p.route_hops("jp_north", "oakland"), 1)
        self.assertIsNone(p.route_hops("sea_of_japan", "oakland"))
        self.assertIn("niigata", p.unreachable["oakland"])
        self.assertNotIn(("sea_of_japan", "oakland"), p.store_at)

    def test_8_generated_rules_reproduce_decisions(self):
        cases = [net(WORKED, depots=["tokyo", "tomakomai"]),
                 net({"A": ["a1", "a2", "h"], "B": ["h", "b1", "b2"]}, depots=["h"])]
        cases += [random_network(seed) for seed in range(200)]
        for n in cases:
            p = plan_routes(n)
            self.assertEqual(verify(p, n, generate(p, n)), [], n)


class CompactRules(unittest.TestCase):
    def test_compact_rules_reproduce_decisions(self):
        cases = [net(WORKED, depots=["tokyo", "tomakomai"])]
        cases += [random_network(seed) for seed in range(200)]
        for n in cases:
            p = plan_routes(n, direct_first=False)
            self.assertEqual(verify_compact(p, n, generate_compact(p, n)), [], n)

    def test_future_proof_rules_pass_self_check(self):
        cases = [net(WORKED, depots=["tokyo", "tomakomai"])] + [random_network(seed) for seed in range(200)]
        for n in cases:
            p = plan_routes(n, direct_first=False)
            self.assertEqual(verify_compact(p, n, generate_compact(p, n, future_proof=True)), [], n)

    def test_future_proof_new_route_at_existing_hub_changes_nothing_else(self):
        from atd.model import Route, Stop
        from atd.planner import make_plan
        from helpers import apply
        n = net({"A": ["a1", "h"], "B": ["h", "b1"], "C": ["b1", "g"], "D": ["g", "d1"]}, depots=["h", "g"])
        for _ in range(2):  # set everything up
            plan = make_plan(plan_routes(n, direct_first=False), n, {(l[0], l[1], l[2], r.key())
                             for l, rs in __import__("atd.rules", fromlist=["x"]).existing_rules(n).items() for r in rs},
                             compact=True, future_proof=True)
            n = apply(n, plan)
        record = {(l[0], l[1], l[2], r.key()) for l, rs in plan.record.items() for r in rs}
        n.routes.append(Route("E", [Stop("h"), Stop("e1")], True, "E"))  # joins hub h
        plan = make_plan(plan_routes(n, direct_first=False), n, record, compact=True, future_proof=True)
        others = [c for c in plan.add + plan.remove if c.loc[0] != "E"]
        self.assertEqual(others, [], [(c.loc, c.rule) for c in others])

    def test_self_check_catches_broken_rules(self):
        from atd.model import Rule
        caught = {"no collect": 0, "stores own": 0, "loads undeliverable": 0}
        for seed in range(40):
            n = random_network(seed)
            p = plan_routes(n, direct_first=False)
            good = generate_compact(p, n)
            if not good:
                continue
            # 1. Drop every Collect rule of one route.
            loc = next((l for l, rs in good.items() if any(r.action == COLLECT for r in rs)), None)
            if loc:
                bad = {**good, loc: [r for r in good[loc] if r.action != COLLECT]}
                caught["no collect"] += bool(verify_compact(p, n, bad))
            # 2. Store the route's own cargo at its hub.
            loc = next((l for l in good if l[1] is not None), None)
            if loc:
                bad = {**good, loc: good[loc] + [Rule(STORE, ROUTE_PORTS, THIS_ROUTE)]}
                caught["stores own"] += bool(verify_compact(p, n, bad))
            # 3. Load cargo for a port nobody can reach.
            g = next(l for l in good if l[1] is None)
            bad = {**good, g: good[g] + [Rule(LOAD, PORT, "nowhere")]}
            caught["loads undeliverable"] += bool(verify_compact(p, n, bad))
        for kind, n in caught.items():
            self.assertGreater(n, 10, kind)

    def test_one_rule_per_hub(self):
        n = net({"A": ["a1", "a2", "h"], "B": ["h", "b1", "b2"], "C": ["h", "c1", "c2", "c3"]}, depots=["h"])
        p = plan_routes(n, direct_first=False)
        rules = generate_compact(p, n)
        # Store everything onwards from the hub except this route's own cargo (the Discord pattern).
        store = [(r.filter, r.target, r.except_cargo) for r in rules[("A", "h", 1)] if r.action == STORE]
        self.assertEqual(store, [(ONWARDS_PORTS, "h", (ROUTE_PORTS, THIS_ROUTE))])
        # Own ports, then one Onwards Ports rule covering both neighbours at the hub.
        self.assertEqual([(r.filter, r.target, r.except_at) for r in rules[("A", None, 0)]],
                         [(ROUTE_PORTS, THIS_ROUTE, ()), (ONWARDS_PORTS, "h", ("h",))])

    def test_shared_port_is_left_out(self):
        # A and B share x as well as the hub: storing "Route Ports | B" would also store x,
        # which A delivers itself. The Except leaves A's own ports (x included) out.
        n = net({"A": ["a1", "x", "h"], "B": ["h", "x", "b1"]}, depots=["h"])
        p = plan_routes(n, direct_first=False)
        store = [(r.filter, r.target, r.except_cargo) for r in generate_compact(p, n)[("A", "h", 1)]
                 if r.action == STORE]
        self.assertEqual(store, [(ONWARDS_PORTS, "h", (ROUTE_PORTS, THIS_ROUTE))])

    def test_compact_is_much_shorter(self):
        for seed in range(30):
            n = random_network(seed, n_ports=30, n_routes=8)
            full = sum(map(len, generate(plan_routes(n), n).values()))
            compact = sum(map(len, generate_compact(plan_routes(n, direct_first=False), n).values()))
            self.assertLessEqual(compact, full)


class EdgeCases(unittest.TestCase):
    def test_destination_is_the_depot(self):
        p = plan_routes(net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"]))
        self.assertNotIn("h", p.stored_here["h"])
        self.assertIn("h", p.load[("A", "a1")])  # delivered directly

    def test_no_reload_at_store_depot(self):
        p = plan_routes(net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"]))
        self.assertNotIn("b1", p.load.get(("A", "h"), set()))

    def test_one_route_at_depot_is_not_a_transfer_depot(self):
        n = net({"A": ["a1", "h"], "B": ["b1", "b2"]}, depots=["h"])
        self.assertEqual(plan_routes(n).transfer_depots, {})

    def test_route_visiting_depot_twice_gets_rules_on_every_visit(self):
        n = net({"A": ["h", "a1", "h", "a2"], "B": ["h", "b1"]}, depots=["h"])
        rules = generate(plan_routes(n), n)
        self.assertIn(("A", "h", 1), rules)
        self.assertEqual(rules[("A", "h", 1)], rules[("A", "h", 2)])

    def test_two_shared_depots_pick_one(self):
        n = net({"A": ["a1", "h1", "h2"], "B": ["h1", "h2", "b1"]}, depots=["h1", "h2"])
        p = plan_routes(n)
        self.assertEqual(p.store_at[("A", "b1")], "h1")  # lowest port id
        self.assertNotIn("b1", p.store.get(("A", "h2"), set()))

    def test_distance_breaks_ties_before_port_id(self):
        n = net({"A": ["a1", "h1", "h2"], "B": ["h1", "h2", "b1"]}, depots=["h1", "h2"])
        km = {("h1", "b1"): 900.0, ("h2", "b1"): 100.0}
        p = plan_routes(n, distance=lambda a, b: km.get((a, b)))
        self.assertEqual(p.store_at[("A", "b1")], "h2")

    def test_inactive_route_is_ignored(self):
        n = net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"], inactive=["B"])
        p = plan_routes(n)
        self.assertEqual(p.transfer_depots, {})
        self.assertIsNone(p.route_hops("A", "b1"))
        self.assertNotIn("b1", p.destinations)

    def test_disabled_depot(self):
        n = net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"])
        self.assertEqual(plan_routes(n, disabled_depots=["h"]).transfer_depots, {})

    def test_rule_shapes(self):
        n = net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"])
        rules = generate(plan_routes(n), n)
        self.assertEqual([(r.action, r.filter, r.target, r.except_at) for r in rules[("A", None, 0)]],
                         [(LOAD, ROUTE_PORTS, "This Route", ()), (LOAD, PORT, "b1", ("h",))])
        self.assertEqual([(r.action, r.filter, r.target) for r in rules[("A", "h", 1)]],
                         [(STORE, PORT, "b1"), (COLLECT, ROUTE_PORTS, "This Route")])


if __name__ == "__main__":
    unittest.main()
