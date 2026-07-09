# chicken

Automations, run via GitHub Actions.

## eBay watch

Tracks eBay searches over time and flags listings (auction or Buy It Now)
priced lower than the lowest BIN price previously seen for that search.

- Search definitions: [config/ebay_searches.yml](config/ebay_searches.yml)
- Script: [scripts/ebay_watch.py](scripts/ebay_watch.py)
- Workflow: [.github/workflows/ebay-watch.yml](.github/workflows/ebay-watch.yml)
- Price history cache (checked into the repo): `data/ebay_cache/<search_id>.json`

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
4. The workflow runs every 6 hours on a schedule, or trigger it manually from
   the Actions tab (`workflow_dispatch`), where you can choose whether hits
   get commented onto the existing open alert issue (default) or open a new
   issue each run.

### How it works

Each run, per search:

1. Fetches up to `MAX_RESULTS` listings sorted by price ascending via eBay's
   Browse API, applying your `condition`/`max_price`/`exclude_keywords`
   filters.
2. Compares each listing's price to the search's cached `lowest_bin_price`
   (the lowest Buy-It-Now price ever observed for that search). Anything
   priced lower — auction or BIN — and not already flagged before is a "hit".
3. Updates the cache: `lowest_bin_price` only ever ratchets down;
   `average_bin_price` is a snapshot of the current run's BIN listings (not
   an all-time average).
4. If there are hits, upserts a GitHub issue labeled `ebay-watch` with the
   list of new lower-priced listings.
5. Commits the updated cache files back to the repo.

The very first run for a new search just seeds the cache — nothing to
compare against yet, so no alerts fire until a second run sees something
cheaper.

### Caveat

eBay's Browse API item-summary `price` field for auction listings isn't
always the live current bid — the script prefers `currentBidPrice` when eBay
returns it, but for some auctions falls back to the summary `price` field,
which may lag the true current bid slightly.
