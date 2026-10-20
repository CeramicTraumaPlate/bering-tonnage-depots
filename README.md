# Auto-Transfer Depots

A fan-made helper for **[Bering Tonnage](https://store.steampowered.com/search/?term=Bering%20Tonnage)**. Connect your routes at depots, and it works out the Load, Store and Collect rules that move cargo through those depots to wherever it's going, across your whole network.

It's an overlay that sits next to the game and tells you which rules to add or remove. It only reads the game's own route export: it never touches the game, its files or your saves. Not affiliated with the game's developer.

## Quick start

The overlay is all you need. There's nothing to install into the game.

1. **Install Python** 3.11 or newer from [python.org](https://www.python.org/downloads/). In the installer, tick **"Add python.exe to PATH"**.
2. **Download this project**: the green **Code** button → **Download ZIP**, then extract it anywhere.
3. **Start the overlay**: double-click `Depots Overlay.bat`.
4. **Switch to Bering Tonnage.** The overlay only shows while the game is the active window, and hides when you switch to anything else, so it's not in your way outside the game. If you'd rather it always showed, set `overlay_only_with_game = false` in `atd.toml`.
5. **In game: Esc → Export Route Data to Clipboard.** The overlay reads it and lists the rules to add or remove, grouped by route and stop, e.g. `+ Store | All | Onwards Ports | Hakata | Except: Route Ports | This Route`, with where on the stop each one goes.
6. **Enter them** in the game's route editor. Click a line in the overlay to tick it off while you work.
7. **Export again.** The overlay checks what you entered, and says "Depots up to date" when everything's in. Do this whenever you change your network.

The overlay can be dragged by its title bar, collapsed with **−** and closed with **×**.

## More about the overlay

The overlay also checks the network as a whole: ports that can't reach each other and which depot would fix that, cargo stuck in depots or on ships, routes that can't keep up, depots filling up, and rules in the wrong order on a stop.

Your own rules are never touched. The overlay remembers which rules it suggested (in `records/`), so later it can suggest removing them when your network changes. "Treat all current rules as suggested" is a one-time cleanup for rules left from earlier suggestions.

### Command line

The same planner as a report, for an export saved to a file (Esc → **Export Route Data to File**):

```
python -m atd.advisor "path\to\export.json" --save MyCompany
```

| Option | Effect |
| --- | --- |
| `--save NAME` | Keys the managed-rule record (`records/NAME.json`). Use one name per save |
| `--commit` | Record the plan on the first run (the first run on a save is otherwise a dry run) |
| `--dry-run` | Show the plan, write nothing |
| `--off` | Plan removal of every managed rule (clean uninstall) |
| `--max-transfers N` | Longest depot chain (default 3) |
| `--config FILE` | Settings file, default `atd.toml` |

## How it decides

For every route and destination it works out how many transfers away the route is. Cargo goes off at any depot one step closer, and only a route exactly one step on collects it, so cargo always gets closer and can't loop. Those decisions are then written as few, readable rules, preferring the game's self-updating filters (`Onwards Ports | <hub>`) so that routes you add later are picked up without editing the others. Before anything is shown, the planner checks its own rules against a model of the game's filters.

## Layout

| Path | What |
| --- | --- |
| `atd/overlay.py` | The overlay window |
| `atd/routing.py` | Transfers per route and destination, and where cargo may go off. No game dependency |
| `atd/rules.py` | Routing decisions → rules, and a model of the game's filters used to self-check |
| `atd/planner.py` | Diff against the rules in the game: your rules win, rule limit, managed-rule record |
| `atd/nudges.py` | Network checks (reachability, stuck cargo, capacity, rule order) |
| `atd/export_parser.py` | Reads the v1.0.3 route export (export_version 2) |
| `atd/advisor.py` | Command line and settings |
| `atd.toml` | Settings |

## Tests

```
python -m unittest discover -s tests
```

Runs the planner on real exports from several saves and on 200 random networks, simulates entering the rules and changing the network, and checks the self-check catches broken rule sets.

## What the game does (confirmed in play)

- Export wording: rules read `Store All for Onwards Ports: Tokyo [port_tokyo].`, Global Rules have their own `global_rules` list, and exceptions read `except at Port: Tokyo [port_tokyo]` (rule doesn't run at that port) or `except Route Ports: This Route`.
- **`Except <filter>` leaves that cargo out of the rule** (confirmed by the developer): `Store | Onwards Ports | Tokyo | Except: Route Ports | This Route` stores everything onwards from Tokyo except cargo for this route's own ports. A rule has one Except: either `At <port>` or a filter.
- **Onwards Ports doesn't chain** (confirmed by the developer): it covers routes collecting at that hub only. To reach further hubs, a route needs its own Load and Store rules naming each one. The overlay works these out.
- **Onwards Ports | H** = ports of every route with a Collect rule at H's depot (H itself excluded). It follows Collect rules as you add them. A Load rule with it picked up exactly that cargo.
- Filters: Any Destination, Route Ports, Onwards Ports, Owned Ports (every port your licenses cover), Port, Country, Region, Continent.
- **Route Ports can target another route** (`Route Ports | JP S`). Compact mode (the default) is built on this: per neighbouring route, one Load ("everything for JP S, except at the hub where it goes off"), one Store at that hub, and a Collect for the route's own ports. Cargo may go off at any depot one step closer to its destination; whichever the ship reaches first takes it, and only a route exactly one step on collects it, so cargo can't loop. Onwards Ports and `Except: Route Ports | This Route` are used where exact; Port rules fill gaps.
- **Onwards Ports counts routes without a ship.** A ship-less route that still has a Collect rule at a hub makes `Onwards Ports | <hub>` include its ports, so other ships load cargo nobody can deliver (seen: JP N's ship full of Naha cargo).
- `Store | All | Onwards Ports | Tokyo` with no exception stores cargo in the depot.
- **Cargo bound for the hub itself is delivered before Store runs.** Evidence (export day 3680): seven routes have Store rules that also match their hub (e.g. JP N `Store | Route Ports | JP S` at Tokyo) and their ships carry hub-bound cargo, yet the Tokyo and Tomakomai depots hold 0 TEU bound for Tokyo or Tomakomai.

## Community guidance used

From "Bering Tonnage Design Patterns" by Susul (CC BY-SA 3.0); one player's experience, not tested mechanics:
- **Rule order on a stop:** Stores (and Unloads) first, then Collects, then Loads. The overlay says where each new rule goes and flags stops that are out of order.
- **Unload means "sell on the market"** (for partial deliveries). Delivery to a cargo's destination happens on its own, so the tool never adds Unload rules.
- **A ship that collects cargo it never stores or delivers slowly fills up** (S5). The overlay checks every route for this, including rules you wrote yourself.

## Adding routes later: what it costs

Rules are kept to the minimum your network needs today. Where a self-updating rule (`Onwards Ports | <hub>`) does the same job as one naming a route, the self-updating one is used, so routes you add later are picked up where that's free.

- **A new route at an existing hub** needs its own rules (about 2-4 beyond the Feeder template) and usually 2-5 on routes that are more than one hub away from it, because the game's Onwards Ports only reaches one hub.
- **A new hub** (a depot that becomes a meeting point) needs a few rules on the routes that can reach it, once.
- **Rule limit:** 40 rules per route, stops and Global Rules together. The overlay warns from 32, and points out Load rules that a route template repeated on every stop: as Global Rules they take one each.

## Recursive Onwards Ports (v1.0.4)

Route templates (Feeder, Star) can be made with **Recursive Onwards Ports**: their Onwards Ports rules then reach every port connected through hubs where routes collect, not just one hub. The option only exists when you first make a route, and the route export doesn't show it, so tell the overlay in `atd.toml`:

```toml
recursive_templates = ["ROK", "HK-VN"]   # or true for every route, false for none
```

The overlay then counts on those routes' template rules to load and store cargo for far-away ports, and only suggests what's missing, mostly Stores and Collects where routes meet a second hub. On one save it needed 5 changes instead of 9; rebuilt as templates, a large network needed 25 instead of 40.

On game releases before 1.1.1 a route marked recursive never gets a Collect on Onwards Ports (the game looped forever when a recursive route collected Onwards Ports for another depot it calls at; fixed in 1.1.1), and the overlay flags any of your own rules that do.

Best for **feeders** (routes with one hub). A recursive template drops everything at its one hub, so on a route between two hubs, cargo for the other hub goes off at the template's hub first and is carried back: it arrives, but takes the long way.

## License

MIT, see [LICENSE](LICENSE). Bering Tonnage is the property of its developer; this project only reads the route export the game provides.
