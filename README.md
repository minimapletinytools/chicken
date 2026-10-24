# chicken

Personal automations, run via GitHub Actions. Each automation lives in its
own top-level folder (scripts, config, its own README) so it can be copied
into another repo on its own; only the corresponding workflow file has to
live under `.github/workflows/` per GitHub's requirements.

## Automations

- [ebay-watch](ebay-watch/) — watches specific eBay or Yahoo Auctions Japan items
  for price drops and new auctions, and collects broad searches into a daily
  listing digest. See [ebay-watch/README.md](ebay-watch/README.md).
