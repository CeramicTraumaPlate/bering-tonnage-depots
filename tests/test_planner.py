"""Requirements 8-10 and rule limits, simulated without the game."""

import tempfile
import unittest
from pathlib import Path

from atd.model import LOAD, PORT, ROUTE_PORTS, STORE, THIS_ROUTE, Rule
from atd.planner import load_record, make_plan, save_record
from atd.routing import plan_routes

from atd.rules import stop_locations

from helpers import apply, net, random_network


def run(network, record=frozenset(), **kw):
    return make_plan(plan_routes(network), network, set(record), **kw)


def record_of(plan):
    return {(loc[0], loc[1], loc[2], r.key()) for loc, rs in plan.desired.items() for r in rs}


class Planner(unittest.TestCase):
    def setUp(self):
        self.n = net({"A": ["a1", "a2", "h"], "B": ["h", "b1", "b2"]}, depots=["h"])

    def test_second_run_changes_nothing(self):  # in-game test 17
        for n in [self.n] + [random_network(s) for s in range(50)]:
            first = run(n)
            applied = apply(n, first)
            second = run(applied, record_of(first))
            self.assertEqual((second.add, second.remove), ([], []))

    def test_compact_second_run_changes_nothing(self):
        from pathlib import Path
        from atd.export_parser import load_export
        real = load_export(Path(__file__).parent / "fixtures" / "export_v2_day1495.json").network
        for n in [self.n, real] + [random_network(s) for s in range(50)]:
            first = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
            applied = apply(n, first)
            second = make_plan(plan_routes(applied, direct_first=False), applied, record_of(first), compact=True)
            self.assertEqual((second.add, second.remove), ([], []))

    def test_real_play_sequence_through_the_record_file(self):
        """What the player does: export, enter some suggestions, export, enter the rest.

        The record goes through save_record/load_record every time, like the overlay.
        It must end with no changes and no warnings about rules the tool suggested.
        """
        from pathlib import Path
        from atd.export_parser import load_export
        fixtures = Path(__file__).parent / "fixtures"
        nets = [load_export(fixtures / "export_v2_day1495.json").network] + [random_network(s) for s in range(30)]
        for n in nets:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "record.json"
                for step in range(6):
                    if step == 3 and n.depots:  # the network changes mid-game: a depot is sold
                        n.depots = n.depots[1:]
                    plan = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True)
                    save_record(path, plan.record, {})
                    if step == 0:  # enter only half of the first list
                        plan.add = plan.add[::2]
                    n = apply(n, plan)
                final = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True)
                self.assertEqual((final.add, final.remove), ([], []))
                self.assertEqual([w for w in final.warnings if "player rule" in w], [])

    def test_adopt_cleans_up_rules_from_old_suggestions(self):
        from pathlib import Path
        from atd.export_parser import load_export
        n = load_export(Path(__file__).parent / "fixtures" / "export_v2_day1856.json").network
        before = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        self.assertTrue(any("player rule" in w for w in before.warnings))  # the stray rules
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "record.json"
            plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True, adopt=True)
            removed = {(c.loc, c.rule.filter, c.rule.target) for c in plan.remove}
            self.assertIn((("route_4", "port_tokyo", 1), ROUTE_PORTS, "route_1"), removed)  # stray Load | JP S
            save_record(path, plan.record, {})
            n = apply(n, plan)
            final = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True)
            self.assertEqual((final.add, final.remove), ([], []))
            self.assertEqual([w for w in final.warnings if "player rule" in w], [])
            # The player's default rules are untouched.
            self.assertTrue(all(any(r.key()[:4] == (LOAD, "All", ROUTE_PORTS, THIS_ROUTE) for r in s.rules)
                                for s in n.route("route_1").stops))

    def test_cleanup_then_enter_everything_day1804(self):
        from pathlib import Path
        from atd.export_parser import load_export
        n = load_export(Path(__file__).parent / "fixtures" / "export_v2_day1804.json").network
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "record.json"
            for step in range(3):
                plan = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True,
                                 adopt=(step == 0))
                save_record(path, plan.record, {})
                n = apply(n, plan)
            final = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True)
            self.assertEqual((final.add, final.remove), ([], []))
            self.assertEqual([w for w in final.warnings if "player rule" in w], [])
        # JP N's ship filled up with Naha cargo: the ship-less HK-TW-JP route's Collect at Tokyo
        # put Naha in "Onwards Ports | Tokyo". Now cargo for a ship-less route's ports is only
        # loaded where a Store drops it at a depot to wait, so no ship carries it round.
        from atd.rules import existing_rules, verify_compact
        self.assertEqual(verify_compact(plan_routes(n, direct_first=False), n, existing_rules(n)), [])

    def test_new_ship_only_adds_what_is_missing_day2343(self):
        """HK-TW-JP just got a ship. Rules that still work are kept; nothing is reshuffled."""
        from pathlib import Path
        from atd.export_parser import load_export
        n = load_export(Path(__file__).parent / "fixtures" / "export_v2_day2343.json").network
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True, adopt=True)
        removed = {(c.loc[0], c.loc[1], c.rule.filter, c.rule.target) for c in plan.remove}
        self.assertNotIn(("route_1", "port_tokyo", ROUTE_PORTS, "route_3"), removed)   # JP N: Store | JP S
        self.assertNotIn(("route_2", "port_tomakomai", ROUTE_PORTS, "route_1"), removed)  # JP W: Store | JP N
        added = {(c.loc[0], c.loc[1], c.rule.action) for c in plan.add}
        self.assertIn(("route_3", "port_tokyo", "Collect"), added)  # JP S finally collects at Tokyo
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "record.json"
            save_record(path, plan.record, {})
            n = apply(n, plan)
            final = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True)
            self.assertEqual((final.add, final.remove), ([], []))
            self.assertEqual([w for w in final.warnings if "player rule" in w], [])

    def test_new_route_rok_day2709(self):
        """ROK just got a ship: only what's needed, no Collects where nothing is dropped off.

        Rules made before future-proofing (future_proof=False reproduces them)."""
        from pathlib import Path
        from atd.export_parser import load_export
        fx = Path(__file__).parent / "fixtures"
        n = load_export(fx / "export_v2_day2709.json").network
        record = load_record(fx / "record_day2709.json")
        plan = make_plan(plan_routes(n, direct_first=False), n, record, compact=True, future_proof=False)
        self.assertLessEqual(len(plan.add) + len(plan.remove), 12)
        tomakomai_rok = [c for c in plan.add if c.loc[1] == "port_tomakomai" and c.rule.target == "route_7"]
        self.assertEqual(tomakomai_rok, [])
        self.assertEqual(len(plan.optional_remove), 2)  # old Collect | HK-TW-JP at Tomakomai, harmless
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "record.json"
            save_record(path, plan.record, {})
            n = apply(n, plan)
            final = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True,
                              future_proof=False)
            self.assertEqual((final.add, final.remove), ([], []))
            self.assertEqual([w for w in final.warnings if "player rule" in w], [])

    def test_player_rule_survives_three_runs(self):  # in-game test 16
        mine = Rule(LOAD, PORT, "zz_far_away")
        self.n.route("A").stops[0].rules.append(mine)
        n, record = self.n, set()
        for _ in range(3):
            plan = run(n, record)
            self.assertNotIn(mine, [c.rule for c in plan.remove])
            n, record = apply(n, plan), record_of(plan)
        self.assertIn(mine, n.route("A").stops[0].rules)

    def test_turning_off_removes_only_managed_rules(self):  # in-game test 18
        mine = Rule(STORE, PORT, "a1")
        self.n.route("B").stops[0].rules.append(mine)
        first = run(self.n)
        applied = apply(self.n, first)
        off = run(applied, record_of(first), auto_transfer=False)
        self.assertEqual(off.add, [])
        cleaned = apply(applied, off)
        self.assertEqual(cleaned.route("B").stops[0].rules, [mine])
        self.assertEqual(cleaned.route("A").global_rules, [])

    def test_no_duplicate_of_player_rule(self):
        for stop in self.n.route("A").stops:
            stop.rules.append(Rule(LOAD, ROUTE_PORTS, THIS_ROUTE))
        plan = run(self.n)
        added = [(c.loc, c.rule.filter) for c in plan.add]
        self.assertNotIn((("A", None, 0), ROUTE_PORTS), added)
        self.assertEqual(len(plan.covered_by_player), 1)

    def test_conflicting_player_rule_is_kept_and_reported(self):
        self.n.route("A").global_rules.append(Rule(LOAD, PORT, "b1"))  # also loads at h
        plan = run(self.n)
        self.assertTrue(any("player rule loads" in w for w in plan.warnings))
        self.assertEqual(plan.remove, [])

    def test_route_removed_drops_its_rules(self):  # in-game test 19, library side
        three = net({"A": ["a1", "h"], "B": ["h", "b1"], "C": ["h", "c1"]}, depots=["h"])
        first = run(three)
        applied = apply(three, first)
        applied.route("C").active = False
        second = run(applied, record_of(first))
        removed_targets = {c.rule.target for c in second.remove}
        self.assertIn("c1", removed_targets)

    def test_rule_limit(self):
        spokes = {f"S{i:02d}": ["h", f"s{i:02d}a", f"s{i:02d}b"] for i in range(30)}
        n = net({"hubline": ["x", "h"], **spokes}, depots=["h"])
        plan = run(n)
        for loc, rules in plan.desired.items():
            self.assertLessEqual(len(rules), 50)
        self.assertIn("hubline", plan.skipped_for_limit)
        # Every destination it stores is also loaded, and vice versa.
        stores = {r.target for loc, rs in plan.desired.items() if loc[0] == "hubline" and loc[1]
                  for r in rs if r.action == STORE}
        loads = {r.target for r in plan.desired[("hubline", None, 0)] if r.filter == PORT}
        self.assertEqual(stores, loads)

    def test_record_round_trip(self):
        plan = run(self.n)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.json"
            save_record(path, plan.desired, {"save": "t"})
            self.assertEqual(load_record(path), record_of(plan))


class NewGameDetection(unittest.TestCase):
    def test_new_game_or_older_save_is_spotted(self):
        from atd.planner import is_other_game
        self.assertTrue(is_other_game({"game_day": 4639}, {"day": 660}))                     # new game
        self.assertTrue(is_other_game({"game_day": 10, "start_region": "japan_pacific"},
                                      {"day": 20, "start_region": "north_sea"}))             # other start
        self.assertFalse(is_other_game({"game_day": 660, "start_region": "japan_pacific"},
                                       {"day": 700, "start_region": "japan_pacific"}))       # same game, later
        self.assertFalse(is_other_game({}, {"day": 1}))                                      # no record yet


class CargoAlreadyInDepot(unittest.TestCase):
    def test_leftover_depot_cargo_gets_collected(self):
        """Cargo stored under earlier rules: no current rule drops it there, but it still has to move."""
        from atd.model import COLLECT
        from atd.rules import evaluate_stop, existing_rules, collectors_of, merge
        # h-t trunk, t-s-m feeder, h-m line. s goes h->m->s on C, so nothing stores s at h now.
        n = net({"A": ["h", "t"], "B": ["t", "s", "m"], "C": ["h", "m"]}, depots=["h", "t", "m"])
        before = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        n = apply(n, before)
        n.depots[0].stored_by_destination = {"s": 1467}
        plan = make_plan(plan_routes(n, direct_first=False), n, record_of(before), compact=True)
        rules = merge(existing_rules(apply(n, plan)), {})
        takers = [r.id for r in n.routes for loc in stop_locations(r) if loc[1] == "h"
                  and "s" in evaluate_stop(r, loc, rules, n, collectors_of(rules)).sets[COLLECT]]
        self.assertTrue(takers, [c.rule for c in plan.add])
        self.assertTrue(all(c.rule.action == COLLECT for c in plan.add), plan.add)



class PlayerRuleWarnings(unittest.TestCase):
    def test_storing_at_any_depot_a_step_closer_is_not_a_conflict(self):
        """Day-553 game: JP S's template stores Tomakomai cargo at Tokyo; the plan also allows Hakata."""
        n = net({"JPN": ["tokyo", "sendai", "toma"], "JPS": ["tokyo", "kobe", "hakata"],
                 "JPW": ["hakata", "niigata", "toma"]}, depots=["hakata", "toma", "tokyo"])
        n.route("JPS").stops[0].rules = [Rule(STORE, PORT, "toma")]
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        self.assertEqual([w for w in plan.warnings if "player rule" in w], [])
        n.route("JPS").stops[0].rules = [Rule(STORE, PORT, "kobe")]  # its own port: a real conflict
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        self.assertTrue(any("stores kobe" in w for w in plan.warnings), plan.warnings)

    def test_cargo_for_a_shipless_route_is_not_a_conflict(self):
        """Day-617 game: ROK has no ship yet, but Onwards Ports | Hakata already includes its ports."""
        from atd.model import COLLECT, ONWARDS_PORTS
        from atd.nudges import WARN, check_network
        from atd.advisor import DEFAULTS
        n = net({"JPS": ["tokyo", "hakata"], "JPW": ["hakata", "niigata"], "ROK": ["busan", "hakata"]},
                depots=["hakata"], inactive=["ROK"])
        n.route("ROK").stops[1].rules = [Rule(COLLECT, ROUTE_PORTS, THIS_ROUTE)]
        n.route("JPW").global_rules = [Rule(LOAD, ONWARDS_PORTS, "hakata", except_at=("hakata",))]
        n.depots[0].stored_by_destination = {"busan": 98}
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        self.assertEqual([w for w in plan.warnings if "player rule" in w], [])
        out = check_network(n, DEFAULTS)
        self.assertFalse(any("busan" in x.text and x.level == WARN and "depot" in x.text for x in out),
                         [x.text for x in out])
        self.assertTrue(any(x.text == "hakata depot: 98 TEU for busan waiting for ROK to get a ship" for x in out),
                        [x.text for x in out])



class SelfUpdatingRules(unittest.TestCase):
    def test_new_korea_route_day705(self):
        """ROK just got a ship. JP S hands it cargo at Hakata with the hub's self-updating Store,
        not a rule naming ROK, and after entering everything nothing more is asked."""
        from atd.export_parser import load_export
        from atd.model import ONWARDS_PORTS
        fixtures = Path(__file__).parent / "fixtures"
        n = load_export(fixtures / "export_v2_day705.json").network
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.json"
            path.write_text((fixtures / "record_day705.json").read_text(encoding="utf-8"), encoding="utf-8")
            plan = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True,
                             future_proof=False)
            self.assertEqual(len(plan.add), 8, plan.add)
            jps = [c.rule for c in plan.add if c.loc[:2] == ("route_2", "port_hakata") and c.rule.action == STORE]
            self.assertEqual([(r.filter, r.target, r.except_cargo) for r in jps],
                             [(ONWARDS_PORTS, "port_hakata", (ROUTE_PORTS, THIS_ROUTE))])
            save_record(path, plan.desired, {"save": "t"})
            n = apply(n, plan)
            final = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True,
                              future_proof=False)
            self.assertEqual((final.add, final.remove), ([], []))

    def test_new_feeders_after_the_korea_route_stay_correct_day705(self):
        """After entering everything, a new feeder at Tokyo or Hakata costs the existing routes only
        a few changes (rules are kept minimal; self-updating ones only win ties), and stays correct."""
        import copy
        from atd.export_parser import load_export
        from atd.model import COLLECT, ONWARDS_PORTS, Route, Stop
        from atd.rules import existing_rules, verify_compact
        fixtures = Path(__file__).parent / "fixtures"
        n = load_export(fixtures / "export_v2_day705.json").network
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.json"
            path.write_text((fixtures / "record_day705.json").read_text(encoding="utf-8"), encoding="utf-8")
            plan = make_plan(plan_routes(n, direct_first=False), n, load_record(path), compact=True)
            self.assertEqual(plan.remove, [])
            save_record(path, plan.record, {"save": "t"})
            n = apply(n, plan)
            for hub, ports in (("port_tokyo", ["port_naha", "port_kaohsiung"]), ("port_hakata", ["port_shanghai"])):
                stops = [Stop(hub, [Rule(STORE, ONWARDS_PORTS, hub, except_cargo=(ROUTE_PORTS, THIS_ROUTE)),
                                    Rule(COLLECT, ROUTE_PORTS, THIS_ROUTE), Rule(LOAD, ROUTE_PORTS, THIS_ROUTE)])]
                stops += [Stop(p, [Rule(LOAD, ROUTE_PORTS, THIS_ROUTE), Rule(LOAD, ONWARDS_PORTS, hub)]) for p in ports]
                n2 = copy.deepcopy(n)
                n2.routes.append(Route("route_new", stops, True, "NEW"))
                p2 = make_plan(plan_routes(n2, direct_first=False), n2, load_record(path), compact=True)
                others = [c for c in p2.add + p2.remove if c.loc[0] != "route_new"]
                self.assertLessEqual(len(others), 5, (hub, others))
                n3 = apply(n2, p2)
                self.assertEqual(verify_compact(plan_routes(n3, direct_first=False), n3, existing_rules(n3)), [])



class RouteLimit(unittest.TestCase):
    def test_near_the_40_rule_limit_points_at_repeated_stop_loads(self):
        from atd.model import ONWARDS_PORTS
        ports = [f"p{i}" for i in range(16)]
        n = net({"A": ["h"] + ports, "B": ["h", "b1"]}, depots=["h"])
        for stop in n.route("A").stops[1:]:
            stop.rules = [Rule(LOAD, ROUTE_PORTS, THIS_ROUTE), Rule(LOAD, ONWARDS_PORTS, "h")]
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        warn = [w for w in plan.warnings if w.startswith("A has")]
        self.assertEqual(len(warn), 1, plan.warnings)
        self.assertIn("of 40 rules. 32 of them are the same 2 Load rules repeated on each stop: "
                      "as Global Rules they'd take 2, freeing 30", warn[0])



class ShiplessRoute(unittest.TestCase):
    def test_no_rule_swaps_while_a_new_route_waits_for_its_ship_day714(self):
        """Day 714: ROK was set up before its ship. Its Collect at Hakata already puts Korea in
        "Onwards Ports | Hakata", so the other routes' self-updating rules must stay as they are,
        and stay again when the ship arrives."""
        import copy
        from atd.export_parser import load_export
        from atd.rules import existing_rules, verify_compact
        fixtures = Path(__file__).parent / "fixtures"
        n = load_export(fixtures / "export_v2_day705.json").network
        plan = make_plan(plan_routes(n, direct_first=False), n, load_record(fixtures / "record_day705.json"),
                         compact=True)
        n = apply(n, plan)
        record = record_of(plan)
        for active in (False, True):
            n = copy.deepcopy(n)
            n.route("route_4").active = active
            p = make_plan(plan_routes(n, direct_first=False), n, record, compact=True)
            self.assertEqual((p.add, p.remove), ([], []), active)
            self.assertEqual(verify_compact(plan_routes(n, direct_first=False), n, existing_rules(n)), [])



class Release104(unittest.TestCase):
    def test_feeder_with_another_hub_gets_the_meeting_hubs_rules(self):
        """v1.0.4, day 48: JP N was drawn as a Feeder for Tomakomai but meets JP S at Tokyo,
        so it needs exactly the Feeder template's three rules for Tokyo."""
        from atd.export_parser import load_export
        from atd.model import COLLECT, ONWARDS_PORTS
        ex = load_export(Path(__file__).parent / "fixtures" / "export_v2_104_day48.json")
        self.assertTrue(ex.tested)
        n = ex.network
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        got = sorted((c.loc[1] or "", c.rule.action, c.rule.filter, c.rule.target, c.rule.except_at, c.rule.except_cargo)
                     for c in plan.add)
        self.assertEqual(got, sorted([
            ("", LOAD, ONWARDS_PORTS, "port_tokyo", ("port_tokyo",), None),
            ("port_tokyo", STORE, ONWARDS_PORTS, "port_tokyo", (), (ROUTE_PORTS, THIS_ROUTE)),
            ("port_tokyo", COLLECT, ROUTE_PORTS, THIS_ROUTE, (), None)]))
        self.assertTrue(all(c.loc[0] == "route_4" for c in plan.add))


    def test_triangle_of_feeders_needs_only_each_routes_second_hub_day375(self):
        """v1.0.4, day 375: three Feeder-template routes meet in a triangle (Tokyo, Hakata,
        Tomakomai). Each needs the template's three rules for its other hub, and nothing for
        routes that don't exist yet: future-proofing only breaks ties."""
        from atd.export_parser import load_export
        n = load_export(Path(__file__).parent / "fixtures" / "export_v2_104_day375.json").network
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        self.assertEqual(len(plan.add), 9, plan.add)
        self.assertEqual(plan.remove, [])
        for rid in ("route_5", "route_6", "route_7"):
            self.assertEqual(sum(1 for c in plan.add if c.loc[0] == rid), 3, rid)



class RecursiveTemplates(unittest.TestCase):
    def test_closure_follows_collect_rules_through_depots(self):
        from atd.model import COLLECT
        from atd.rules import collectors_of, existing_rules, onwards_closure
        n = net({"A": ["a1", "h"], "B": ["h", "k"], "C": ["k", "c1"], "D": ["x", "d1"]}, depots=["h", "k", "x"])
        for rid, port in (("A", "h"), ("B", "h"), ("B", "k"), ("C", "k")):
            stop = next(s for s in n.route(rid).stops if s.port == port)
            stop.rules = [Rule(COLLECT, ROUTE_PORTS, THIS_ROUTE)]
        col = collectors_of(existing_rules(n))
        self.assertEqual(onwards_closure("h", n, col), {"a1", "k", "c1"})  # D isn't connected

    def test_new_game_with_recursive_feeders_day670(self):
        """Every route made as a recursive Feeder: JP S only needs its Hakata rules and relays,
        not Loads for every far port, and the plan settles."""
        import sys
        from atd.export_parser import load_export
        from atd.advisor import recursive_routes
        sys.path.insert(0, str(Path(__file__).parent))
        n = load_export(Path(__file__).parent / "fixtures" / "export_v2_104_day670_recursive.json").network
        normal = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        rec = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True,
                        recursive_templates=recursive_routes(n, True))
        self.assertEqual((len(normal.add), len(rec.add)), (9, 5))
        self.assertTrue(all(c.loc[0] == "route_1" for c in rec.add))  # all on JP S
        n2 = apply(n, rec)
        again = make_plan(plan_routes(n2, direct_first=False), n2, record_of(rec), compact=True,
                          recursive_templates=recursive_routes(n2, True))
        self.assertEqual((again.add, again.remove), ([], []))

    def test_setting_by_route_name(self):
        from atd.advisor import recursive_routes
        n = net({"ROK": ["a", "h"], "JP S": ["h", "t"]})
        self.assertEqual(recursive_routes(n, ["rok"]), {"ROK"})
        self.assertIs(recursive_routes(n, False), False)


class SuggestedFixes(unittest.TestCase):
    def test_conflict_warning_names_rule_to_remove_day3787(self):
        """CAN-TYO's Store | Onwards Ports | Guangzhou at Tokyo parks Vietnam cargo where the plan
        doesn't: the warning comes with that rule as a suggested removal."""
        from pathlib import Path
        from atd.export_parser import load_export
        from atd.model import ONWARDS_PORTS
        n = load_export(Path(__file__).parent / "fixtures" / "export_v3_11_day3787.json").network
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        self.assertTrue([w for w in plan.warnings if "CAN-TYO @ Tokyo" in w and "stores" in w])
        got = {(c.loc[0], c.loc[1], c.rule.action, c.rule.filter, c.rule.target) for c in plan.suggest_remove}
        self.assertIn(("route_7", "port_tokyo", STORE, ONWARDS_PORTS, "port_guangzhou"), got)
        # Rules the plan relies on are not suggested
        self.assertNotIn(("route_7", "port_tokyo", STORE, ONWARDS_PORTS, "port_hakata"), got)


    def test_template_rules_that_work_are_kept_day322(self):
        """Hub-and-spoke rules from route templates that already work stay put: the plan only
        adds, and does not replace them with its own equivalents."""
        from pathlib import Path
        from atd.export_parser import load_export
        n = load_export(Path(__file__).parent / "fixtures" / "export_v3_11_day322.json").network
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        self.assertEqual((len(plan.add), len(plan.remove), len(plan.optional_remove)), (9, 0, 0))
        self.assertEqual([w for w in plan.warnings if "player rule" in w], [])

    def test_loads_use_excepted_filters_to_save_rules_day2709(self):
        """Eight single-Port Loads that one `Route Ports | <route>` Load with an Except covers
        are written as that one rule (day 2709: 66 rules from a clean slate, now 50)."""
        import copy
        from pathlib import Path
        from atd.export_parser import load_export
        n = copy.deepcopy(load_export(Path(__file__).parent / "fixtures" / "export_v2_day2709.json").network)
        for r in n.routes:
            r.global_rules = [Rule(LOAD, ROUTE_PORTS, THIS_ROUTE)]
            for s in r.stops:
                s.rules = []
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True)
        n = apply(n, plan)
        self.assertLessEqual(sum(len(s.rules) for r in n.routes for s in r.stops)
                             + sum(len(r.global_rules) for r in n.routes), 50)

    def test_recursive_routes_never_get_onwards_collects_day3787(self):
        """Developers' bug: Recursive Onwards Ports plus a Collect on Onwards Ports for another
        depot the route calls at loops forever. The plan never writes one, and flags yours."""
        from pathlib import Path
        from atd.export_parser import load_export
        from atd.model import COLLECT, ONWARDS_PORTS
        n = load_export(Path(__file__).parent / "fixtures" / "export_v3_11_day3787.json").network
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True, recursive_templates=True)
        uses = lambda r: r.filter == ONWARDS_PORTS or (r.except_cargo and r.except_cargo[0] == ONWARDS_PORTS)
        self.assertEqual([c for c in plan.add if c.rule.action == COLLECT and uses(c.rule)], [])
        flagged = {w.split(" @ ")[0] for w in plan.warnings if "loops forever" in w}
        self.assertEqual(flagged, {"JP S", "CAN-HKT", "CAN-TYO"})
        suggested = {c.loc[0] for c in plan.suggest_remove if c.rule.action == COLLECT and c.rule.filter == ONWARDS_PORTS}
        self.assertEqual(suggested, {"route_1", "route_5", "route_7"})
        # Not a recursive route: nothing changes
        plain = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True, recursive_templates=False)
        self.assertEqual([w for w in plain.warnings if "loops forever" in w], [])

    def test_release_111_export_reads_and_plans_day498(self):
        """1.1.1 exports add a per-route "template" field; everything else reads as in 1.1."""
        from pathlib import Path
        from atd.export_parser import load_export
        e = load_export(Path(__file__).parent / "fixtures" / "export_v3_111_day498.json")
        self.assertTrue(e.tested)
        self.assertEqual([x.raw for r in e.network.routes for s in r.stops for x in s.rules if not x.parsed], [])
        plan = make_plan(plan_routes(e.network, direct_first=False), e.network, set(), compact=True)
        self.assertEqual((len(plan.add), len(plan.remove)), (9, 0))

    def test_recursive_template_rules_raise_no_conflict_warning_day498(self):
        """With Recursive Onwards Ports on, the template's Store parks cargo at Tokyo that JP S
        collects and carries on: it arrives, so there is nothing to warn about or to remove."""
        from pathlib import Path
        from atd.export_parser import load_export
        n = load_export(Path(__file__).parent / "fixtures" / "export_v3_111_day498.json").network
        plan = make_plan(plan_routes(n, direct_first=False), n, set(), compact=True, recursive_templates=True)
        self.assertEqual(len(plan.add), 5)
        self.assertEqual(plan.suggest_remove, [])
        self.assertEqual([w for w in plan.warnings if "player rule" in w], [])

    def test_loop_ban_lifts_from_release_111_day3787(self):
        """The developers fixed the recursive Collect loop in 1.1.1: from then on such Collects are
        allowed and your own are not flagged. Older and unknown releases keep the ban."""
        from pathlib import Path
        from atd.export_parser import load_export
        from atd.planner import has_recursive_loop_bug
        self.assertEqual([has_recursive_loop_bug(r) for r in (None, "1.0.4", "1.1", "1.1.1", "1.2")],
                         [True, True, True, False, False])
        n = load_export(Path(__file__).parent / "fixtures" / "export_v3_11_day3787.json").network
        routing = plan_routes(n, direct_first=False)
        old = make_plan(routing, n, set(), compact=True, recursive_templates=True, game_release="1.1")
        new = make_plan(routing, n, set(), compact=True, recursive_templates=True, game_release="1.1.1")
        self.assertTrue([w for w in old.warnings if "loops forever" in w])
        self.assertEqual([w for w in new.warnings if "loops forever" in w], [])


if __name__ == "__main__":
    unittest.main()
