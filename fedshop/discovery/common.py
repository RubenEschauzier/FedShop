import csv
import io
import json
import os
from pathlib import Path

import requests
from omegaconf import OmegaConf
from pyoxigraph import RdfFormat, parse

from ..utils import fedshop_logger, load_config

LOGGER = fedshop_logger(Path(__file__).name)

BSBM = "http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/vocabulary/"
FS = "http://fedshop.example.org/vocab#"        
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SEEALSO = "http://www.w3.org/2000/01/rdf-schema#seeAlso"
OWL_SAMEAS = "http://www.w3.org/2002/07/owl#sameAs"
PIM_STORAGE = "http://www.w3.org/ns/pim/space#Storage"
VOID_ENDPOINT = "http://rdfs.org/ns/void#sparqlEndpoint"
COUNTRIES = "http://downlode.org/rdf/iso-3166/countries#"

DEFAULTS = {
    "base_url": "http://localhost:3000/",
    "classical": {"ratio": 0.5, "seed": 42, "members": None},
    "members": {"shops": 20, "review_sites": 20, "pool": 20000, "offers": 2000, "reviews": 10000, "seed": 42},
    "brands": {"products": 200000, "count": 400, "zipf": 0.8, "min_products": 20, "service_share": 0.25, "seed": 42},
    "links": {"reviews": 2, "where_to_buy": 3, "also_reviewed": 0, "brand_where_to_buy": 2, "brand_reviews": "all",
              "seed": 42},
    "virtuoso": {"compose_file": None, "container": "fedshop-discovery-virtuoso", "isql_port": 1112,
                 "http_port": 8891, "first_service_port": 34300},
    "css": {"port": 3000, "config": None},
    "workload": {"templates_dir": None, "per_template": 5, "max_candidates": 40, "max_rows": 100000, "seed": 42},
}


class DiscoveryConfig:
    """FedShop config plus the `discovery` section, with defaults filled in."""

    def __init__(self, configfile):
        self.configfile = configfile
        self.conf = load_config(configfile)
        gen = self.conf["generation"]
        section = self.conf.get("discovery")
        section = OmegaConf.to_container(section, resolve=True) if section is not None else {}

        def merged(key):
            value = dict(DEFAULTS[key]) if isinstance(DEFAULTS[key], dict) else DEFAULTS[key]
            if section.get(key) is not None:
                if isinstance(value, dict):
                    value.update(dict(section[key]))
                else:
                    value = section[key]
            return value

        self.workdir = gen["workdir"]
        self.base = merged("base_url")
        self.members_conf = merged("members")
        self.classical_conf = merged("classical")
        self.brands_conf = merged("brands")
        self.links_conf = merged("links")
        self.virtuoso = merged("virtuoso")
        self.css = merged("css")
        self.workload_conf = merged("workload")
        self.outdir = section.get("workdir") or f"{self.workdir}/benchmark/discovery"
        self.dataset_dir = f"{self.outdir}/members"       
        self.discovery_dir = f"{self.workdir}/discovery"
        self.virtuoso["compose_file"] = self.virtuoso["compose_file"] or f"{self.discovery_dir}/docker/virtuoso.yml"
        self.css["config"] = self.css["config"] or f"{self.discovery_dir}/css-config.json"
        self.workload_conf["templates_dir"] = self.workload_conf["templates_dir"] or f"{self.discovery_dir}/queries"

        self.pool = int(self.members_conf["pool"])                    
        self.member_names = ([f"vendor{i}" for i in range(int(self.members_conf["shops"]))]
                             + [f"ratingsite{i}" for i in range(int(self.members_conf["review_sites"]))])

    # The discovery Virtuoso container: every owner's graph, one query service per composite
    @property
    def endpoint(self):
        return f"http://localhost:{self.virtuoso['http_port']}/sparql"

    def ns(self, owner):
        return f"{self.base}{owner}/"

    def path(self, *parts):
        p = Path(self.outdir, *parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)


def owner_of(dconf, iri):
    """The owner (first path segment under the base URL) of an IRI, or None."""
    if not iri.startswith(dconf.base):
        return None
    rest = iri[len(dconf.base):]
    return rest.split("/", 1)[0] if "/" in rest else None


def load_owners(dconf):
    with open(dconf.path("owners.json")) as fs:
        return json.load(fs)


def read_nquads(path):
    """Yield (s, p, o) N-Triples terms of a FedShop N-Quads file, without duplicates or graphs.

    Terms keep their lexical form (FedShop's "2008-04-22"^^xsd:dateTime stays as written).
    """
    seen = set()
    for quad in parse(path=str(path), format=RdfFormat.N_QUADS):
        triple = (str(quad.subject), str(quad.predicate), str(quad.object))
        if triple not in seen:
            seen.add(triple)
            yield triple


def sparql_csv(endpoint, query, timeout=600):
    """Run a SELECT query and return its rows as dicts (IRIs without brackets)."""
    return list(csv.DictReader(io.StringIO(sparql_raw_csv(endpoint, query, timeout))))


def sparql_raw_csv(endpoint, query, timeout=600):
    r = requests.post(endpoint, data={"query": query}, headers={"Accept": "text/csv"}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"SPARQL error {r.status_code}: {r.text[:500]}")
    return r.text


def wait_for(url, timeout=300):
    import time
    start = time.time()
    while time.time() - start < timeout:
        try:
            requests.get(url, timeout=5)
            return
        except requests.RequestException:
            time.sleep(1)
    raise TimeoutError(f"{url} did not come up within {timeout} s")
