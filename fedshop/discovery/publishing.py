"""Restructure FedShop's data into publishers, each with its own storage.

FedShop lets every shop and review site keep a full copy of each product, producer, feature and
type it deals with, linked to a global BSBM IRI with owl:sameAs. Here each is described once:

- a brand (producer) publishes its products and their specification (catalog.py);
- the taxonomy publishes product types, product features and countries;
- a shop publishes its vendor record, its offers and one listing per product it sells;
- a review site publishes its record, its reviews and one page per product it reviews;
- a reviewer publishes their profile (moved out of the review site), which lists the reviews they
  wrote with `foaf:made`, at whichever review site.

An entity's IRI is the URL of the document that describes it plus a fragment (<base>vendor13/Offer7#it in
document <base>vendor13/Offer7), so what a server says about the document (its size, its type,
when it changed) is not about the entity. Countries keep their hash IRIs in one document
(<base>taxonomy/countries#ES). Publishers can group entities into fewer documents (layout.py):
- shops and review sites with product pages describe their offers and reviews in their listings
  (<base>vendor13/Product42#Offer7); every offer and review states its identifier
  (`dcterms:identifier "Offer7"`) for that;
- review sites by day describe their reviews in a document per review date
  (<base>ratingsite2/reviews/2007-03-05#Review9);
- brands by month describe their products in a document per publish month
  (<base>producer22/products/2005-06#Product77).

Every publisher's own record (a brand's, shop's or review site's record, a reviewer's profile) names its
storage with `pim:storage`, as a WebID profile does, and the storage root lists every document
(ldp:contains). So reaching a publisher's record makes all of its documents reachable, and the
query templates reach the records of the entities they use (`bsbm:producer`, `bsbm:vendor`,
`dc:publisher`, `rev:reviewer`).

A listing (or review page) keeps FedShop's local product IRI but no longer repeats the product:
it says `fs:listingOf <brand product>`. Offers and reviews point to the listing, as in FedShop.

Listings of the same product link to each other with rdfs:seeAlso (a shop's listing to a few
review sites' pages, a page to a few shops' listings), and a brand's product links to a few
listings ("where to buy", "reviewed at"). Peers are drawn per listing, seeded. An engine follows
rdfs:seeAlso whatever the query, so these links make nearly every listing reachable from any
product. With `links.brand_listings` the brand instead states, for every listing of its product,
`<listing> fs:listingOf <product>` in the product's document (and in its graph): an engine
following the query only reaches the listings when the query asks for listings (a pattern
`?listing fs:listingOf ...`). The experiments set the seeAlso counts to 0 with it.

With markets (markets.py), shops, review sites, reviewers and brands belong to a market, and a
member's listings are of its market's products: WatDiv's local product k becomes product
pool[market][k] (the listing is named after it: <base>vendor13/Product<n>#it). The records'
countries are of their market, and with `brands.type_spread` a product's types are redrawn from
those available in its brand's market. markets.json says which publisher and type is in which market.

Documents get triples that let a traversal engine follow the query (cMatch: a triple matching a
query pattern leads to the IRIs in it). They are copies of triples the publisher already states, placed
in a second document of the same publisher, so the data behind the services doesn't change:

- a listing's document repeats `<offer> bsbm:product <listing>` of each of its offers, and a
  review page's document `<review> bsbm:reviewFor <page>` of each of its reviews (not with product
  pages, where the listing holds its offers or reviews);
- a brand's own record (<brand>/ProducerN) repeats `<product> bsbm:producer <record>` of each of
  its products, as a brand's catalog page lists its range.

The taxonomy's product type links with rdfs:seeAlso to the records of the brands that make it (these
links are the taxonomy's own triples).

Everything is written as one N-Triples file per publisher, with the IRIs the documents are served
at (after the layout): data/known/ (shops/, review-sites/) and data/discoverable/ (the taxonomy,
shops/, review-sites/, producers/, reviewers/), as publishers.json's `file` says. Triples placed in a document
other than their subject's (the copies above) are written to placements.nq, with that document as
graph.
"""
import csv
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import layout
from .catalog import Catalog
from .common import (BSBM, COUNTRIES, DCTERMS_IDENTIFIER, FS, LOGGER, OWL_SAMEAS, PIM_STORAGE_OF, RDF_TYPE,
                     RDFS_LABEL, SEEALSO, graph_file, graph_order, read_nquads)
from .markets import Markets

FEDSHOP_MEMBER = re.compile(r"^http://www\.((?:vendor|ratingsite)\d+)\.fr/(.*)$")
GLOBAL = re.compile(r"^(ProductType|ProductFeature|Product|Producer)(\d+)$")
LOCAL = re.compile(r"^([A-Za-z]+)(\d+)$")
DC_PUBLISHER = "http://purl.org/dc/elements/1.1/publisher"
REV_REVIEWER = "http://purl.org/stuff/rev#reviewer"
FOAF_MADE = "http://xmlns.com/foaf/0.1/made"
LISTING_OF = f"{FS}listingOf"
LISTING = f"{FS}Listing"
DIRECTIONS = {  # (from kind, to kind) -> links setting
    ("vendor", "ratingsite"): "reviews",
    ("ratingsite", "vendor"): "where_to_buy",
    ("ratingsite", "ratingsite"): "also_reviewed",
}
XSD = "http://www.w3.org/2001/XMLSchema#"
DATE_ONLY = re.compile(r'^"(\d{4}-\d{2}-\d{2})"\^\^<' + re.escape(XSD) + r'dateTime>$')
DATE = re.compile(r'^"(\d{4}-\d{2}-\d{2})')
BSBM_COUNTRY = f"<{BSBM}country>"
PRODUCT_TYPE = re.compile(r"^<" + re.escape(BSBM) + r"ProductType(\d+)>$")
RECORD_TYPES = {f"<{BSBM}{kind}>" for kind in ("Producer", "Vendor", "RatingSite", "Person")}   # a publisher's own record
DOUBLE = re.compile(r'^"([^"]*)"\^\^<' + re.escape(XSD) + r'double>$')


def canonical_literal(term):
    """Literals as both documents and Virtuoso write them, so a triple has one form everywhere.

    FedShop writes dates as "2008-04-22"^^xsd:dateTime (not a valid dateTime; Virtuoso returns it
    with T00:00:00) and prices as xsd:double (which Virtuoso returns with binary noise). Dates get
    their time of day and prices become xsd:decimal, without trailing zeros as Virtuoso writes them
    ("8325.70" is "8325.7", "8325.00" is "8325"): otherwise an engine that reads a price from both
    a document and a service gets two different terms.
    """
    m = DATE_ONLY.match(term)
    if m:
        return f'"{m.group(1)}T00:00:00"^^<{XSD}dateTime>'
    m = DOUBLE.match(term)
    if m:
        value = m.group(1)
        if "." in value:
            value = value.rstrip("0").rstrip(".") or "0"
        return f'"{value}"^^<{XSD}decimal>'
    return term


def kind_of(name):
    return re.sub(r"\d+$", "", name)


def number(name):
    return int(re.sub(r"\D", "", name) or 0)


def nt(s, p, o):
    return f"{s} {p} {o} .\n"


def doc(iri):
    """The document that describes an entity: its IRI without fragment."""
    return iri.split("#", 1)[0]


def choose_known(dconf, names, member_market=None):
    """Shops and review sites known up front, stratified over both kinds, and over the markets
    (member_market: name -> market), so that every market has known shops and review sites."""
    explicit = dconf.known_conf.get("members")
    if explicit:
        return sorted(set(explicit) & set(names), key=lambda n: (kind_of(n), number(n)))
    rng = random.Random(dconf.known_conf.get("seed", 42))
    ratio = float(dconf.known_conf.get("ratio", 0.5))
    market = member_market or {}
    chosen = []
    for kind in ("vendor", "ratingsite"):
        for m in sorted({market.get(n, 0) for n in names}):
            group = sorted((n for n in names if kind_of(n) == kind and market.get(n, 0) == m), key=number)
            count = round(ratio * len(group))
            if member_market and group:
                count = max(1, count)                  # every market has one known shop and review site
            chosen += rng.sample(group, count)
    return sorted(chosen, key=lambda n: (kind_of(n), number(n)))


def choose_layouts(dconf, names, known):
    """Each shop's and review site's layout (layout.py): the shares of `layout.shops` and
    `layout.review_sites`, stratified over known or not; the rest a document per offer or review."""
    conf = dconf.layout_conf
    if "product_pages" in conf:
        raise ValueError("layout.product_pages is now given per kind: layout.shops.product_pages, "
                         "layout.review_sites.product_pages")
    rng = random.Random(conf.get("seed", 42))
    layouts = {n: layout.PER_RESOURCE for n in names}
    for kind, key, kinds_layouts in (("vendor", "shops", (layout.PRODUCT_PAGES,)),
                                     ("ratingsite", "review_sites", (layout.PRODUCT_PAGES, layout.BY_DAY))):
        shares = dict(conf.get(key) or {})
        unknown = set(shares) - {name.replace("-", "_") for name in kinds_layouts}
        if unknown:
            raise ValueError(f"layout.{key}: unknown layouts {sorted(unknown)}")
        if sum(float(s) for s in shares.values()) > 1:
            raise ValueError(f"layout.{key}: the shares add up to more than 1")
        for is_known in (True, False):
            rest = sorted((n for n in names if kind_of(n) == kind and (n in known) == is_known), key=number)
            size = len(rest)
            for name in kinds_layouts:
                count = round(float(shares.get(name.replace("-", "_"), 0)) * size)
                chosen = rng.sample(rest, min(count, len(rest)))
                layouts.update((n, name) for n in chosen)
                rest = [n for n in rest if n not in chosen]
    return layouts


SERVICE_KINDS = {"producer": "brands", "vendor": "shops", "ratingsite": "review_sites"}   # kind -> services key
SIZE_FIELDS = {"producer": "products", "vendor": "offers", "ratingsite": "reviews"}      # what a publisher's size counts
SELECTIONS = ("largest", "data")


def choose_services(conf, sizes):
    """The discoverable publishers that run a query service (`services`). The known shops and review
    sites always do, the taxonomy and reviewers never, so they are not chosen here.

    sizes: kind ("producer", "vendor", "ratingsite") -> {publisher: size}: a brand's products, a
    discovered shop's offers, a discovered review site's reviews. Per kind, with `services.<key>` as share:
    - select `largest`: the largest publishers (ties by number), round(share * their number) of them;
    - select `data`: the publishers in a seeded random order (one order per kind), as many as it takes
      for their sizes to reach share of the kind's total. A larger share takes a longer prefix of the
      same order, so it keeps every service of a smaller share; it overshoots by less than one publisher.
    """
    select = conf.get("select", "largest")
    if select not in SELECTIONS:
        raise ValueError(f"services.select: {select!r} is not one of {', '.join(SELECTIONS)}")
    chosen = set()
    for kind, key in SERVICE_KINDS.items():
        share = float(conf[key])
        if not 0 <= share <= 1:
            raise ValueError(f"services.{key}: {share} is not between 0 and 1")
        group = sizes.get(kind, {})
        names = sorted(group, key=number)
        if select == "largest":
            chosen.update(sorted(names, key=lambda n: (-group[n], number(n)))[:round(share * len(names))])
        else:
            random.Random(f"{conf.get('seed', 42)}|{key}").shuffle(names)
            target, total = share * sum(group.values()), 0
            for name in names:
                if total >= target:
                    break
                chosen.add(name)
                total += group[name]
    return chosen


class Publishing:
    """FedShop's catalog and members, restructured into publishers: build() fills every publisher's
    triples, the links and the placements, and returns publishers.json."""

    def __init__(self, dconf):
        self.dconf = dconf
        self.catalog = Catalog(dconf)
        self.members = sorted(dconf.member_names, key=lambda n: (kind_of(n), number(n)))  # vendorN, ratingsiteN
        self.data = defaultdict(list)          # publisher -> N-Triples lines
        self.placements = []                   # N-Quads lines: a triple and the document it is also placed in
        self.categories = defaultdict(set)     # type IRI -> brands that make it (the taxonomy links to their records)
        self.listings = defaultdict(dict)      # product number -> {publisher: listing IRI}
        self.countries = set()
        self.reviewer_iri = {}                 # (site, local Person name) -> reviewer IRI
        self.reviewers = []
        self.reviewer_site = {}                # reviewer -> review site
        self.markets = Markets(dconf, self.catalog, self.members, self.catalog.type_numbers())
        self.known = set(choose_known(dconf, self.members, self.markets.member_market if self.markets.enabled else None))
        self.layouts = choose_layouts(dconf, self.members, self.known)
        self.product_pages = {m for m, name in self.layouts.items() if name == layout.PRODUCT_PAGES}
        self.brands_layout = dconf.layout_conf.get("brands", layout.PER_PRODUCT)
        if self.brands_layout not in (layout.PER_PRODUCT, layout.BY_MONTH):
            raise ValueError(f"layout.brands: {self.brands_layout!r} is neither {layout.PER_PRODUCT} nor {layout.BY_MONTH}")
        self.months = {}                       # product number -> publish month (by-month brands)
        self.review_day = {}                   # (site, local Review name) -> review date (by-day review sites)
        self.entities = {}                     # member -> its offers or reviews

    # ----- IRIs -----
    def brand_name(self, brand):
        return f"producer{brand}"

    def product_iri(self, n):
        ns = self.dconf.ns(self.brand_name(self.catalog.brand_of[n]))
        if self.brands_layout == layout.BY_MONTH:
            return f"{ns}products/{self.publish_month(n)}#Product{n}"
        return f"{ns}Product{n}#it"

    def publish_month(self, n):
        if n not in self.months:
            dates = [o for _, p, o in self.catalog.product(n) if p == f"<{BSBM}publishDate>"]
            if len(dates) != 1:
                raise ValueError(f"Product{n} has {len(dates)} publish dates, not one")
            self.months[n] = DATE.match(dates[0]).group(1)[:7]
        return self.months[n]

    def producer_iri(self, brand):
        return f"{self.dconf.ns(self.brand_name(brand))}Producer{brand}#it"

    def map_iri(self, iri):
        m = FEDSHOP_MEMBER.match(iri)
        if m:
            member, local = m.groups()
            if local.startswith("Person"):
                return self.reviewer_iri[(member, local)]
            if self.layouts.get(member) == layout.BY_DAY and local.startswith("Review"):
                if (member, local) not in self.review_day:
                    raise ValueError(f"{member}'s {local} has no review date to group it by")
                return f"{self.dconf.ns(member)}reviews/{self.review_day[(member, local)]}#{local}"
            if self.markets.enabled and LOCAL.match(local).group(1) == "Product":
                local = f"Product{self.market_product(member, int(local[len('Product'):]))}"
            return f"{self.dconf.ns(member)}{local}#it"
        if iri.startswith(BSBM):
            g = GLOBAL.match(iri[len(BSBM):])
            if g:
                kind, n = g.group(1), int(g.group(2))
                if kind in ("ProductType", "ProductFeature"):
                    return f"{self.dconf.ns('taxonomy')}{kind}{n}#it"
                if kind == "Product":
                    return self.product_iri(n)
                if kind == "Producer":
                    return self.producer_iri(n)
        if iri.startswith(COUNTRIES):
            code = iri[len(COUNTRIES):]
            self.countries.add(code)
            return f"{self.dconf.ns('taxonomy')}countries#{code}"
        return iri

    def market_product(self, member, k):
        """The product a member's WatDiv product k is: k itself, or with markets pool[market][k]."""
        if not self.markets.enabled:
            return k
        pool = self.markets.pool[self.markets.member_market[member]]
        if k >= len(pool):
            raise ValueError(f"{member} deals in Product{k}, beyond its market's pool of {len(pool)}: "
                             "regenerate the members (members --force) after changing markets")
        return pool[k]

    def country_iri(self, market, name, original):
        """A record's country: one of its market's (markets.py), or WatDiv's for a global brand."""
        code = self.markets.country(market, name, original[1:-1][len(COUNTRIES):])
        return self.map_term(f"<{COUNTRIES}{code}>")

    def map_term(self, term):
        if term.startswith("<"):
            return f"<{self.map_iri(term[1:-1])}>"
        return canonical_literal(term)

    # ----- publishers' data -----
    def build_reviewers(self):
        """Number the reviewers by review site, then by person, as FedShop numbers them."""
        for site in (m for m in self.members if kind_of(m) == "ratingsite"):
            persons = sorted({FEDSHOP_MEMBER.match(s[1:-1]).group(2) for s, p, o in self.read(site)
                              if p == f"<{RDF_TYPE}>" and o == f"<{BSBM}Person>"}, key=number)
            for local in persons:
                name = f"reviewer{len(self.reviewers)}"
                self.reviewer_iri[(site, local)] = f"{self.dconf.ns(name)}profile#it"
                self.reviewers.append(name)
                self.reviewer_site[name] = site

    def read(self, member):
        return read_nquads(Path(self.dconf.members_dir, f"{member}.nq"))

    def build_members(self):
        for member in self.members:
            if self.layouts[member] == layout.BY_DAY:
                for s, p, o in self.read(member):
                    if p == f"<{BSBM}reviewDate>":
                        self.review_day[(member, FEDSHOP_MEMBER.match(s[1:-1]).group(2))] = DATE.match(o).group(1)
            copies = {}
            identified = set()
            for s, p, o in self.read(member):
                local = FEDSHOP_MEMBER.match(s[1:-1]).group(2)
                kind = LOCAL.match(local).group(1)
                if kind in ("Offer", "Review") and local not in identified:
                    identified.add(local)
                    self.data[member].append(nt(self.map_term(s), f"<{DCTERMS_IDENTIFIER}>", f'"{local}"'))
                if kind == "Product":
                    if p == f"<{OWL_SAMEAS}>":
                        copies[s] = self.market_product(member, int(GLOBAL.match(o[1:-1][len(BSBM):]).group(2)))
                    continue                                   # the brand publishes the specification
                if kind in ("ProductType", "ProductFeature", "Producer"):
                    continue                                   # the taxonomy and the brands publish these
                market = self.markets.member_market[member]
                if kind == "Person":
                    if p == f"<{DC_PUBLISHER}>":
                        continue
                    subject = self.map_term(s)
                    reviewer = subject[1:-1].split("/")[-2]
                    obj = self.country_iri(market, reviewer, o) if p == BSBM_COUNTRY else self.map_term(o)
                    self.data[reviewer].append(nt(subject, p, obj))
                    continue
                obj = self.country_iri(market, member, o) if p == BSBM_COUNTRY else self.map_term(o)
                self.data[member].append(nt(self.map_term(s), p, obj))
            self.entities[member] = len(identified)
            for copy, n in sorted(copies.items(), key=lambda kv: kv[1]):
                listing = self.map_term(copy)
                self.listings[n][member] = listing[1:-1]
                self.data[member].append(nt(listing, f"<{RDF_TYPE}>", f"<{LISTING}>"))
                self.data[member].append(nt(listing, f"<{LISTING_OF}>", f"<{self.product_iri(n)}>"))

    def build_reviewer_links(self):
        """A reviewer's profile lists the reviews they wrote: `<reviewer> foaf:made <review>`."""
        for site in (m for m in self.members if kind_of(m) == "ratingsite"):
            for line in self.data[site]:
                review, p, reviewer = line[:-3].split(" ", 2)
                if p == f"<{REV_REVIEWER}>":
                    self.data[reviewer[1:-1].split("/")[-2]].append(nt(reviewer, f"<{FOAF_MADE}>", review))

    def spec(self, n):
        """The brand's triples for product n. With brands.type_spread, each of WatDiv's product types is
        replaced by one available in the brand's market (markets.py), without duplicates."""
        out, seen = [], set()
        for s, p, o in self.catalog.product(n):
            if p == f"<{OWL_SAMEAS}>":
                continue
            if p == f"<{BSBM}producer>":
                o = f"<{self.producer_iri(self.catalog.brand_of[n])}>"
            if p == f"<{RDF_TYPE}>":
                t = PRODUCT_TYPE.match(o)
                if t:
                    o = f"<{BSBM}ProductType{self.markets.remap_type(self.catalog, n, int(t.group(1)))}>"
            triple = (f"<{self.product_iri(n)}>", p, self.map_term(o))
            if triple not in seen:
                seen.add(triple)
                out.append(triple)
        return out

    def build_brands(self):
        for brand in range(self.catalog.count):
            name = self.brand_name(brand)
            market = self.markets.brand_market[brand]
            for s, p, o in self.catalog.producer(brand):
                if p == f"<{BSBM}country>":
                    self.data[name].append(nt(f"<{self.producer_iri(brand)}>", p, self.country_iri(market, name, o)))
                elif p != f"<{OWL_SAMEAS}>":
                    self.data[name].append(nt(f"<{self.producer_iri(brand)}>", p, self.map_term(o)))
        for n in range(self.catalog.n):
            name = self.brand_name(self.catalog.brand_of[n])
            self.data[name].extend(nt(*t) for t in self.spec(n))

    def build_taxonomy(self):
        for path in self.catalog.taxonomy_files():
            for s, p, o in read_nquads(path):
                if p != f"<{OWL_SAMEAS}>":
                    self.data["taxonomy"].append(nt(self.map_term(s), p, self.map_term(o)))
        for code in sorted(self.countries):
            self.data["taxonomy"].append(nt(f"<{self.dconf.ns('taxonomy')}countries#{code}>", f"<{RDFS_LABEL}>", f'"{code}"'))

    def build_storages(self):
        """Every publisher's own record names its storage: `<record> pim:storage <storage>`, as a WebID
        profile does. The taxonomy has no record."""
        for name in [n for n in self.data if n != "taxonomy"]:
            ns = self.dconf.ns(name)
            records = set()
            for line in self.data[name]:
                s, p, o = line[:-3].split(" ", 2)
                if p == f"<{RDF_TYPE}>" and o in RECORD_TYPES and s.startswith(f"<{ns}"):
                    records.add(s)
            if len(records) != 1:
                raise ValueError(f"{name} has {len(records)} records of its own, not one: {sorted(records)[:3]}")
            self.data[name].append(nt(records.pop(), f"<{PIM_STORAGE_OF}>", f"<{ns}>"))

    # ----- links -----
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
                        links.append((product, member, holders[member], peer, holders[peer], "seeAlso"))
            brand = self.brand_name(self.catalog.brand_of[n])
            for key, dst in (("brand_where_to_buy", "vendor"), ("brand_reviews", "ratingsite")):
                peers = sorted((m for m in holders if kind_of(m) == dst), key=number)
                for peer in sample(key, n, peers):
                    links.append((product, brand, product, peer, holders[peer], "seeAlso"))
            if conf.get("brand_listings"):
                for peer in sorted(holders, key=lambda m: (kind_of(m), number(m))):
                    links.append((product, brand, product, peer, holders[peer], "listingOf"))
        for product, publisher, source, peer, target, via in links:
            if via == "seeAlso":
                self.data[publisher].append(nt(f"<{source}>", f"<{SEEALSO}>", f"<{target}>"))
            else:
                # The brand's own triple about another publisher's listing: in its graph (for its
                # service) and placed in the product's document (fragment.py leaves it out of the listing's)
                line = nt(f"<{target}>", f"<{LISTING_OF}>", f"<{source}>")
                self.data[publisher].append(line)
                self.placements.append(f"{line[:-3]} <{doc(source)}> .\n")
        self.links = links

    def build_placements(self):
        """Triples also placed in another document of their publisher: copies for traversal."""
        def place(line, document):
            self.placements.append(f"{line[:-3]} <{document}> .\n")

        backlinks = (f"<{BSBM}product>", f"<{BSBM}reviewFor>")
        for member in (m for m in self.members if m not in self.product_pages):
            for line in self.data[member]:
                s, p, o = line[:-3].split(" ", 2)
                if p in backlinks:
                    place(line, doc(o[1:-1]))
        taxonomy = self.dconf.ns("taxonomy")
        for brand in range(self.catalog.count):
            name = self.brand_name(brand)
            for line in self.data[name]:
                s, p, o = line[:-3].split(" ", 2)
                if p == f"<{BSBM}producer>":
                    place(line, doc(o[1:-1]))
                elif p == f"<{RDF_TYPE}>" and o.startswith(f"<{taxonomy}ProductType"):
                    self.categories[o[1:-1]].add(name)
        for type_iri, brands in sorted(self.categories.items()):
            for name in sorted(brands, key=number):
                self.data["taxonomy"].append(nt(f"<{type_iri}>", f"<{SEEALSO}>", f"<{doc(self.producer_iri(number(name)))}>"))

    # ----- publishers.json -----
    def publishers(self):
        """Every publisher: kind, known or not, service, layout (shops, review sites and brands), size
        (products, offers or reviews), storage, graph file, triples, port and endpoint.

        Known shops and review sites run a service, the taxonomy and reviewers don't, and the others are
        chosen by `services` (choose_services). Every shop, review site and brand has its own port, also
        without a service, so an endpoint's URL doesn't depend on which other publishers run one."""
        dconf = self.dconf
        # The taxonomy is a vocabulary published as documents only: no query service, and not known up front
        publishers = {"taxonomy": {"kind": "taxonomy", "known": False, "service": False}}
        for m in self.members:
            publishers[m] = {"kind": kind_of(m), "known": m in self.known, "service": None, "layout": self.layouts[m],
                             SIZE_FIELDS[kind_of(m)]: self.entities[m]}
        for brand in range(self.catalog.count):
            publishers[self.brand_name(brand)] = {"kind": "producer", "known": False, "service": None,
                                                  "products": self.catalog.sizes[brand], "layout": self.brands_layout}
        for r in self.reviewers:
            publishers[r] = {"kind": "reviewer", "known": False, "service": False}
        sizes = defaultdict(dict)
        for name, info in publishers.items():
            if info["kind"] in SERVICE_KINDS and not info["known"]:
                sizes[info["kind"]][name] = info[SIZE_FIELDS[info["kind"]]]
        services = choose_services(dconf.services_conf, sizes)
        port = int(dconf.virtuoso["first_service_port"])
        for name, info in publishers.items():
            if info["kind"] in SERVICE_KINDS:
                info["service"] = info["known"] or name in services
            info["storage"] = dconf.ns(name)
            info["file"] = graph_file(name, info)
            info["triples"] = len(self.data[name])
            if info["service"]:
                info["port"] = port
                info["endpoint"] = f"http://{dconf.public_host}:{port}/{name}/sparql"
            if info["kind"] in SERVICE_KINDS:
                port += 1
        return publishers

    def build(self):
        self.build_reviewers()
        self.build_members()
        self.build_reviewer_links()
        self.build_brands()
        self.build_taxonomy()
        self.build_links()
        self.build_placements()
        self.build_storages()
        return self.publishers()


def write(dconf, pub, publishers):
    for old in dconf.dir("data").rglob("*.nt"):
        old.unlink()
    for name, info in publishers.items():
        Path(dconf.data_file(name, info)).write_text("".join(sorted(set(pub.data[name]), key=graph_order)))
    Path(dconf.path("publishers.json")).write_text(json.dumps(publishers, indent=2))
    Path(dconf.path("placements.nq")).write_text("".join(sorted(set(pub.placements))))
    Path(dconf.path("categories.json")).write_text(json.dumps({t: sorted(b, key=number) for t, b in sorted(pub.categories.items())}, indent=1))
    Path(dconf.path("markets.json")).write_text(json.dumps(
        pub.markets.describe(publishers, pub.reviewer_site, lambda t: f"{dconf.ns('taxonomy')}ProductType{t}#it"), indent=1))
    with open(dconf.path("links.tsv"), "w", newline="") as fs:
        writer = csv.writer(fs, delimiter="\t")
        writer.writerow(["product", "publisher", "from", "peer", "to", "via"])
        writer.writerows(pub.links)


def read_links(dconf):
    """links.tsv's rows: (product, publisher, from, peer, to, via), via rdfs:seeAlso ("seeAlso", followed
    for every query) or the brand's fs:listingOf ("listingOf", followed when the query asks for listings)."""
    with open(dconf.path("links.tsv"), newline="") as fs:
        rows = [tuple(r) for r in list(csv.reader(fs, delimiter="\t"))[1:]]
    return [r if len(r) == 6 else (*r, "seeAlso") for r in rows]


def run(dconf):
    pub = Publishing(dconf)
    publishers = pub.build()
    write(dconf, pub, publishers)
    layout.run(dconf, publishers)
    kinds = defaultdict(lambda: defaultdict(int))
    for info in publishers.values():
        k = kinds[info["kind"]]
        k["publishers"] += 1
        k["known"] += info["known"]
        k["services"] += info["service"]
        if info.get("layout"):
            k[info["layout"]] += 1
        k["triples"] += info["triples"]
    summary = {"publishers": {k: dict(v) for k, v in kinds.items()},
               "listings": sum(len(h) for h in pub.listings.values()), "listed_products": len(pub.listings),
               "links": Counter(l[5] for l in pub.links), "placed_triples": len(pub.placements),
               "type_brand_links": sum(len(b) for b in pub.categories.values()),
               "markets": pub.markets.names}
    LOGGER.info(json.dumps(summary))
    return summary
