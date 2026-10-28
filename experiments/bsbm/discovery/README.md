# Discovery-based FedShop

A marketplace built from FedShop's data and published as Linked Data: shops, review sites, brands, a taxonomy and reviewers each publish their data as documents, and many of them also run a SPARQL endpoint. A query engine starts from a catalog of known query services and has to discover the rest through references in the data.

## Publishers

| Publisher | Publishes | Query service | In the catalog | Full config | Small config |
|---|---|---|---|---|---|
| Taxonomy | product types, product features, countries, links to the brands' category pages | yes | yes | 1 (434,538 triples) | 1 |
| Brand (producer) | its products and their specification, its own record | the largest brands (`brands.service_share`) | never | 400 (100 with a service) | 40 (10) |
| Shop (vendor) | its record, its offers, one listing per product it sells | yes | half (`classical.ratio`) | 20, ≈14,600 triples each | 20, ≈155 each |
| Review site | its record, its reviews, one page per product it reviews | yes | half | 20, ≈76,600 triples each | 20, ≈730 each |
| Reviewer | their profile | no | never | 9,277 | 87 |

`config.yaml` is the benchmark: 20 shops and 20 review sites (FedShop's batch 1) at FedShop's full per-member rates. `config_small.yaml` uses FedShop's small rates for development.

Every publisher has a storage `<base_url><publisher>/` (`http://localhost:3000/vendor13/`) with one document per subject it describes, and publishers with a service also run a SPARQL endpoint over the same data.

An entity's IRI is the URL of its document plus `#it`: the offer `vendor13/Offer7#it` is described in the document `vendor13/Offer7`. What the server says about a document (its type, size, modification date) is then not about the entity. Below, entities are named by their document.

## Data model

- **Generation.** Everything comes from FedShop's own WatDiv templates, run by the benchmark into its own folder; FedShop's datasets are not used or changed.
  - **Brand catalogs:** FedShop's product template for `brands.products` products (200,000 full, 20,000 small) and `brands.count` producers. Numbers of types and features follow from the catalog size by FedShop's formulas.
  - **Shops and review sites:** FedShop's vendor and rating-site templates, one run per member, against that catalog. Each member deals in the catalog's first `members.pool` products (20,000 full, 200 small): about 1 offer and 5 reviews per product, so not every member sells everything. Offers and reviews per member are drawn like FedShop's `normal_dist` (average 2,000 and 10,000 in the full config, as FedShop's full config), but seeded.
- **Brands.** A brand's product `producer22/Product77` carries the full BSBM specification: label, comment, types, features, numeric and textual properties, producer. Brand sizes follow rank^-`zipf` (0.8) with at least `min_products`: in the full config 16,676 products for the largest brand and 136 for the smallest, and the 100 brands with a service hold 67% of all products.
- **Listings.** A shop or review site keeps FedShop's local product IRI (`vendor13/Product77`) as its listing (or review page). The listing no longer repeats the product; it says `fs:listingOf <producer22/Product77>`. Offers and reviews point to the listing, as in FedShop.
- **Reviewers.** Reviewer profiles move out of the review sites into `reviewer<k>/profile`. Reviews point to them with `rev:reviewer`, and a profile lists the reviews its reviewer wrote, at any review site, with `foaf:made`.
- **Taxonomy.** FedShop's types and features, without copies, plus every country as `taxonomy/countries#US`. Each type links with `rdfs:seeAlso` to the category pages of the brands that make it.
- **Links.** `rdfs:seeAlso` between listings of the same product: a shop's listing to `reviews` (2) review pages, a review page to `where_to_buy` (3) listings, a review page to `also_reviewed` (0) other pages. A brand's product links to `brand_where_to_buy` (2) listings and to every review page of the product (`brand_reviews: all`, "reviewed at"). Peers are drawn per listing, seeded. Shops never link to shops, and review sites don't link to each other.
- **Documents for traversal.** Some documents repeat triples their publisher already states in another document, so that a traversal engine following the query's patterns (cMatch: a triple matching a pattern leads to the IRIs in it) gets from one document to the next. The data behind the services doesn't change.
  - **Listings and review pages** repeat `<offer> bsbm:product <listing>` of each of their offers and `<review> bsbm:reviewFor <page>` of each of their reviews, so a reached listing leads to its offers and a review page to its reviews.
  - **Category pages.** A brand has a page per product type it makes (`producer22/types/ProductType551`) that repeats `<product> rdf:type <type>` of its products of that type. With the taxonomy's links, a type in a query leads to every product of that type.
  - **Brand records.** A brand's own record (`producer22/Producer22`) repeats `<product> bsbm:producer <record>` of each of its products, as a brand's catalog page lists its range.

`fs:` is `http://fedshop.example.org/vocab#`; everything else is BSBM's vocabulary.

## Reference answers

The reference answer of every instance (`results.csv`) is its answer over all published data, computed on the analysis endpoint with every publisher's graph: a pattern outside `SERVICE` matches any publisher, and a pattern inside a `SERVICE` clause gets what that service holds.

## Serving

| What | Software | Where |
|---|---|---|
| Documents and storage roots | Community Solid Server 7.2.0 (`tools/css`, `css-config.json`), behind the proxy | `http://localhost:3000/` |
| Query services of the catalog (known up front) | Virtuoso, container `fedshop-discovery-virtuoso-catalog` (`docker/virtuoso.yml`), behind the proxy | `http://localhost:343xx/<publisher>/sparql` |
| Query services of the other publishers (discovered) | Virtuoso, container `fedshop-discovery-virtuoso`, behind the proxy | `http://localhost:343xx/<publisher>/sparql` |
| Request counts, CPU and memory per component | the counting proxy (`tools/proxy/proxy.js`) | control API on `http://localhost:3999/` |
| Every publisher's graph, for the analysis only | container `fedshop-discovery-virtuoso`, not proxied | `http://localhost:8891/sparql` |

- **Documents.** rdf-dataset-fragmenter 2.10.0 (`tools/fragmenter`) writes them with its subject strategy: each triple goes to the document of its subject. Triples that go in another document than their subject's (`placements.nq`: the repeated triples for traversal) are placed in one more fragmenter run, with a strategy that sends each quad to the document named by its graph (`tools/fragmenter/place.js`).
- **Storage roots.** Each publisher's directory is a container. Its `.meta` types it as `pim:Storage` (CSS advertises that with `Link: <…pim/space#Storage>; rel="type"`) and names its service with `void:sparqlEndpoint`. CSS lists the documents with `ldp:contains`.
- **The server root** (`/`) is not readable, so it can't be used as an index of all storages.
- **Query services.** Each service is a Virtuoso virtual endpoint whose default graph is its publisher's graph, built the way FedShop builds its member endpoints. Its data equals the union of its publisher's documents.
- **Containers.** Two Virtuoso containers, so CPU and memory can be told apart: `fedshop-discovery-virtuoso-catalog` holds the catalog's graphs and serves the catalog's services; `fedshop-discovery-virtuoso` holds every publisher's graph (the analysis endpoint) and serves the other services. They run next to FedShop's own containers, on their own ports (isql 1112 and 1113, HTTP 8891 and 8892, services on 44300 onwards behind the proxy), and never touch them. Their connection limits are raised (64 HTTP threads, 64 idle keep-alive connections): at Virtuoso's defaults (20 threads, 10 idle keep-alive connections) reused connections are dropped without notice, and engines fail with errors like "fetch failed".

### Counting requests

The proxy holds the public ports: 3000 for documents and every service's port from 34300. The Solid server and Virtuoso listen on the public port + `proxy.port_offset` (13000, 44300 onwards), so every engine reaches them only through the proxy and is counted the same way, whatever it is written in. Requests to Virtuoso go over a fresh connection each and at most `proxy.max_inflight` at once; the rest wait in the proxy.

Every request is counted by source and by kind:

| | Values |
|---|---|
| Source | `known`: a publisher in the known set (the catalog by default, or the publishers a query's `SERVICE` clauses name); `discovered`: any other publisher; `other`: no publisher (the server root) |
| Kind | `document`, `storage-root` (`/<publisher>/`), `container` (another path ending in `/`), `server-root` (`/`), `sparql` (a query service) |

`GET /stats` gives the counts since the last reset: `total`, `known_sources`, `discovered_sources` and `other`, each with requests, bytes and errors per kind, plus counts per publisher. So requests to known sources against discovered ones are `known_sources.requests` and `discovered_sources.requests`, and the discovery work itself (documents and storage roots) against querying (`sparql`) is the split by kind.

```bash
curl -X POST localhost:3999/reset -d '{"label": "q07-2", "known": ["vendor13", "taxonomy"]}'   # before an instance
curl localhost:3999/stats                                                                     # after it
```

`serve --request-log requests.jsonl` also writes every request (label, URL, publisher, kind, source, status, bytes, time) as one JSON line.

### CPU and memory per component

`GET /stats` also has `resources`: CPU and memory since the last reset for each component, so they can be attributed to the instance that ran between `POST /reset` and `GET /stats`.

| Component | What is measured |
|---|---|
| `css` | the Solid server's process: CPU of all its threads, resident memory |
| `catalog_endpoints` | the catalog container's cgroup: CPU, memory (`memory.current` − `inactive_file`, as `docker stats` reports it) |
| `discovered_endpoints` | the main container's cgroup, the same way |
| `proxy` | the proxy itself, including its own sampling (about 2% of a core at 50 ms) |

For each: `cpu_seconds` and `cpu_percent.avg` are exact, from the CPU counters read at the reset and at `/stats`, so even an instance of a few milliseconds is measured (tested: 20 ms of work measured as 0.0206 s). `cpu_percent.min`/`max` and `memory_bytes.min`/`max`/`avg` come from samples every `proxy.sample_ms` (50 ms), plus one at the reset and one at `/stats`; `samples` says how many. A peak shorter than the interval can be missed by min/max, never by the totals. 100% CPU is one core. `GET /stats?samples=1` adds every sample (`series`).

## How an engine finds things

An engine is given a query. Its `SERVICE` clauses name the endpoints the user knows (catalog members: the taxonomy and half of the shops and review sites). The engine gets no list of endpoints and discovers the rest from the data:
- **Any IRI leads to its storage.** Dereference it at CSS, walk up to the storage root, and read the service from the root. This covers a listing's `fs:listingOf` to a brand, a review's `rev:reviewer` to a reviewer, and a constant in the query.
- **A product leads to other members.** Through the `rdfs:seeAlso` links of reached listings, and of the brand itself, it leads to the other shops' and review sites' listings of that product.
- **A listing leads to its offers, a review page to its reviews.** Through the repeated `bsbm:product` and `bsbm:reviewFor` triples, which match the queries' patterns.
- **A reviewer leads to their reviews.** Their profile's `foaf:made` triples, for queries that ask for a reviewer's reviews.
- **A brand leads to its products.** Its record's `bsbm:producer` triples match patterns like `?product bsbm:producer ?producer`.
- **A type leads to its products.** The taxonomy's type links to the brands' category pages, whose `rdf:type` triples match patterns like `?product rdf:type <type>`.

So an engine can answer a query either from documents alone, following links (as many documents as the answer touches: tens for one product, thousands for a type), or by finding the services and sending them sub-queries.

## Workload

Templates in `queries/`, with `{{Name}}` placeholders. q01–q12 follow FedShop's templates; q13–q16 are new.

**Known and open parts.** The part of a query whose sources the user knows sits in `SERVICE` clauses on catalog members' endpoints (`{{Shop}}`, `{{Site}}`, `{{Taxonomy}}`), as in FedShop's reference source assignment (RSA) queries. The rest names no source: the engine has to find it, through documents and links or through the services of the publishers it discovers. Federation over the known part is therefore a few targeted requests, and the work that varies is discovery and delegation to discovered services.

| Query | Kind | Known part (`SERVICE`) | Open part |
|---|---|---|---|
| q01 | analytical | – | products of a type above a numeric bound, offered somewhere |
| q02 | lookup | feature labels (taxonomy) | one product's details, at its brand |
| q03 | analytical | – | products of a type within two numeric bounds, lacking a feature, offered somewhere |
| q04 | analytical | – | products of either of two types, each with its own bound, offered somewhere |
| q05 | analytical | – | products of the same type with close numeric values |
| q06 | analytical | my shop's products and prices | those whose label contains a word |
| q07 | product | my shop's offers for a product | its label, its reviews at every site, with reviewer names |
| q08 | product | – | a product's English reviews at every site, with reviewers |
| q09 | lookup | a review at my review site | its reviewer's name and country |
| q10 | product | my shop's price for a product | cheaper offers elsewhere, delivered within some days |
| q11 | control | everything my shop says about an offer | – |
| q12 | lookup | an offer at my shop, its vendor | the product's label, at its brand |
| q13 | analytical | – | the other products of a product's brand, with their offers |
| q14 | analytical | – | well-rated reviews of other products of the same type |
| q15 | product | my shop's offer for a product, its vendor's country | reviews of the product by reviewers from that country |
| q16 | analytical | my shop's products and prices | the well-rated reviews of those of a type |

Each template's header gives its kind, the range of answer sizes (`# rows:`) and how many publishers outside its `SERVICE` clauses an answer must involve (`# discovered:`). Values are chosen per template from the data, seeded, aiming at that range: thresholds come from the values of the products they filter, rating bounds from the rating distribution, and the multi-member templates use products with enough reviews or offers. Every candidate is run once with each pattern in its own `GRAPH ?g`, which tells which publisher supplied each pattern. A candidate is kept when:
- **Its answer size is within range.**
- **It needs discovery** (except q11, the control): part of the answer needs a publisher the query doesn't name, is reachable, and involves at least `discovered` such publishers.

Delegation gains are reported, not selected on: for every service sub-query in the answer, the matches of its patterns one by one divided by the solutions of the sub-query as a whole.

The full config gives 80 instances (5 per template): 40 hybrid-join, 35 discovery-only and 5 known-only (q11).

| Template | Rows | Publishers beyond the named ones | Median best gain (where) |
|---|---|---|---|
| q01 / q03 / q04 / q05 | 20–81 / 56–82 / 34–54 / 24–144 | 29–112 | 942× / 670× / 2,350× / 1,545× (brands) |
| q06 | 10–18 | 14–29 | – |
| q07 / q08 | 10–26 / 10–14 | 18–24 | 21,766× / 32,668× (review sites) |
| q10 | 2–5 | 2–4 | 4,506× (shops) |
| q13 | 59–345 | 20–21 | 3× |
| q14 / q16 | 24–88 / 10–35 | 14–54 | 133× / 31× (review sites) |
| q15 | 6–18 | 11–30 | 2,601× (review sites) |
| q02, q09, q12 (lookups) | 1–72 | 1–5 | – |
| q11 (control) | 9 | 0 | – |

## Pipeline

Run every command from the FedShop root. Each step is one self-contained command that names its config; it reads the outputs of the steps before it and the parameters listed below. `uv run fedshop-discovery all <config>` runs steps 1–7 in one go.

**Once:** build FedShop's WatDiv generator and install the two Node tools.

```bash
scripts/build-watdiv.sh
(cd experiments/bsbm/discovery/tools/css && npm ci)
(cd experiments/bsbm/discovery/tools/fragmenter && npm ci)
```

**Generation**, with the full config (for development, use `experiments/bsbm/snakefile/config_small.yaml` instead):

```bash
# 1. Brand catalogs: FedShop's product template, once (--force regenerates) -> catalog/
uv run fedshop-discovery catalog experiments/bsbm/snakefile/config.yaml

# 2. Shops and review sites: FedShop's vendor and rating-site templates, one run per member;
#    existing members are kept (--force regenerates) -> members/
uv run fedshop-discovery members experiments/bsbm/snakefile/config.yaml

# 3. Publishers: FedShop's data restructured into publishers, links, placements, categories
#    -> data/, publishers.json, links.tsv, placements.nq, categories.json
uv run fedshop-discovery own experiments/bsbm/snakefile/config.yaml

# 4. Query services: (re)create the Virtuoso container from data/ (needs Docker)
uv run fedshop-discovery load experiments/bsbm/snakefile/config.yaml

# 5. Documents: rdf-dataset-fragmenter, plus the storage roots' metadata -> web/
uv run fedshop-discovery fragment experiments/bsbm/snakefile/config.yaml

# 6. Workload: instantiate the templates, analyse and select the instances (starts Virtuoso)
#    -> workload/, workload-summary.csv, sources/
uv run fedshop-discovery build-workload experiments/bsbm/snakefile/config.yaml

# 7. Plain forms of every instance (starts Virtuoso) -> workload/*/instance_*/plain*.{sparql,csv}
uv run fedshop-discovery plain experiments/bsbm/snakefile/config.yaml
```

**Serving** the documents and services behind the counting proxy (keeps running; starts the Virtuoso container if needed):

```bash
uv run fedshop-discovery serve experiments/bsbm/snakefile/config.yaml --log-level warn
```

`fedshop-discovery check <config> query.sparql` analyses any query. FedShop's own data and Virtuoso containers are not used.

**Overrides.** `fedshop-discovery --overrides file.yaml <step> <config>` merges the YAML file (keys of the `discovery` section) over the config; `--overrides` can be repeated and is applied in order. Give the same overrides to every step.

### Parameters

Every parameter comes from the config's `discovery` section. These are the values that give the benchmark described here; a key left out of the config falls back to `DEFAULTS` in `fedshop/discovery/common.py`, which are the full values. Steps 6 and 7 also need the Virtuoso ports of step 4.

| Key | Full | Small | Read by | Meaning |
|---|---|---|---|---|
| `workdir` | `benchmark/discovery` | `benchmark/discovery-small` | all | outputs, under `experiments/bsbm/` |
| `base_url` | `http://localhost:3000/` | same | own, fragment, build-workload, plain, serve | storage root of each publisher: `<base_url><publisher>/` |
| `brands.products` | 200,000 | 20,000 | catalog, own | products in all brand catalogs together |
| `brands.count` | 400 | 40 | catalog, own | brands (FedShop's `producer_n`) |
| `brands.zipf` | 0.8 | same | own | brand size ∝ rank^-zipf |
| `brands.min_products` | 20 | same | own | products of the smallest brand |
| `brands.service_share` | 0.25 | same | own | share of brands, largest first, with a service |
| `brands.seed` | 42 | same | own | product → brand assignment |
| `members.shops` | 20 | 20 | members, own | shops (`vendor0`…) |
| `members.review_sites` | 20 | 20 | members, own | review sites (`ratingsite0`…) |
| `members.pool` | 20,000 | 200 | members, own | the catalog's first products, which members deal in |
| `members.offers` | 2,000 | 20 | members | average offers per shop (`normal_dist(3, 1, avg)`) |
| `members.reviews` | 10,000 | 100 | members | average reviews per review site; reviewers per site = reviews / 20 |
| `members.seed` | 42 | same | members | each member's offer or review count |
| `classical.ratio` | 0.5 | same | own | share of shops and of review sites in the catalog |
| `classical.members` | – (drawn) | same | own | an explicit list of catalog members instead of drawing them |
| `classical.seed` | 42 | same | own | which members are in the catalog |
| `links.reviews` | 2 | same | own | shop listing → review pages |
| `links.where_to_buy` | 3 | same | own | review page → shop listings |
| `links.also_reviewed` | 0 | same | own | review page → other review pages |
| `links.brand_where_to_buy` | 2 | same | own | brand product → shop listings |
| `links.brand_reviews` | `all` | same | own | brand product → review pages |
| `links.seed` | 42 | same | own | peers per listing |
| `virtuoso.compose_file` | `discovery/docker/virtuoso.yml` | same | load, and steps that start Virtuoso | |
| `virtuoso.container` | `fedshop-discovery-virtuoso` | same | load, serve | every graph (analysis) and the discovered services |
| `virtuoso.isql_port` / `http_port` | 1112 / 8891 | same | load, build-workload, plain, check | |
| `virtuoso.catalog_container` | `fedshop-discovery-virtuoso-catalog` | same | load, serve | the catalog's graphs and services |
| `virtuoso.catalog_isql_port` / `catalog_http_port` | 1113 / 8892 | same | load, check | |
| `virtuoso.external` | false | same | load, build-workload, plain, check | the containers are run by someone else (jbr): steps only wait for them |
| `virtuoso.host` / `catalog_host` | `localhost` / `localhost` | same | build-workload, plain, check, serve | where the containers are reached |
| `virtuoso.wait_timeout` | 600 | same | steps that start Virtuoso | seconds to wait for the containers |
| `virtuoso.first_service_port` | 34300 | same | own, load, serve | public port of the first query service; the rest follow |
| `css.port` / `css.config` | 3000 / `discovery/css-config.json` | same | serve | public port of the documents |
| `proxy.port_offset` | 10000 | same | load, serve | the Solid server and the services listen on their public port + this |
| `proxy.control_port` | 3999 | same | serve | `GET /stats`, `POST /reset` |
| `proxy.max_inflight` | 48 | same | serve | requests to Virtuoso at once; the rest wait in the proxy |
| `proxy.sample_ms` | 50 | same | serve | CPU and memory sampling interval, for min and max |
| `workload.templates_dir` | `discovery/queries` | same | build-workload | |
| `workload.per_template` | 5 | same | build-workload | instances kept per template |
| `workload.max_candidates` | 40 | same | build-workload | candidates tried per template |
| `workload.max_rows` | 100,000 | same | build-workload | candidates with more derivations than this are skipped |
| `workload.seed` | 42 | same | build-workload | value selection per template |

The WatDiv runs (steps 1–2) also read FedShop's `generation.schema.{product,vendor,ratingsite}` sections of the same config, for the templates and their word counts; the counts above override theirs, and FedShop's numbers of product types and features follow from `brands.products` by its formulas.

| | Full config | Small config |
|---|---|---|
| Time | ≈30 min (catalog and members 5, own 2, load 2, fragment 15, workload 3) | ≈1 min |
| Disk | ≈10.6 GB (catalog 2.5, members 2.1, data 1.5, placements 0.45, documents 4.0, plus Virtuoso) | ≈0.7 GB |
| Triples / documents / services | 8,758,874 / 759,900 (280,923 of them category pages) / 141 | not rebuilt yet |

The Virtuoso containers hold one config at a time: `load` switches, and `build-workload` and `check` refuse to run when they hold another config's data. Outputs go to `benchmark/discovery` (full) and `benchmark/discovery-small`.

## Running with jbr

`jbr-experiment-fedshop-discovery/` (in this repository, published separately) is a [jbr](https://github.com/rubensworks/jbr.js) experiment type for this benchmark: `jbr prepare` generates it and `jbr run` runs the workload against an engine hook, with correctness, request counts and CPU and memory per component for every query. It builds the generator as a Docker image from a commit of this repository and runs every step in containers on one Docker network:

- **Data and documents:** `catalog`, `members`, `own`, `fragment`, and `load-scripts`, which writes the loading of both Virtuoso containers as `initdb.d` scripts (`virtuoso/<main|catalog>/load.sql`) instead of loading them with `docker exec`.
- **Query services:** both Virtuoso containers load their graphs on first start, from those scripts.
- **Workload:** `build-workload` and `plain` with `virtuoso.external`, against the containers by name, and `serving-plan`, which writes `serving.json`: the Solid server's command and the proxy's map, so the experiment serves the benchmark without reading the config itself.

`base_url` names the proxy's container (`http://fedshop-proxy:3000/`), so the IRIs, the storage roots' service links and the queries' `SERVICE` clauses all resolve to the proxy on that network. See the package's README for its parameters and outputs.

## Running an engine

The benchmark has no runner of its own; runners live in their own project. What an engine gets per instance (`workload/<query>/instance_<i>/`):

- **The query.** `injected.sparql`: its `SERVICE` clauses name the endpoints the user knows. Seeds for traversal are the query's own IRIs. No endpoint list is given.
- **Or its plain form.** `plain.sparql` is the same query without `SERVICE` clauses, for engines that select sources themselves: give them the catalog's endpoints (`sources/catalog-endpoints.tsv`) and the seeds. Without `SERVICE`, a pattern matches any publisher, so the plain form has its own answer, `plain-results.csv`; where `SERVICE` named "my shop" (q06, q07, q10, q15, q16) it is larger.
- **The reference answer.** `results.csv` (or `plain-results.csv`). Compare answers as sets: publishers with a service serve every triple twice, from their service and from their documents, so an engine that merges sources as a bag repeats rows. All templates are `SELECT DISTINCT`.
- **Request counts.** Before an instance, `POST /reset` on the proxy's control port with the instance as label and, for the `SERVICE` form, the publishers its `SERVICE` clauses name as `known`; after it, `GET /stats` (see Serving).

## Outputs (`experiments/bsbm/benchmark/discovery/`)

| File | Content |
|---|---|
| `catalog/` | the WatDiv run for the brand catalogs |
| `members/` | the WatDiv runs for the shops and review sites |
| `data/<publisher>.nt` | every publisher's triples, as loaded into Virtuoso |
| `publishers.json` | every publisher: kind, storage, in the catalog or not, service and endpoint, triples |
| `links.tsv` | every link: product, publisher, from, peer, to |
| `placements.nq` | triples placed in another document than their subject's, with that document as graph |
| `categories.json` | every product type and the brands with a category page for it |
| `web/` | the documents CSS serves |
| `sources/` | the catalog (`catalog-endpoints.tsv`, `catalog-fedx.ttl`); `discovered-storages.tsv` is for analysis only |
| `workload/<query>/instance_<i>/` | `injected.sparql`, `results.csv`, `reachable.csv`, `analysis.json`; `plain.sparql`, `plain-results.csv` |
| `proxy.json` | the proxy's ports and publishers, written by `serve` |
| `workload-summary.csv` | every candidate, kept or not, and why |

## Deviations from FedShop

**Data**
- **One description instead of copies.** FedShop gives every shop and review site a full copy of each product, producer, feature and type, linked to a global IRI with `owl:sameAs`. Here each is described once, products by their brand and types and features by the taxonomy; members keep only a listing with `fs:listingOf`. In the small config, for FedShop's 200 products and what they use, 70,069 copied triples became 8,752.
- **New members.** Brands (400) and a taxonomy, which FedShop doesn't have as members. Reviewers become members of their own.
- **Own generation.** The benchmark runs FedShop's templates itself, with seeded offer and review counts. Shops and review sites have FedShop's full per-member rates, but there are 20 of each (FedShop's batch 1) instead of FedShop's 100.
- **A smaller pool, a larger catalog.** Shops and review sites deal in 20,000 products instead of FedShop's 200,000, which keeps about 1 offer and 5 reviews per product with 20 members of each kind. Brands own 200,000 products, most of them sold nowhere.
- **New brands.** Products are given to 400 brands by a Zipf distribution, instead of FedShop's producer assignment.
- **Links and category pages are new.** FedShop has no links between members and no documents.
- **IRIs.** All IRIs move under `http://localhost:3000/<publisher>/`, and entities get hash IRIs (`<document>#it`), apart from their documents; countries move into the taxonomy.
- **Literals.** Dates gain their time of day (FedShop's `"2008-04-22"^^xsd:dateTime` is not a valid dateTime) and prices become `xsd:decimal` instead of `xsd:double`. That way documents and Virtuoso write every triple the same way.

**Federation**
- **Known sources in `SERVICE` clauses instead of a fixed federation.** Only part of the federation is known up front: the taxonomy and half of the shops and review sites. A query names the ones it uses in `SERVICE` clauses, as FedShop's RSA queries do; everything else is discovered.
- **Documents next to services.** Every member publishes documents (CSS). Brands without a service and reviewers have documents only.
- **One federation size per build.** The benchmark builds one federation in its own Virtuoso container, not FedShop's 10 growing batches and batch containers; a larger federation is another build with more members.
- **Requests counted by source.** Like FedShop's proxy, the benchmark's proxy counts every engine's requests, and it also tells known from discovered sources and documents from services.

**Queries**
- **Rewritten templates.** q01–q12 are rewritten: `?local owl:sameAs <global>` becomes `?listing fs:listingOf <product>`, specification patterns move to the brand's product, and the known part goes in `SERVICE` clauses.
- **Tied to what shops sell.** q01, q03 and q04 also require the product to be offered by some shop, and q06 to be offered by my shop.
- **Features.** In this data a product's features are independent of its types, so a type plus two or three features leaves at most one product. q01 and q04 select by type and numeric bounds instead, q03 keeps one feature (as the one to lack), and q05 asks for the same type instead of a shared feature. Its windows are ±120 and ±170 instead of ±20 and ±70.
- **q07** is a product page with my shop's offers and every site's reviews; **q10** is a price check against my shop's price instead of a country and date filter.
- **q09** also asks for the reviewer's name and country, so it crosses into the reviewer's storage.
- **Sets, not bags.** Every template is `SELECT DISTINCT` (sources overlap), and `LIMIT`/`OFFSET` are dropped: answers are compared as complete sets, which also gives throughput curves. Time to the first *k* results can be read off a run instead.
- **No `ORDER BY`.** Without `LIMIT` it only orders the output, and it would block streaming: an engine could emit nothing before it has everything.
- **New templates.** q13–q16 are new.

**Workload**
- **Own value selection.** Values come from this benchmark's own selection on the new data, not from FedShop's query generator.
- **New selection rules.** Instances are kept by answer size, need for discovery and the number of publishers involved, the same rules for every template. Delegation gains are reported only.

## TODO

- **Rebuild the numbers.** Cached specs (copies of a brand's spec, some stale, held by shops and review sites) have been removed, and q02 and q05 no longer pick products by them. The workload results and the data sizes in this README are from the build that still had them.

- **Oracle federation baseline.** Give a federation engine (FedX, and the existing federation approaches, most of which are in Comunica) every discovered publisher's endpoint up front: the federation cost if discovery were free. The publishers each answer needs are in `analysis.json` (`storages_in_answer`). Publishers without a service (small brands, reviewers) have no endpoint, so queries that need them can't be answered this way; give their documents as sources, or report those queries separately.
