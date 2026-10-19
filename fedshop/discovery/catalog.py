"""The marketplace's raw data, generated with FedShop's own WatDiv templates.

- Brand catalogs: FedShop's product template, run once for `brands.products` products and
  `brands.count` producers. Numbers of product types and features follow from the catalog size
  through FedShop's (BSBM's) formulas.
- Shops and review sites: FedShop's vendor and rating-site templates, one run per member, against
  that catalog. They deal in the catalog's first `members.pool` products, the way FedShop's members
  deal in its product pool, so brands sell far more products than any shop lists. Each member's
  number of offers or reviews is drawn like FedShop draws it (normal_dist(3, 1, avg)), but seeded.

Products are then given to brands by size: brand k gets a share proportional to (k+1)^-zipf, with
at least `min_products`, so a few brands are big and the long tail stays moderate.
"""
import math
import random
import subprocess
import sys
from pathlib import Path

from omegaconf import OmegaConf

from ..utils import load_config
from .common import LOGGER, read_nquads


def watdiv_dir(dconf):
    return dconf.path("catalog", "watdiv")


def generate(dconf, force=False):
    """Run FedShop's product template for the brand catalogs (once; `force` regenerates)."""
    out = Path(watdiv_dir(dconf))
    if out.exists() and any(out.glob("Product/*.nq")) and not force:
        LOGGER.info(f"Brand catalog already generated in {out}")
        return
    conf = load_config(dconf.configfile)
    params = conf.generation.schema.product.params
    params.product_n = int(dconf.brands_conf["products"])
    params.producer_n = int(dconf.brands_conf["count"])
    conf.generation.schema.product.export_output_dir = str(out)
    configfile = dconf.path("catalog", "config.yaml")
    OmegaConf.save(conf, configfile, resolve=True)      # feature_n and type_n follow from product_n here
    LOGGER.info(f"Generating {params.product_n} products for {params.producer_n} brands with WatDiv...")
    subprocess.run([sys.executable, "-m", "fedshop.generate", "generate", configfile, "product", str(out)], check=True)


def fedshop_count(rng, avg, mu=3, sigma=1):
    """FedShop's normal_dist(mu, sigma, avg), with a seeded generator."""
    value = rng.gauss(mu, sigma)
    while value < 0:
        value = rng.gauss(mu, sigma)
    return int((value / mu) * avg + 1)


def generate_members(dconf, force=False):
    """Run FedShop's vendor and rating-site templates against the brand catalog, one run per member."""
    out = Path(dconf.dataset_dir)
    out.mkdir(parents=True, exist_ok=True)
    conf = dconf.members_conf
    catalog_products = str(Path(watdiv_dir(dconf)))
    for name in dconf.member_names:
        target = out / f"{name}.nq"
        if target.exists() and not force:
            continue
        section = "vendor" if name.startswith("vendor") else "ratingsite"
        rng = random.Random(f"{conf['seed']}|{name}")
        cfg = load_config(dconf.configfile)
        schema = cfg.generation.schema[section]
        schema.params.product_n = dconf.pool
        if section == "vendor":
            schema.params.offer_n = fedshop_count(rng, float(conf["offers"]))
        else:
            schema.params.review_n = fedshop_count(rng, float(conf["reviews"]))
            schema.params.person_n = max(1, schema.params.review_n // 20)
        schema.export_output_dir = str(out)
        schema.export_dep_output_dir = catalog_products
        configfile = str(out / f"{name}.config.yaml")
        OmegaConf.save(cfg, configfile, resolve=True)
        number = int(name[len(section):])
        LOGGER.info(f"Generating {name} ({'offers' if section == 'vendor' else 'reviews'}: "
                    f"{schema.params.get('offer_n') or schema.params.get('review_n')})...")
        subprocess.run([sys.executable, "-m", "fedshop.generate", "generate", configfile, section, str(target),
                        "--id", str(number)], check=True, stdout=subprocess.DEVNULL)


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
    """Products (global number -> triples), their brands, producers, types and features."""

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
        n_services = max(1, round(float(conf["service_share"]) * self.count))
        self.services = set(range(n_services))          # the largest brands
        self.dir = Path(watdiv_dir(dconf))

    def product(self, number):
        return list(read_nquads(self.dir / "Product" / f"Product{number}.nq"))

    def producer(self, brand):
        return list(read_nquads(self.dir / "Producer" / f"Producer{brand}.nq"))

    def taxonomy_files(self):
        for kind in ("ProductType", "ProductFeature"):
            yield from sorted((self.dir / kind).glob("*.nq"))
