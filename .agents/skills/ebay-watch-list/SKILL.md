---
name: ebay-watch-list
description: >-
  Use this skill when the user asks to add, remove, or modify entries in the
  eBay watch list, or asks how the watch list config works.
---

# Managing the eBay Watch List

The watch list lives at
[ebay-watch/config/ebay_searches.yml](file:///Users/giraffe/kitchen/faucet/chicken/ebay-watch/config/ebay_searches.yml).

Each entry is a YAML list item tracking one eBay saved search.

## Adding an entry

Append a new block to the end of the file:

```yaml
- id: some-unique-slug          # required — used as cache filename; renaming resets history
  tags: [tag1, tag2]            # optional — free-text tags for grouping/filtering
  query: "eBay search text"     # required — the search string sent to eBay
```

### Rules

- **`id`** must be unique across the file and stable — it becomes the cache
  filename, so changing it resets that search's price history.
- **`query`** is the literal eBay search text.
- **`tags`** is an optional list used for grouping in alerts and the price
  chart. Use lowercase, hyphenated names (e.g. `stuffed-animal`, `knives`).

## Optional fields

| Field                       | Values / Default                                                    |
| :-------------------------- | :------------------------------------------------------------------ |
| `priority`                  | `"high"` or `"normal"` (default `"normal"`)                         |
| `price_drop_threshold_pct`  | integer, overrides the global default (20)                          |
| `category_ids`              | comma-separated eBay category IDs                                   |
| `condition`                 | `NEW`, `USED`, `NEW_OTHER`, `CERTIFIED_REFURBISHED`, `SELLER_REFURBISHED` |
| `max_price`                 | hard ceiling in USD; listings above this are ignored                 |
| `exclude_keywords`          | list of case-insensitive substrings to skip                         |
| `marketplace`               | defaults to `EBAY_US`                                               |

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
