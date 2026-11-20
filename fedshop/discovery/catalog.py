"""The marketplace's raw data, generated with FedShop's own WatDiv templates.

- Product catalog: FedShop's product template, run once for `brands.products` products and
  `brands.count` producers. Numbers of product types and features follow from the catalog size
  through FedShop's (BSBM's) formulas.
- Shops and review sites: FedShop's vendor and rating-site templates, one run per member, against
  that catalog. They deal in the catalog's first `members.pool` products, the way FedShop's members
  deal in its product pool, so brands sell far more products than any shop lists. Each member's
  number of offers or reviews is drawn like FedShop draws it (normal_dist(3, 1, avg)), but seeded.
  A review site has one reviewer per `members.reviews_per_reviewer` reviews.
  With markets (markets.py), a member deals in its market's pool of `members.pool / markets.count`
  products: WatDiv draws from the first that many, and publishing.py maps them 1:1 to the market's.

Products are then given to brands by size: brand k gets a share proportional to (k+1)^-zipf, with
at least `min_products`, so a few brands are big and the long tail stays moderate.

`dconf` is a common.DiscoveryConfig. What this module reads from it (keys of the config's
`discovery` section; values of the full benchmark, experiments/bsbm/snakefile/config.yaml):
- `dconf.configfile`: the FedShop config. Its `generation.schema.{product,vendor,ratingsite}`
  sections give the WatDiv templates and their word counts; the counts below override theirs.
- `dconf.brands_conf`: `discovery.brands`
    products       int    products in the catalog, over all brands           40000
    count          int    brands (producers)                                400
    zipf           float  brand size ~ rank^-zipf                           0.8
    min_products   int    products of the smallest brand                    20
    seed           any    seed of the product -> brand assignment           42
- `dconf.members_conf`: `discovery.members`
    offers         number average offers per shop (normal_dist avg)         2000
    reviews        number average reviews per review site                   10000
    reviews_per_reviewer  int  reviews per reviewer, on average             20
    seed           any    seed of each member's offer or review count       42
- `dconf.markets_conf`: `discovery.markets` (only `count`, for the pool size; see markets.py)
- `dconf.watdiv_conf`: `discovery.watdiv`, what makes WatDiv's runs reproducible (scripts/watdiv-seed.patch)
    seed           any    seed of every WatDiv run and of its template      42
                          parameters (each run's own: seed|run)
    today          date   end of the date ranges the templates leave open   2026-11-16
- `dconf.pool`: `discovery.members.pool`, products shops and review sites deal in     20000
- `dconf.member_names`: vendor0..vendor<shops-1>, ratingsite0..ratingsite<review_sites-1>,
  from `discovery.members.shops` and `.review_sites`                                   20, 20
- `dconf.members_dir`: where the members go, <outdir>/members
- `dconf.path(...)`: a path under <outdir> (`discovery.workdir`, default
  <generation.workdir>/benchmark/discovery), its parent directory created
"""
import hashlib
import math
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from omegaconf import OmegaConf

from ..utils import load_config
from .common import LOGGER, read_nquads
from .markets import pool_size


def watdiv_dir(dconf):
    return dconf.path("catalog", "watdiv")


def run_seed(dconf, run):
    """A WatDiv run's own seed, derived from `watdiv.seed` and the run's name: one seed for every run
    would give all members the same draws."""
    return int.from_bytes(hashlib.sha256(f"{dconf.watdiv_conf['seed']}|{run}".encode()).digest()[:4], "big")


def run_config(dconf, run):
    """The FedShop config for a WatDiv run. Its resolvers (omega_conf.py) draw template parameters, such as
    word counts and the number of features, from numpy's global generator, seeded here with the run's seed;
    the caller saves it resolved, so these draws happen now."""
    np.random.seed(run_seed(dconf, run))
    return load_config(dconf.configfile)


def watdiv_env(dconf, run):
    """The environment of a WatDiv run: its seed, and the end of open date ranges, which WatDiv would
    otherwise take from today's date."""
    return {**os.environ, "WATDIV_SEED": str(run_seed(dconf, run)), "WATDIV_TODAY": str(dconf.watdiv_conf["today"])}


def generate(dconf, force=False):
    """Run FedShop's product template for the product catalog (once; `force` regenerates).

    Reads dconf.configfile, dconf.brands_conf["products"] and ["count"]; writes <outdir>/catalog/.
    """
    out = Path(watdiv_dir(dconf))
    if force and out.exists():
        shutil.rmtree(out)                              # a smaller catalog must not keep the old products
    if out.exists() and any(out.glob("Product/*.nq")) and not force:
        have, want = sum(1 for _ in out.glob("Product/*.nq")), int(dconf.brands_conf["products"])
        if have != want:
            raise ValueError(f"The product catalog in {out} has {have} products, but brands.products is {want}: "
                             "regenerate it with --force (types and features follow from the catalog size)")
        LOGGER.info(f"Product catalog already generated in {out}")
        return
    conf = run_config(dconf, "product")
    params = conf.generation.schema.product.params
    params.product_n = int(dconf.brands_conf["products"])
    params.producer_n = int(dconf.brands_conf["count"])
    conf.generation.schema.product.export_output_dir = str(out)
    configfile = dconf.path("catalog", "config.yaml")
    OmegaConf.save(conf, configfile, resolve=True)      # feature_n and type_n follow from product_n here
    LOGGER.info(f"Generating {params.product_n} products for {params.producer_n} brands with WatDiv...")
    subprocess.run([sys.executable, "-m", "fedshop.generate", "generate", configfile, "product", str(out)], check=True,
                   env=watdiv_env(dconf, "product"))


def fedshop_count(rng, avg, mu=3, sigma=1):
    """FedShop's normal_dist(mu, sigma, avg), with a seeded generator."""
    value = rng.gauss(mu, sigma)
    while value < 0:
        value = rng.gauss(mu, sigma)
    return int((value / mu) * avg + 1)


def generate_members(dconf, force=False):
    """Run FedShop's vendor and rating-site templates against the product catalog, one run per member.

    Reads dconf.configfile, dconf.members_conf["offers"], ["reviews"] and ["seed"], dconf.pool and
    dconf.member_names; needs <outdir>/catalog/ (generate) and writes dconf.members_dir.
    An existing member is kept unless `force`.
    """
    out = dconf.dir("members")
    conf = dconf.members_conf
    catalog_products = str(Path(watdiv_dir(dconf)))
    for name in dconf.member_names:
        target = out / f"{name}.nq"
        if target.exists() and not force:
            continue
        section = "vendor" if name.startswith("vendor") else "ratingsite"
        rng = random.Random(f"{conf['seed']}|{name}")
        cfg = run_config(dconf, name)
        schema = cfg.generation.schema[section]
        schema.params.product_n = pool_size(dconf)        # its market's pool (all of it without markets)
        if section == "vendor":
            schema.params.offer_n = fedshop_count(rng, float(conf["offers"]))
        else:
            schema.params.review_n = fedshop_count(rng, float(conf["reviews"]))
            schema.params.person_n = max(1, schema.params.review_n // int(conf.get("reviews_per_reviewer", 20)))
        schema.export_output_dir = str(out)
        schema.export_dep_output_dir = catalog_products
        configfile = str(out / f"{name}.config.yaml")
        OmegaConf.save(cfg, configfile, resolve=True)
        number = int(name[len(section):])
        LOGGER.info(f"Generating {name} ({'offers' if section == 'vendor' else 'reviews'}: "
                    f"{schema.params.get('offer_n') or schema.params.get('review_n')})...")
        subprocess.run([sys.executable, "-m", "fedshop.generate", "generate", configfile, section, str(target),
                        "--id", str(number)], check=True, stdout=subprocess.DEVNULL, env=watdiv_env(dconf, name))


def brand_sizes(n_products, count, zipf, min_products):
    """Products per brand, largest first: proportional to rank^-zipf, at least min_products, summing to n."""
    weights = [(k + 1) ** -zipf for k in range(count)]
    total = sum(weights)
    sizes = [max(min_products, math.floor(n_products * w / total)) for w in weights]
    sizes[0] += n_products - sum(sizes)
    if sizes[0] < min_products:
        raise ValueError("brands.products is too small for brands.count and brands.min_products")
    return sizes


class Catalog:
    """Products (global number -> triples), their brands, producers, types and features.

    Reads every key of dconf.brands_conf and dconf.pool; needs <outdir>/catalog/ (generate).
    """

    def __init__(self, dconf):
        conf = dconf.brands_conf
        self.n = int(conf["products"])
        if self.n < dconf.pool:
            raise ValueError(f"brands.products ({self.n}) must be at least members.pool ({dconf.pool})")
        self.count = int(conf["count"])
        self.sizes = brand_sizes(self.n, self.count, float(conf["zipf"]), int(conf["min_products"]))
        order = list(range(self.n))
        random.Random(conf["seed"]).shuffle(order)
        self.brand_of = {}
        start = 0
        for brand, size in enumerate(self.sizes):
            for number in order[start:start + size]:
                self.brand_of[number] = brand
            start += size
        self.dir = Path(watdiv_dir(dconf))

    def product(self, number):
        return list(read_nquads(self.dir / "Product" / f"Product{number}.nq"))

    def producer(self, brand):
        """The producer record WatDiv wrote for a brand. WatDiv only writes the producers its own draws
        used (not deterministically), while brands are assigned here: a brand without one borrows the
        next brand's record (its properties; publishing.py writes them under the brand's own IRI)."""
        for k in range(self.count):
            path = self.dir / "Producer" / f"Producer{(brand + k) % self.count}.nq"
            if path.exists():
                if k:
                    LOGGER.warning(f"WatDiv wrote no Producer{brand}: it borrows Producer{(brand + k) % self.count}'s record")
                return list(read_nquads(path))
        raise FileNotFoundError(f"No producer records in {self.dir / 'Producer'}")

    def type_numbers(self):
        return sorted(int(p.stem[len("ProductType"):]) for p in (self.dir / "ProductType").glob("*.nq"))

    def taxonomy_files(self):
        for kind in ("ProductType", "ProductFeature"):
            yield from sorted((self.dir / kind).glob("*.nq"))
