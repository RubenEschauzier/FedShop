"""Instantiate the templates, analyse every candidate and write the selected workload.

Templates live in experiments/bsbm/discovery/queries with {{Name}} placeholders. Their values are
chosen per template from the data (an anchor product some shop sells or some site reviews, one of
its types and features, thresholds just below its values, an offer, a review, ...), seeded, so
every candidate has at least one answer. A candidate is kept when:

- its reference answer (claim semantics) is not empty;
- part of it needs a discovered owner and can be reached from the catalog;
- for the templates built around a brand's specification star (q01, q03, q04, q05, q06), sending
  that star to the brand's service saves at least `gain_min`: the star's patterns, evaluated one
  by one at the service, match that many times more triples than the star as a whole.

Every kept instance gets injected.sparql, results.csv (claim semantics), union-results.csv (every
triple admitted, as a classical engine would combine the sources), reachable.csv (the part of the
reference answer reachable from the catalog) and analysis.json.
"""
import csv
import datetime
import io
import json
import random
import re
import shutil
from collections import defaultdict
from pathlib import Path

from .common import BSBM, FS, LOGGER, RDF_TYPE, RDFS_LABEL, load_owners, sparql_csv, sparql_raw_csv
from .ownership import read_links
from .provenance import MODIFIERS, TERM, Analyzer, _expand, claimed_regex, derivations, rewrite

STAR_TEMPLATES = {"q01", "q03", "q04", "q05", "q06"}
FIELDS = ["query", "instance", "selected", "reason", "category", "solutions", "union_solutions",
          "solutions_only_under_union", "derivations_dropped_by_claims", "reachable_share",
          "reachable_needing_discovery", "reachable_mixed", "solutions_needing_reviewers",
          "solutions_needing_lookup_only", "brands_needed", "lookup_only_needed", "storages_needed",
          "storages_reached_best_case", "link_rounds", "max_gain", "gain_at", "reference_rows", "values", "error"]
PREFIXES = """PREFIX bsbm: <http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/vocabulary/>
PREFIX fs: <http://fedshop.example.org/vocab#>
"""


# ----- values -----
class Values:
    """Candidate values for the templates, read from the owners' graphs (cached specs excluded)."""

    def __init__(self, dconf, owners):
        self.dconf = dconf
        self.endpoint = dconf.endpoint
        own = lambda var: f"FILTER(STRSTARTS(STR({var}), STR(?g)))"
        self.sold = self.column(f"SELECT DISTINCT ?x WHERE {{ GRAPH ?g {{ ?l fs:listingOf ?x . ?o bsbm:product ?l . {own('?l')} }} }}")
        self.reviewed = self.column(f"SELECT DISTINCT ?x WHERE {{ GRAPH ?g {{ ?l fs:listingOf ?x . ?r bsbm:reviewFor ?l . {own('?l')} }} }}")
        self.offers = self.column("SELECT DISTINCT ?x WHERE { GRAPH ?g { ?x a bsbm:Offer } }")
        self.reviews = self.column("SELECT DISTINCT ?x WHERE { GRAPH ?g { ?x a bsbm:Review } }")
        self.features = self.column(f"SELECT DISTINCT ?x WHERE {{ GRAPH <{dconf.ns('taxonomy')}> {{ ?x a bsbm:ProductFeature }} }}")
        cached = json.loads(Path(dconf.path("replicas.json")).read_text())
        self.stale = sorted({c["product"] for c in cached if c["stale"]})
        self.cached = sorted({c["product"] for c in cached} - set(self.stale))

    def claim_cases(self, rng, pool):
        """Products with a stale cached spec first, then a fresh one, then the rest: what claims change."""
        pool = set(pool)
        groups = [[p for p in self.stale if p in pool], [p for p in self.cached if p in pool],
                  sorted(pool - set(self.stale) - set(self.cached))]
        out = []
        for group in groups:
            rng.shuffle(group)
            out += group
        return out

    def column(self, query):
        return sorted(r["x"] for r in sparql_csv(self.endpoint, PREFIXES + query))

    def spec(self, product):
        rows = sparql_csv(self.endpoint, f"SELECT ?p ?o WHERE {{ GRAPH ?g {{ <{product}> ?p ?o }} FILTER(STRSTARTS(STR(<{product}>), STR(?g))) }}")
        spec = defaultdict(list)
        for r in rows:
            spec[r["p"]].append(r["o"])
        spec["types"] = sorted(t for t in spec[RDF_TYPE] if t.startswith(self.dconf.ns("taxonomy")))
        spec["features"] = sorted(spec[f"{BSBM}productFeature"])
        return spec

    def offer_facts(self, product):
        return sparql_csv(self.endpoint, PREFIXES + f"""SELECT ?offer ?validTo ?days ?country WHERE {{ GRAPH ?g {{
            ?l fs:listingOf <{product}> . ?offer bsbm:product ?l . ?offer bsbm:validTo ?validTo .
            ?offer bsbm:deliveryDays ?days . ?offer bsbm:vendor ?v . ?v bsbm:country ?country }} }} ORDER BY ?offer""")


def num(spec, i):
    values = spec.get(f"{BSBM}productPropertyNumeric{i}")
    return int(float(values[0])) if values else None


def earlier(date, days):
    d = datetime.date.fromisoformat(date[:10]) - datetime.timedelta(days=days)
    return f"{d.isoformat()}T00:00:00"


def candidates(values, query, rng, limit):
    """Yield dicts of placeholder values for a template, seeded."""
    def shuffled(items):
        items = list(items)
        rng.shuffle(items)
        return items

    if query in ("q01", "q03", "q04", "q05", "q06"):
        products = values.claim_cases(rng, values.sold) if query == "q05" else shuffled(values.sold)
        for product in products[:limit]:
            spec = values.spec(product)
            types, features = spec["types"], spec["features"]
            n1, n2, n3 = num(spec, 1), num(spec, 2), num(spec, 3)
            if query == "q01" and types and len(features) >= 2 and n1:
                f1, f2 = rng.sample(features, 2)
                yield {"ProductType": rng.choice(types), "ProductFeature1": f1, "ProductFeature2": f2,
                       "x": max(0, n1 - rng.randint(1, 300))}
            elif query == "q03" and types and features and n1 and n3:
                others = [f for f in values.features if f not in features]
                yield {"ProductType": rng.choice(types), "ProductFeature1": rng.choice(features),
                       "ProductFeature2": rng.choice(others), "x": max(0, n1 - rng.randint(1, 300)),
                       "y": n3 + rng.randint(1, 300)}
            elif query == "q04" and types and len(features) >= 3 and n1 and n2:
                f1, f2, f3 = rng.sample(features, 3)
                yield {"ProductType": rng.choice(types), "ProductFeature1": f1, "ProductFeature2": f2,
                       "ProductFeature3": f3, "x": max(0, n1 - rng.randint(1, 300)), "y": max(0, n2 - rng.randint(1, 300))}
            elif query == "q05":
                yield {"Product": product}
            elif query == "q06" and spec.get(RDFS_LABEL):
                yield {"word": rng.choice(spec[RDFS_LABEL][0].split())}
    elif query == "q02":
        for product in values.claim_cases(rng, set(values.sold) | set(values.reviewed))[:limit]:
            yield {"Product": product}
    elif query in ("q07", "q10"):
        for product in shuffled(values.sold)[:limit]:
            facts = values.offer_facts(product)
            if facts:
                fact = rng.choice(facts)
                v = {"Product": product, "Country": fact["country"], "date": earlier(fact["validTo"], rng.randint(1, 20))}
                if query == "q10":
                    v["days"] = int(fact["days"])
                yield v
    elif query in ("q08", "q13", "q14", "q15"):
        pool = values.reviewed if query in ("q08", "q14", "q15") else values.sold
        for product in shuffled(pool)[:limit]:
            yield {"Product": product}
    elif query == "q09":
        for review in shuffled(values.reviews)[:limit]:
            yield {"Review": review}
    elif query in ("q11", "q12"):
        for offer in shuffled(values.offers)[:limit]:
            yield {"Offer": offer}


def fill(template, values):
    text = template
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    missing = re.findall(r"\{\{(\w+)\}\}", text)
    if missing:
        raise KeyError(f"no value for {missing}")
    return text


# ----- delegation gain -----
def pattern_layout(query):
    """For every triple pattern, in order: its terms, whether it is optional, and its union branch."""
    prefixes = dict(re.findall(r"PREFIX\s+(\w*):\s*<([^>]*)>", query, re.I))
    layout, optional_depth, depth, branch = [], [], 0, 0
    body = query[re.search(r"SELECT\s.*?\sWHERE", query, re.S | re.I).end():]
    for line in body.splitlines():
        s = line.strip()
        single = re.match(r"^OPTIONAL\s*\{\s*(.+?)\s*\.?\s*\}\s*$", s, re.I)
        if single:
            layout.append((tuple(_expand(t, prefixes) for t in TERM.findall(single.group(1).rstrip(".").strip())), True, branch))
            continue
        if re.match(r"^OPTIONAL\s*\{\s*$", s, re.I):
            depth += 1
            optional_depth.append(depth)
            continue
        if re.match(r"^\}\s*UNION\s*\{$", s, re.I):
            branch += 1
            continue
        if s.startswith("{") and s.endswith("{") or s == "{":
            depth += 1
            continue
        if s.startswith("}"):
            if optional_depth and optional_depth[-1] == depth:
                optional_depth.pop()
            depth -= 1
            continue
        if s and not s.upper().startswith(("FILTER", "SELECT", "PREFIX", "ORDER", "#")) and s.endswith(".") \
                and len(TERM.findall(s.rstrip("."))) == 3:
            layout.append((tuple(_expand(t, prefixes) for t in TERM.findall(s.rstrip(".").strip())), bool(optional_depth), branch))
    return layout


def filters(query):
    return [m.group(0) for m in re.finditer(r"FILTER\s*\(.*\)\s*$|FILTER\s+regex\(.*\)\s*$", query, re.M | re.I)]


def variables(text):
    return set(re.findall(r"\?\w+", text))


def gains(dconf, owners, query, patterns, rows, per_service=3):
    """What sending a sub-query to a service saves, for the sub-queries services supply in the answer.

    For every derivation and service, B' is the set of required patterns that service supplied.
    Evaluated pattern by pattern at the service, B' matches sum(singles) times; sent as a whole
    (with the filters over B's variables) it returns `joined` solutions. gain = sum(singles) / joined.
    """
    layout = pattern_layout(query)
    if len(layout) != len(patterns):
        return []
    services = {i["namespace"]: n for n, i in owners.items() if i["service"]}
    required = [i for i, (_, optional, _) in enumerate(layout, start=1) if not optional]
    fl = filters(query)
    subqueries = defaultdict(set)
    for row in rows:
        by_graph = defaultdict(list)
        for i in required:
            if row.get(f"g{i}") in services:
                by_graph[row[f"g{i}"]].append(i)
        for graph, idxs in by_graph.items():
            if len(idxs) >= 2:
                subqueries[graph].add(tuple(idxs))

    def count(graph, selected):
        vs = set().union(*(variables(" ".join(patterns[i - 1])) for i in selected))
        body = " ".join(f"{' '.join(patterns[i - 1])} ." for i in selected)
        fs = " ".join(f for f in fl if variables(f) and variables(f) <= vs)
        return int(sparql_csv(dconf.endpoint, f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{ {body} {fs} }} }}")[0]["n"])

    out = []
    for graph in sorted(subqueries):
        for idxs in sorted(subqueries[graph], key=len, reverse=True)[:per_service]:
            singles = [count(graph, [i]) for i in idxs]
            joined = count(graph, idxs)
            out.append({"service": services[graph], "patterns": list(idxs), "singles": singles, "joined": joined,
                        "gain": round(sum(singles) / max(joined, 1), 1),
                        "classical": owners[services[graph]]["classical"]})
    return out


def reference_query(text, regex):
    """The query under claim semantics, with its own projection and modifiers.

    Each pattern only matches admitted triples. Graph variables are projected away inside a
    DISTINCT subquery over the query's own variables, so a triple admitted from two graphs (a
    fresh cached copy of a lookup-only brand's spec) counts once, as it does in the union of graphs.
    """
    inner, _, patterns = rewrite(text, regex)
    prefix_lines = [l for l in text.splitlines() if l.strip().upper().startswith("PREFIX")]
    select = re.search(r"SELECT\s+.*?(?=\s+WHERE)", text, re.S | re.I).group(0)
    modifiers = "\n".join(m.group(0) for m in MODIFIERS.finditer(text))
    own_vars = sorted(set().union(*(variables(" ".join(p)) for p in patterns)))
    inner = "\n".join(l for l in inner.splitlines() if not l.strip().upper().startswith("PREFIX"))
    inner = inner.replace("SELECT DISTINCT * WHERE", f"SELECT DISTINCT {' '.join(own_vars)} WHERE", 1)
    return "\n".join(prefix_lines) + f"\n{select} WHERE {{\n{{ {inner} }}\n}}\n{modifiers}\n"


# ----- writing -----
def write_instance(outdir, text, record, reference, union_reference, projection, reachable):
    Path(outdir).mkdir(parents=True, exist_ok=True)
    Path(outdir, "injected.sparql").write_text(text)
    Path(outdir, "results.csv").write_text(reference)
    Path(outdir, "union-results.csv").write_text(union_reference)
    rows = list(csv.reader(io.StringIO(reference)))
    header = rows[0] if rows else []
    with open(Path(outdir, "reachable.csv"), "w", newline="") as fs:
        writer = csv.writer(fs)
        writer.writerow(header)
        writer.writerows(r for r in rows[1:] if tuple(dict(zip(header, r)).get(v, "") for v in projection) in reachable)
    Path(outdir, "analysis.json").write_text(json.dumps(record, indent=2))


def write_sources(dconf, owners):
    outdir = Path(dconf.path("sources", "x")).parent
    catalog = {n: i for n, i in owners.items() if i["classical"]}
    with open(outdir / "catalog-endpoints.tsv", "w") as fs:
        for name, info in catalog.items():
            fs.write(f"{name}\t{info['namespace']}\t{info['endpoint']}\n")
    with open(outdir / "catalog-fedx.ttl", "w") as fs:
        fs.write("@prefix sd: <http://www.w3.org/ns/sparql-service-description#> .\n"
                 "@prefix fedx: <http://rdf4j.org/config/federation#> .\n\n")
        for info in catalog.values():
            fs.write(f'<{info["namespace"]}> a sd:Service ;\n    fedx:store "SPARQLEndpoint";\n'
                     f'    sd:endpoint "{info["endpoint"]}";\n    fedx:supportsASKQueries true .\n\n')
    with open(outdir / "discovered-storages.tsv", "w") as fs:   # for analysis only: an engine has to find these
        for name, info in owners.items():
            if not info["classical"]:
                fs.write(f"{name}\t{info['namespace']}\t{info.get('endpoint') or ''}\n")


def analyse(dconf, owners, analyzer, regex, text):
    max_rows = dconf.workload_conf["max_rows"]
    projection, patterns, rows = derivations(dconf.endpoint, text, max_rows, regex)
    _, _, union_rows = derivations(dconf.endpoint, text, max_rows)
    record = analyzer.analyse(projection, patterns, rows, union_rows)
    record["gains"] = gains(dconf, owners, text, patterns, rows)
    discovered = [g for g in record["gains"] if not g["classical"]]
    best = max(discovered, key=lambda g: g["gain"], default=None)
    record["max_gain"] = best["gain"] if best else 0
    record["gain_at"] = best["service"] if best else ""
    return record, projection, set(analyzer.reachable)


def build(dconf):
    owners = load_owners(dconf)
    analyzer = Analyzer(dconf, owners, read_links(dconf))
    regex = claimed_regex(dconf, owners)
    values = Values(dconf, owners)
    conf = dconf.workload_conf
    outdir = Path(dconf.outdir, "workload")
    shutil.rmtree(outdir, ignore_errors=True)
    records = []
    for tpl in sorted(Path(conf["templates_dir"]).glob("q*.sparql")):
        query, template = tpl.stem, tpl.read_text()
        rng = random.Random(f"{conf['seed']}-{query}")
        kept = tried = 0
        for vals in candidates(values, query, rng, conf["max_candidates"]):
            if kept == conf["per_template"]:
                break
            tried += 1
            text = fill(template, vals)
            record = {"query": query, "instance": kept, "values": vals}
            try:
                result, projection, reachable = analyse(dconf, owners, analyzer, regex, text)
                record.update(result)
            except (OverflowError, RuntimeError, ValueError) as e:
                record.update({"selected": False, "reason": "error", "error": str(e)[:300]})
                records.append(record)
                continue
            if record["solutions"] == 0:
                reason = "empty"
            elif record["reachable_needing_discovery"] == 0:
                reason = "no reachable answer needs discovery"
            elif query in STAR_TEMPLATES and record["max_gain"] < conf["gain_min"]:
                reason = f"gain {record['max_gain']} < {conf['gain_min']}"
            else:
                reason = ""
            record["selected"] = not reason
            record["reason"] = reason or "kept"
            if record["selected"]:
                reference = sparql_raw_csv(dconf.endpoint, reference_query(text, regex))
                union_reference = sparql_raw_csv(dconf.endpoint, text)
                record["reference_rows"] = max(len(reference.strip().splitlines()) - 1, 0)
                write_instance(outdir / query / f"instance_{kept}", text, record, reference, union_reference,
                               projection, reachable)
                kept += 1
            records.append(record)
        LOGGER.info(f"{query}: kept {kept}/{conf['per_template']} after {tried} candidates")
    write_sources(dconf, owners)
    with open(Path(dconf.outdir, "workload-summary.csv"), "w", newline="") as fs:
        writer = csv.DictWriter(fs, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = dict(r)
            row["storages_needed"] = " ".join(row.get("storages_needed") or [])
            row["values"] = json.dumps(row.get("values"))
            if not row.get("selected"):
                row["instance"] = ""
            writer.writerow(row)
    return records


def check(dconf, text):
    owners = load_owners(dconf)
    analyzer = Analyzer(dconf, owners, read_links(dconf))
    record, _, _ = analyse(dconf, owners, analyzer, claimed_regex(dconf, owners), text)
    return record
