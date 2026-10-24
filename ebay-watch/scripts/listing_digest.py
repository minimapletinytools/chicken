"""Daily discovery history and a static inbox for broad marketplace searches."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json

from search_config import check_cache_source, search_type_of, search_url, source_of

DIGEST_DAYS = 7
SEEN_DAYS = 90


def update_discoveries(cache: dict, items: list[dict], now: datetime, coverage: dict) -> None:
    timestamp = now.isoformat()
    today = now.date().isoformat()
    seeded = cache.get("search_type") == "generic" and cache.get("discovery_seeded", False)
    cutoff = (now - timedelta(days=SEEN_DAYS)).isoformat()
    seen = {item_id: last_seen for item_id, last_seen in cache.get("seen_listings", {}).items()
            if last_seen >= cutoff}
    new_items = {item["id"]: dict(item, first_seen=timestamp)
                 for item in items if seeded and item["id"] not in seen}
    seen.update((item["id"], timestamp) for item in items)
    day_cutoff = (now.date() - timedelta(days=DIGEST_DAYS - 1)).isoformat()
    days = {day: record for day, record in cache.get("discovery_days", {}).items() if day >= day_cutoff}
    record = days.setdefault(today, {"items": [], "baseline": not seeded, "limited": False})
    # Pushes/manual reruns on the same date append without losing the morning's discoveries.
    combined = {item["id"]: item for item in record["items"]}
    combined.update(new_items)
    record.update(items=list(combined.values()), checked_at=timestamp,
                  limited=record["limited"] or coverage.get("limited", False),
                  fetched=coverage.get("fetched", len(items)), total=coverage.get("total"))
    cache.update(search_type="generic", discovery_seeded=True, seen_listings=seen,
                 discovery_days=days, last_checked=timestamp)


def money(value, currency: str) -> str:
    if value is None:
        return "—"
    return f"{currency} {value:,.0f}" if currency == "JPY" else f"{currency} {value:,.2f}"


def safe_link(url: str | None) -> str:
    return escape(url, quote=True) if isinstance(url, str) and url.startswith("https://") else "#"


def render_digest(searches: list[dict], cache_dir, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    records = []
    for search in searches:
        if search_type_of(search) != "generic":
            continue
        path = cache_dir / f"{search['id']}.json"
        cache = json.loads(path.read_text()) if path.exists() else {}
        if cache:
            check_cache_source(cache, search)
        records.append((search, cache))
    body = ["<h1>New listings</h1>",
            '<p><a href="index.html">Specific-item price history</a></p>',
            f"<p>Updated {escape(now.strftime('%Y-%m-%d %H:%M UTC'))}. New means first observed after the initial baseline.</p>",
            "<p>Browse by day and search. Prices are snapshots; open the listing for the current bid. All dates are UTC.</p>",
            '<button type="button" onclick="document.querySelectorAll(\'details\').forEach(d => d.open = true)">Expand all</button>',
            '<button type="button" onclick="document.querySelectorAll(\'details\').forEach(d => d.open = false)">Collapse all</button>',
            "<h2>Search status</h2><ul>"]
    for search, cache in records:
        record = cache.get("discovery_days", {}).get(today)
        label = escape(search["query"])
        link = safe_link(search_url(search))
        if record:
            status = f"{len(record['items'])} new matches today"
            status += f"; last checked {record['checked_at']}"
            if record.get("baseline"):
                status += "; initial baseline recorded"
        else:
            status = "Not updated today — " + (f"last checked {cache['last_checked']}" if cache.get("last_checked") else "waiting for first successful run")
        body.append(f'<li><a href="{link}" target="_blank" rel="noopener">{label}</a>: {escape(status)}</li>')
    body.append("</ul>")
    for offset in range(DIGEST_DAYS):
        day = (now.date() - timedelta(days=offset)).isoformat()
        groups = [(search, cache.get("discovery_days", {}).get(day)) for search, cache in records]
        count = sum(len(record["items"]) for _, record in groups if record)
        body.append(f'<details class="day" {"open" if offset == 0 else ""}><summary>{day} · {count} new matches</summary>')
        if not any(record for _, record in groups):
            body.append("<p>No successful runs recorded for this day.</p>")
        for search, record in groups:
            if not record:
                continue
            label = escape(search["query"])
            tags = escape(", ".join(search.get("tags", [])))
            source = "Yahoo Auctions" if source_of(search) == "yahoo_auctions" else "eBay"
            body.append(f'<details class="search"><summary>{label} · {len(record["items"])} new · {tags} · {source}</summary>')
            body.append(f'<p><a href="{safe_link(search_url(search))}" target="_blank" rel="noopener">Open full search</a></p>')
            if record.get("baseline"):
                body.append("<p>Initial baseline: existing listings were marked as seen.</p>")
            if record.get("limited"):
                body.append('<p class="warning">Fetch limit reached: this digest may omit listings. Increase max_results or narrow this search.</p>')
            items = record["items"]
            if not items:
                body.append("<p>No new listings observed.</p>")
            else:
                body.append('<div class="table"><table><thead><tr><th>Listing</th><th>Bid / price</th><th>Buyout</th><th>Ends (UTC)</th></tr></thead><tbody>')
                for item in sorted(items, key=lambda item: (item.get("ends_at") or "9999", item["title"])):
                    currency = item.get("currency", "JPY" if source_of(search) == "yahoo_auctions" else "USD")
                    price = money(item["price"], currency)
                    if item.get("buying_option") == "AUCTION":
                        price += " bid"
                    end = item.get("ends_at") or "—"
                    body.append(f'<tr><td><a href="{safe_link(item.get("url"))}" target="_blank" rel="noopener">{escape(item["title"])}</a></td>'
                                f'<td>{escape(price)}</td><td>{escape(money(item.get("buyout_price"), currency))}</td><td>{escape(end)}</td></tr>')
                body.append("</tbody></table></div>")
            body.append("</details>")
        body.append("</details>")
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>New listings — Auction Watch</title>
<style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:auto;padding:24px;background:#111;color:#eee}
a{color:#8ab4f8}summary{cursor:pointer;padding:12px;font-weight:600}details{border:1px solid #444;border-radius:6px;margin:12px 0}
.search{margin:12px}.search p{padding:0 12px}.table{overflow-x:auto}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:12px;border-top:1px solid #333}td:first-child{min-width:240px;width:55%}
td:not(:first-child){white-space:nowrap}button{padding:8px;cursor:pointer}.warning{color:#ffd166}</style>
</head><body>""" + "\n".join(body) + "</body></html>"
