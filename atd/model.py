"""Plain data types for a shipping network. No game dependency."""

from __future__ import annotations

from dataclasses import dataclass, field

# Rule vocabulary, as seen in the game UI.
LOAD, UNLOAD, STORE, COLLECT, WAIT, SKIP = "Load", "Unload", "Store", "Collect", "Wait", "Skip"
ACTIONS = (LOAD, UNLOAD, STORE, COLLECT, WAIT, SKIP)

PORT, ROUTE_PORTS, ONWARDS_PORTS, REGION = "Port", "Route Ports", "Onwards Ports", "Region"
OWNED_PORTS, ANY_DESTINATION = "Owned Ports", "Any Destination"
COUNTRY, CONTINENT = "Country", "Continent"
# As listed in the rule editor's filter menu (v1.0.3).
FILTERS = (ANY_DESTINATION, ROUTE_PORTS, ONWARDS_PORTS, OWNED_PORTS, PORT, COUNTRY, REGION, CONTINENT)

THIS_ROUTE = "This Route"


@dataclass(frozen=True)
class Rule:
    """One game rule: action, amount, filter, target, plus optional exceptions.

    except_at:    ports where the rule does not run ("except at Port: X").
    except_cargo: (filter, target) - cargo matching it is left out of the rule
                  ("except Route Ports: This Route"). Confirmed by the developer.
    A rule has one Except: either except_at or except_cargo.
    `raw` keeps the original text for rules read from an export, so unparsed
    rules can still be shown and compared.
    """

    action: str
    filter: str | None = None
    target: str | None = None  # port id, THIS_ROUTE, or region name
    amount: str = "All"
    except_at: tuple[str, ...] = ()
    except_cargo: tuple[str, str] | None = None
    raw: str | None = field(default=None, compare=False)

    @property
    def parsed(self) -> bool:
        return self.filter is not None

    def key(self) -> tuple:
        """Content identity used to match rules across runs."""
        return (self.action, self.amount, self.filter, self.target, tuple(sorted(self.except_at)),
                self.except_cargo)


@dataclass
class Stop:
    port: str
    rules: list[Rule] = field(default_factory=list)


@dataclass
class Route:
    id: str
    stops: list[Stop]
    active: bool = True
    name: str = ""
    global_rules: list[Rule] = field(default_factory=list)

    @property
    def ports(self) -> list[str]:
        return [s.port for s in self.stops]

    @property
    def port_set(self) -> frozenset[str]:
        return frozenset(self.ports)


@dataclass
class Depot:
    port: str
    enabled: bool = True
    capacity_teu: int | None = None
    stored_teu: int | None = None
    stored_by_destination: dict[str, int] = field(default_factory=dict)


@dataclass
class Network:
    routes: list[Route]
    depots: list[Depot]
    port_names: dict[str, str] = field(default_factory=dict)

    def route(self, route_id: str) -> Route:
        for r in self.routes:
            if r.id == route_id:
                return r
        raise KeyError(route_id)

    def name(self, port: str) -> str:
        n = self.port_names.get(port)
        return f"{n} [{port}]" if n else port
