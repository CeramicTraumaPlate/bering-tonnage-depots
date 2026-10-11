"""The parser and advisor against a real v1.0.3 export."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from atd import advisor
from atd.export_parser import _parse_rule, load_export
from atd.model import COLLECT, LOAD, ONWARDS_PORTS, PORT, ROUTE_PORTS, STORE, THIS_ROUTE
from atd.routing import plan_routes

FIXTURE = Path(__file__).parent / "fixtures" / "export_v2_japan.json"


class Parser(unittest.TestCase):
    def setUp(self):
        self.export = load_export(FIXTURE)
        self.net = self.export.network

    def test_routes_and_stops(self):
        self.assertTrue(self.export.tested)
        self.assertEqual([r.id for r in self.net.routes], ["route_1", "route_2", "route_3", "route_4"])
        self.assertEqual(self.net.route("route_2").ports,
                         ["port_tokyo", "port_sendai", "port_tomakomai", "port_sendai"])
        self.assertEqual([r.active for r in self.net.routes], [True, True, True, False])
        self.assertEqual(self.net.name("port_sendai"), "Sendai-Shiogama [port_sendai]")

    def test_stop_rules(self):
        rule = self.net.route("route_1").stops[0].rules[0]
        self.assertEqual((rule.action, rule.amount, rule.filter, rule.target),
                         (LOAD, "All", ROUTE_PORTS, THIS_ROUTE))
        # Unassigned routes have no mooring sentence.
        self.assertEqual(len(self.net.route("route_4").stops[0].rules), 1)

    def test_depot_and_cargo(self):
        self.assertEqual([(d.port, d.capacity_teu) for d in self.net.depots], [("port_tokyo", 500)])
        self.assertEqual(self.export.ship_cargo["route_3"]["port_niigata"], 122)

    def test_unknown_wording_is_kept(self):
        rule = _parse_rule("Frobnicate the containers.", {})
        self.assertFalse(rule.parsed)
        self.assertEqual(rule.raw, "Frobnicate the containers.")

    def test_guessed_wordings(self):
        names = {}
        r = _parse_rule("Store All for Port: Oakland [port_oakland].", names)
        self.assertEqual((r.action, r.filter, r.target), (STORE, PORT, "port_oakland"))
        r = _parse_rule("Load All for Port: Oakland [port_oakland], except at Tokyo [port_tokyo].", names)
        self.assertEqual(r.except_at, ("port_tokyo",))


class RealRuleWording(unittest.TestCase):
    """Wording seen in a real export after entering Collect, Store and Global Rules (day 1495)."""

    def setUp(self):
        self.net = load_export(Path(__file__).parent / "fixtures" / "export_v2_day1495.json").network

    def test_global_rules(self):
        self.assertEqual(self.net.route("route_1").global_rules[0].key()[:4],
                         (LOAD, "All", ROUTE_PORTS, THIS_ROUTE))
        g = self.net.route("route_4").global_rules[0]
        self.assertEqual((g.action, g.filter, g.target, g.except_at, g.except_cargo),
                         (LOAD, ONWARDS_PORTS, "port_tokyo", ("port_tokyo",), None))

    def test_collect_then_load_on_one_stop(self):
        rules = self.net.route("route_1").stops[4].rules
        self.assertEqual([(r.action, r.filter, r.target) for r in rules],
                         [(COLLECT, ROUTE_PORTS, THIS_ROUTE), (LOAD, ROUTE_PORTS, THIS_ROUTE)])

    def test_except_cargo_parsed(self):
        store = self.net.route("route_4").stops[3].rules[0]
        self.assertEqual((store.action, store.filter, store.target, store.except_cargo),
                         (STORE, ONWARDS_PORTS, "port_tokyo", (ROUTE_PORTS, THIS_ROUTE)))
        self.assertEqual(store.except_at, ())

    def test_every_rule_parsed(self):
        for r in self.net.routes:
            for rule in r.global_rules + [x for s in r.stops for x in s.rules]:
                self.assertTrue(rule.parsed, rule.raw)

    def test_except_cargo_leaves_out_matching_cargo(self):
        # "Store | Onwards Ports | Tokyo | Except: Route Ports | This Route" on HK-TW-JP:
        # JP S and JP E collect at Tokyo, so it stores their ports, minus HK-TW-JP's own.
        from atd.rules import collectors_of, existing_rules, rule_destinations
        hk = self.net.route("route_4")
        dests = rule_destinations(hk.stops[3].rules[0], hk, self.net, collectors_of(existing_rules(self.net)))
        jp_s_e = self.net.route("route_1").port_set | self.net.route("route_2").port_set
        self.assertEqual(dests, set(jp_s_e) - {"port_tokyo"} - hk.port_set)
        self.assertIn("port_kobe", dests)


class AdvisorOnRealExport(unittest.TestCase):
    def setUp(self):
        self.net = load_export(FIXTURE).network
        self.plan = plan_routes(self.net)

    def test_tokyo_is_the_transfer_depot(self):
        self.assertEqual(self.plan.transfer_depots, {"port_tokyo": ["route_1", "route_2"]})

    def test_jp_s_leaves_tomakomai_cargo_to_jp_w_at_hakata(self):
        self.assertEqual(self.plan.route_hops("route_1", "port_tomakomai"), 1)
        self.assertNotIn("port_tomakomai", self.plan.load[("route_1", "port_hakata")])
        self.assertIn("port_tomakomai", self.plan.load[("route_3", "port_hakata")])
        self.assertIn("port_sendai", self.plan.load[("route_1", "port_hakata")])

    def test_jp_w_is_cut_off(self):
        self.assertIsNone(self.plan.route_hops("route_3", "port_tokyo"))
        self.assertIn("port_niigata", self.plan.unreachable)

    def test_cli_first_run_is_dry(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = advisor.main([str(FIXTURE), "--records", d, "--config", str(Path(d) / "none.toml")])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("first run on this save is always a dry run", text)
            self.assertIn("Tokyo [port_tokyo]: JP S, JP E", text)
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
