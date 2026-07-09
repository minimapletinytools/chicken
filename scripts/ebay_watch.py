#!/usr/bin/env python3
"""
Watches eBay searches for BIN/auction listings priced below the lowest
price previously seen for that search, using the official eBay Browse API.

Env vars required:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET  - eBay developer app credentials
  GITHUB_TOKEN                        - for creating/updating the alert issue
  GITHUB_REPOSITORY                   - "owner/repo" (set automatically in GHA)

Optional env vars:
  ISSUE_MODE   "comment" (default, reuse one open issue) or "new"
  ISSUE_LABEL  label used to find/tag the alert issue (default "ebay-watch")
  CONFIG_PATH  path to searches config (default config/ebay_searches.yml)
  CACHE_DIR    path to cache directory (default data/ebay_cache)
  MAX_RESULTS  listings fetched per search, sorted by price asc (default 50)
"""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config" / "ebay_searches.yml"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ROOT / "data" / "ebay_cache"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "50"))
ISSUE_MODE = os.environ.get("ISSUE_MODE", "comment")
ISSUE_LABEL = os.environ.get("ISSUE_LABEL", "ebay-watch")
SEEN_ID_CAP = 500

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def get_ebay_token(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        EBAY_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_ebay(token: str, search: dict) -> list[dict]:
    filters = []
    if search.get("condition"):
        filters.append(f"conditions:{{{search['condition']}}}")
    if search.get("max_price"):
        filters.append(f"price:[..{search['max_price']}],priceCurrency:USD")

    params = {
        "q": search["query"],
        "limit": str(MAX_RESULTS),
        "sort": "price",
    }
    if filters:
        params["filter"] = ",".join(filters)
    if search.get("category_ids"):
        params["category_ids"] = str(search["category_ids"])

    resp = requests.get(
        EBAY_SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": search.get("marketplace", "EBAY_US"),
        },
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    excludes = [kw.lower() for kw in search.get("exclude_keywords", [])]
    items = []
    for item in data.get("itemSummaries", []):
        title = item.get("title", "")
        if any(kw in title.lower() for kw in excludes):
            continue

        buying_options = item.get("buyingOptions", [])
        is_auction = "AUCTION" in buying_options
        # For auctions the Browse API sometimes exposes the live bid
        # separately; fall back to `price` (starting/current price) if not.
        price_field = item.get("currentBidPrice") or item.get("price")
        if not price_field or price_field.get("value") is None:
            continue

        items.append(
            {
                "id": item.get("itemId"),
                "title": title,
                "price": float(price_field["value"]),
                "currency": price_field.get("currency", "USD"),
                "buying_option": "AUCTION" if is_auction else "FIXED_PRICE",
                "url": item.get("itemWebUrl"),
            }
        )
    return items


def load_cache(search_id: str) -> dict:
    path = CACHE_DIR / f"{search_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {
        "search_id": search_id,
        "lowest_bin_price": None,
        "lowest_bin_item": None,
        "average_bin_price": None,
        "bin_sample_count": 0,
        "seen_item_ids": [],
        "last_checked": None,
    }


def save_cache(search_id: str, cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{search_id}.json"
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def process_search(token: str, search: dict) -> tuple[dict, list[dict]]:
    search_id = search["id"]
    cache = load_cache(search_id)
    items = search_ebay(token, search)

    bin_items = [i for i in items if i["buying_option"] == "FIXED_PRICE"]
    now = datetime.now(timezone.utc).isoformat()

    prior_lowest = cache.get("lowest_bin_price")
    seen_ids = set(cache.get("seen_item_ids", []))

    hits = []
    if prior_lowest is not None:
        for item in items:
            if item["id"] in seen_ids:
                continue
            if item["price"] < prior_lowest:
                hits.append(
                    {
                        **item,
                        "search_id": search_id,
                        "search_query": search["query"],
                        "prior_lowest": prior_lowest,
                        "group": search.get("group"),
                        "priority": search.get("priority", "normal"),
                    }
                )

    # Update cache: lowest ratchets down over time; average reflects the
    # most recent run's snapshot rather than an all-time running average.
    if bin_items:
        current_lowest_item = min(bin_items, key=lambda i: i["price"])
        current_lowest = current_lowest_item["price"]
        current_average = sum(i["price"] for i in bin_items) / len(bin_items)

        if prior_lowest is None or current_lowest < prior_lowest:
            cache["lowest_bin_price"] = current_lowest
            cache["lowest_bin_item"] = {
                "id": current_lowest_item["id"],
                "title": current_lowest_item["title"],
                "url": current_lowest_item["url"],
                "seen_at": now,
            }

        cache["average_bin_price"] = round(current_average, 2)
        cache["bin_sample_count"] = len(bin_items)

    seen_ids.update(item["id"] for item in items)
    cache["seen_item_ids"] = list(seen_ids)[-SEEN_ID_CAP:]
    cache["last_checked"] = now

    save_cache(search_id, cache)
    return cache, hits


def format_hit(hit: dict) -> str:
    kind = "Auction" if hit["buying_option"] == "AUCTION" else "BIN"
    flag = "🔴 **HIGH PRIORITY** — " if hit.get("priority") == "high" else ""
    group = f" [{hit['group']}]" if hit.get("group") else ""
    return (
        f"- {flag}**[{hit['title']}]({hit['url']})** — "
        f"{kind} ${hit['price']:.2f} (prior lowest ${hit['prior_lowest']:.2f}) "
        f"· search `{hit['search_id']}`{group}"
    )


def github_request(method: str, path: str, token: str, **kwargs) -> requests.Response:
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


def upsert_issue(gh_token: str, body: str) -> None:
    if ISSUE_MODE == "new":
        github_request(
            "POST",
            "/issues",
            gh_token,
            json={"title": f"eBay watch alert — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", "body": body, "labels": [ISSUE_LABEL]},
        )
        return

    existing = github_request("GET", f"/issues?labels={ISSUE_LABEL}&state=open", gh_token).json()
    if existing:
        issue_number = existing[0]["number"]
        github_request("POST", f"/issues/{issue_number}/comments", gh_token, json={"body": body})
    else:
        github_request(
            "POST",
            "/issues",
            gh_token,
            json={"title": "eBay watch alerts", "body": body, "labels": [ISSUE_LABEL]},
        )


def main() -> None:
    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]
    gh_token = os.environ.get("GITHUB_TOKEN")

    searches = yaml.safe_load(CONFIG_PATH.read_text()) or []
    token = get_ebay_token(client_id, client_secret)

    all_hits = []
    summary_lines = ["# eBay watch run\n"]
    for search in searches:
        try:
            cache, hits = process_search(token, search)
        except requests.HTTPError as e:
            print(f"[{search['id']}] eBay API error: {e}", file=sys.stderr)
            summary_lines.append(f"- `{search['id']}`: ERROR {e}\n")
            continue

        all_hits.extend(hits)
        summary_lines.append(
            f"- `{search['id']}`: lowest ${cache['lowest_bin_price']}, "
            f"avg ${cache['average_bin_price']}, {len(hits)} new low hit(s)\n"
        )

    all_hits.sort(key=lambda h: (0 if h.get("priority") == "high" else 1, h["price"]))

    summary = "".join(summary_lines)
    print(summary)
    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        with open(step_summary_path, "a") as f:
            f.write(summary)

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a") as f:
            f.write(f"hits_found={'true' if all_hits else 'false'}\n")

    if all_hits and gh_token:
        body_lines = [f"### New listings below prior lowest seen price\n_{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}_\n"]
        body_lines.extend(format_hit(h) for h in all_hits)
        upsert_issue(gh_token, "\n".join(body_lines))
    elif all_hits:
        print("Hits found but no GITHUB_TOKEN set, skipping issue creation.", file=sys.stderr)


if __name__ == "__main__":
    main()
