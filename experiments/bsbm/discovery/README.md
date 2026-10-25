# Discovery-based FedShop

A marketplace built from FedShop's data in which every triple has one owner: the member whose namespace its subject is in. A query engine starts from a catalog of known query services and has to discover the rest through references in the data, while some members cache data they don't own.

## Owners

| Owner | Owns | Query service | In the catalog | Full config | Small config |
|---|---|---|---|---|---|
| Taxonomy | product types, product features, countries, links to the brands' category pages | yes | yes | 1 (434,538 triples) | 1 |
| Brand (producer) | its products and their specification, its own record | the largest brands (`brands.service_share`) | never | 400 (100 with a service) | 40 (10) |
| Shop (vendor) | its record, its offers, one listing per product it sells | yes | half (`classical.ratio`) | 20, ≈14,600 triples each | 20, ≈155 each |
| Review site | its record, its reviews, one page per product it reviews | yes | half | 20, ≈76,600 triples each | 20, ≈730 each |
| Reviewer | their profile | no | never | 9,277 | 87 |

`config.yaml` is the benchmark: 20 shops and 20 review sites (FedShop's batch 1) at FedShop's full per-member rates. `config_small.yaml` uses FedShop's small rates for development.

Every owner's namespace is `<base_url><owner>/` (`http://localhost:3000/vendor13/`). It is a storage of documents, one per subject, and, for owners with a service, a SPARQL endpoint that answers for that owner only.

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
- **Documents for traversal.** Some documents repeat triples their owner already has elsewhere, so that a traversal engine following the query's patterns (cMatch: a triple matching a pattern leads to the IRIs in it) gets from one document to the next. The owners' data and services don't change.
  - **Listings and review pages** repeat `<offer> bsbm:product <listing>` of each of their offers and `<review> bsbm:reviewFor <page>` of each of their reviews, so a reached listing leads to its offers and a review page to its reviews.
  - **Category pages.** A brand has a page per product type it makes (`producer22/types/ProductType551`) that repeats `<product> rdf:type <type>` of its products of that type. With the taxonomy's links, a type in a query leads to every product of that type.
  - **Brand records.** A brand's own record (`producer22/Producer22`) repeats `<product> bsbm:producer <record>` of each of its products, as a brand's catalog page lists its range.

`fs:` is `http://fedshop.example.org/vocab#`; everything else is BSBM's vocabulary.

## Claims

Every owner with a query service claims its namespace: the triples whose subject is in it. A triple is admitted from the source that holds it if that source is its subject's owner, or if nobody claims the subject.

Every triple is held by its subject's owner only, so the claims hold in this environment: enforcing them lets an engine skip sources without changing the answer. The reference answer of every instance (`results.csv`) is the claim semantics, and it equals `union-results.csv`, which admits every triple, as an engine merging all sources does; `build-workload` warns if an instance's two answers differ. Patterns inside a `SERVICE` clause are not subject to claims: they get whatever that service holds.

## Serving

| What | Software | Where |
|---|---|---|
| Documents and storage roots | Community Solid Server 7.2.0 (`tools/css`, `css-config.json`) | `http://localhost:3000/` |
| Query services | Virtuoso, container `fedshop-discovery-virtuoso` (`docker/virtuoso.yml`) | `http://localhost:343xx/<owner>/sparql` |
| Every owner's graph, for the analysis | the same container | `http://localhost:8891/sparql` |

- **Documents.** rdf-dataset-fragmenter 2.10.0 (`tools/fragmenter`) writes them with its subject strategy: each triple goes to the document of its subject. Triples that go in another document than their subject's (`placements.nq`: the repeated triples for traversal) are placed in one more fragmenter run, with a strategy that sends each quad to the document named by its graph (`tools/fragmenter/place.js`).
- **Storage roots.** Each owner's directory is a container. Its `.meta` types it as `pim:Storage` (CSS advertises that with `Link: <…pim/space#Storage>; rel="type"`) and names its service with `void:sparqlEndpoint`. CSS lists the documents with `ldp:contains`.
- **The server root** (`/`) is not readable, so it can't be used as an index of all storages.
- **Query services.** Each service is a Virtuoso virtual endpoint whose default graph is its owner's graph, built the way FedShop builds its member endpoints. Its data equals the union of its owner's documents.
- **Container.** The discovery container runs next to FedShop's own containers, on its own ports (isql 1112, HTTP 8891, services from 34300), and never touches them.

## How an engine finds things

An engine is given a query. Its `SERVICE` clauses name the endpoints the user knows (catalog members: the taxonomy and half of the shops and review sites). The engine gets no list of endpoints and discovers the rest from the data:
- **Any IRI leads to its owner.** Dereference it at CSS, walk up to the storage root, and read the service from the root. This covers a listing's `fs:listingOf` to a brand, a review's `rev:reviewer` to a reviewer, and a constant in the query.
- **A product leads to other members.** Through the `rdfs:seeAlso` links of reached listings, and of the brand itself, it leads to the other shops' and review sites' listings of that product.
- **A listing leads to its offers, a review page to its reviews.** Through the repeated `bsbm:product` and `bsbm:reviewFor` triples, which match the queries' patterns.
- **A reviewer leads to their reviews.** Their profile's `foaf:made` triples, for queries that ask for a reviewer's reviews.
- **A brand leads to its products.** Its record's `bsbm:producer` triples match patterns like `?product bsbm:producer ?producer`.
- **A type leads to its products.** The taxonomy's type links to the brands' category pages, whose `rdf:type` triples match patterns like `?product rdf:type <type>`.

So an engine can answer a query either from documents alone, following links (as many documents as the answer touches: tens for one product, thousands for a type), or by finding the services and sending them sub-queries.

## Workload

Templates in `queries/`, with `{{Name}}` placeholders. q01–q12 follow FedShop's templates; q13–q16 are new.

**Known and open parts.** The part of a query whose sources the user knows sits in `SERVICE` clauses on catalog members' endpoints (`{{Shop}}`, `{{Site}}`, `{{Taxonomy}}`), as in FedShop's reference source assignment (RSA) queries. The rest names no source: the engine has to find it, through documents and links or through the services of the owners it discovers. Federation over the known part is therefore a few targeted requests, and the work that varies is discovery, delegation to discovered services, and claims.

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

Each template's header gives its kind, the range of answer sizes (`# rows:`) and how many owners outside its `SERVICE` clauses an answer must involve (`# discovered:`). Values are chosen per template from the data, seeded, aiming at that range: thresholds come from the values of the products they filter, rating bounds from the rating distribution, and the multi-member templates use products with enough reviews or offers. Every candidate is run once with each pattern in its own `GRAPH ?g`, under claims and under union semantics. A candidate is kept when:
- **Its answer size is within range** under claims.
- **It needs discovery** (except q11, the control): part of the answer needs an owner the query doesn't name, is reachable, and involves at least `discovered` such owners.

Delegation gains are reported, not selected on: for every service sub-query in the answer, the matches of its patterns one by one divided by the solutions of the sub-query as a whole.

The full config gives 80 instances (5 per template): 40 hybrid-join, 35 discovery-only and 5 known-only (q11).

| Template | Rows | Owners beyond the named ones | Median best gain (where) |
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

# 3. Owners: every triple to its owner, links, placements, categories
#    -> data/, owners.json, links.tsv, placements.nq, categories.json
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

**Serving** the documents (keeps running; starts the Virtuoso container if needed):

```bash
uv run fedshop-discovery serve experiments/bsbm/snakefile/config.yaml --log-level warn
```

`fedshop-discovery check <config> query.sparql` analyses any query. FedShop's own data and Virtuoso containers are not used.

### Parameters

Every parameter comes from the config's `discovery` section. These are the values that give the benchmark described here; a key left out of the config falls back to `DEFAULTS` in `fedshop/discovery/common.py`, which are the full values. Steps 6 and 7 also need the Virtuoso ports of step 4.

| Key | Full | Small | Read by | Meaning |
|---|---|---|---|---|
| `workdir` | `benchmark/discovery` | `benchmark/discovery-small` | all | outputs, under `experiments/bsbm/` |
| `base_url` | `http://localhost:3000/` | same | own, fragment, build-workload, plain, serve | namespace root: `<base_url><owner>/` |
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
| `virtuoso.container` | `fedshop-discovery-virtuoso` | same | load | |
| `virtuoso.isql_port` / `http_port` | 1112 / 8891 | same | load, build-workload, plain, check | |
| `virtuoso.first_service_port` | 34300 | same | own | port of the first query service; the rest follow |
| `css.port` / `css.config` | 3000 / `discovery/css-config.json` | same | serve | |
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

The Virtuoso container holds one config at a time: `load` switches, and `build-workload` and `check` refuse to run when the container holds another config's data. Outputs go to `benchmark/discovery` (full) and `benchmark/discovery-small`.

## Running an engine

```bash
B=experiments/bsbm   # from the FedShop root; use absolute paths elsewhere
node -r ./$B/discovery/virtuoso-fetch.js <engine>/bin/query.js \
  <seed IRIs> -f $B/benchmark/discovery/workload/q12/instance_0/injected.sparql --lenient -t text/csv
```

- **Sources.** The query's `SERVICE` clauses name the known endpoints; seeds are the query's own IRIs. No endpoint list is given. `sources/catalog-endpoints.tsv` lists the endpoints templates may name, for engines or experiments that want them.
- **Plain form.** `plain.sparql` is the same query without `SERVICE` clauses, for engines that select sources themselves: give them the catalog's endpoints and the seeds. Without `SERVICE`, a pattern matches any owner, so the plain form has its own answers (`plain-results.csv`, `plain-union-results.csv`); where `SERVICE` named "my shop" (q06, q07, q10, q15, q16) they are larger.

### Comunica experiments

```bash
experiments/bsbm/discovery/comunica/run-experiment.sh                      # plain form, catalog endpoints, no bind joins
experiments/bsbm/discovery/comunica/run-experiment.sh plain-default --comunica-config ""   # the same with Comunica's own config
experiments/bsbm/discovery/comunica/run-experiment.sh service-no-bind-join --form service  # SERVICE form, seeds only
```

The script starts Virtuoso and CSS if they aren't running, then runs `fedshop-discovery run-comunica` over every instance. Each instance runs in its own process group, killed after 180 s; the output is read as it streams, so every correct row gets its arrival time, and a killed run keeps what it returned. Results go to `benchmark/discovery/runs/<name>/`: `summary.md` (per template), `results.jsonl` (per instance: status, time, correct rows, claim violations, documents and service requests, first result, arrival times), `outputs/` and `logs/`. A run started again with the same name and settings resumes where it stopped. The engine defaults to `~/projects/comunica-feature-link-traversal-default/engines/query-sparql-link-traversal-solid` (`--engine` or `COMUNICA_ENGINE` to change).

`comunica/config-no-bind-join.json` is the engine's Solid default config without bind joins: it imports the same parts, except the join actors, which it defines itself without `ActorRdfJoinMultiBind`, `ActorRdfJoinMultiBindSource`, `ActorRdfJoinMultiSmallestFilterBindings` and `ActorRdfJoinOptionalBind`. Joins are then symmetric hash, nested loop and multi-smallest; each pattern goes to each source once instead of once per binding. It is loaded through the engine's `query-dynamic.js` (`COMUNICA_CONFIG`), so the engine itself is unchanged.

- **`virtuoso-fetch.js`:** Node.js engines need it against Virtuoso. It sends every request with `Connection: close` (Virtuoso keeps at most 10 idle connections and silently drops the rest, and Node's `fetch` then fails with "fetch failed"), keeps at most `VIRTUOSO_LIMIT` (16) requests in flight (all services share one server with 20 HTTP threads, and a flood gets connect timeouts), retries requests whose connection timed out or was reset, and writes request counts to stderr every 2 s. `no-keepalive.js` only does the first.
- **Compare answers as sets.** Classical owners serve every triple twice, from their service and from their documents, so an engine that merges sources as a bag repeats rows (q12#0: 512 rows, 1 distinct). All templates are `SELECT DISTINCT`.

## Outputs (`experiments/bsbm/benchmark/discovery/`)

| File | Content |
|---|---|
| `catalog/` | the WatDiv run for the brand catalogs |
| `members/` | the WatDiv runs for the shops and review sites |
| `data/<owner>.nt` | every owner's triples, as loaded into Virtuoso |
| `owners.json` | every owner: kind, namespace, in the catalog or not, service and endpoint, triples |
| `links.tsv` | every link: product, owner, from, peer, to |
| `placements.nq` | triples placed in another document than their subject's, with that document as graph |
| `categories.json` | every product type and the brands with a category page for it |
| `web/` | the documents CSS serves |
| `sources/` | the catalog (`catalog-endpoints.tsv`, `catalog-fedx.ttl`); `discovered-storages.tsv` is for analysis only |
| `workload/<query>/instance_<i>/` | `injected.sparql`, `results.csv` (claims), `union-results.csv`, `reachable.csv`, `analysis.json`; `plain.sparql`, `plain-results.csv`, `plain-union-results.csv` |
| `runs/<name>/` | engine runs: `summary.md`, `results.jsonl`, `outputs/`, `logs/` |
| `workload-summary.csv` | every candidate, kept or not, and why |

## Deviations from FedShop

**Data**
- **Owners instead of copies.** FedShop gives every shop and review site a full copy of each product, producer, feature and type, linked to a global IRI with `owl:sameAs`. Here each exists once, at its owner; members keep only a listing with `fs:listingOf`. In the small config, for FedShop's 200 products and what they use, 70,069 copied triples became 8,752 owned ones.
- **New members.** Brands (40) and a taxonomy, which FedShop doesn't have as members. Reviewers become members of their own.
- **Own generation.** The benchmark runs FedShop's templates itself, with seeded offer and review counts. Shops and review sites have FedShop's full per-member rates, but there are 20 of each (FedShop's batch 1) instead of FedShop's 100.
- **A smaller pool, a larger catalog.** Shops and review sites deal in 20,000 products instead of FedShop's 200,000, which keeps about 1 offer and 5 reviews per product with 20 members of each kind. Brands own 200,000 products, most of them sold nowhere.
- **New brands.** Products are given to 400 brands by a Zipf distribution, instead of FedShop's producer assignment.
- **Links and category pages are new.** FedShop has no links between members and no documents.
- **IRIs.** All IRIs move under `http://localhost:3000/<owner>/`, and entities get hash IRIs (`<document>#it`), apart from their documents; countries move into the taxonomy.
- **Literals.** Dates gain their time of day (FedShop's `"2008-04-22"^^xsd:dateTime` is not a valid dateTime) and prices become `xsd:decimal` instead of `xsd:double`. That way documents and Virtuoso write every triple the same way.

**Federation**
- **Known sources in `SERVICE` clauses instead of a fixed federation.** Only part of the federation is known up front: the taxonomy and half of the shops and review sites. A query names the ones it uses in `SERVICE` clauses, as FedShop's RSA queries do; everything else is discovered.
- **Documents next to services.** Every member publishes documents (CSS). Brands without a service and reviewers have documents only.
- **One federation size.** The benchmark has one federation in its own Virtuoso container, not FedShop's 10 growing batches, batch containers and proxy.

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
- **New selection rules.** Instances are kept by answer size, need for discovery and the number of owners involved, the same rules for every template. Delegation gains are reported only.
- **New reference.** The reference answer is the claim semantics. Every triple is held by its owner only, so it equals plain evaluation over the union of members.

## TODO

- **Rebuild the numbers.** Cached specs (copies of a brand's spec, some stale, held by shops and review sites) have been removed. The workload results and the data sizes in this README are from the build that still had them.

- **Oracle federation baseline.** Give a federation engine (FedX, and the existing federation approaches, most of which are in Comunica) every discovered owner's endpoint up front: the federation cost if discovery were free. The owners each answer needs are in `analysis.json` (`storages_in_answer`). Owners without a service (small brands, reviewers) have no endpoint, so queries that need them can't be answered this way; give their documents as sources, or report those queries separately.
