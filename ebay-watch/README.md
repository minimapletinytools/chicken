# eBay watch

Tracks eBay searches over time (BIN price range and listing counts) and
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

1. Create a free eBay developer account at https://developer.ebay.com and
   create an application (Production keyset). You only need the **Client ID**
   and **Client Secret** — this uses the client-credentials app token, no
   user login/OAuth flow required.
2. In the repo's Settings → Secrets and variables → Actions, add:
   - `EBAY_CLIENT_ID`
   - `EBAY_CLIENT_SECRET`
3. Edit [config/ebay_searches.yml](config/ebay_searches.yml) — replace the
   example entry with the searches you actually want to track.
4. The workflow runs automatically on push to `main`, daily on a schedule,
   or manually from the Actions tab (`workflow_dispatch`), where you can choose
   the issue mode (comment on the existing alert issue vs. open a new one each
   run), alert threshold, and max comments per issue.

### How it works

Each run, per search:

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
too. After every run, `scripts/generate_chart.py` regenerates
[docs/index.html](docs/index.html): a self-contained line chart of BIN
min/median/max price over time per search, with checkboxes to filter which
tags' lines are shown and a dropdown to switch the plotted metric. Hovering
a line highlights its legend entry and vice versa.

Each legend entry has two icons: 🔍 opens that search on eBay in a new tab,
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
