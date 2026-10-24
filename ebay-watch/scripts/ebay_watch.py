#!/usr/bin/env python3
"""
Watches eBay (official Browse API) or Yahoo Auctions Japan (Firecrawl).

Each search selects exactly one source, defaulting to eBay. Yahoo's adapter
keeps JPY bids and buyouts separate, seeds the first run, and then alerts
on newly observed auctions and buyout price drops. The details below describe
the eBay path.

Does two separate item_summary/search calls per search, not one:
  - BIN listings, filter buyingOptions:{FIXED_PRICE}, sort=price (ascending)
  - auction listings, filter buyingOptions:{AUCTION}, sort=newlyListed

Two reasons this isn't a single combined call:
  1. eBay's Browse API only returns FIXED_PRICE listings *by default* — a
     pure auction with no Buy-It-Now option is silently excluded unless the
     buyingOptions filter explicitly asks for AUCTION too. Without this,
     the script would never see pure auctions at all (dual-format "auction
     with BIN" listings would still slip through, since those carry
     FIXED_PRICE as well — which is what made this easy to miss).
  2. Sorting auctions by newlyListed instead of price means a brand-new
     auction surfaces on the very next run regardless of its starting
     price, rather than getting buried behind up to MAX_RESULTS cheaper
     BIN listings in a single price-sorted call (and never appearing at
     all if this search reliably has more than MAX_RESULTS BIN listings).
     BIN listings stay sorted by price, since that's what guarantees the
     true min is always captured for price-drop-threshold alerting.

Each run records a snapshot of BIN price range (min/max/median) and listing
counts per search, so the history can be charted later. Since BIN results
are capped at MAX_RESULTS, "max"/"median" are biased toward the cheap end
of the market (a heuristic over the true population) — "min" is the one
reliable stat since it's always within that page. Logs fetched-vs-total
counts per search (see eBay's `total` field) so under-coverage is visible
in the run output rather than silently missing listings.

Opens/updates a GitHub issue for two kinds of events:
  - BIN minimum drops significantly below its all-time low (default 20%,
    configurable globally or per search)
  - a not-previously-seen auction listing is priced below the median history BIN
    price (median over last 2 months)

Env vars required:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET  - required only for eBay searches
  FIRECRAWL_KEY                      - required only for Yahoo Auctions searches
  GITHUB_TOKEN                        - for creating/updating the alert issue
  GITHUB_REPOSITORY                   - "owner/repo" (set automatically in GHA)

Optional env vars:
  ISSUE_MODE                "comment" (default, reuse one open issue) or "new"
  ISSUE_LABEL                label used to find/tag the alert issue (default "ebay-watch")
  MAX_ISSUE_COMMENTS         max comments on an issue before closing and rolling over to a new one (default 10)
  CONFIG_PATH                path to searches config (default <this folder>/config/ebay_searches.yml)
  CACHE_DIR                  path to cache directory (default <repo root>/.github/data/ebay-watch)
  MAX_RESULTS                listings fetched per search per buying-option
                               (BIN and auction each fetch up to this many,
                               so up to 2x this per search), sorted per the
                               rules above (default 50)
  PRICE_DROP_THRESHOLD_PCT   default alert threshold in percent (default 20);
                               overridable per search via `price_drop_threshold_pct`
  HISTORY_CAP                max history snapshots kept per search (default 500)
  SEEN_AUCTION_CAP           max auction ids remembered per search, oldest
                               dropped first (default 500)

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from search_config import check_cache_source, result_limit, search_type_of, source_of, validate_searches
from listing_digest import update_discoveries
from yahoo_auctions import search_yahoo

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = AUTOMATION_ROOT.parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", AUTOMATION_ROOT / "config" / "ebay_searches.yml"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", REPO_ROOT / ".github" / "data" / "ebay-watch"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "50"))
ISSUE_MODE = os.environ.get("ISSUE_MODE", "comment")
ISSUE_LABEL = os.environ.get("ISSUE_LABEL", "ebay-watch")
MAX_ISSUE_COMMENTS = int(os.environ.get("MAX_ISSUE_COMMENTS", "10"))
DEFAULT_DROP_THRESHOLD_PCT = float(os.environ.get("PRICE_DROP_THRESHOLD_PCT", "20"))
HISTORY_CAP = int(os.environ.get("HISTORY_CAP", "500"))
SEEN_AUCTION_CAP = int(os.environ.get("SEEN_AUCTION_CAP", "500"))

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


def fetch_ebay_page(token: str, search: dict, buying_options: str, sort: str) -> tuple[list[dict], int]:
    """One Browse API call. Returns (parsed items, eBay-reported total matches)."""
    filters = [f"buyingOptions:{{{buying_options}}}"]
    if search.get("condition"):
        filters.append(f"conditions:{{{search['condition']}}}")
    if search.get("max_price"):
        filters.append(f"price:[..{search['max_price']}],priceCurrency:USD")

    params = {
        "q": search["query"],
        "limit": str(result_limit(search, MAX_RESULTS)),
        "sort": sort,
        "filter": ",".join(filters),
    }
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

        buying_opts = item.get("buyingOptions", [])
        is_auction = "AUCTION" in buying_opts
        # For auctions the Browse API sometimes exposes the live bid
        # separately; fall back to `price` (starting/current price) if not.
        price_field = item.get("currentBidPrice") or item.get("price")
        if not price_field or price_field.get("value") is None:
            continue

        item_id = item.get("itemId")
        if not item_id:
            continue

        items.append(
            {
                "id": item_id,
                "title": title,
                "price": float(price_field["value"]),
                "currency": price_field.get("currency", "USD"),
                "buying_option": "AUCTION" if is_auction else "FIXED_PRICE",
                "url": item.get("itemWebUrl"),
            }
        )
    return items, data.get("total", len(items))


def search_ebay(token: str, search: dict) -> list[dict]:
    bin_sort = "newlyListed" if search_type_of(search) == "generic" else "price"
    bin_items, bin_total = fetch_ebay_page(token, search, "FIXED_PRICE", bin_sort)
    auction_items, auction_total = fetch_ebay_page(token, search, "AUCTION", "newlyListed")

    search_id = search["id"]
    limit = result_limit(search, MAX_RESULTS)
    search["_coverage"] = {"limited": bin_total > limit or auction_total > limit,
                           "total": bin_total + auction_total,
                           "fetched": len({item["id"] for item in bin_items + auction_items})}
    print(
        f"[{search_id}] fetched {len(bin_items)}/{bin_total} BIN match(es), "
        f"{len(auction_items)}/{auction_total} auction match(es)"
    )
    if bin_total > len(bin_items):
        print(
            f"[{search_id}] WARNING: {bin_total - len(bin_items)} BIN listing(s) not fetched "
            f"(result limit={limit}) — fetched prices may not reflect the full market",
            file=sys.stderr,
        )
    if auction_total > len(auction_items):
        print(
            f"[{search_id}] WARNING: {auction_total - len(auction_items)} auction listing(s) not fetched "
            f"(result limit={limit}) — some new auctions may be missed this run",
            file=sys.stderr,
        )

    # A dual-format ("auction with Buy It Now") listing can appear in both
    # pages; either copy classifies identically since buying_option is
    # derived from the item's own data, not which call fetched it.
    combined = {item["id"]: item for item in bin_items}
    combined.update((item["id"], item) for item in auction_items)
    return list(combined.values())


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
                "seen_auction_ids": [],
                "last_checked": data.get("last_checked"),
            }
        data.setdefault("seen_auction_ids", [])
        return data
    return {
        "search_id": search_id,
        "all_time_min_bin_price": None,
        "all_time_min_bin_item": None,
        "history": [],
        "seen_auction_ids": [],
        "last_checked": None,
    }


def save_cache(search_id: str, cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{search_id}.json"
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def get_median_history_bin(history: list[dict], days: int = 60, now: datetime | None = None) -> float | None:
    if not history:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    recent_medians = []
    for entry in history:
        ts_str = entry.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff and entry.get("bin_median") is not None:
            recent_medians.append(entry["bin_median"])

    if not recent_medians:
        recent_medians = [e["bin_median"] for e in history if e.get("bin_median") is not None]

    if not recent_medians:
        return None

    return round(statistics.median(recent_medians), 2)


def process_search(token: str | None, search: dict, firecrawl_key: str | None = None) -> tuple[dict, list[dict]]:
    search_id = search["id"]
    cache_exists = (CACHE_DIR / f"{search_id}.json").exists()
    cache = load_cache(search_id)
    source = source_of(search)
    if cache_exists:
        check_cache_source(cache, search)
    seeded = cache.get("last_checked") is not None
    if source == "yahoo_auctions":
        if not firecrawl_key:
            raise ValueError("FIRECRAWL_KEY is required for Yahoo Auctions")
        items = search_yahoo(firecrawl_key, search, result_limit(search, MAX_RESULTS))
    else:
        if not token:
            raise ValueError("eBay credentials are required for eBay searches")
        items = search_ebay(token, search)
    currencies = {item["currency"] for item in items}
    currency = next(iter(currencies), cache.get("currency", "JPY" if source == "yahoo_auctions" else "USD"))
    prior_currency = cache.get("currency", "USD" if cache_exists else currency)
    if len(currencies) > 1 or currency != prior_currency:
        raise ValueError(f"[{search_id}] currency differs from cached history; use a new id")
    cache.update(source=source, currency=currency)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    if search_type_of(search) == "generic":
        update_discoveries(cache, items, now_dt, search.get("_coverage", {}))
        save_cache(search_id, cache)
        return cache, []
    cache["search_type"] = "specific"

    bin_items = [i for i in items if i["buying_option"] == "FIXED_PRICE"]
    if source == "yahoo_auctions":
        bin_items += [dict(item, price=item["buyout_price"])
                      for item in items if item["buying_option"] == "AUCTION"
                      and item.get("buyout_price") is not None
                      and (search.get("max_price") is None or item["buyout_price"] <= search["max_price"])]
    bin_prices = [i["price"] for i in bin_items]
    auction_items = [i for i in items if i["buying_option"] == "AUCTION"]
    current_min_item = min(bin_items, key=lambda i: i["price"]) if bin_items else None

    cache.setdefault("history", []).append(
        {
            "timestamp": now,
            "total_count": len(items),
            "bin_count": len(bin_items),
            "bin_min": min(bin_prices) if bin_prices else None,
            "bin_max": max(bin_prices) if bin_prices else None,
            "bin_median": round(statistics.median(bin_prices), 2) if bin_prices else None,
            # Link to this run's actual cheapest listing (as opposed to
            # all_time_min_bin_item below, which can go stale/sold once a
            # cheaper item has since been listed and the record-holder ends).
            "bin_min_url": current_min_item["url"] if current_min_item else None,
            "currency": currency,
            "auction_count": len(auction_items),
            "auction_min": min((item["price"] for item in auction_items), default=None),
        }
    )
    cache["history"] = cache["history"][-HISTORY_CAP:]
    cache["last_checked"] = now

    alerts = []
    prior_min = cache.get("all_time_min_bin_price")

    if current_min_item:
        current_min = current_min_item["price"]

        if prior_min:
            threshold_pct = search.get("price_drop_threshold_pct", DEFAULT_DROP_THRESHOLD_PCT)
            drop_pct = (prior_min - current_min) / prior_min * 100
            if drop_pct >= threshold_pct:
                alerts.append(
                    {
                        "kind": "price_drop",
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
                )

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

    # New (not-previously-seen) auctions priced below the median history BIN
    # price (median over last 2 months). Each auction only ever triggers once
    # (tracked via seen_auction_ids), even if it stays cheap across several runs.
    median_hist_bin = get_median_history_bin(cache["history"], days=60, now=now_dt)
    seen_auctions = list(cache.get("seen_auction_ids", []))
    seen_auction_set = set(seen_auctions)
    if source == "yahoo_auctions" and seeded:
        for item in auction_items:
            if item["id"] not in seen_auction_set:
                alerts.append({
                    "kind": "yahoo_new_auction", "search_id": search_id,
                    "search_query": search["query"], "tags": search.get("tags", []),
                    "priority": search.get("priority", "normal"), "current_min": item["price"],
                    "drop_pct": 0, "title": item["title"], "url": item["url"],
                    "ends_at": item.get("ends_at"),
                })
    elif source == "ebay" and median_hist_bin is not None:
        for item in auction_items:
            if item["id"] in seen_auction_set or item["price"] >= median_hist_bin:
                continue
            discount_pct = (median_hist_bin - item["price"]) / median_hist_bin * 100
            alerts.append(
                {
                    "kind": "new_auction",
                    "search_id": search_id,
                    "search_query": search["query"],
                    "tags": search.get("tags", []),
                    "priority": search.get("priority", "normal"),
                    "median_bin": median_hist_bin,
                    "current_min": item["price"],
                    "drop_pct": discount_pct,
                    "title": item["title"],
                    "url": item["url"],
                }
            )
    for item in auction_items:
        if item["id"] not in seen_auction_set:
            seen_auctions.append(item["id"])
            seen_auction_set.add(item["id"])
    cache["seen_auction_ids"] = seen_auctions[-SEEN_AUCTION_CAP:]

    for alert in alerts:
        alert.update(source=source, currency=currency)
    save_cache(search_id, cache)
    return cache, alerts


def format_alert(alert: dict) -> str:
    flag = "🔴 **HIGH PRIORITY** — " if alert.get("priority") == "high" else ""
    tags = f" [{', '.join(alert['tags'])}]" if alert.get("tags") else ""
    currency = alert.get("currency", "USD")
    money = lambda value: f"{currency} {value:,.0f}" if currency == "JPY" else f"{currency} {value:,.2f}"
    source = "Yahoo Auctions" if alert.get("source") == "yahoo_auctions" else "eBay"
    suffix = f" · {source} · search `{alert['search_id']}`{tags}"
    if alert["kind"] == "yahoo_new_auction":
        ends = f" · ends {alert['ends_at']}" if alert.get("ends_at") else ""
        return (f"- {flag}🔨 **{alert['search_query']}** new auction: {money(alert['current_min'])} bid — "
                f"[{alert['title']}]({alert['url']}){ends}{suffix}")
    if alert["kind"] == "new_auction":
        return (
            f"- {flag}🔨 **{alert['search_query']}** auction below median BIN history: "
            f"{money(alert['current_min'])} bid vs {money(alert['median_bin'])} median BIN "
            f"({alert['drop_pct']:.1f}% under) — [{alert['title']}]({alert['url']}) · "
            f"{source} · search `{alert['search_id']}`{tags}"
        )
    return (
        f"- {flag}🔻 **{alert['search_query']}** min BIN dropped "
        f"{alert['drop_pct']:.1f}% ({money(alert['prior_min'])} → {money(alert['current_min'])}) — "
        f"[{alert['title']}]({alert['url']}){suffix}"
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
            json={"title": f"Auction watch alert — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", "body": body, "labels": [ISSUE_LABEL]},
        )
        return

    existing = github_request("GET", f"/issues?labels={ISSUE_LABEL}&state=open", gh_token).json()
    if existing:
        issue = existing[0]
        issue_number = issue["number"]
        comment_count = issue.get("comments", 0)
        if comment_count >= MAX_ISSUE_COMMENTS:
            github_request("PATCH", f"/issues/{issue_number}", gh_token, json={"state": "closed"})
            github_request(
                "POST",
                "/issues",
                gh_token,
                json={"title": "Auction watch alerts", "body": body, "labels": [ISSUE_LABEL]},
            )
        else:
            github_request("POST", f"/issues/{issue_number}/comments", gh_token, json={"body": body})
            if comment_count + 1 >= MAX_ISSUE_COMMENTS:
                github_request("PATCH", f"/issues/{issue_number}", gh_token, json={"state": "closed"})
    else:
        github_request(
            "POST",
            "/issues",
            gh_token,
            json={"title": "Auction watch alerts", "body": body, "labels": [ISSUE_LABEL]},
        )


def main() -> None:
    gh_token = os.environ.get("GITHUB_TOKEN")

    searches = yaml.safe_load(CONFIG_PATH.read_text()) or []
    validate_searches(searches)
    if MAX_RESULTS <= 0:
        raise ValueError("MAX_RESULTS must be positive")
    token = None
    token_error = None
    if any(source_of(search) == "ebay" for search in searches):
        try:
            token = get_ebay_token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
        except (KeyError, requests.RequestException) as exc:
            token_error = f"eBay authentication failed ({type(exc).__name__})"

    all_alerts = []
    summary_lines = ["# Auction watch run\n"]
    for search in searches:
        try:
            if source_of(search) == "ebay" and token_error:
                raise ValueError(token_error)
            cache, alerts = process_search(token, search, os.environ.get("FIRECRAWL_KEY"))
        except (requests.RequestException, ValueError) as e:
            # Do not log response bodies or request headers (credentials).
            detail = str(e) if isinstance(e, ValueError) else type(e).__name__
            print(f"[{search['id']}] {source_of(search)} error: {detail}", file=sys.stderr)
            summary_lines.append(f"- `{search['id']}`: ERROR {detail}\n")
            continue

        if search_type_of(search) == "generic":
            today = datetime.now(timezone.utc).date().isoformat()
            record = cache["discovery_days"][today]
            warning = " — fetch limit reached; coverage incomplete" if record["limited"] else ""
            summary_lines.append(f"- `{search['id']}` (generic): {len(record['items'])} new matches in daily digest{warning}\n")
            continue
        latest = cache["history"][-1]
        price_drops = [a for a in alerts if a["kind"] == "price_drop"]
        new_auctions = [a for a in alerts if a["kind"] in ("new_auction", "yahoo_new_auction")]
        note = ""
        if price_drops:
            note += f" — 🔻 {price_drops[0]['drop_pct']:.1f}% BIN drop"
        if new_auctions:
            note += f" — 🔨 {len(new_auctions)} new auction alert(s)"
        summary_lines.append(
            f"- `{search['id']}` ({source_of(search)}, {cache['currency']}): {latest['bin_count']} BIN / {latest['total_count']} total, "
            f"min {latest['bin_min']}, max {latest['bin_max']}, median {latest['bin_median']}"
            f"{note}\n"
        )
        all_alerts.extend(alerts)

    all_alerts.sort(key=lambda a: (0 if a.get("priority") == "high" else 1, -a["drop_pct"]))

    if any(search_type_of(search) == "generic" for search in searches):
        summary_lines.append("\nDaily listing digest: `ebaywatcher/new-listings.html` on the Pages site.\n")
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
        body_lines = [f"### Auction watch alerts\n_{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}_\n"]
        body_lines.extend(format_alert(a) for a in all_alerts)
        upsert_issue(gh_token, "\n".join(body_lines))
    elif all_alerts:
        print("Alerts found but no GITHUB_TOKEN set, skipping issue creation.", file=sys.stderr)


if __name__ == "__main__":
    main()
