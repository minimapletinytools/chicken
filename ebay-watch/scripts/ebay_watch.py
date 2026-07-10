#!/usr/bin/env python3
"""
Watches eBay searches over time, using the official eBay Browse API.

Each run records a snapshot of BIN price range (min/max/median) and listing
counts per search, so the history can be charted later. Since results are
fetched sorted by price ascending and capped at MAX_RESULTS, "max"/"median"
are biased toward the cheap end of the market (a heuristic over the true
population) — "min" is the one reliable stat since it's always within the
first page.

Opens/updates a GitHub issue when a search's BIN minimum drops significantly
below its all-time low (default 20%, configurable globally or per search).

Env vars required:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET  - eBay developer app credentials
  GITHUB_TOKEN                        - for creating/updating the alert issue
  GITHUB_REPOSITORY                   - "owner/repo" (set automatically in GHA)

Optional env vars:
  ISSUE_MODE                "comment" (default, reuse one open issue) or "new"
  ISSUE_LABEL                label used to find/tag the alert issue (default "ebay-watch")
  CONFIG_PATH                path to searches config (default <this folder>/config/ebay_searches.yml)
  CACHE_DIR                  path to cache directory (default <repo root>/.github/data/ebay-watch)
  MAX_RESULTS                listings fetched per search, sorted by price asc (default 50)
  PRICE_DROP_THRESHOLD_PCT   default alert threshold in percent (default 20);
                               overridable per search via `price_drop_threshold_pct`
  HISTORY_CAP                max history snapshots kept per search (default 500)

This script assumes it lives at <automation folder>/scripts/ebay_watch.py,
with the automation folder placed at the root of a git repo (so that
resolving two directories up from the repo root reaches ".github/data").
That's the only structural assumption — everything else is config-driven,
so the whole automation folder can be copied into another repo as-is.
"""
from __future__ import annotations

import base64
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = AUTOMATION_ROOT.parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", AUTOMATION_ROOT / "config" / "ebay_searches.yml"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", REPO_ROOT / ".github" / "data" / "ebay-watch"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "50"))
ISSUE_MODE = os.environ.get("ISSUE_MODE", "comment")
ISSUE_LABEL = os.environ.get("ISSUE_LABEL", "ebay-watch")
DEFAULT_DROP_THRESHOLD_PCT = float(os.environ.get("PRICE_DROP_THRESHOLD_PCT", "20"))
HISTORY_CAP = int(os.environ.get("HISTORY_CAP", "500"))

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
        data = json.loads(path.read_text())
        if "all_time_min_bin_price" not in data:
            # Migrate from the older lowest_bin_price/average_bin_price/
            # seen_item_ids schema, preserving the real all-time low so
            # alerting doesn't silently reset to "unseeded".
            data = {
                "search_id": data.get("search_id", search_id),
                "all_time_min_bin_price": data.get("lowest_bin_price"),
                "all_time_min_bin_item": data.get("lowest_bin_item"),
                "history": [],
                "last_checked": data.get("last_checked"),
            }
        return data
    return {
        "search_id": search_id,
        "all_time_min_bin_price": None,
        "all_time_min_bin_item": None,
        "history": [],
        "last_checked": None,
    }


def save_cache(search_id: str, cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{search_id}.json"
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def process_search(token: str, search: dict) -> tuple[dict, dict | None]:
    search_id = search["id"]
    cache = load_cache(search_id)
    items = search_ebay(token, search)
    now = datetime.now(timezone.utc).isoformat()

    bin_items = [i for i in items if i["buying_option"] == "FIXED_PRICE"]
    bin_prices = [i["price"] for i in bin_items]

    cache.setdefault("history", []).append(
        {
            "timestamp": now,
            "total_count": len(items),
            "bin_count": len(bin_items),
            "bin_min": min(bin_prices) if bin_prices else None,
            "bin_max": max(bin_prices) if bin_prices else None,
            "bin_median": round(statistics.median(bin_prices), 2) if bin_prices else None,
        }
    )
    cache["history"] = cache["history"][-HISTORY_CAP:]
    cache["last_checked"] = now

    alert = None
    prior_min = cache.get("all_time_min_bin_price")
    if bin_items:
        current_min_item = min(bin_items, key=lambda i: i["price"])
        current_min = current_min_item["price"]

        if prior_min:
            threshold_pct = search.get("price_drop_threshold_pct", DEFAULT_DROP_THRESHOLD_PCT)
            drop_pct = (prior_min - current_min) / prior_min * 100
            if drop_pct >= threshold_pct:
                alert = {
                    "search_id": search_id,
                    "search_query": search["query"],
                    "tags": search.get("tags", []),
                    "priority": search.get("priority", "normal"),
                    "prior_min": prior_min,
                    "current_min": current_min,
                    "drop_pct": drop_pct,
                    "title": current_min_item["title"],
                    "url": current_min_item["url"],
                }

        # Ratchets down; a repeat of the same low won't re-alert since the
        # next drop has to clear the threshold against this new floor.
        if prior_min is None or current_min < prior_min:
            cache["all_time_min_bin_price"] = current_min
            cache["all_time_min_bin_item"] = {
                "id": current_min_item["id"],
                "title": current_min_item["title"],
                "url": current_min_item["url"],
                "seen_at": now,
            }

    save_cache(search_id, cache)
    return cache, alert


def format_alert(alert: dict) -> str:
    flag = "🔴 **HIGH PRIORITY** — " if alert.get("priority") == "high" else ""
    tags = f" [{', '.join(alert['tags'])}]" if alert.get("tags") else ""
    return (
        f"- {flag}🔻 **{alert['search_query']}** min BIN dropped "
        f"{alert['drop_pct']:.1f}% (${alert['prior_min']:.2f} → ${alert['current_min']:.2f}) — "
        f"[{alert['title']}]({alert['url']}) · search `{alert['search_id']}`{tags}"
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

    all_alerts = []
    summary_lines = ["# eBay watch run\n"]
    for search in searches:
        try:
            cache, alert = process_search(token, search)
        except requests.HTTPError as e:
            print(f"[{search['id']}] eBay API error: {e}", file=sys.stderr)
            summary_lines.append(f"- `{search['id']}`: ERROR {e}\n")
            continue

        latest = cache["history"][-1]
        drop_note = f" — 🔻 {alert['drop_pct']:.1f}% drop" if alert else ""
        summary_lines.append(
            f"- `{search['id']}`: {latest['bin_count']} BIN / {latest['total_count']} total, "
            f"min ${latest['bin_min']}, max ${latest['bin_max']}, median ${latest['bin_median']}"
            f"{drop_note}\n"
        )
        if alert:
            all_alerts.append(alert)

    all_alerts.sort(key=lambda a: (0 if a.get("priority") == "high" else 1, -a["drop_pct"]))

    summary = "".join(summary_lines)
    print(summary)
    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        with open(step_summary_path, "a") as f:
            f.write(summary)

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a") as f:
            f.write(f"alerts_found={'true' if all_alerts else 'false'}\n")

    if all_alerts and gh_token:
        body_lines = [f"### Significant BIN price drops\n_{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}_\n"]
        body_lines.extend(format_alert(a) for a in all_alerts)
        upsert_issue(gh_token, "\n".join(body_lines))
    elif all_alerts:
        print("Alerts found but no GITHUB_TOKEN set, skipping issue creation.", file=sys.stderr)


if __name__ == "__main__":
    main()
