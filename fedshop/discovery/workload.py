"""Instantiate the templates, analyse every candidate and write the selected workload.

Templates live in experiments/bsbm/discovery/queries with {{Name}} placeholders. The part of a
query whose sources the user knows sits in SERVICE clauses on catalog members' endpoints ({{Shop}},
{{Site}}, {{Taxonomy}}), as in FedShop's reference source assignment; the rest names no source and
has to be discovered. Each template states in its header:

- `kind`: lookup, product (one product across members), analytical, or control (answered by its
  SERVICE clauses alone);
- `rows`: the range of answer sizes its instances must have;
- `discovered`: how many publishers outside its SERVICE clauses an answer must involve, at least.

Values are chosen per template from the data, seeded, aiming at the size range: thresholds are set
from the values of the products they filter, products are drawn from those with enough reviews or
offers. A candidate is kept when its reference answer has a size within range, and, unless it is a
control, part of it needs a discovered publisher, can be reached, and involves at least
`discovered` publishers beyond the named ones. Delegation gains are reported, not selected on.

Every kept instance gets injected.sparql, results.csv (its answer over all published data, with
SERVICE patterns answered by their service), reachable.csv and analysis.json. Its plain form,
plain.sparql, is the same query without SERVICE clauses, for engines that select sources
themselves (given the catalog's endpoints); it has its own answer, plain-results.csv, since without
SERVICE a pattern matches any publisher.
"""
import csv
import io
import json
import random
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from .common import BSBM, FS, LOGGER, RDF_TYPE, RDFS_LABEL, load_owners, owner_of, sparql_csv, sparql_raw_csv
from .ownership import read_links
from .provenance import MODIFIERS, TERM, Analyzer, _expand, derivations, rewrite, service_endpoints

FIELDS = ["query", "instance", "kind", "selected", "reason", "category", "solutions", "reachable_share",
          "reachable_needing_discovery", "reachable_mixed", "discovered_in_answer", "solutions_needing_reviewers",
          "solutions_needing_lookup_only", "brands_needed", "lookup_only_needed", "storages_needed",
          "storages_reached_best_case", "link_rounds", "max_gain", "gain_at", "reference_rows", "values", "error"]
PREFIXES = """PREFIX bsbm: <http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/vocabulary/>
PREFIX fs: <http://fedshop.example.org/vocab#>
PREFIX rev: <http://purl.org/stuff/rev#>
"""

def template_meta(text):
    """The header of a template: kind, rows (lo, hi) and discovered."""
    meta = dict(re.findall(r"^#\s*(kind|rows|discovered):\s*(\S+)", text, re.M))
    lo, hi = (int(x) for x in meta.get("rows", "1-100000").split("-"))
    return {"kind": meta.get("kind", "product"), "rows": (lo, hi), "discovered": int(meta.get("discovered", 1))}


# ----- values -----
class Values:
    """What the templates' values are drawn from, read from the publishers' graphs."""

    def __init__(self, dconf, owners):
        self.dconf = dconf
        self.endpoint = dconf.endpoint
        self.owners = owners
        known = {n: i for n, i in owners.items() if i["classical"]}
        self.shops = sorted((n for n, i in known.items() if i["kind"] == "vendor"), key=lambda n: int(n[6:]))
        self.sites = sorted((n for n, i in known.items() if i["kind"] == "ratingsite"), key=lambda n: int(n[10:]))
        self.taxonomy = owners["taxonomy"]["endpoint"]

        self.offers_at = defaultdict(lambda: defaultdict(list))   # shop -> product -> offers
        self.offer_count = Counter()
        for r in self.rows("SELECT ?g ?x ?o WHERE { GRAPH ?g { ?l fs:listingOf ?x . ?o bsbm:product ?l } }"):
            self.offers_at[owner_of(dconf, r["g"])][r["x"]].append(r["o"])
            self.offer_count[r["x"]] += 1
        self.sold = sorted(self.offer_count)
        self.reviews_at = defaultdict(list)                         # site -> reviews
        self.review_count, self.english_count = Counter(), Counter()
        self.review_sites = defaultdict(set)
        for r in self.rows("SELECT ?g ?x ?r ?lang WHERE { GRAPH ?g { ?l fs:listingOf ?x . ?r bsbm:reviewFor ?l . ?r rev:text ?t } "
                           "BIND(lang(?t) AS ?lang) }"):
            site = owner_of(dconf, r["g"])
            self.reviews_at[site].append(r["r"])
            self.review_count[r["x"]] += 1
            self.english_count[r["x"]] += r["lang"] == "en"
            self.review_sites[r["x"]].add(site)
        self.reviewed = sorted(self.review_count)
        listed = "GRAPH ?h { ?l fs:listingOf ?x } GRAPH ?g { ?x ?p ?v }"
        self.types, self.features, self.label, self.num = defaultdict(list), defaultdict(list), {}, defaultdict(dict)
        taxonomy = dconf.ns("taxonomy")
        for r in self.rows(f"SELECT DISTINCT ?x ?p ?v WHERE {{ {listed} FILTER(?p IN (rdf:type, bsbm:productFeature, rdfs:label, "
                           f"bsbm:productPropertyNumeric1, bsbm:productPropertyNumeric2, bsbm:productPropertyNumeric3)) }}"):
            x, p, v = r["x"], r["p"], r["v"]
            if p == RDF_TYPE and v.startswith(f"{taxonomy}ProductType"):
                self.types[x].append(v)
            elif p == f"{BSBM}productFeature":
                self.features[x].append(v)
            elif p == RDFS_LABEL:
                self.label[x] = v
            elif p.startswith(f"{BSBM}productPropertyNumeric"):
                self.num[x][int(p[-1])] = int(float(v))
        self.of_type = defaultdict(set)                              # type -> listed products
        for x, ts in self.types.items():
            for t in ts:
                self.of_type[t].add(x)
        self.brand_sold = Counter(owner_of(dconf, x) for x in self.sold)
        ratings = {int(r["r"]): int(r["n"]) for r in self.rows(
            "SELECT ?r (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?x bsbm:rating1 ?r } } GROUP BY ?r")}
        reviews = sum(self.review_count.values())
        self.rating_share = {r: sum(n for k, n in ratings.items() if k >= r) / reviews for r in range(1, 11)}

    def rows(self, query):
        return sparql_csv(self.endpoint, PREFIXES + "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
                          "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n" + query)

    def endpoint_of(self, name):
        return self.owners[name]["endpoint"]


def rating_threshold(values, reviews, target):
    """The rating r for which about `target` of `reviews` reviews are rated r or more."""
    return min(range(1, 11), key=lambda r: abs(reviews * values.rating_share[r] - target))


def threshold_above(values, target):
    """x such that about `target` of the values are above x."""
    values = sorted(values, reverse=True)
    return values[min(target, len(values)) - 1] - 1 if values else 0


def candidates(values, query, rng, limit, meta):
    """Yield dicts of placeholder values for a template, seeded, aiming at its rows range."""
    lo, hi = meta["rows"]
    target = lambda: rng.randint(lo, max(lo, min(hi, 3 * lo + 20)))

    def shuffled(items):
        items = sorted(items)
        rng.shuffle(items)
        return items

    sold = set(values.sold)
    if query in ("q01", "q03", "q04"):
        types = [t for t in shuffled(values.of_type) if len(values.of_type[t] & sold) >= lo]
        for t in types[:limit]:
            pool = sorted(values.of_type[t] & sold)
            if query == "q01":
                yield {"ProductType": t, "x": threshold_above([values.num[x].get(1, 0) for x in pool], target())}
            elif query == "q03":
                share = min(1.0, target() / len(pool)) ** 0.5            # split the selectivity over two bounds
                x = threshold_above([values.num[p].get(1, 0) for p in pool], max(1, round(share * len(pool))))
                y = sorted(values.num[p].get(3, 0) for p in pool)[max(0, round(share * len(pool)) - 1)] + 1
                feature = rng.choice(sorted(values.features[rng.choice(pool)]) or ["none"])
                yield {"ProductType": t, "x": x, "y": y, "ProductFeature": feature}
            else:
                other = rng.choice([u for u in types if u != t])
                half = max(1, target() // 2)
                yield {"ProductType1": t, "ProductType2": other,
                       "x": threshold_above([values.num[p].get(1, 0) for p in pool], half),
                       "y": threshold_above([values.num[p].get(2, 0) for p in values.of_type[other] & sold], half)}
    elif query == "q02":
        for product in shuffled(values.label)[:limit]:
            yield {"Product": product, "Taxonomy": values.taxonomy}
    elif query == "q05":
        for product in shuffled(sold)[:limit]:
            if values.types[product] and len(values.num[product]) >= 2:
                yield {"Product": product, "ProductType": rng.choice(sorted(values.types[product]))}
    elif query == "q06":
        for shop in [rng.choice(values.shops) for _ in range(limit)]:
            labels = [values.label[x].lower() for x in values.offers_at[shop] if x in values.label]
            grams = Counter(g for l in labels for g in {l[i:i + 3] for i in range(len(l) - 2)} if g.isalpha())
            fitting = sorted(g for g, n in grams.items() if lo <= n <= hi)
            if fitting:
                yield {"Shop": values.endpoint_of(shop), "word": rng.choice(fitting)}
    elif query in ("q07", "q10", "q15"):
        need = {"q07": lambda x: lo <= values.review_count[x] <= hi and len(values.review_sites[x]) >= 3,
                "q10": lambda x: values.offer_count[x] >= 4,
                "q15": lambda x: values.review_count[x] >= 25}[query]
        pairs = [(shop, x) for shop in values.shops for x in values.offers_at[shop] if need(x)]
        for shop, product in shuffled(pairs)[:limit]:
            v = {"Shop": values.endpoint_of(shop), "Product": product}
            if query == "q10":
                v["days"] = rng.randint(5, 21)
            yield v
    elif query == "q08":
        pool = [x for x in values.reviewed if lo <= values.english_count[x] <= hi and len(values.review_sites[x]) >= 3]
        for product in shuffled(pool)[:limit]:
            yield {"Product": product}
    elif query == "q09":
        for site in [rng.choice(values.sites) for _ in range(limit)]:
            yield {"Site": values.endpoint_of(site), "Review": rng.choice(sorted(values.reviews_at[site]))}
    elif query in ("q11", "q12"):
        for shop in [rng.choice(values.shops) for _ in range(limit)]:
            offers = sorted(o for os in values.offers_at[shop].values() for o in os)
            yield {"Shop": values.endpoint_of(shop), "Offer": rng.choice(offers)}
    elif query == "q13":
        pool = [x for x in values.sold if lo <= values.brand_sold[owner_of(values.dconf, x)] <= hi]
        for product in shuffled(pool)[:limit]:
            yield {"Product": product}
    elif query == "q14":
        for product in shuffled(values.reviewed)[:limit]:
            if values.types[product]:
                t = rng.choice(sorted(values.types[product]))
                reviews = sum(values.review_count[x] for x in values.of_type[t])
                yield {"Product": product, "ProductType": t, "r": rating_threshold(values, reviews, target())}
    elif query == "q16":
        pairs = []
        for shop in values.shops:
            by_type = defaultdict(int)
            for x in values.offers_at[shop]:
                for t in values.types.get(x, []):
                    by_type[t] += values.review_count[x]
            pairs += [(shop, t, n) for t, n in by_type.items() if n >= lo]
        for shop, t, reviews in shuffled(pairs)[:limit]:
            yield {"Shop": values.endpoint_of(shop), "ProductType": t, "r": rating_threshold(values, reviews, target())}


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
        if s.startswith("{") and s.endswith("{") or s == "{" or re.match(r"^SERVICE\s*<[^>]+>\s*\{$", s, re.I):
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


def gains(dconf, owners, query, patterns, rows, known, per_service=3):
    """What sending a sub-query to a service saves, for the sub-queries services supply in the answer.

    For every derivation and service, B' is the set of required patterns that service supplied.
    Evaluated pattern by pattern at the service, B' matches sum(singles) times; sent as a whole
    (with the filters over B's variables) it returns `joined` solutions. gain = sum(singles) / joined.
    """
    layout = pattern_layout(query)
    if len(layout) != len(patterns):
        return []
    services = {i["storage"]: n for n, i in owners.items() if i["service"]}
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
                        "known": graph in known})
    return out


def reference_query(text, services):
    """The query over all published data, with its own projection and modifiers.

    Each pattern matches any publisher's graph; SERVICE patterns match their service's graph. Graph
    variables are projected away inside a DISTINCT subquery over the query's own variables, so a
    triple found in two graphs counts once, as it does in the union of graphs.
    """
    inner, _, patterns = rewrite(text, services=services)
    prefix_lines = [l for l in text.splitlines() if l.strip().upper().startswith("PREFIX")]
    select = re.search(r"SELECT\s+.*?(?=\s+WHERE)", text, re.S | re.I).group(0)
    modifiers = "\n".join(m.group(0) for m in MODIFIERS.finditer(text))
    own_vars = sorted(set().union(*(variables(" ".join(p)) for p in patterns)))
    inner = "\n".join(l for l in inner.splitlines() if not l.strip().upper().startswith("PREFIX"))
    inner = inner.replace("SELECT DISTINCT * WHERE", f"SELECT DISTINCT {' '.join(own_vars)} WHERE", 1)
    return "\n".join(prefix_lines) + f"\n{select} WHERE {{\n{{ {inner} }}\n}}\n{modifiers}\n"


# ----- plain forms -----
def plain_query(text):
    """The query without its SERVICE clauses: their patterns stay, as a plain group."""
    lines = text.splitlines()
    drop, i = set(), 0
    while i < len(lines):
        if re.match(r"^\s*SERVICE\s*<[^>]+>\s*\{\s*$", lines[i], re.I):
            depth, j = 1, i + 1
            while depth:
                t = lines[j].strip()
                depth += t.endswith("{") - t.startswith("}")
                j += 1
            drop |= {i, j - 1}
            i = j
        else:
            i += 1
    return "\n".join(l for k, l in enumerate(lines) if k not in drop) + "\n"


def write_plain(dconf, inst, services):
    """plain.sparql and its answer for one instance directory."""
    text = Path(inst, "injected.sparql").read_text()
    plain = plain_query(text)
    Path(inst, "plain.sparql").write_text(plain)
    if plain == text:
        shutil.copy(Path(inst, "results.csv"), Path(inst, "plain-results.csv"))
    else:
        Path(inst, "plain-results.csv").write_text(sparql_raw_csv(dconf.endpoint, reference_query(plain, services)))


def plain_forms(dconf):
    """Write the plain form of every instance of an existing workload."""
    owners = load_owners(dconf)
    services = service_map(owners)
    insts = sorted(Path(dconf.outdir, "workload").glob("q*/instance_*"))
    for inst in insts:
        write_plain(dconf, inst, services)
    return insts


# ----- writing -----
def write_instance(outdir, text, record, reference, projection, reachable):
    Path(outdir).mkdir(parents=True, exist_ok=True)
    Path(outdir, "injected.sparql").write_text(text)
    Path(outdir, "results.csv").write_text(reference)
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
            fs.write(f"{name}\t{info['storage']}\t{info['endpoint']}\n")
    with open(outdir / "catalog-fedx.ttl", "w") as fs:
        fs.write("@prefix sd: <http://www.w3.org/ns/sparql-service-description#> .\n"
                 "@prefix fedx: <http://rdf4j.org/config/federation#> .\n\n")
        for info in catalog.values():
            fs.write(f'<{info["storage"]}> a sd:Service ;\n    fedx:store "SPARQLEndpoint";\n'
                     f'    sd:endpoint "{info["endpoint"]}";\n    fedx:supportsASKQueries true .\n\n')
    with open(outdir / "discovered-storages.tsv", "w") as fs:   # for analysis only: an engine has to find these
        for name, info in owners.items():
            if not info["classical"]:
                fs.write(f"{name}\t{info['storage']}\t{info.get('endpoint') or ''}\n")


def service_map(owners):
    """Endpoint -> storage of every publisher with a service."""
    return {i["endpoint"]: i["storage"] for i in owners.values() if i["service"]}


def analyse(dconf, owners, analyzer, text):
    max_rows = dconf.workload_conf["max_rows"]
    services = service_map(owners)
    endpoints = service_endpoints(text)
    known = {services[e] for e in endpoints if e in services} if endpoints else None
    projection, patterns, rows = derivations(dconf.endpoint, text, max_rows, services)
    record = analyzer.analyse(projection, patterns, rows, known=known if known is not None else set())
    record["gains"] = gains(dconf, owners, text, patterns, rows, known or set())
    discovered = [g for g in record["gains"] if not g["known"]]
    best = max(discovered, key=lambda g: g["gain"], default=None)
    record["max_gain"] = best["gain"] if best else 0
    record["gain_at"] = best["service"] if best else ""
    return record, projection, set(analyzer.reachable)


def build(dconf):
    owners = load_owners(dconf)
    analyzer = Analyzer(dconf, owners, read_links(dconf))
    values = Values(dconf, owners)
    conf = dconf.workload_conf
    outdir = Path(dconf.outdir, "workload")
    shutil.rmtree(outdir, ignore_errors=True)
    records = []
    services = service_map(owners)
    for tpl in sorted(Path(conf["templates_dir"]).glob("q*.sparql")):
        query, template = tpl.stem, tpl.read_text()
        meta = template_meta(template)
        rng = random.Random(f"{conf['seed']}-{query}")
        kept = tried = 0
        for vals in candidates(values, query, rng, conf["max_candidates"], meta):
            if kept == conf["per_template"]:
                break
            tried += 1
            text = fill(template, vals)
            record = {"query": query, "instance": kept, "kind": meta["kind"], "values": vals}
            try:
                result, projection, reachable = analyse(dconf, owners, analyzer, text)
                record.update(result)
            except (OverflowError, RuntimeError, ValueError) as e:
                record.update({"selected": False, "reason": "error", "error": str(e)[:300]})
                records.append(record)
                continue
            lo, hi = meta["rows"]
            control = meta["kind"] == "control"
            if record["solutions"] == 0:
                reason = "empty"
            elif not lo <= record["solutions"] <= hi:
                reason = f"{record['solutions']} rows, not {lo}-{hi}"
            elif not control and record["reachable_needing_discovery"] == 0:
                reason = "no reachable answer needs discovery"
            elif not control and record["discovered_in_answer"] < meta["discovered"]:
                reason = f"{record['discovered_in_answer']} discovered publishers, not {meta['discovered']}"
            else:
                reason = ""
            record["selected"] = not reason
            record["reason"] = reason or "kept"
            if record["selected"]:
                reference = sparql_raw_csv(dconf.endpoint, reference_query(text, services))
                record["reference_rows"] = max(len(reference.strip().splitlines()) - 1, 0)
                write_instance(outdir / query / f"instance_{kept}", text, record, reference, projection, reachable)
                write_plain(dconf, outdir / query / f"instance_{kept}", services)
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
    record, _, _ = analyse(dconf, owners, analyzer, text)
    return record
