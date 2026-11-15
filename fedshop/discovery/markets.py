"""Markets: groups of countries whose shops, review sites, reviewers and brands deal with each other.

Without markets (`markets.count` 1, the default) every shop and review site deals in the same pool
of products, so a whole shop links to products of nearly every brand and a link traversal engine
that crawls a whole publisher reaches nearly everything. With markets:

- every brand has a home market: the `markets.global_brands` largest brands are global, the others
  are dealt out over the markets by size rank (round robin), so each market gets a similar mix of
  big and small brands;
- every market has its own pool of `members.pool / markets.count` products: products of its own
  brands, plus a share (`markets.global_share`) of the global brands' products, the same ones in
  every market. The global brands are the bridges between markets;
- shop i and review site i are in market i mod count, and so are the review site's reviewers.
  Shops and review sites known up front are chosen per market (publishing.choose_known), so every
  market has a known shop and a known review site;
- the records of shops, review sites, reviewers and local brands get a country of their market
  (`markets.countries`, one list per market); global brands keep WatDiv's;
- with `brands.type_spread`, every product type is available in one market, in two, or in all of
  them, and a product's types are redrawn from those available in its brand's market (global
  brands: the types available everywhere). A query over a type then touches as many markets as
  the type is available in.

Keys of the config's `discovery` section (dconf.markets_conf, dconf.brands_conf):
    markets.count          int     markets (1: none)                              1
    markets.countries      list    country codes per market                       one list per market
    markets.global_brands  int     largest brands sold in every market            0
    markets.global_share   float   share of a market's pool from global brands    0
    markets.seed           any     seed of countries and the pools' order         42
    brands.type_spread     dict    share of types per spread: {1: .6, 2: .3, all: .1}, or none
"""
import random
import re
import zlib
from collections import defaultdict

GLOBAL = -1          # the market of global brands and their products


def market_count(dconf):
    return max(1, int(dconf.markets_conf.get("count") or 1))


def pool_size(dconf):
    """Products a member's WatDiv run deals in: its market's pool."""
    return dconf.pool // market_count(dconf)


def member_market(name, count):
    """Shop i and review site i are in market i mod count."""
    return int(re.sub(r"\D", "", name)) % count


def spread_shares(conf):
    """brands.type_spread as [(spread, share)], spread an int or "all"; None without it."""
    spread = conf.get("type_spread")
    if not spread:
        return None
    shares = [(("all" if str(k) == "all" else int(k)), float(v)) for k, v in dict(spread).items()]
    if abs(sum(s for _, s in shares) - 1) > 1e-6:
        raise ValueError(f"brands.type_spread: the shares add up to {sum(s for _, s in shares)}, not 1")
    return shares


class Markets:
    """Every brand's, member's and product type's market, and every market's product pool.

    catalog: catalog.Catalog. members: the shop and review-site names. types: the product type numbers.
    """

    def __init__(self, dconf, catalog, members, types):
        conf = dconf.markets_conf
        self.count = market_count(dconf)
        self.enabled = self.count > 1
        self.seed = conf.get("seed", 42)
        countries = conf.get("countries")
        if self.enabled:
            if not countries or len(countries) != self.count:
                raise ValueError(f"markets.countries must give {self.count} lists of country codes, one per market")
            self.countries = [list(c) for c in countries]
        else:
            self.countries = [None]
        self.names = ["-".join(c) for c in self.countries] if self.enabled else ["all"]

        n_global = int(conf.get("global_brands") or 0) if self.enabled else 0
        self.brand_market = {b: (GLOBAL if b < n_global else (b - n_global) % self.count) if self.enabled else 0
                             for b in range(catalog.count)}
        self.member_market = {m: member_market(m, self.count) for m in members}

        # Each market's pool, in a seeded order: WatDiv's local product k is pool[market][k]
        size = pool_size(dconf)
        n_shared = round(float(conf.get("global_share") or 0) * size) if n_global else 0
        by_market = defaultdict(list)
        for n in range(catalog.n):
            by_market[self.brand_market[catalog.brand_of[n]]].append(n)
        if self.enabled:
            shared = by_market[GLOBAL][:n_shared]
            if len(shared) < n_shared:
                raise ValueError(f"The global brands have {len(shared)} products, not the {n_shared} each market shares")
            self.pool = {}
            for m in range(self.count):
                local = by_market[m][:size - n_shared]
                if len(local) < size - n_shared:
                    raise ValueError(f"Market {self.names[m]} has {len(local)} products of its own brands, "
                                     f"not the {size - n_shared} its pool needs: lower members.pool")
                pool = local + shared
                random.Random(f"{self.seed}|pool|{m}").shuffle(pool)
                self.pool[m] = pool
        else:
            self.pool = {0: list(range(size))}

        # Product types: their markets, and per market the types available there
        shares = spread_shares(dconf.brands_conf)
        self.type_markets = {}
        self.available = None
        if shares and self.enabled:
            order = sorted(types)
            random.Random(f"{self.seed}|types").shuffle(order)
            start = 0
            for k, (spread, share) in enumerate(shares):
                end = len(order) if k == len(shares) - 1 else start + round(share * len(order))
                for i, t in enumerate(order[start:end]):
                    if spread == "all" or spread >= self.count:
                        self.type_markets[t] = frozenset(range(self.count))
                    else:
                        home = i % self.count
                        step = 1 + (i // self.count) % (self.count - 1)   # vary the other markets
                        self.type_markets[t] = frozenset((home + j * step) % self.count for j in range(spread))
                start = end
            everywhere = sorted(t for t, ms in self.type_markets.items() if len(ms) == self.count)
            self.available = {m: sorted(t for t, ms in self.type_markets.items() if m in ms) for m in range(self.count)}
            self.available[GLOBAL] = everywhere
            if not everywhere:
                raise ValueError("brands.type_spread gives no type available in every market, for the global brands")
        else:
            self.type_markets = {t: frozenset(range(self.count)) for t in types}

    def product_market(self, catalog, n):
        return self.brand_market[catalog.brand_of[n]]

    def remap_type(self, catalog, n, t):
        """The type product n gets for WatDiv's type t: one available in its brand's market."""
        if self.available is None:
            return t
        avail = self.available[self.product_market(catalog, n)]
        return avail[zlib.crc32(str(t).encode()) % len(avail)]

    def country(self, market, name, original):
        """A country code for a record in a market (seeded by its publisher's name); global keeps its own."""
        if not self.enabled or market == GLOBAL:
            return original
        return random.Random(f"{self.seed}|country|{name}").choice(self.countries[market])

    def name(self, market):
        return "global" if market == GLOBAL else self.names[market]

    def describe(self, publishers, reviewer_site, type_iri):
        """markets.json: every publisher's market and every type's markets (type_iri: number -> IRI)."""
        out = {}
        for name, info in publishers.items():
            kind = info["kind"]
            if kind == "producer":
                out[name] = self.name(self.brand_market[int(name[len("producer"):])])
            elif kind in ("vendor", "ratingsite"):
                out[name] = self.name(self.member_market[name])
            elif kind == "reviewer":
                out[name] = self.name(self.member_market[reviewer_site[name]])
        return {"markets": self.names, "publishers": out,
                "types": {type_iri(t): sorted(self.names[m] for m in ms) for t, ms in sorted(self.type_markets.items())}}
