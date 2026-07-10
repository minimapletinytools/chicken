# chicken

Automations, run via GitHub Actions.

## eBay watch

Tracks eBay searches over time (BIN price range and listing counts) and
opens an alert when a search's minimum Buy-It-Now price drops significantly
below its all-time low.

- Search definitions: [config/ebay_searches.yml](config/ebay_searches.yml)
- Script: [scripts/ebay_watch.py](scripts/ebay_watch.py)
- Chart generator: [scripts/generate_chart.py](scripts/generate_chart.py)
- Workflow: [.github/workflows/ebay-watch.yml](.github/workflows/ebay-watch.yml)
- Price history cache (checked into the repo): `data/ebay_cache/<search_id>.json`
- Price history chart (regenerated each run): [docs/index.html](docs/index.html)

### Setup

1. Create a free eBay developer account at https://developer.ebay.com and
   create an application (Production keyset). You only need the **Client ID**
   and **Client Secret** — this uses the client-credentials app token, no
   user login/OAuth flow required.
2. In this repo's Settings → Secrets and variables → Actions, add:
   - `EBAY_CLIENT_ID`
   - `EBAY_CLIENT_SECRET`
3. Edit [config/ebay_searches.yml](config/ebay_searches.yml) — replace the
   example entry with the searches you actually want to track.
4. The workflow runs roughly every 3 days on a schedule, or trigger it
   manually from the Actions tab (`workflow_dispatch`), where you can choose
   the issue mode (comment on the existing alert issue vs. open a new one
   each run) and override the alert threshold for that run.

### How it works

Each run, per search:

1. Fetches up to `MAX_RESULTS` listings sorted by price ascending via eBay's
   Browse API, applying your `condition`/`max_price`/`exclude_keywords`
   filters.
2. Records a snapshot in `history`: timestamp, BIN listing count, total
   listing count, and BIN min/max/median — so you can chart price range
   evolution over time later (e.g. with `jq` or a notebook).
3. Compares the current BIN min to `all_time_min_bin_price` (the lowest BIN
   price ever observed for that search, which only ratchets down). If it
   dropped by at least `price_drop_threshold_pct` (default 20%, set via the
   `PRICE_DROP_THRESHOLD_PCT` env var or overridden per search in the
   config), that's an alert.
4. If there are alerts, upserts a GitHub issue labeled `ebay-watch` listing
   each drop, sorted with `priority: high` searches first.
5. Commits the updated cache files back to the repo.

The very first run for a new search just seeds the cache — nothing to
compare against yet, so no alert fires until a later run sees a big enough
drop. A repeat of the same low won't re-alert either, since the next drop
has to clear the threshold against the new floor.

### Tags and the price history chart

Each search can have a `tags` list (e.g. `[knives, spyderco]`) for grouping
and filtering — `priority` (`high`/`normal`) is folded in as an implicit tag
too. After every run, `scripts/generate_chart.py` regenerates
[docs/index.html](docs/index.html): a self-contained line chart of BIN
min/median/max price over time per search, with checkboxes to filter which
tags' lines are shown and a dropdown to switch the plotted metric.

Open it directly (`open docs/index.html`) or enable GitHub Pages for this
repo pointed at the `docs/` folder to view it at a URL. It needs network
access at view time to load Chart.js from a CDN.

To regenerate it manually: `python scripts/generate_chart.py`.

### Caveats

- Since results are fetched sorted by price ascending and capped at
  `MAX_RESULTS`, `bin_min` is reliable (always within the first page), but
  `bin_max`/`bin_median` are biased toward the cheap end of the market —
  they describe the cheapest N listings, not the true population. Good
  enough as a rough trend heuristic, not exact stats.
- eBay's Browse API item-summary `price` field for auction listings isn't
  always the live current bid — the script prefers `currentBidPrice` when
  eBay returns it, but for some auctions falls back to the summary `price`
  field, which may lag the true current bid slightly.
