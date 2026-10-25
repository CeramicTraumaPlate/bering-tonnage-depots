"""Network health nudges."""

import unittest
from pathlib import Path

from atd.advisor import DEFAULTS
from atd.export_parser import load_export
from atd.model import Route, Stop
from atd.nudges import OK, WARN, check_network

from helpers import net

DAY1495 = Path(__file__).parent / "fixtures" / "export_v2_day1495.json"


def texts(nudges, level=None):
    return [n.text for n in nudges if level is None or n.level == level]


class Nudges(unittest.TestCase):
    def test_connected_network(self):
        n = load_export(DAY1495).network
        out = check_network(n, DEFAULTS)
        self.assertEqual(texts(out, OK), ["All 12 ports can reach each other"])
        self.assertEqual(texts(out, WARN), [])

    def test_singapore_route_connects_at_the_limit(self):
        n = load_export(DAY1495).network
        n.routes.append(Route("route_5", [Stop("port_singapore"), Stop("port_hong_kong")], True, "SG-HK"))
        out = check_network(n, DEFAULTS)
        self.assertEqual(texts(out, OK), ["All 13 ports can reach each other"])
        self.assertTrue(any("3 of 3 transfers" in t for t in texts(out)), texts(out))

    def test_suggests_the_depot_that_connects(self):
        n = net({"A": ["a1", "x"], "B": ["x", "b1"], "C": ["c1", "c2"]})
        out = check_network(n, DEFAULTS)
        self.assertTrue(any("Buy a depot at x" in t and "connects 2 of 14 pairs" in t for t in texts(out)), texts(out))
        self.assertIn("C shares no port with any other route", texts(out, WARN))

    def test_suggests_raising_the_limit(self):
        n = net({"A": ["a1", "h1"], "B": ["h1", "h2"], "C": ["h2", "c1"]}, depots=["h1", "h2"])
        out = check_network(n, dict(DEFAULTS, max_transfers=1))
        self.assertTrue(any("Raise max_transfers to 2" in t and "connects everything" in t for t in texts(out)),
                        texts(out))

    def test_real_network_with_shipless_route_and_stuck_cargo(self):
        from atd.export_parser import load_export
        from atd.routing import plan_routes
        export = load_export(Path(__file__).parent / "fixtures" / "export_v2_day1804.json")
        n = export.network
        dests = {d for cargo in export.ship_cargo.values() for d in cargo}
        routing = plan_routes(n, 3, extra_destinations=dests, direct_first=False)
        out = texts(check_network(n, DEFAULTS, routing, export.ship_cargo))
        self.assertIn("All 9 ports can reach each other", out)
        self.assertIn("Hong Kong, Kaohsiung, Naha: only on HK-TW-JP Route, which has no ship, "
                      "so nothing delivers there", out)
        self.assertFalse(any(t.startswith("Fix:") for t in out), out)  # no bogus depot fixes
        self.assertIn("Tokyo depot: 1,832 TEU for Hakata waiting, and no route collects it there yet", out)
        self.assertTrue(any(t.startswith("JP N's ship carries 1,183 TEU no route can deliver (Naha 1,034")
                            for t in out), out)

    def test_route_that_cannot_keep_up(self):
        from atd.export_parser import load_export
        from atd.routing import plan_routes
        export = load_export(Path(__file__).parent / "fixtures" / "export_v2_day4639.json")
        self.assertEqual(export.ship_capacity["route_6"], 3400)
        self.assertEqual(export.slowest_leg["route_6"], 10)
        n = export.network
        out = texts(check_network(n, DEFAULTS, plan_routes(n, 3, direct_first=False), export.ship_cargo,
                                  export.ship_capacity, export.slowest_leg), WARN)
        behind = [t for t in out if "can't keep up" in t]
        self.assertEqual(len(behind), 1, out)
        self.assertTrue(behind[0].startswith("HK-TW-JP Route can't keep up: 31,701 TEU waiting for it"), behind)
        self.assertIn("slowest leg runs at 10% speed", behind[0])

    def test_route_without_ship(self):
        n = net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"], inactive=["B"])
        self.assertIn("B has no ship, so it isn't used", texts(check_network(n, DEFAULTS)))



class RuleOrderAndCollect(unittest.TestCase):
    def test_out_of_order_stop_is_flagged(self):
        from atd.model import COLLECT, LOAD, ROUTE_PORTS, STORE, THIS_ROUTE, Rule
        from atd.nudges import rules_out_of_order
        n = net({"A": ["a1", "h"], "B": ["h", "b1"]}, depots=["h"])
        n.route("A").stops[1].rules = [Rule(STORE, ROUTE_PORTS, "B"), Rule(COLLECT, ROUTE_PORTS, THIS_ROUTE),
                                       Rule(LOAD, ROUTE_PORTS, THIS_ROUTE)]
        self.assertEqual(rules_out_of_order(n), [])
        n.route("A").stops[1].rules.append(Rule(STORE, ROUTE_PORTS, "C"))  # added at the bottom
        out = rules_out_of_order(n)
        self.assertEqual(len(out), 1)
        self.assertIn("A stop 2 h", out[0].text)

    def test_collect_never_dropped_off(self):
        from atd.model import COLLECT, ROUTE_PORTS, STORE, THIS_ROUTE, Rule
        n = net({"A": ["a1", "h"], "B": ["h", "b1"], "C": ["h", "c1"]}, depots=["h"])
        # A stores B's cargo at h; C wrongly collects it there and has nowhere to drop it.
        n.route("A").stops[1].rules = [Rule(STORE, ROUTE_PORTS, "B")]
        n.route("B").stops[0].rules = [Rule(COLLECT, ROUTE_PORTS, THIS_ROUTE)]
        n.route("C").stops[0].rules = [Rule(COLLECT, ROUTE_PORTS, "B")]
        out = texts(check_network(n, DEFAULTS), WARN)
        self.assertTrue(any(t.startswith("C collects cargo for b1 at h but never drops it off") for t in out), out)
        self.assertFalse(any(t.startswith("B collects") for t in out), out)

    def test_placement_hint(self):
        from atd.model import COLLECT, LOAD, ROUTE_PORTS, STORE, THIS_ROUTE, Rule
        from atd.overlay import _placement
        stop = [Rule(COLLECT, ROUTE_PORTS, THIS_ROUTE), Rule(LOAD, ROUTE_PORTS, THIS_ROUTE)]
        self.assertEqual(_placement(Rule(STORE, ROUTE_PORTS, "B"), stop), "↑ move to the top of the stop")
        self.assertEqual(_placement(Rule(COLLECT, ROUTE_PORTS, "B"), stop), "↑ move above the stop's Load rule")
        self.assertEqual(_placement(Rule(COLLECT, ROUTE_PORTS, "B"), stop[:1]), "")


class RecursiveTemplates(unittest.TestCase):
    def test_collect_check_reads_recursive_rules_like_the_planner_laplata_day4085(self):
        """ARG collects Belem cargo at Ushuaia; its template's recursive Store at Buenos Aires drops
        it off there (BR S and BR N take it on). Read as recursive, there is nothing to warn about."""
        from atd.planner import game_rules, load_record
        fixtures = Path(__file__).parent / "fixtures"
        n = load_export(fixtures / "export_v3_111_laplata_day4085.json").network
        record = load_record(fixtures / "record_laplata_day4085.json")
        settings = dict(DEFAULTS, recursive_templates=True, max_transfers=5)
        plain = check_network(n, settings)
        self.assertTrue([t for t in texts(plain, WARN) if "never drops it off" in t])
        read_as_planner = check_network(n, settings, rules=game_rules(n, record, True))
        self.assertEqual([t for t in texts(read_as_planner, WARN) if "never drops it off" in t], [])


if __name__ == "__main__":
    unittest.main()
