# Auction watch

Tracks eBay and Yahoo Auctions Japan searches over time. Each YAML entry
selects one source and supplies that source's own search terms. eBay tracks
BIN price range and listing counts and
opens an alert for two kinds of events: a search's minimum Buy-It-Now price
dropping significantly below its all-time low, or an auction listing
showing up priced below the median history BIN price (over the last 2 months).

Designed to be portable: this folder plus the workflow file below are the
whole automation. To reuse it in another repo, copy this `ebay-watch/`
folder to that repo's root and copy
[.github/workflows/ebay-watch.yml](../.github/workflows/ebay-watch.yml) into
its `.github/workflows/` — nothing else needs to change.

- Search definitions: [config/ebay_searches.yml](config/ebay_searches.yml)
- Script: [scripts/ebay_watch.py](scripts/ebay_watch.py)
- Chart generator: [scripts/generate_chart.py](scripts/generate_chart.py)
- Workflow: [.github/workflows/ebay-watch.yml](../.github/workflows/ebay-watch.yml)
- Price history cache (checked into the repo, outside this folder since it's
  run-specific state, not shareable template): `.github/data/ebay-watch/<search_id>.json`
- Price history chart (regenerated each run): [docs/index.html](docs/index.html)

### Setup

1. For eBay searches, create a free eBay developer account at https://developer.ebay.com and
   create an application (Production keyset). You only need the **Client ID**
   and **Client Secret** — this uses the client-credentials app token, no
   user login/OAuth flow required.
2. In the repo's Settings → Secrets and variables → Actions, add:
   - `EBAY_CLIENT_ID`
   - `EBAY_CLIENT_SECRET`
   For Yahoo Auctions searches, add a Firecrawl API key as the Actions secret
   `FIRECRAWL_KEY`. Keys belong in Actions secrets or local environment variables,
   never in committed files. Yahoo-only runs do not require eBay credentials.
3. Edit [config/ebay_searches.yml](config/ebay_searches.yml) — replace the
   example entry with the searches you actually want to track.
4. The workflow runs automatically on push to `main`, daily on a schedule,
   or manually from the Actions tab (`workflow_dispatch`), where you can choose
   the issue mode (comment on the existing alert issue vs. open a new one each
   run), alert threshold, and max comments per issue.

### Choosing the search source

```yaml
- id: spyderco-dodo
  source: ebay                  # optional; ebay is the default
  query: "Spyderco Dodo"

- id: spyderco-dodo-yahoo
  source: yahoo_auctions
  query: "スパイダルコ ドードー"
  tags: [knives, spyderco]
  max_price: 30000               # JPY; eBay's max_price is USD
```

An entry searches exactly one source. To track both, create two entries
with distinct IDs and independently chosen queries. Queries are sent literally;
they are not translated. Unknown sources, duplicate IDs, and invalid prices are
rejected before searching. `category_ids`, `condition`, and `marketplace` are
eBay-only and are rejected on Yahoo entries. `tags`, `priority`,
`exclude_keywords`, `max_price`, and `price_drop_threshold_pct` work on both.

Keep IDs stable. Cached histories record the source and currency, and changing
either requires a new ID. Existing caches without metadata are treated as eBay
USD histories. No currency conversion or cross-source price comparisons occur.

### Yahoo Auctions through Firecrawl

Yahoo search pages are fetched exclusively using Firecrawl's `/v2/scrape`
endpoint, with Japanese location/language settings and fresh HTML (`maxAge: 0`).
Prices are parsed from the displayed HTML, including tax where shown; no AI
extraction step is needed. Only actual search results are included: catalog
suggestions, recommendations, ads, and Yahoo flea-market listings are excluded.

Searches fetch the newest results in pages of 50, stopping at `MAX_RESULTS`
(default 50, before keyword/price filtering). A final page may fetch more than
the cap; only up to `MAX_RESULTS` listings are retained. These snapshots describe
the fetched sample, so the cheapest buyout across the whole market is not
guaranteed. A warning shows when the search exceeds the limit. Firecrawl requests
consume credits; raising `MAX_RESULTS` above 50 can require more requests per run.

Current bid (`現在`) and buyout (`即決`) are tracked separately. A listing with
both contributes its bid to auction tracking and its buyout to BIN history.
`max_price` is in JPY: an affordable bid is retained even if its buyout exceeds
the ceiling, but that buyout is excluded from BIN statistics. Shipping, proxy
fees, and import charges are not included. Closing times are included in new
auction alerts when present on the page.

The first successful Yahoo run seeds history without alerts. Later runs alert
once per newly observed auction that passes the filters, even without BIN
history. Fixed-price/buyout listings use the existing price-drop rule. Auctions
are deduplicated by listing ID, subject to the existing `SEEN_AUCTION_CAP` limit
(default 500). Yahoo new-auction alerts do not imply a discount versus BIN prices.

HTTP failures, blocked pages, missing prices, unrecognized layouts, and partial
pages leave that search's cache unchanged; other searches continue. A confirmed
empty search records an empty snapshot. Each failure is reported in the run log
and summary. A missing or failing eBay credential does not stop Yahoo searches.

### How eBay works

Each run, per eBay search:

1. Makes two separate eBay Browse API calls — one for BIN listings
   (`buyingOptions:{FIXED_PRICE}`, sorted by price ascending, up to
   `MAX_RESULTS`) and one for auctions (`buyingOptions:{AUCTION}`, sorted by
   `newlyListed`, up to `MAX_RESULTS`) — applying your
   `condition`/`max_price`/`exclude_keywords` filters to both. This isn't
   one combined call because eBay's Browse API only returns FIXED_PRICE
   listings *by default*; a pure auction with no Buy-It-Now option is
   silently excluded unless the buyingOptions filter explicitly asks for
   AUCTION too. Auctions are sorted by recency rather than price so a
   brand-new one surfaces on the next run regardless of its starting price,
   instead of getting buried behind cheaper BIN listings. Logs
   fetched-vs-total counts per search (from eBay's own `total` field) so
   under-coverage — more matching listings than `MAX_RESULTS` fetched — is
   visible in the run output rather than silently missing listings.
2. Records a snapshot in `history`: timestamp, BIN listing count, total
   listing count, and BIN min/max/median — so you can chart price range
   evolution over time later (e.g. with `jq` or a notebook).
3. Compares the current BIN min to `all_time_min_bin_price` (the lowest BIN
   price ever observed for that search, which only ratchets down). If it
   dropped by at least `price_drop_threshold_pct` (default 20%, set via the
   `PRICE_DROP_THRESHOLD_PCT` env var or overridden per search in the
   config), that's a **price drop alert**.
4. Checks auction listings: if an auction listing not seen in a prior run is
   priced below the median history BIN price (median over the last 2 months),
   that's an **auction alert** — a real deal, as opposed to a high bid that's
   already reached typical BIN market prices. Each auction only ever triggers
   this once (tracked via `seen_auction_ids`), even if it stays cheap across
   several runs.
5. If there are alerts, upserts a GitHub issue labeled `ebay-watch` listing
   them, sorted with `priority: high` searches first. In comment mode, once an
   issue reaches 10 comments (configurable via `MAX_ISSUE_COMMENTS`), it closes
   the issue and opens a new one so issues don't grow indefinitely.
6. Commits the updated cache files and chart back to the repo.
7. Packages and deploys the price history chart to GitHub Pages via
   `actions/upload-pages-artifact` and `actions/deploy-pages` under the
   `ebaywatcher` folder (with root redirecting to `/ebaywatcher/`).

The very first run for a new search just seeds the cache — nothing to
compare against yet, so no alert fires until a later run sees a big enough
BIN drop or a cheap-enough new auction. A repeat of the same BIN low won't
re-alert either, since the next drop has to clear the threshold against the
new floor. A search that never has any BIN listings can't establish
`all_time_min_bin_price`, so it'll never produce a new-auction alert either
— there's nothing to compare against.

### Tags and the price history chart

Each search can have a `tags` list (e.g. `[knives, spyderco]`) for grouping
and filtering — `priority` (`high`/`normal`) is folded in as an implicit tag
too, along with the search source. After every run, `scripts/generate_chart.py` regenerates
[docs/index.html](docs/index.html): a self-contained line chart of BIN
min/median/max price over time per search, with checkboxes to filter which
tags' lines are shown and dropdowns to switch the metric and currency. USD and
JPY appear in separate views. The auction minimum metric shows bid history,
including Yahoo searches with no buyouts. Hovering
a line highlights its legend entry and vice versa.

Each legend entry shows the source and has two icons: 🔍 opens that search on its marketplace in a new tab,
and 🏷️ opens the actual listing that was cheapest as of the most recent run
(sourced from `bin_min_url` in that search's cache history — not the
all-time-low listing, which may already be sold/delisted). The 🏷️ icon is
omitted for searches with no BIN listing in their history yet.

Open it directly (`open ebay-watch/docs/index.html` from the repo root). It
needs network access at view time to load Chart.js from a CDN. Note this
lives at `ebay-watch/docs/`, not a repo-root `docs/` folder, to keep the
whole automation self-contained — GitHub Pages' simple "deploy from branch"
option only serves a root-level `/docs` folder, so hosting this chart at a
URL would need a custom Pages deployment step (e.g. via
`actions/upload-pages-artifact`) rather than the one-click settings toggle.

To regenerate it manually: `python scripts/generate_chart.py` (run from
anywhere — paths resolve relative to this file, not your working directory).

### Verification

Install `ebay-watch/requirements.txt`, then run from the repository root:

```sh
python -m unittest discover -s ebay-watch/tests -v
```

Tests use sanitized Yahoo HTML captured through Firecrawl and mocked network
requests. They do not consume credits, contact eBay, or create GitHub issues.

### Specific items versus broad discovery searches

`search_type` chooses the workflow independently of the marketplace or category:

```yaml
- id: yahoo-japanese-tools-nankin-kanna
  source: yahoo_auctions
  search_type: generic
  tags: [japanese-tools]
  query: "南京鉋"
  max_results: 500               # optional; default for generic Yahoo searches
```

- `specific` (default): the existing price-history and alert workflow for a
  particular model or item.
- `generic`: a daily discovery digest for broad terms, including both auctions
  and fixed-price listings. These searches do not generate individual price or
  new-auction alerts, and are omitted from the price chart.

All seven Japanese-tool searches use `generic`. The category remains the
`japanese-tools` tag; `search_type` controls behavior.

Bookmark **`/ebaywatcher/new-listings.html` on the deployed Pages site**, or use
the “Daily new listings” link in the deployment job summary. The existing daily
workflow refreshes this page. You can also open [docs/new-listings.html](docs/new-listings.html)
locally after regenerating the chart. The chart links to the digest and vice
versa. Each day has expandable groups per search, with new-match counts,
direct listing links, bid/price, buyout and closing time where available.
Auctions ending soon appear first within each group. An item matching multiple
searches appears in each matching group; counts are matches, not unique items.

The first successful generic run records a baseline without adding existing
listings to the digest, including when switching a search from specific to
generic. Later runs report listings first observed since that baseline. Pushes
and manual reruns merge discoveries into the same UTC day, so they do not erase
the day's earlier results. The digest keeps seven calendar days, and listing
IDs are remembered for 90 days after their last observation, separately from
the specific-item auction-ID cap. A stale search is explicitly marked when it
has not completed a successful run today. “New” means newly observed, not a
guarantee of the marketplace's original posting date. A relisting with a new ID
is a new discovery.

Yahoo generic searches scan up to **500 newest results per run** by default
(at most 10 Firecrawl page requests per search); `max_results` can override this
from 1 to 10,000. Seven broad searches can therefore use up to 70 page requests
per run, including baseline and manual/push runs. Specific searches retain
their existing `MAX_RESULTS` default. eBay generic searches sort both listing
types by recency and default to `MAX_RESULTS` per type; their `max_results`
override is limited to 200 per type.

When matches exceed the fetch cap, the digest displays a coverage warning and
a link to the full marketplace search. Increase `max_results` or narrow the
query if needed. The digest cannot guarantee all new listings when more arrive
between runs than the cap allows, and listings that start and end between
checks can be missed. Failures preserve previous discoveries. Generating the
HTML page itself makes no marketplace or Firecrawl calls.

### Caveats

- Since BIN results are fetched sorted by price ascending and capped at
  `MAX_RESULTS`, `bin_min` is reliable (always within the first page), but
  `bin_max`/`bin_median` are biased toward the cheap end of the market —
  they describe the cheapest N listings, not the true population. Good
  enough as a rough trend heuristic, not exact stats. Auctions don't have
  this bias (sorted by recency, not price), but are similarly capped at
  `MAX_RESULTS` — a search with more than that many active auctions could
  still miss some; check the run log's fetched-vs-total counts.
- eBay's Browse API item-summary `price` field for auction listings isn't
  always the live current bid — the script prefers `currentBidPrice` when
  eBay returns it, but for some auctions falls back to the summary `price`
  field, which may lag the true current bid slightly.
