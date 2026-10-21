"""How far link traversal gets on each instance, and how many documents it takes.

The engine is simulated the way Comunica's link-traversal engine follows links: rdfs:seeAlso
always, and cMatch, which follows every IRI in a triple that matches one of the query's patterns.
Only the patterns outside SERVICE clauses count; those inside are answered by their service.
Seeds are the query's own IRIs and the IRIs a SERVICE block binds to variables the rest of the
query uses, when that block is where the query starts: it has a constant of its own, or the rest of
the query has none. A block without constants in a query that has them elsewhere is the target of
a bind join instead, and seeds nothing. The query is then evaluated (rdflib) with each
SERVICE block over its owner's data and everything else over the documents reached, and compared
with the reference (claims) and the union answer. Nothing else is fetched: no storage roots, no
services of discovered owners.

Each instance runs in its own process, killed at the timeout.
"""
import csv
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .common import LOGGER, DiscoveryConfig, load_owners

SEEALSO = "http://www.w3.org/2000/01/rdf-schema#seeAlso"


def norm(v):
    v = str(v).strip()
    try:
        return str(Decimal(v).normalize())
    except InvalidOperation:
        return v


def answer_rows(path):
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        return header, {tuple(norm(x) for x in row) for row in r}


def service_blocks(text):
    """(endpoint, inner lines, start, end) of every top-level SERVICE block, by line."""
    lines, blocks, i = text.splitlines(), [], 0
    while i < len(lines):
        m = re.match(r"^\s*SERVICE\s*<([^>]+)>\s*\{\s*$", lines[i], re.I)
        if not m:
            i += 1
            continue
        depth, j = 1, i + 1
        while depth:
            s = lines[j].strip()
            depth += s.endswith("{") - s.startswith("}")
            j += 1
        blocks.append((m.group(1), lines[i + 1:j - 1], i, j))
        i = j
    return blocks


def simulate(dconf, inst, max_docs):
    from rdflib import Dataset, Graph, URIRef, Variable
    from rdflib.plugins.sparql import prepareQuery
    from rdflib.plugins.sparql.algebra import traverse

    owners = load_owners(dconf)
    by_endpoint = {i["endpoint"]: (n, i["namespace"]) for n, i in owners.items() if i["service"]}
    text = (inst / "injected.sparql").read_text()
    prefixes = "\n".join(l for l in text.splitlines() if l.strip().upper().startswith("PREFIX"))
    blocks = service_blocks(text)
    lines = text.splitlines()
    open_text = "\n".join(l for k, l in enumerate(lines) if not any(a <= k < b for _, _, a, b in blocks))

    def patterns(query_text):
        out = []
        traverse(prepareQuery(query_text).algebra,
                 visitPost=lambda node: out.extend(node.triples) if getattr(node, "name", None) == "BGP" else None)
        return out

    open_patterns = patterns(open_text)
    open_vars = {str(t) for p in open_patterns for t in p if isinstance(t, Variable)}

    ds = Dataset()
    seeds = set(re.findall(r"<(" + re.escape(dconf.base) + r"[^>]+)>", open_text))
    service_seeds = 0
    open_constants = bool(seeds)
    for endpoint, inner, _, _ in blocks:
        name, ns = by_endpoint[endpoint]
        starts = not open_constants or any(re.search(r'<[^>?]+>|"', l) for l in inner)
        g = ds.graph(URIRef(ns))
        for f in (f"{name}.nt", f"{name}.replicas.nt"):
            path = Path(dconf.path("data", f))
            if path.exists():
                g.parse(path, format="nt")
        for row in (g.query(prefixes + "\nSELECT * WHERE {\n" + "\n".join(inner) + "\n}") if starts else []):
            for var, value in row.asdict().items():
                if var in open_vars and isinstance(value, URIRef) and str(value).startswith(dconf.base):
                    if str(value) not in seeds:
                        service_seeds += 1
                    seeds.add(str(value))

    def doc_of(iri):
        iri = iri.split("#", 1)[0]
        return iri if iri.startswith(dconf.base) and not iri.endswith("/") else None

    def matches(triple):
        return any(all(isinstance(p, Variable) or p == t for t, p in zip(triple, pat)) for pat in open_patterns)

    web = Path(dconf.path("web", "x")).parent
    docs = ds.default_context
    seen, queue = set(), [d for d in map(doc_of, sorted(seeds)) if d]
    start = time.time()
    while queue and len(seen) < max_docs:
        doc = queue.pop()
        if doc in seen:
            continue
        seen.add(doc)
        path = web / (doc[len(dconf.base):] + ".nq")
        if not path.exists():
            continue
        dg = Graph()
        dg.parse(path, format="nt")
        for t in dg:
            docs.add(t)
            follow = [t[2]] if str(t[1]) == SEEALSO else []
            if matches(t):
                follow += [x for x in t if isinstance(x, URIRef)]
            for x in follow:
                d = doc_of(str(x))
                if d and d not in seen:
                    queue.append(d)
    crawl_time = time.time() - start
    # The documents reached, by their owner: a composite (documents and a service) or documents only,
    # and the owner's kind.
    by_owner = {}
    for doc in seen:
        name = doc[len(dconf.base):].split("/", 1)[0]
        info = owners.get(name)
        if info:
            key = ("composite" if info["service"] else "documents_only", info["kind"])
            by_owner[key] = by_owner.get(key, 0) + 1
    composite = sum(n for (c, _), n in by_owner.items() if c == "composite")

    query = text
    for endpoint, _, _, _ in blocks:
        query = query.replace(f"SERVICE <{endpoint}>", f"GRAPH <{by_endpoint[endpoint][1]}>")
    header, ref = answer_rows(inst / "results.csv")
    _, union = answer_rows(inst / "union-results.csv")
    got = set()
    for b in ds.query(query).bindings:
        got.add(tuple(norm(b[Variable(v)]) if b.get(Variable(v)) is not None else "" for v in header))
    return {"documents": len(seen), "truncated": bool(queue), "seeds": len(seeds), "seeds_from_services": service_seeds,
            "rows": len(got), "correct": len(got & ref), "reference": len(ref), "complete": ref <= got,
            "union_only_rows": len((got - ref) & union), "other_rows": len(got - union),
            "crawl_seconds": round(crawl_time, 1), "documents_at_composites": composite,
            "documents_by_owner": {f"{c}/{k}": n for (c, k), n in sorted(by_owner.items())}}


def run(dconf, timeout=600, workers=4, max_docs=50000):
    """Simulate traversal for every instance; writes traversal.json next to each and a summary."""
    insts = sorted(Path(dconf.outdir, "workload").glob("q*/instance_*"))

    def one(inst):
        start = time.time()
        try:
            p = subprocess.run([sys.executable, "-m", "fedshop.discovery.traversal", dconf.configfile, str(inst), str(max_docs)],
                               capture_output=True, text=True, timeout=timeout)
            result = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {"error": p.stderr.strip()[-300:]}
        except subprocess.TimeoutExpired:
            result = {"error": f"killed after {timeout} s"}
        result["seconds"] = round(time.time() - start, 1)
        (inst / "traversal.json").write_text(json.dumps(result, indent=2))
        analysis = json.loads((inst / "analysis.json").read_text())
        analysis["traversal"] = result
        (inst / "analysis.json").write_text(json.dumps(analysis, indent=2))
        return inst, result

    rows = []
    with ThreadPoolExecutor(workers) as ex:
        for inst, r in ex.map(one, insts):
            LOGGER.info(f"{inst.parent.name}/{inst.name}: {r}")
            rows.append({"query": inst.parent.name, "instance": inst.name.split("_")[1], **r})
    keys = ["query", "instance", "complete", "correct", "reference", "rows", "union_only_rows", "other_rows",
            "documents", "documents_at_composites", "truncated", "seeds", "seeds_from_services", "crawl_seconds", "seconds", "error"]
    with open(dconf.path("traversal-summary.csv"), "w", newline="") as fs:
        writer = csv.DictWriter(fs, keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return rows


if __name__ == "__main__":
    configfile, inst, max_docs = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    print(json.dumps(simulate(DiscoveryConfig(configfile), inst, max_docs)))
