"""Per-solution provenance and reachability of a query.

The query is rewritten so that every triple pattern sits in its own `GRAPH ?gN { ... }`, and run
over every publisher's graph. Each derivation then tells which publisher's graph supplied each
pattern. A pattern inside `SERVICE <endpoint> { ... }` is pinned to the graph of that endpoint's
publisher: a SERVICE clause asks one source what it has. Every other pattern matches any graph, so
the answer is the query's answer over all published data.

Reachability. Discovery starts at the publishers whose services the query names (or, for a query
without SERVICE clauses, at the catalog). In each round the values the query binds lead further:
its constants, and the terms of patterns supplied by reached publishers. An IRI leads to the
storage that serves its document (dereference it; the storage root names the service, if any). A
brand product leads, through the rdfs:seeAlso links of reached listings and of the brand itself, to
the other publishers' listings of that product. A solution is reachable if some derivation only
uses reached publishers.
"""
import json
import re
from collections import defaultdict
from pathlib import Path

from rdflib.plugins.sparql.algebra import translateQuery, traverse
from rdflib.plugins.sparql.parser import parseQuery

from .common import sparql_csv

TERM = re.compile(r'<[^>]*>|"(?:[^"\\]|\\.)*"(?:\^\^<[^>]*>|\^\^\S+|@[\w-]+)?|\S+')
SERVICE_OPEN = re.compile(r"^SERVICE\s*<([^>]+)>\s*\{$", re.I)
SKIP = re.compile(r"^\s*(PREFIX|SELECT|WHERE|FILTER|ORDER|LIMIT|OFFSET|#|\}|\{|UNION)", re.I)
MODIFIERS = re.compile(r"^\s*(ORDER\s+BY|LIMIT|OFFSET).*$", re.M | re.I)


def _expand(term, prefixes):
    if term.startswith("<") or term.startswith("?") or term.startswith('"') or term[0].isdigit() or term == "a":
        return term
    if ":" in term:
        pfx, local = term.split(":", 1)
        if pfx in prefixes:
            return f"<{prefixes[pfx]}{local}>"
    return term


def _count_patterns(query):
    count = [0]

    def visit(node):
        if getattr(node, "name", None) == "BGP":
            count[0] += len(node.triples)
    traverse(translateQuery(parseQuery(re.sub(r"SERVICE\s*<", "GRAPH <", query, flags=re.I))).algebra, visitPre=visit)
    return count[0]


def service_endpoints(query):
    """The endpoints the query's SERVICE clauses name, in order."""
    return list(dict.fromkeys(re.findall(r"SERVICE\s*<([^>]+)>", query, re.I)))


def rewrite(query, keep_projection=False, services=None):
    """Wrap each triple pattern of a one-pattern-per-line query in its own GRAPH clause.

    A SERVICE block becomes a plain group whose patterns are pinned to the graph services[endpoint]. Returns
    (query, projection, patterns) where patterns[i] = (s, p, o) for ?g{i+1}. Unless keep_projection,
    the query selects DISTINCT * without solution modifiers (every derivation).
    """
    prefixes = dict(re.findall(r"PREFIX\s+(\w*):\s*<([^>]*)>", query, re.I))
    services = services or {}
    patterns, out = [], []
    groups = []   # per open group: the graph a SERVICE block pins its patterns to, or None

    def wrap(text):
        terms = [_expand(t, prefixes) for t in TERM.findall(text.strip().rstrip(".").strip())]
        if len(terms) != 3:
            raise ValueError(f"Cannot parse triple pattern: {text!r}")
        patterns.append(tuple(terms))
        g = f"?g{len(patterns)}"
        block = f"GRAPH {g} {{ {' '.join(terms)} . }}"
        pinned = next((ns for ns in reversed(groups) if ns), None)
        if pinned:
            return f"{{ {block} FILTER ({g} = <{pinned}>) }}"
        return block

    for line in query.splitlines():
        s = line.strip()
        service = SERVICE_OPEN.match(s)
        m = re.match(r"^OPTIONAL\s*\{\s*(.+?)\s*\.?\s*\}\s*$", s, re.I)
        if service:
            if service.group(1) not in services:
                raise ValueError(f"SERVICE <{service.group(1)}> is not a publisher's endpoint")
            groups.append(services[service.group(1)])
            out.append(line[:len(line) - len(line.lstrip())] + "{")
        elif m:
            out.append(f"OPTIONAL {{ {wrap(m.group(1))} }}")
        elif s.endswith("{"):
            if s.startswith("}"):
                groups.pop()
            groups.append(None)
            out.append(line)
        elif s.startswith("}"):
            groups.pop()
            out.append(line)
        elif s and not SKIP.match(s) and len(TERM.findall(s.rstrip("."))) == 3 and s.endswith("."):
            out.append(wrap(s))
        else:
            out.append(line)

    expected = _count_patterns(query)
    if expected != len(patterns):
        raise ValueError(f"Rewrote {len(patterns)} triple patterns but the query has {expected}")

    q = "\n".join(out)
    projection = re.search(r"SELECT\s+(?:DISTINCT\s+|REDUCED\s+)?(.*?)\s+WHERE", q, re.S | re.I).group(1).split()
    if not keep_projection:
        q = re.sub(r"SELECT\s+(?:DISTINCT\s+|REDUCED\s+)?.*?\s+WHERE", "SELECT DISTINCT * WHERE", q, count=1, flags=re.S | re.I)
        q = MODIFIERS.sub("", q)
    return q, [v.lstrip("?") for v in projection], patterns


def derivations(endpoint, query, max_rows, services=None):
    """Run the provenance query. Returns (projection, patterns, rows) or raises if too large."""
    pquery, projection, patterns = rewrite(query, services=services)
    rows = sparql_csv(endpoint, pquery + f"\nLIMIT {max_rows + 1}")
    if len(rows) > max_rows:
        raise OverflowError(f"more than {max_rows} derivations")
    return projection, patterns, rows


class Analyzer:
    """Reachability and categories over the publishers, given the links between listings."""

    def __init__(self, dconf, owners, links):
        self.base = dconf.base
        self.owners = owners
        self.ns = {n: i["storage"] for n, i in owners.items()}
        self.classical = {i["storage"] for i in owners.values() if i["classical"]}
        self.discovered = {i["storage"] for i in owners.values() if not i["classical"]}
        self.reviewers = {i["storage"] for i in owners.values() if i["kind"] == "reviewer"}
        self.brands = {i["storage"] for i in owners.values() if i["kind"] == "producer"}
        self.lookup_only = {i["storage"] for i in owners.values() if not i["service"]}
        self.adj = defaultdict(lambda: defaultdict(set))   # product -> publisher storage -> peer storages
        self.holders = defaultdict(set)                     # product -> publishers with a listing, and its brand
        for product, owner, _, peer, _ in links:
            self.adj[product][self.ns[owner]].add(self.ns[peer])
            self.holders[product] |= {self.ns[owner], self.ns[peer]}
        # A type leads to the brands with a category page for it (the taxonomy's seeAlso links).
        categories = Path(dconf.path("categories.json"))
        self.categories = ({t: {self.ns[b] for b in brands} for t, brands in json.loads(categories.read_text()).items()}
                           if categories.exists() else {})

    def storage(self, iri):
        if not iri or not iri.startswith(self.base):
            return None
        rest = iri[len(self.base):]
        return f"{self.base}{rest.split('/', 1)[0]}/" if "/" in rest else None

    def follow(self, product, reached):
        """Publishers whose listings of product are reached through seeAlso links from reached ones."""
        adj = self.adj.get(product)
        if not adj:
            return set()
        seen = {o for o in self.holders[product] if o in reached}
        frontier = set(seen)
        while frontier:
            frontier = set().union(*(adj.get(o, set()) for o in frontier)) - seen
            seen |= frontier
        return seen - reached

    def reach(self, patterns, row):
        constants = {t[1:-1] for tp in patterns for t in tp if t.startswith("<")}
        reached = set(self.classical) | {o for o in map(self.storage, constants) if o}
        reached |= set().union(*(self.categories.get(c, set()) for c in constants))
        rounds = 1
        while True:
            values = set(constants)
            for i, tp in enumerate(patterns, start=1):
                if row.get(f"g{i}") in reached:
                    values |= {row.get(t[1:]) if t.startswith("?") else t[1:-1] for t in tp if t[0] in "?<"}
            new = set()
            for value in filter(None, values):
                o = self.storage(value)
                if o and o not in reached:
                    new.add(o)
                if value in self.adj:
                    new |= self.follow(value, reached | new)
            new -= reached
            if not new:
                return reached, rounds
            reached |= new
            rounds += 1

    def analyse(self, projection, patterns, rows, known=None):
        """known: the storages of the publishers the query names in SERVICE clauses. They are where
        discovery starts and everything else counts as discovered. Without it, the catalog is."""
        catalog = {i["storage"] for i in self.owners.values() if i["classical"]}
        self.classical = catalog if known is None else set(known)
        self.discovered = set(self.ns.values()) - self.classical
        solutions = defaultdict(list)
        for row in rows:
            solutions[tuple(row.get(v, "") for v in projection)].append(row)

        stats = defaultdict(int)
        storages, needed, reached_any, max_rounds = set(), set(), set(), 0
        self.reachable = set()
        for key, derivs in solutions.items():
            classical_only = mixed = discoverable = reached_mixed = False
            sol_needed = None
            for row in derivs:
                graphs = {row[f"g{i}"] for i in range(1, len(patterns) + 1) if row.get(f"g{i}")}
                storages |= graphs
                found = graphs & self.discovered
                classical_only |= not found
                mixed |= bool(found) and bool(graphs & self.classical)
                reached, rounds = self.reach(patterns, row)
                if graphs <= reached:
                    discoverable = True
                    reached_mixed |= bool(found) and bool(graphs & self.classical)
                    max_rounds = max(max_rounds, rounds)
                    reached_any |= reached & self.discovered
                if sol_needed is None or len(found) < len(sol_needed):
                    sol_needed = found
            stats["classical_only"] += classical_only
            stats["needs_discovery"] += not classical_only
            stats["mixed"] += mixed
            stats["discoverable"] += discoverable
            if discoverable:
                self.reachable.add(key)
            stats["reachable_needing_discovery"] += discoverable and not classical_only
            stats["reachable_mixed"] += reached_mixed and not classical_only
            if not classical_only:
                needed |= sol_needed
                stats["needs_reviewers"] += bool(sol_needed & self.reviewers)
                stats["needs_lookup_only"] += bool(sol_needed & self.lookup_only)

        n = len(solutions)
        if n == 0:
            category = "empty"
        elif stats["needs_discovery"] == 0:
            category = "known-only"
        elif stats["mixed"] > 0:
            category = "hybrid-join"
        elif stats["classical_only"] > 0:
            category = "hybrid-union"
        else:
            category = "discovery-only"
        short = lambda s: sorted(re.sub(f"^{re.escape(self.base)}|/$", "", o) for o in s)
        record = {
            "category": category,
            "solutions": n,
            "derivations": len(rows),
            "known_only_solutions": stats["classical_only"],
            "solutions_needing_discovery": stats["needs_discovery"],
            "solutions_needing_reviewers": stats["needs_reviewers"],
            "solutions_needing_lookup_only": stats["needs_lookup_only"],
            "mixed_solutions": stats["mixed"],
            "discoverable_solutions": stats["discoverable"],
            "reachable_share": round(stats["discoverable"] / n, 3) if n else 0,
            "reachable_needing_discovery": stats["reachable_needing_discovery"],
            "reachable_mixed": stats["reachable_mixed"],
            "storages_in_answer": short(storages),
            "known": short(self.classical) if known is not None else [],
            "discovered_in_answer": len(storages - self.classical),
            "storages_needed": short(needed),
            "brands_needed": len(needed & self.brands),
            "lookup_only_needed": len(needed & self.lookup_only),
            "storages_reached_best_case": len(reached_any),
            "link_rounds": max_rounds,
        }
        return record
