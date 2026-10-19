# Discovery-based FedShop

A marketplace built from FedShop's data in which every triple has one owner: the member whose namespace its subject is in. A query engine starts from a catalog of known query services and has to discover the rest through references in the data, while some members cache data they don't own.

## Owners

| Owner | Owns | Query service | In the catalog | Full config | Small config |
|---|---|---|---|---|---|
| Taxonomy | product types, product features, countries | yes | yes | 1 | 1 |
| Brand (producer) | its products and their specification, its own record | the largest brands (`brands.service_share`) | never | 400 (100 with a service) | 40 (10) |
| Shop (vendor) | its record, its offers, one listing per product it sells | yes | half (`classical.ratio`) | 20, ≈14,600 triples each | 20, ≈155 each |
| Review site | its record, its reviews, one page per product it reviews | yes | half | 20, ≈76,600 triples each | 20, ≈730 each |
| Reviewer | their profile | no | never | 9,277 | 87 |

`config.yaml` is the benchmark: 20 shops and 20 review sites (FedShop's batch 1) at FedShop's full per-member rates. `config_small.yaml` uses FedShop's small rates for development.

Every owner's namespace is `<base_url><owner>/` (`http://localhost:3000/vendor13/`). It is a storage of documents, one per subject, and, for owners with a service, a SPARQL endpoint that answers for that owner only.

## Data model

- **Generation.** Everything comes from FedShop's own WatDiv templates, run by the benchmark into its own folder; FedShop's datasets are not used or changed.
  - **Brand catalogs:** FedShop's product template for `brands.products` products (200,000 full, 20,000 small) and `brands.count` producers. Numbers of types and features follow from the catalog size by FedShop's formulas.
  - **Shops and review sites:** FedShop's vendor and rating-site templates, one run per member, against that catalog. Each member deals in the catalog's first `members.pool` products (20,000 full, 200 small): about 1 offer and 5 reviews per product, so not every member sells everything. Offers and reviews per member are drawn like FedShop's `normal_dist` (average 2,000 and 10,000 in the full config, as FedShop's full config), but seeded.
- **Brands.** A brand's product `producer22/Product77` carries the full BSBM specification: label, comment, types, features, numeric and textual properties, producer. Brand sizes follow rank^-`zipf` (0.8) with at least `min_products`: in the full config 16,676 products for the largest brand and 136 for the smallest, and the 100 brands with a service hold 67% of all products.
- **Listings.** A shop or review site keeps FedShop's local product IRI (`vendor13/Product77`) as its listing (or review page). The listing no longer repeats the product; it says `fs:listingOf <producer22/Product77>`. Offers and reviews point to the listing, as in FedShop.
- **Reviewers.** Reviewer profiles move out of the review sites into `reviewer<k>/me`. Reviews point to them with `rev:reviewer`.
- **Taxonomy.** FedShop's types and features, without copies, plus every country as `taxonomy/countries#US`.
- **Links.** `rdfs:seeAlso` between listings of the same product: a shop's listing to `reviews` (2) review pages, a review page to `where_to_buy` (3) listings, a review page to `also_reviewed` (0) other pages. A brand's product links to `brand_where_to_buy` (2) listings and `brand_reviews` (2) pages. Peers are drawn per listing, seeded. Shops never link to shops.
- **Cached specs.** A share of listings (`replicas.share`, 0.25) carries a copy of the brand's spec sheet (label and numeric properties) under the brand's product IRI. Some of these copies are stale (`replicas.stale`, 0.3): their numbers are off by 50–400.

`fs:` is `http://fedshop.example.org/vocab#`; everything else is BSBM's vocabulary.

## Claims

Every owner with a query service claims its namespace: the triples whose subject is in it. A triple is admitted from the source that holds it if that source is its subject's owner, or if nobody claims the subject. A shop's cached copy of a brand's spec is dropped when the brand runs a service, and admitted alongside the brand's own documents when the brand is lookup-only.

The reference answer of every instance (`results.csv`) is the claim semantics. `union-results.csv` admits every triple, which is what an engine gets by merging all sources.

## Serving

| What | Software | Where |
|---|---|---|
| Documents and storage roots | Community Solid Server 7.2.0 (`tools/css`, `css-config.json`) | `http://localhost:3000/` |
| Query services | Virtuoso, container `fedshop-discovery-virtuoso` (`docker/virtuoso.yml`) | `http://localhost:343xx/<owner>/sparql` |
| Every owner's graph, for the analysis | the same container | `http://localhost:8891/sparql` |

- **Documents.** rdf-dataset-fragmenter 2.10.0 (`tools/fragmenter`) writes them with its subject strategy: each triple goes to the document of its subject. Cached specs are placed in the listing that carries them with the fragmenter's constant strategy (`tools/fragmenter/cached-specs.js`).
- **Storage roots.** Each owner's directory is a container. Its `.meta` types it as `pim:Storage` (CSS advertises that with `Link: <…pim/space#Storage>; rel="type"`) and names its service with `void:sparqlEndpoint`. CSS lists the documents with `ldp:contains`.
- **The server root** (`/`) is not readable, so it can't be used as an index of all storages.
- **Query services.** Each service is a Virtuoso virtual endpoint whose default graph is its owner's graph, built the way FedShop builds its member endpoints. Its data equals the union of its owner's documents.
- **Container.** The discovery container runs next to FedShop's own containers, on its own ports (isql 1112, HTTP 8891, services from 34300), and never touches them.

## How an engine finds things

An engine is given the catalog (`sources/catalog-endpoints.tsv`). It discovers the rest from the data:
- **Any IRI leads to its owner.** Dereference it at CSS, walk up to the storage root, and read the service from the root. This covers a listing's `fs:listingOf` to a brand, a review's `rev:reviewer` to a reviewer, and a constant in the query.
- **A product leads to other members.** Through the `rdfs:seeAlso` links of reached listings, and of the brand itself, it leads to the other shops' and review sites' listings of that product.

## Workload

Templates in `queries/`, with `{{Name}}` placeholders. q01–q12 rewrite FedShop's templates for this model; q13–q15 are new.

| Query | Asks for |
|---|---|
| q01, q03, q04 | products of a type with given features and numeric bounds, among those some shop sells |
| q02 | the details of one product |
| q05 | products similar to a given one, among those some shop sells |
| q06 | products whose label contains a word, among those some shop sells |
| q07 | a product page: current offers from vendors in a country, reviews with reviewer names |
| q08 | the English reviews of a product with their reviewers |
| q09 | the reviewer of a review, with name and country |
| q10 | fast, current offers for a product from vendors in a country |
| q11 | everything about an offer |
| q12 | an offer with its product label and vendor |
| q13 | offers for other products of the same brand |
| q14 | reviews of other products sharing a feature |
| q15 | reviews by reviewers from the country of a vendor offering the product |

Values are chosen per template from the data, seeded, so every candidate has an answer. For example: an anchor product some shop sells, one of its types and features, a threshold just below its value. For q02 and q05, products with stale cached specs are tried first. Every candidate is run once with each pattern in its own `GRAPH ?g`, under claims and under union semantics. A candidate is kept when:
- **Its answer isn't empty** under claims.
- **It needs discovery:** part of the answer needs a discovered owner and is reachable from the catalog.
- **Delegation pays off** (q01, q03, q04, q05, q06 only): the sub-query a service supplies saves at least `gain_min` (10). The gain is the matches of its patterns one by one at that service, divided by the solutions of the sub-query as a whole.

The full config gives 75 instances (5 per template): 59 hybrid-join, 15 discovery-only and 1 hybrid-union.
- **Discovery:** 49 instances need a brand, 37 a lookup-only owner, and 20 a reviewer. 17 reach only part of their answer.
- **Delegation:** the median of each instance's best gain, by template:

  | Template | Service | Gain |
  |---|---|---|
  | q01, q03, q04, q06 | brand | 2,718–7,073× |
  | q08 | review site | 29,665× |
  | q15 | review site / shop | 13,141× / 2,122× |
  | q10 | shop | 5,130× |
  | q12 | shop | 1,371× |
- **Claims:** 32 instances lose derivations under claims, and in 6 of them claims change the answer.

## Pipeline

```bash
CONFIG=experiments/bsbm/snakefile/config.yaml      # or config_small.yaml
# Once: FedShop's WatDiv generator (generators/watdiv, as in FedShop's quickstart) and the two Node tools
(cd experiments/bsbm/discovery/tools/css && npm ci) && (cd experiments/bsbm/discovery/tools/fragmenter && npm ci)
# Brand catalogs, shops and review sites, owners, query services, documents, workload
uv run fedshop-discovery all $CONFIG
# Documents (keeps running; starts the Virtuoso container if needed)
uv run fedshop-discovery serve $CONFIG
```

`all` runs `catalog` (brand catalogs, then shops and review sites), `own`, `load`, `fragment` and `build-workload`, which can also be run one by one. `fedshop-discovery check $CONFIG query.sparql` analyses any query. FedShop's own data and Virtuoso containers are not used.

| | Full config | Small config |
|---|---|---|
| Time | ≈20 min (catalog and members 5, own 2, load 2, fragment 10, workload 1) | ≈1 min |
| Disk | ≈9 GB (catalog 2.5, members 2.1, data 1.5, documents 2.7, plus Virtuoso) | ≈0.7 GB |
| Triples / documents / services | 8,477,951 / 478,977 / 141 | 696,050 / 32,742 / 51 |

The Virtuoso container holds one config at a time: `load` switches, and `build-workload` and `check` refuse to run when the container holds another config's data. Outputs go to `benchmark/discovery` (full) and `benchmark/discovery-small`.

## Running an engine

```bash
B=experiments/bsbm   # from the FedShop root; use absolute paths elsewhere
node -r ./$B/discovery/no-keepalive.js <engine>/bin/query.js \
  $(cut -f3 $B/benchmark/discovery/sources/catalog-endpoints.tsv | sed 's/^/sparql@/') \
  <seed IRIs> -f $B/benchmark/discovery/workload/q12/instance_0/injected.sparql --lenient -t text/csv
```

- **`no-keepalive.js`:** Node.js engines need it against Virtuoso. Virtuoso keeps at most 10 idle connections and silently drops the rest, and Node's `fetch` then fails with "fetch failed".
- **Compare answers as sets.** Classical owners serve every triple twice, from their service and from their documents, so an engine that merges sources as a bag repeats rows (q12#0: 512 rows, 1 distinct). All templates are `SELECT DISTINCT`.

## Outputs (`experiments/bsbm/benchmark/discovery/`)

| File | Content |
|---|---|
| `catalog/` | the WatDiv run for the brand catalogs |
| `members/` | the WatDiv runs for the shops and review sites |
| `data/<owner>.nt`, `data/<owner>.replicas.nt` | every owner's triples and cached specs, as loaded into Virtuoso |
| `owners.json` | every owner: kind, namespace, in the catalog or not, service and endpoint, triples |
| `links.tsv` | every link: product, owner, from, peer, to |
| `replicas.json` | every cached spec: listing, product, stale or not |
| `web/` | the documents CSS serves |
| `sources/` | the catalog (`catalog-endpoints.tsv`, `catalog-fedx.ttl`); `discovered-storages.tsv` is for analysis only |
| `workload/<query>/instance_<i>/` | `injected.sparql`, `results.csv` (claims), `union-results.csv`, `reachable.csv`, `analysis.json` |
| `workload-summary.csv` | every candidate, kept or not, and why |

## Deviations from FedShop

**Data**
- **Owners instead of copies.** FedShop gives every shop and review site a full copy of each product, producer, feature and type, linked to a global IRI with `owl:sameAs`. Here each exists once, at its owner; members keep only a listing with `fs:listingOf`. In the small config, for FedShop's 200 products and what they use, 70,069 copied triples became 8,752 owned ones.
- **New members.** Brands (40) and a taxonomy, which FedShop doesn't have as members. Reviewers become members of their own.
- **Own generation.** The benchmark runs FedShop's templates itself, with seeded offer and review counts. Shops and review sites have FedShop's full per-member rates, but there are 20 of each (FedShop's batch 1) instead of FedShop's 100.
- **A smaller pool, a larger catalog.** Shops and review sites deal in 20,000 products instead of FedShop's 200,000, which keeps about 1 offer and 5 reviews per product with 20 members of each kind. Brands own 200,000 products, most of them sold nowhere.
- **New brands.** Products are given to 400 brands by a Zipf distribution, instead of FedShop's producer assignment.
- **Links and cached specs are new.** FedShop has no links between members and no data held by a non-owner.
- **IRIs.** All IRIs move under `http://localhost:3000/<owner>/`; countries move into the taxonomy.
- **Literals.** Dates gain their time of day (FedShop's `"2008-04-22"^^xsd:dateTime` is not a valid dateTime) and prices become `xsd:decimal` instead of `xsd:double`. That way documents and Virtuoso write every triple the same way.

**Federation**
- **A catalog instead of a fixed federation.** Only part of the federation is known up front: the taxonomy and half of the shops and review sites. Everything else is discovered.
- **Documents next to services.** Every member publishes documents (CSS). Brands without a service and reviewers have documents only.
- **One federation size.** The benchmark has one federation in its own Virtuoso container, not FedShop's 10 growing batches, batch containers and proxy.

**Queries**
- **Rewritten templates.** q01–q12 are rewritten: `?local owl:sameAs <global>` becomes `?listing fs:listingOf <product>`, and specification patterns move to the brand's product.
- **Tied to what shops sell.** q01 and q03–q06 also require the product to be offered by some shop, which makes them hybrid queries instead of pure catalog lookups.
- **q05's similarity windows** are widened (±120 and ±170 instead of ±20 and ±70), since candidates are limited to sold products.
- **q09** also asks for the reviewer's name and country, so it crosses into the reviewer's storage.
- **q07 and q10** take their country, and q10 its delivery-days bound, from an offer of the product instead of FedShop's fixed US and 3 days, so they have answers.
- **Sets, not bags.** Every template is `SELECT DISTINCT` (sources overlap), and `LIMIT`/`OFFSET` are dropped (a reachable share of a top-k answer is not well defined).
- **New templates.** q13–q15 are new.

**Workload**
- **Own value selection.** Values come from this benchmark's own selection on the new data, not from FedShop's query generator.
- **New selection rules.** Instances are kept by reachability and, for the brand-star templates, by delegation gain.
- **New reference.** The reference answer is the claim semantics, not plain evaluation over the union of members.
