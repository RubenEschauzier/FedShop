"""Give every triple one owner: the member whose namespace its subject is in.

FedShop lets every shop and review site keep a full copy of each product, producer, feature and
type it deals with, linked to a global BSBM IRI with owl:sameAs. Here:

- a brand (producer) owns its products and their specification (catalog.py);
- the taxonomy owns product types, product features and countries;
- a shop owns its vendor record, its offers and one listing per product it sells;
- a review site owns its record, its reviews and one page per product it reviews;
- a reviewer owns their profile (moved out of the review site).

A listing (or review page) keeps FedShop's local product IRI but no longer repeats the product:
it says `fs:listingOf <brand product>`. Offers and reviews point to the listing, as in FedShop.

Listings of the same product link to each other with rdfs:seeAlso (a shop's listing to a few
review sites' pages, a page to a few shops' listings), and a brand's product links to a few
listings ("where to buy", "reviewed at"). Peers are drawn per listing, seeded.

Some listings carry a cached copy of the brand's spec sheet: triples whose subject is the brand's
product, held by the shop or review site. Some of those copies are stale. Under the claims of the
brands' query services they are dropped; for lookup-only brands nobody claims them.

Everything is written as one N-Triples file per owner (data/<owner>.nt, plus
data/<owner>.replicas.nt for cached specs), with the IRIs the documents are served at.
"""
import csv
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from .catalog import Catalog
from .common import BSBM, COUNTRIES, FS, LOGGER, OWL_SAMEAS, RDF_TYPE, RDFS_LABEL, SEEALSO, read_nquads

FEDSHOP_MEMBER = re.compile(r"^http://www\.((?:vendor|ratingsite)\d+)\.fr/(.*)$")
GLOBAL = re.compile(r"^(ProductType|ProductFeature|Product|Producer)(\d+)$")
LOCAL = re.compile(r"^([A-Za-z]+)(\d+)$")
NUMERIC = [f"{BSBM}productPropertyNumeric{i}" for i in range(1, 6)]
DC_PUBLISHER = "http://purl.org/dc/elements/1.1/publisher"
LISTING_OF = f"{FS}listingOf"
LISTING = f"{FS}Listing"
DIRECTIONS = {  # (from kind, to kind) -> links setting
    ("vendor", "ratingsite"): "reviews",
    ("ratingsite", "vendor"): "where_to_buy",
    ("ratingsite", "ratingsite"): "also_reviewed",
}


XSD = "http://www.w3.org/2001/XMLSchema#"
DATE_ONLY = re.compile(r'^"(\d{4}-\d{2}-\d{2})"\^\^<' + re.escape(XSD) + r'dateTime>$')
DOUBLE = re.compile(r'^"([^"]*)"\^\^<' + re.escape(XSD) + r'double>$')


def canonical_literal(term):
    """Literals as both documents and Virtuoso write them, so a triple has one form everywhere.

    FedShop writes dates as "2008-04-22"^^xsd:dateTime (not a valid dateTime; Virtuoso returns it
    with T00:00:00) and prices as xsd:double (which Virtuoso returns with binary noise). Dates get
    their time of day and prices become xsd:decimal.
    """
    m = DATE_ONLY.match(term)
    if m:
        return f'"{m.group(1)}T00:00:00"^^<{XSD}dateTime>'
    m = DOUBLE.match(term)
    if m:
        return f'"{m.group(1)}"^^<{XSD}decimal>'
    return term


def kind_of(name):
    return re.sub(r"\d+$", "", name)


def number(name):
    return int(re.sub(r"\D", "", name) or 0)


def nt(s, p, o):
    return f"{s} {p} {o} .\n"


def choose_classical(dconf, names):
    """Shops and review sites in the catalog, stratified over both kinds."""
    explicit = dconf.classical_conf.get("members")
    if explicit:
        return sorted(set(explicit) & set(names), key=lambda n: (kind_of(n), number(n)))
    rng = random.Random(dconf.classical_conf.get("seed", 42))
    ratio = float(dconf.classical_conf.get("ratio", 0.5))
    chosen = []
    for kind in ("vendor", "ratingsite"):
        group = sorted((n for n in names if kind_of(n) == kind), key=number)
        chosen += rng.sample(group, round(ratio * len(group)))
    return sorted(chosen, key=lambda n: (kind_of(n), number(n)))


class Ownership:

    def __init__(self, dconf):
        self.dconf = dconf
        self.catalog = Catalog(dconf)
        self.members = sorted(dconf.member_names, key=lambda n: (kind_of(n), number(n)))  # vendorN, ratingsiteN
        self.data = defaultdict(list)          # owner -> N-Triples lines
        self.replicas = defaultdict(list)      # owner -> N-Triples lines of cached specs
        self.replica_docs = []                 # {owner, doc, product, stale, triples}
        self.listings = defaultdict(dict)      # product number -> {owner: listing IRI}
        self.countries = set()
        self.reviewer_iri = {}                 # (site, local Person name) -> reviewer IRI
        self.reviewers = []

    # ----- IRIs -----
    def brand_name(self, brand):
        return f"producer{brand}"

    def product_iri(self, n):
        return f"{self.dconf.ns(self.brand_name(self.catalog.brand_of[n]))}Product{n}"

    def map_iri(self, iri):
        m = FEDSHOP_MEMBER.match(iri)
        if m:
            member, local = m.groups()
            if local.startswith("Person"):
                return self.reviewer_iri[(member, local)]
            return f"{self.dconf.ns(member)}{local}"
        if iri.startswith(BSBM):
            g = GLOBAL.match(iri[len(BSBM):])
            if g:
                kind, n = g.group(1), int(g.group(2))
                if kind in ("ProductType", "ProductFeature"):
                    return f"{self.dconf.ns('taxonomy')}{kind}{n}"
                if kind == "Product":
                    return self.product_iri(n)
                if kind == "Producer":
                    return f"{self.dconf.ns(self.brand_name(n))}Producer{n}"
        if iri.startswith(COUNTRIES):
            code = iri[len(COUNTRIES):]
            self.countries.add(code)
            return f"{self.dconf.ns('taxonomy')}countries#{code}"
        return iri

    def map_term(self, term):
        if term.startswith("<"):
            return f"<{self.map_iri(term[1:-1])}>"
        return canonical_literal(term)

    # ----- owners' data -----
    def build_reviewers(self):
        """Number the reviewers by review site, then by person, as FedShop numbers them."""
        for site in (m for m in self.members if kind_of(m) == "ratingsite"):
            persons = sorted({FEDSHOP_MEMBER.match(s[1:-1]).group(2) for s, p, o in self.read(site)
                              if p == f"<{RDF_TYPE}>" and o == f"<{BSBM}Person>"}, key=number)
            for local in persons:
                name = f"reviewer{len(self.reviewers)}"
                self.reviewer_iri[(site, local)] = f"{self.dconf.ns(name)}me"
                self.reviewers.append(name)

    def read(self, member):
        return read_nquads(Path(self.dconf.dataset_dir, f"{member}.nq"))

    def build_members(self):
        for member in self.members:
            copies = {}
            for s, p, o in self.read(member):
                local = FEDSHOP_MEMBER.match(s[1:-1]).group(2)
                kind = LOCAL.match(local).group(1)
                if kind == "Product":
                    if p == f"<{OWL_SAMEAS}>":
                        copies[s] = int(GLOBAL.match(o[1:-1][len(BSBM):]).group(2))
                    continue                                   # the brand owns the specification
                if kind in ("ProductType", "ProductFeature", "Producer"):
                    continue                                   # the taxonomy and the brands own these
                if kind == "Person":
                    if p == f"<{DC_PUBLISHER}>":
                        continue
                    subject = self.map_term(s)
                    self.data[subject[1:-1].split("/")[-2]].append(nt(subject, p, self.map_term(o)))
                    continue
                self.data[member].append(nt(self.map_term(s), p, self.map_term(o)))
            for copy, n in sorted(copies.items(), key=lambda kv: kv[1]):
                listing = self.map_term(copy)
                self.listings[n][member] = listing[1:-1]
                self.data[member].append(nt(listing, f"<{RDF_TYPE}>", f"<{LISTING}>"))
                self.data[member].append(nt(listing, f"<{LISTING_OF}>", f"<{self.product_iri(n)}>"))

    def spec(self, n):
        """The brand's triples for product n."""
        out = []
        for s, p, o in self.catalog.product(n):
            if p in (f"<{OWL_SAMEAS}>",):
                continue
            if p == f"<{BSBM}producer>":
                o = f"<{self.dconf.ns(self.brand_name(self.catalog.brand_of[n]))}Producer{self.catalog.brand_of[n]}>"
            out.append((f"<{self.product_iri(n)}>", p, self.map_term(o)))
        return out

    def build_brands(self):
        self.specs = {}
        for brand in range(self.catalog.count):
            name = self.brand_name(brand)
            for s, p, o in self.catalog.producer(brand):
                if p != f"<{OWL_SAMEAS}>":
                    self.data[name].append(nt(f"<{self.dconf.ns(name)}Producer{brand}>", p, self.map_term(o)))
        for n in range(self.catalog.n):
            triples = self.spec(n)
            if n in self.listings:
                self.specs[n] = triples
            name = self.brand_name(self.catalog.brand_of[n])
            self.data[name].extend(nt(*t) for t in triples)

    def build_taxonomy(self):
        for path in self.catalog.taxonomy_files():
            for s, p, o in read_nquads(path):
                if p != f"<{OWL_SAMEAS}>":
                    self.data["taxonomy"].append(nt(self.map_term(s), p, self.map_term(o)))
        for code in sorted(self.countries):
            self.data["taxonomy"].append(nt(f"<{self.dconf.ns('taxonomy')}countries#{code}>", f"<{RDFS_LABEL}>", f'"{code}"'))

    # ----- links and cached specs -----
    def build_links(self):
        conf = self.dconf.links_conf
        links = []

        def sample(key, scope, peers):
            count = conf.get(key, 0)
            if not count or not peers:
                return []
            if count == "all":
                return peers
            return sorted(random.Random(f"{conf['seed']}|{key}|{scope}").sample(peers, min(int(count), len(peers))))

        for n in sorted(self.listings):
            holders = self.listings[n]
            product = self.product_iri(n)
            for member in sorted(holders, key=lambda m: (kind_of(m), number(m))):
                for (src, dst), key in DIRECTIONS.items():
                    if kind_of(member) != src:
                        continue
                    peers = sorted((m for m in holders if m != member and kind_of(m) == dst), key=number)
                    for peer in sample(key, f"{n}|{member}", peers):
                        links.append((product, member, holders[member], peer, holders[peer]))
            brand = self.brand_name(self.catalog.brand_of[n])
            for key, dst in (("brand_where_to_buy", "vendor"), ("brand_reviews", "ratingsite")):
                peers = sorted((m for m in holders if kind_of(m) == dst), key=number)
                for peer in sample(key, n, peers):
                    links.append((product, brand, product, peer, holders[peer]))
        for product, owner, source, peer, target in links:
            self.data[owner].append(nt(f"<{source}>", f"<{SEEALSO}>", f"<{target}>"))
        self.links = links

    def build_replicas(self):
        conf = self.dconf.replicas_conf
        share, stale = float(conf["share"]), float(conf["stale"])
        for n in sorted(self.listings):
            spec = [t for t in self.specs[n] if t[1] in (f"<{RDFS_LABEL}>", *(f"<{p}>" for p in NUMERIC))]
            for member, listing in sorted(self.listings[n].items()):
                rng = random.Random(f"{conf['seed']}|{listing}")
                if rng.random() >= share:
                    continue
                is_stale = rng.random() < stale
                triples = []
                for s, p, o in spec:
                    if is_stale and p != f"<{RDFS_LABEL}>":
                        value = int(re.match(r'^"(\d+)"', o).group(1))
                        value = min(2000, max(1, value + rng.choice((-1, 1)) * rng.randint(50, 400)))
                        o = f'"{value}"^^<http://www.w3.org/2001/XMLSchema#integer>'
                    triples.append(nt(s, p, o))
                self.replicas[member].extend(triples)
                self.replica_docs.append({"owner": member, "doc": listing, "product": self.product_iri(n),
                                          "stale": is_stale, "triples": "".join(triples)})

    # ----- owners -----
    def owners(self):
        dconf = self.dconf
        classical = set(choose_classical(dconf, self.members))
        owners = {"taxonomy": {"kind": "taxonomy", "classical": True, "service": True}}
        for m in self.members:
            owners[m] = {"kind": kind_of(m), "classical": m in classical, "service": True}
        for brand in range(self.catalog.count):
            owners[self.brand_name(brand)] = {"kind": "producer", "classical": False,
                                              "service": brand in self.catalog.services,
                                              "products": self.catalog.sizes[brand]}
        for r in self.reviewers:
            owners[r] = {"kind": "reviewer", "classical": False, "service": False}
        port = int(dconf.virtuoso["first_service_port"])
        for name, info in owners.items():
            info["namespace"] = dconf.ns(name)
            info["triples"] = len(self.data[name]) + len(self.replicas[name])
            info["cached_specs"] = len([d for d in self.replica_docs if d["owner"] == name])
            if info["service"]:
                info["port"] = port
                info["endpoint"] = f"http://localhost:{port}/{name}/sparql"
                port += 1
        return owners

    def build(self):
        self.build_reviewers()
        self.build_members()
        self.build_brands()
        self.build_taxonomy()
        self.build_links()
        self.build_replicas()
        return self.owners()


def write(dconf, own, owners):
    data_dir = Path(dconf.path("data", "x")).parent
    for old in data_dir.glob("*.nt"):
        old.unlink()
    for name in owners:
        Path(data_dir, f"{name}.nt").write_text("".join(sorted(set(own.data[name]))))
        if own.replicas[name]:
            Path(data_dir, f"{name}.replicas.nt").write_text("".join(sorted(set(own.replicas[name]))))
    Path(dconf.path("owners.json")).write_text(json.dumps(owners, indent=2))
    Path(dconf.path("replicas.json")).write_text(json.dumps(own.replica_docs, indent=1))
    with open(dconf.path("links.tsv"), "w", newline="") as fs:
        writer = csv.writer(fs, delimiter="\t")
        writer.writerow(["product", "owner", "from", "peer", "to"])
        writer.writerows(own.links)


def read_links(dconf):
    with open(dconf.path("links.tsv"), newline="") as fs:
        return [tuple(r) for r in list(csv.reader(fs, delimiter="\t"))[1:]]


def run(dconf):
    own = Ownership(dconf)
    owners = own.build()
    write(dconf, own, owners)
    kinds = defaultdict(lambda: defaultdict(int))
    for info in owners.values():
        k = kinds[info["kind"]]
        k["owners"] += 1
        k["classical"] += info["classical"]
        k["services"] += info["service"]
        k["triples"] += info["triples"]
    summary = {"owners": {k: dict(v) for k, v in kinds.items()},
               "listings": sum(len(h) for h in own.listings.values()), "listed_products": len(own.listings),
               "links": len(own.links), "cached_specs": len(own.replica_docs),
               "stale_cached_specs": sum(d["stale"] for d in own.replica_docs)}
    LOGGER.info(json.dumps(summary))
    return summary
