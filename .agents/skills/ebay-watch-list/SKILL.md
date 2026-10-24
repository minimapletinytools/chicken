---
name: ebay-watch-list
description: >-
  Use this skill when the user asks to add, remove, or modify entries in the
  eBay watch list, or asks how the watch list config works.
---

# Managing the eBay Watch List

The watch list lives at
[ebay-watch/config/ebay_searches.yml](file:///Users/giraffe/kitchen/faucet/chicken/ebay-watch/config/ebay_searches.yml).

Each entry is a YAML list item tracking exactly one eBay or Yahoo Auctions Japan search.

## Adding an entry

Append a new block to the end of the file:

```yaml
- id: some-unique-slug          # required — used as cache filename; renaming resets history
  tags: [tag1, tag2]            # optional — free-text tags for grouping/filtering
  source: ebay                 # optional — ebay (default) or yahoo_auctions
  search_type: specific        # optional — specific (default) or generic
  query: "search text"         # required — literal text sent to that source
```

### Rules

- **`id`** must be unique across the file and stable — it becomes the cache
  filename, so changing it resets that search's price history.
- **`query`** is the literal search text for the selected source; no translation occurs.
- **`source`** selects exactly one of `ebay` (default) or `yahoo_auctions`.
  To watch both, use separate entries with distinct IDs and independently chosen
  queries. Changing the source or currency of an existing cache requires a new ID.
- Yahoo Auctions is fetched through Firecrawl and requires the `FIRECRAWL_KEY`
  Actions secret/environment variable. Never put API keys in config or code.
- Yahoo's first run seeds history; later runs alert on new auctions matching
  filters and on buyout price drops for specific searches. Yahoo does not need
  BIN history to alert.
- **`search_type`** is `specific` (default: price-history alerts for particular
  items) or `generic` (broad discovery: daily new-listings digest, no individual
  price/auction alerts). This field is independent of source and category tags.
  Use `generic` for broad terms likely to return many different items.
- Generic searches seed their own baseline on first successful run, then
  collect new auction and fixed-price links by UTC day. The digest lives at
  `ebay-watch/docs/new-listings.html`, deployed to `/ebaywatcher/new-listings.html`.
  It retains seven days; IDs are remembered for 90 days after last observation.
- **`tags`** is an optional list used for grouping in alerts and the price
  chart. Use lowercase, hyphenated names (e.g. `stuffed-animal`, `knives`).

## Optional fields

| Field                       | Values / Default                                                    |
| :-------------------------- | :------------------------------------------------------------------ |
| `priority`                  | `"high"` or `"normal"` (default `"normal"`)                         |
| `search_type`               | `specific` (default) or `generic`                                  |
| `max_results`               | Yahoo generic defaults to 500 (up to 10 page requests); others use MAX_RESULTS (50). Allowed: Yahoo 1–10,000, eBay 1–200 per type. |
| `price_drop_threshold_pct`  | integer, overrides the global default (20)                          |
| `category_ids`              | comma-separated eBay category IDs                                   |
| `condition`                 | `NEW`, `USED`, `NEW_OTHER`, `CERTIFIED_REFURBISHED`, `SELLER_REFURBISHED` |
| `max_price`                 | hard ceiling: USD for eBay, JPY for Yahoo; bid and buyout filtered separately |
| `exclude_keywords`          | list of case-insensitive substrings to skip                         |
| `marketplace`               | defaults to `EBAY_US`                                               |

`category_ids`, `condition`, and `marketplace` are supported only on eBay;
do not add them to Yahoo entries.

Yahoo example:

```yaml
- id: spyderco-dodo-yahoo
  source: yahoo_auctions
  query: "スパイダルコ ドードー"
  max_price: 30000
  tags: [knives, spyderco]
```

## Removing an entry

Delete the entire `- id: ...` block (including all its fields) from the YAML
file. Note: this does **not** delete its cached price history in
`.github/data/ebay-watch/<id>.json` — remove that file separately if desired.

## Example: full entry

```yaml
- id: jellycat-bo-bigfoot
  tags: [jellycat, stuffed-animal]
  priority: high
  query: "Jellycat Bo Bigfoot"
  condition: NEW
  max_price: 50
  exclude_keywords: [keychain, clip]
```
