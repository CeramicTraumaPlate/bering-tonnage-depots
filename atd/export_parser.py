"""Read the game's "Export Route Data" JSON into a Network.

The export describes stops and rules as English sentences, for example:
  "1. Hakata [port_hakata], Japan. Mooring $136/day. Load All for Route Ports: This Route."
Only the Load wording has been seen so far. Rules that don't match a known
pattern are kept as unparsed player rules, never dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import ACTIONS, FILTERS, THIS_ROUTE, Depot, Network, Route, Rule, Stop

TESTED = {("1.0.3", 2)}  # (release, export_version) pairs this parser was checked against

INACTIVE_STATUSES = {"unassigned", "unavailable", "paused", "retiring", "laid up"}

_ID = r"\[([a-z0-9_]+)\]"
_STOP = re.compile(r"^\s*(\d+)\.\s+(.+?)\s+" + _ID + r"(?:,\s*([^.]*))?\.\s*(.*)$")
_MOORING = re.compile(r"^Mooring \$[\d,]+/day\.\s*")
_ACTION_START = r"(?=(?:" + "|".join(ACTIONS) + r")\b)"
_RULE = re.compile(
    r"^(?P<action>" + "|".join(ACTIONS) + r")\s+(?P<amount>\S+)\s+for\s+"
    r"(?P<filter>" + "|".join(map(re.escape, FILTERS)) + r"):\s+(?P<rest>.+?)\.?$",
    re.IGNORECASE,
)
_FILTER_TARGET = re.compile(r"^(?P<filter>" + "|".join(map(re.escape, FILTERS)) + r"):\s+(?P<target>.+)$",
                            re.IGNORECASE)
_NAMED = re.compile(r"^(.*?)\s*" + _ID + r"$")


@dataclass
class Export:
    network: Network
    release: str | None
    export_version: int | None
    meta: dict = field(default_factory=dict)
    ship_cargo: dict[str, dict[str, int]] = field(default_factory=dict)  # route -> dest -> teu
    ship_capacity: dict[str, int] = field(default_factory=dict)          # route -> TEU of its ship(s)
    slowest_leg: dict[str, int] = field(default_factory=dict)            # route -> lowest leg speed %
    notes: list[str] = field(default_factory=list)

    @property
    def tested(self) -> bool:
        return (self.release, self.export_version) in TESTED


def load_export(path: Path) -> Export:
    return parse_export(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def parse_export(data: dict) -> Export:
    names: dict[str, str] = {}
    notes: list[str] = []

    routes = []
    slowest_leg: dict[str, int] = {}
    for r in data.get("routes", []):
        name, rid = _split_named(r["name"], names)
        stops = []
        for text in r.get("stops", []):
            port, rules = _parse_stop(text, names)
            stops.append(Stop(port, rules))
        status = (r.get("status") or "").strip()
        active = bool(r.get("ship")) and status.lower() not in INACTIVE_STATUSES
        glob = [_parse_rule(t, names) for t in r.get("global_rules", []) or []]
        routes.append(Route(rid, stops, active, name, glob))
        speeds = [int(x) for leg in r.get("legs", []) or [] for x in re.findall(r"(\d+)% speed", leg)]
        if speeds:
            slowest_leg[rid] = min(speeds)

    depots = []
    for d in data.get("depots", []):
        _, port = _split_named(d["port"], names)
        by_dest = {_split_named(k, names)[1]: v for k, v in (d.get("stored_teu_by_destination") or {}).items()}
        depots.append(Depot(port, True, d.get("capacity_teu"), d.get("stored_teu"), by_dest))
        if d.get("stored_teu") and not by_dest:
            notes.append(f"Depot {port} holds {d['stored_teu']} TEU but the export doesn't say for where")

    ship_cargo: dict[str, dict[str, int]] = {}
    ship_capacity: dict[str, int] = {}
    for s in data.get("ships", []):
        m = re.search(_ID, s.get("route") or "")
        if m:
            ship_cargo[m.group(1)] = {_split_named(k, names)[1]: v
                                      for k, v in (s.get("cargo_teu_by_destination") or {}).items()}
            cap = re.search(r"([\d,]+) TEU", s.get("vessel") or "")
            if cap:
                ship_capacity[m.group(1)] = ship_capacity.get(m.group(1), 0) + int(cap.group(1).replace(",", ""))

    unparsed = sum(1 for r in routes for rule in r.global_rules + [x for s in r.stops for x in s.rules]
                   if not rule.parsed)
    if unparsed:
        notes.append(f"{unparsed} rule(s) used wording the parser doesn't know yet; treated as player rules")

    meta = {k: data.get(k) for k in ("format", "release", "export_version", "save_version", "date", "day",
                                     "start_region")}
    return Export(Network(routes, depots, names), data.get("release"), data.get("export_version"),
                  meta=meta, ship_cargo=ship_cargo, notes=notes, ship_capacity=ship_capacity,
                  slowest_leg=slowest_leg)


def _parse_stop(text: str, names: dict[str, str]) -> tuple[str, list[Rule]]:
    m = _STOP.match(text)
    if not m:
        raise ValueError(f"Unrecognized stop: {text!r}")
    _, port_name, port, _country, rest = m.groups()
    names.setdefault(port, port_name)
    rest = _MOORING.sub("", rest.strip())
    parts = [p.strip() for p in re.split(r"(?<=\.)\s+" + _ACTION_START, rest) if p.strip()]
    return port, [_parse_rule(p, names) for p in parts]


def _parse_rule(text: str, names: dict[str, str]) -> Rule:
    m = _RULE.match(text.strip())
    if not m:
        return Rule(action=text.split(" ", 1)[0], raw=text)
    action = m.group("action").capitalize()
    filt = _canonical_filter(m.group("filter"))
    # "Tokyo [port_tokyo], except at Port: Tokyo [port_tokyo], except Route Ports: This Route"
    target_text, *clauses = re.split(r",\s+except\s+", m.group("rest"))
    target = _target(target_text, names)
    except_at: list[str] = []
    except_cargo = None
    for clause in clauses:
        if clause.lower().startswith("at "):
            ids = re.findall(_ID, clause)
            for name, ident in re.findall(r"([^,:\[]+?)\s*\[([a-z0-9_]+)\]", clause):
                names.setdefault(ident, name.strip())
            if not ids:
                return Rule(action=action, raw=text)  # "except at Region: ..." etc.: not modelled yet
            except_at += ids
        else:
            ft = _FILTER_TARGET.match(clause.strip())
            if not ft:
                return Rule(action=action, raw=text)
            except_cargo = (_canonical_filter(ft.group("filter")), _target(ft.group("target"), names))
    return Rule(action, filt, target, m.group("amount"), tuple(sorted(except_at)), except_cargo, raw=text)


def _canonical_filter(text: str) -> str:
    return next(f for f in FILTERS if f.lower() == text.strip().lower())


def _target(text: str, names: dict[str, str]) -> str:
    text = text.strip()
    if text.lower() == THIS_ROUTE.lower():
        return THIS_ROUTE
    return _split_named(text, names)[1]


def _split_named(text: str, names: dict[str, str]) -> tuple[str, str]:
    """'Tokyo [port_tokyo]' -> ('Tokyo', 'port_tokyo'); remembers port names."""
    m = _NAMED.match(text.strip())
    if not m:
        return text, text
    name, ident = m.group(1).strip(), m.group(2)
    if ident.startswith("port_"):
        names.setdefault(ident, name)
    return name, ident
