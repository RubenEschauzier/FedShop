# Plan: make the benchmark solvable by link traversal

Status: **Steps 1–4 implemented and measured (2026-11-10), not committed.** Step 5: the generator image
`fedshop-discovery-generator:traversal-fixes` is built from the working tree and the jbr experiment
`experiment-fedshop-discovery-storage-roots` points at it; `jbr prepare` and the engine runs are still to do.
See **Results** at the end. Decisions taken: D1a, D2 as suggested, **D3: no global brands** (4 global
brands bridge every market, see Results), D4 60/30/10, D5 100.

## Why

The comparison is plain link traversal against composite sources. A composite source covers a whole
publisher (a review site, a brand, …) and is reached from any one of that publisher's documents. To
keep this fair, traversal must also be able to reach a whole publisher, so the publisher patterns,
`pim:storage` and `ldp:contains` **stay as they are**.

The problem: at bf98523, traversal reaches roughly 121k–126k of the ~162k documents and containers
for almost every query. Even q09 (one review's author: 3 documents needed) reaches 13k–24k. Traversal
manages about 30k requests in the 120 s timeout, so 31 of 41 queries timed out.

There are three causes, plus one source of noise:

| Cause | Example |
|---|---|
| `rdfs:seeAlso` is followed for every query | `producer52/products/2004-02#Product0 seeAlso ratingsite0/Product0#it` → `seeAlso vendor13/Product0#it` → … The brand document holds ~45 products, each with a trail like this, so traversal spreads to nearly every listing. |
| Everyone sells everything | Crawling `vendor13/` in full reaches products of 289 of the 400 brands, and so every other shop and review site. |
| Every product type spans the whole catalog | `taxonomy/ProductType322` → 220 brands, so the analytical queries (q01, q03, q04, q14, q16) cover the whole web. |
| Noise: 9,301 reviewer publishers | Each has one document and no service. A full crawl of a review site reaches all ~470 of its reviewers, and neither engine can shortcut that. |

## Expected effect (simulator: median documents reached)

These numbers come from `tools/reach/reach.py`, run on today's data with links cut or replaced,
**not** on regenerated data. Trust the relative effects more than the exact counts.

| Query | Today | + Fix A | + Fix B (5 markets) | + Fix C (type in 1 market) |
|---|---|---|---|---|
| q07 / q08 | 126k | 53k / 62k | 14k / 15k | same |
| q09 | 13k–24k | 4.5k | 4.5k | same |
| q10 | 49k | 2k | 77 | same |
| q12 / q13 | 102k / 121k | 105k / 121k | 7k / 14k | same |
| q15 | 126k | 95k | 22k | same |
| q01 / q03 / q04 | 121k | 121k | 52k | 13k / 14k / 14k |
| q05 | 78k–115k | 12k | 12k | 2.4k |
| q14 / q16 | 125k | 121k / 125k | 109k / 125k | 39k / 25k |

## Steps, in order

Each step can be built and checked on its own. Rerun only from the step named in "Rerun from".

### Step 1: fewer reviewers per review site

- **What:** each review site gets `review_n // reviews_per_reviewer` people instead of `review_n // 20`.
  With 100, that's ~100 reviewers per site and ~2,000 in total instead of 9,301.
- **Example:** reviewer8122 now writes 20 reviews at ratingsite17; with 100 per reviewer, a reviewer
  writes ~100 reviews.
- **Where:**
  - `fedshop/discovery/catalog.py` → `generate_members`: the line `person_n = max(1, schema.params.review_n // 20)`.
  - `common.py` `DEFAULTS["members"]`: new key `reviews_per_reviewer: 20`, so the default leaves the
    old behaviour unchanged.
- **Config:** `members.reviews_per_reviewer: 100` (decision D5).
- **Rerun from:** `members --force` (only the rating sites change, but `--force` regenerates every member).
- **Check:** `publishers.json` has about 2,000 reviewers; q15 still finds instances (it needs
  products with ≥ 25 reviews).

### Step 2 (Fix A): links that are only followed when the query asks for listings

- **What:**
  1. Stop writing shop ↔ review-site `seeAlso`.
  2. Replace the brand's `seeAlso` to listings with the triple `<listing> fs:listingOf <product>`,
     written into the brand's product document, for **every** listing of the product.
  3. Keep the taxonomy's type → brand `seeAlso`.
- **Example, before**, in `producer52/products/2004-02`:
  ```turtle
  <…2004-02#Product0> rdfs:seeAlso <vendor13/Product0#it> .
  ```
  **After:**
  ```turtle
  <vendor13/Product0#it> fs:listingOf <producer52/products/2004-02#Product0> .
  ```
  q07's pattern `?offerListing fs:listingOf <…#Product0>` matches this triple, and the engine
  follows the subject (`vendor13/Product0`), because the pattern has a variable there. That is
  the same mechanism the existing `bsbm:product` and `bsbm:reviewFor` copies rely on (checked in
  `ActorExtractLinksQuadPatternQuery.ts`, `onlyVariables: true`). Templates without an
  `fs:listingOf` pattern (q02, q05, q09, q11) no longer follow these links, and none of them
  needs listings.
- **Where:**
  - `publishing.py` → `build_links`: drop the `DIRECTIONS` loop, or set `links.reviews: 0` and
    `links.where_to_buy: 0`, which does the same through config. Remove `brand_where_to_buy` and
    `brand_reviews`.
  - Then add the brand triples in one of two ways (decision **D1**):
    - **D1a (recommended):** append them to `self.data[brand]` in `build_brands`/`spec`, so they
      are the brand's own data and also in its service. That matches today, where the `seeAlso`
      links are brand data too, so composite sources see the same links.
    - **D1b:** add them to `placements.nq` in `build_placements`, so they are in documents only.
      The brand's service then lacks them, and a composite-source engine that covers the brand
      through its service can no longer find the shops.
  - **Keep `links.tsv` consistent: the workload's reachability analysis depends on it.**
    `workload.py` passes it (`read_links`) to `provenance.py` `Analyzer`, which builds
    `adj[product][publisher] → peers` from it and follows those links in `reach()`/`follow()`.
    - Write one row per (product, brand → every listing), and no rows between members.
    - In `Analyzer.reach`, follow `adj` only when the query has an `fs:listingOf` pattern. That's
      what the engine does now: these links are matched by pattern, not followed for every query.
    - Update the docstrings of `publishing.py` and `provenance.py`, which describe the `seeAlso` links.
- **Rerun from:** `publish` (members unchanged).
- **Check:**
  1. Run `tools/reach/reach.py` with no flags: q10 is ~2k and q09 ~4.5k.
  2. In `analysis.json`, `reachable_share` stays 1.0 for q07, q08, q10 and q15, i.e. traversal can
     still reach the whole answer.
  3. If you want certainty, run the engine on one q07 instance and see that it fetches
     `vendorX/ProductN`.
- **D1a side effect:** with the triples in the brand's graph, the `GRAPH ?g` analysis can attribute
  `?l fs:listingOf ?p` to the brand as well, which may shift "needs discovery" selection a bit.
  Check `workload-summary.csv` against the previous build.

### Step 3 (Fix B): markets by country

- **What:** every shop, review site, reviewer and **brand** gets a home market. Members deal only in
  their market's products, except products of a few global brands, which are sold in every market
  and are the deliberate bridges between markets.
- **Example (5 markets):**
  ```
  US:     4 shops (2 known), 4 review sites (2 known), ~76 local brands, US reviewers
  West:   UK, FR, ES          …
  DACH+:  DE, AT, RU          …
  JKR:    JP, KR              …
  CN:     CN                  …
  global: e.g. the 4 largest brands (they run a service), sold everywhere
  ```
- **Where:**
  1. **Market of each brand:** `catalog.py` `Catalog.__init__`. The `markets.global_brands` largest
     brands are global; the rest are dealt out over the markets by rank (round-robin), so every
     market gets a similar Zipf mix of brand sizes. This only needs the config and
     `brand_sizes()`, so the members step can compute it too.
  2. **Each market's product pool:** `pool_m = members.pool / markets.count` products. They come
     from the market's local brands, with a share (`markets.global_share`, e.g. 5%) from the global
     brands' products. The same global products appear in every market.
  3. **Members deal in their market's pool:**
     - `catalog.py` `generate_members`: run WatDiv with `product_n = pool_m` instead of `pool`, so
       its local product numbers are 0..pool_m−1.
     - `publishing.py` `build_members`: where `copies[s] = int(GLOBAL.match(o…))` is set, map the
       local number k to `market_pool[market][k]`, a 1:1 mapping. (Don't squash a range of
       `pool` numbers onto `pool_m` products: that would give two listings of the same product
       in one shop.) Shops of the same market share the same mapping, so they list the same
       products, which q07, q08 and q10 need.
  4. **Market of each member:** a new `choose_markets(dconf, names, known)` next to `choose_known`.
     Draw the known members **per market**, so every market has a known shop and a known review
     site (q06, q07, q09, q11, q12, q15 and q16 need "my shop" or "my site"). Reviewers inherit
     their site's market.
  5. **Countries:** rewrite `bsbm:country` in `build_members` (vendor, rating-site and person
     records) and in `build_brands` (producer records) to a country of the market, seeded. Global
     brands keep WatDiv's country.
- **Config:** new section `markets: {count: 5, countries: [[US], [UK, FR, ES], [DE, AT, RU], [JP, KR], [CN]], global_brands: 4, global_share: 0.05, seed: 42}`
  (decisions D2, D3). Leaving out `markets` (or `count: 1`) keeps today's behaviour.
- **Rerun from:** `members --force` (`product_n` changes), then everything after it.
- **Check:**
  1. Each shop's listings cover only its market's brands plus the global ones (count brands per
     member, as in the analysis: today 289 of 400).
  2. `reach.py`: q07, q08, q12 and q13 under ~15k, q15 under ~25k.
  3. q07 still finds instances: it needs a known US shop, and q10 needs US offers.
- **Watch:**
  - In the US market every vendor is in the US, so q10's US filter and q15's country match stop
    filtering anything. Check the template `# rows` ranges after `build-workload` and retune.
  - With only 4 shops per market, q07's and q10's "offers at every shop" answers get smaller.

### Step 4 (Fix C): product types belong to markets, and instances are chosen by spread

- **What:**
  - Each product type gets a home market and a spread: most types are niche (1 market), some
    regional (2 markets), a few global.
  - A product only gets types that are available in its brand's market. Products of global brands
    get global types.
  - The workload picks analytical instances across spreads, so you get a cost curve instead of a wall.
- **Example:**
  ```
  ProductType322: home US, spread 1  → made only by US brands → q01 on it reaches ~13k documents
  ProductType140: home West, spread 2 (West + DACH+)          → ~27k
  ProductType7:   global                                      → whole web: traversal times out,
                                                                composite sources don't
  ```
- **Where:**
  1. **Types per market:** `Catalog` computes `type_markets[t]`, seeded, from a spread distribution.
  2. **Remap a product's types:** `publishing.py` `spec(n)`. For each WatDiv-drawn type `t` of
     product n, write `avail[market][crc(t) % len(avail[market])]` instead, and drop duplicates.
     This keeps WatDiv's number of types per product. `categories` (type → brands) and the taxonomy
     `seeAlso` then follow automatically in `build_placements`.
  3. **Workload:** `workload.py` `candidates()` for q01, q03, q04, q14 and q16.
     - Order the shuffled types so the `per_template` instances cover the spread buckets,
       e.g. 2 × spread 1, 2 × spread 2, 1 × global.
     - q04: pick the second type from types sharing a market with the first, for the low-spread
       instances.
     - q14: the reference product is already of type t, so it lives in t's market. Check this.
  4. **Report the spread:** add `markets_touched` to `analysis.json` (markets of `storages_in_answer`)
     and to `FIELDS`, next to the delegation gain.
- **Config:** `brands.type_spread: {1: 0.6, 2: 0.3, all: 0.1}` (decision D4).
- **Rerun from:** `publish`, then `build-workload`.
- **Check:** `reach.py`: spread-1 instances of q01, q03 and q04 under ~15k, q16 under ~25k. q14 will
  probably stay at ~40k (it reads every review page of the type): accept it or retune.
- **Document as a deviation from FedShop:** product types are no longer WatDiv's independent draw.

### Step 5: rebuild, measure, document

1. **Rebuild the generator image:** `docker build -f experiments/bsbm/discovery/docker/Dockerfile-generator …`.
   Then update `generatorImage` in the jbr experiment, and add the new keys to its
   `input/discovery-overrides.yaml`.
2. **Simulate:** `jbr prepare`, then run `tools/reach/reach.py <benchmark dir> cache.pkl` without
   flags. The flags (`SELECTIVE`, `CLUSTERS`, `MU`, `TYPE_*`) only emulated the fixes on the old data.
3. **Run the engines:** `jbr run` with plain traversal and with the composite-sources config.
4. **Fix q06:** its plain form has no IRI to start from, so it returns 0 results instantly. Seed it
   with my shop's storage, or keep the shop's IRI in the plain form.
5. **Later, in the jbr package:** report recall at the timeout and time to first and k-th result,
   so queries that don't finish still get a score.
6. **README:** update the publishers table, Links, Parameters and Deviations, and the numbers.

## Decisions to make before starting

| # | Question | Suggestion |
|---|---|---|
| D1 | Should the `fs:listingOf` triples in brand documents be the brand's own data (also in its service), or placements in its documents only? | Brand's own data (D1a) |
| D2 | How many markets, and how to group WatDiv's 10 countries? | 5: US · UK/FR/ES · DE/AT/RU · JP/KR · CN |
| D3 | How many global brands, and what share of each market's pool comes from them? | 4 largest, 5% |
| D4 | Type spread distribution | 60% one market, 30% two, 10% global |
| D5 | Reviews per reviewer | 100 (≈2,000 reviewers) |

## Tools

- `tools/reach/reach.py <benchmark dir> [cache.pkl]` simulates the link closure of every workload
  instance: pattern matching (variables only), `seeAlso`/`sameAs`, `pim:storage`, `ldp:contains`,
  and walk-up. It counts documents and containers only: no `.meta` requests, no services, no
  dynamic filter. Output is one JSON line per instance.
  - Environment variables: `ONLY=q07,q09`; ablations `NO_LDP`, `NO_SEEALSO`, `NO_PUBPAT`,
    `NO_STORAGE`, `SEEALSO_FROM=producer,taxonomy`; emulations `SELECTIVE`, `CLUSTERS=k`, `MU=x`,
    `TYPE_BRANDS=n`, `TYPE_MARKETS=n`.
  - The first run builds `cache.pkl` (~20 s); a full run takes ~1 min.
- `tools/reach/summarize.py out.jsonl` prints medians per template, by publisher kind.
- `tools/reach/compare.py name=out.jsonl[:benchmark-dir] ...` prints a table of medians per template, one
  column per run (with the benchmark dir, also its total documents).

## Results (2026-11-10)

Each stage was generated with `jbr prepare` (image `traversal-fixes`) and simulated with
`tools/reach/reach.py` (no flags). Median documents reached per template, (min–max) over its 5
instances; `tools/reach/compare.py` prints this table. Every stage kept 80 instances, and in s4b every
instance has `reachable_share` 1.0.

| Query | today (bf98523) | s1: fewer reviewers | s2: + Fix A | s3: + markets, 4 global brands | s4: + types per market | s4b: s4 without global brands |
|---|---|---|---|---|---|---|
| q01 | 121.5k | 121.4k | 121.6k | 78.0k | 77.9k | 30.7k (15.3k–61.7k) |
| q02 | 777 (471–4.9k) | 1.1k (289–9.2k) | 85 (63–88) | 80 (52–88) | 50 (39–88) | 80 (34–84) |
| q03 | 121.5k | 121.4k | 121.6k | 78.0k | 78.0k | 30.8k (15.1k–62.3k) |
| q04 | 121.5k | 121.4k | 121.6k | 78.0k | 78.0k | 31.3k (15.3k–78.0k) |
| q05 | 99.3k (77.7k–115.1k) | 113.6k | 15.0k (13.1k–15.8k) | 13.5k | 6.0k (3.5k–16.6k) | 12.4k (3.4k–16.4k) |
| q06 | 0 (no seed) | 109.0k | 110.0k | 18.0k | 17.9k | 15.3k (15.1k–15.6k) |
| q07 | 126.5k | 112.0k | 47.9k (29.7k–67.2k) | 12.9k (11.3k–32.8k) | 12.9k (11.2k–34.5k) | 11.2k (8.9k–12.9k) |
| q08 | 126.0k | 111.5k | 58.5k (52.7k–68.7k) | 10.4k (9.7k–26.3k) | 11.3k (9.8k–35.2k) | 10.7k (9.8k–11.2k) |
| q09 | 20.1k (13.4k–23.7k) | 21.0k | 4.5k (2.8k–5.4k) | 2.7k | 2.6k | 2.6k (1.8k–2.9k) |
| q10 | 48.8k | 48.9k | 2.4k (1.1k–4.9k) | 1.9k | 1.9k | 2.6k (1.7k–2.7k) |
| q11 | 7.0k (4.7k–9.2k) | 7.1k | 1.1k | 953 | 954 | 966 (792–1.1k) |
| q12 | 108.2k | 108.6k | 108.6k | 18.3k | 18.3k | 15.6k (15.1k–16.3k) |
| q13 | 121.5k | 121.4k | 121.6k | 78.0k | 77.9k | 15.3k (15.1k–16.3k) |
| q14 | 125.1k | 125.1k | 125.2k | 81.6k | 81.6k | 32.1k (16.4k–81.6k) |
| q15 | 126.5k | 112.0k | 107.5k (76.9k–107.9k) | 39.4k (12.7k–44.4k) | 31.8k (13.6k–54.1k) | 13.0k (11.6k–13.8k) |
| q16 | 125.2k | 125.1k | 125.3k | 81.7k | 81.6k | 31.9k (16.4k–81.7k) |
| documents in total | 152k | 145k | 145k | 101k | 101k | 101k |

What each step did:
- **Fewer reviewers** (9,301 → 2,080): only the queries that crawl review sites with reviewers (q07, q08, q15) drop, by ~14k.
- **Fix A** cuts every query whose cost came from `seeAlso` alone: q02 ×10, q05 ×7, q09 ×4, q10 ×20, q07/q08 ×2.
  Queries with a variable pattern `?listing fs:listingOf ?product` that crawl a whole shop (q01, q03, q04,
  q06, q12–q14, q16) still reach everything: the crawled shop leads to products of nearly every brand.
- **Markets** fix that, but **global brands undo it**: a shop sells some global products, the query crawls
  the global brand (`bsbm:producer ?brand`), and the brand's `fs:listingOf` triples lead to listings in every
  market. With 4 global brands the analytical queries and q13 reach 78k of 101k documents (s3, s4). Without
  them (s4b) a query that stays in one market reaches about one market (~15k).
- **Types per market** give the analytical templates a cost curve by spread (s4b, per instance):
  1 market ≈ 15k, 2 markets ≈ 31k, 3 ≈ 62k, all 5 ≈ 78k. q16's instances follow their type's spread (its
  answer is in my shop's market, but the type's brands are wherever the type is).
- With the ~30k-request budget (120 s at ~270 requests/s), s4b puts every lookup and product query, q05, q06,
  q12, q13 and the one-market analytical instances well within it, the two-market ones at its edge, and the
  global ones beyond it: there traversal should time out and composite sources not.
- Documents: 152k → 101k. A market's shops deal in 4,000 products instead of 20,000, so each shop has fewer,
  fuller product pages; and 7,200 fewer reviewer profiles.

Also changed:
- **q06** names my shop's vendor record (`?offer bsbm:vendor <{{Vendor}}>`), so its plain form starts there
  (15k documents instead of 0).
- **Producer records**: WatDiv writes a producer record only for the producers its own draws used, and not
  deterministically (one catalog run had 397 of 400), which crashed `publish`. A brand without a record now
  borrows the next one's properties (`catalog.py` `Catalog.producer`).
- `fragment.py` leaves a publisher's triples about other publishers' resources (the brand's `fs:listingOf`)
  out of the subject's document; they are only in the brand's graph and, as placements, in its documents.

Still to do:
1. `npm run jbr -- prepare` in the experiment (the old generation marker was moved to
   `generated/fedshop-discovery.bf98523.json`, so prepare regenerates), then `jbr run` with plain traversal and
   with composite sources.
2. Decide whether `experiments/bsbm/snakefile/config.yaml` (the "full config") gets these values; for now
   only the experiment's overrides set them, and the defaults keep the bf98523 behaviour.
3. Rebuild the README's numbers (publishers table, workload table, sizes).
4. Recall at timeout and time to first/k-th result in the jbr package.
5. Maybe: one market is still ~20k documents (~15k reached); fewer products per market (`members.pool`) or
   more markets would bring one-market queries further below the budget.
