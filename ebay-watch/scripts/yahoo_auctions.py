"""Yahoo Auctions Japan search, fetched exclusively through Firecrawl."""
from datetime import datetime, timezone
import re
import sys

from bs4 import BeautifulSoup
import requests

from search_config import search_url

FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"


def parse_page(html: str) -> tuple[list[dict], int, int]:
    soup = BeautifulSoup(html, "html.parser")
    # Parse visible prices, not tracking attributes, which may exclude tax.
    titles = soup.select("#searchItems [data-grid-item-title]")
    counts = re.findall(r"([\d,]+)\s*[〜～–-]\s*([\d,]+)件\s*/\s*([\d,]+)件", soup.get_text(" ", strip=True))
    if not titles:
        text = soup.get_text(" ", strip=True)
        compact_text = soup.get_text("", strip=True)
        if re.search(r"すべて\s*0件", text) and any(marker in compact_text for marker in (
            "一致する商品はありません", "該当する商品はありません",
            "検索条件に一致する商品が見つかりません", "条件に一致する商品は見つかりませんでした",
        )):
            return [], 0, 1
        raise ValueError("Yahoo page has no recognized search results; cache was not updated")
    if not counts:
        raise ValueError("Yahoo result count missing; page may be incomplete")
    start, end_position, total = (int(value.replace(",", "")) for value in counts[0])
    if len(titles) != end_position - start + 1:
        raise ValueError("Yahoo result page is incomplete; cache was not updated")
    items = {}
    for title in titles:
        link = title.find_parent("a")
        if link is None or "_cl_vmodule:aal;" not in link.get("data-cl-params", ""):
            raise ValueError("Yahoo result link is missing or unrecognized")
        if not title.get_text(strip=True):
            raise ValueError("Yahoo listing title is missing")
        match = re.fullmatch(r"https://auctions\.yahoo\.co\.jp/jp/auction/([a-zA-Z0-9]+)", link.get("href", ""))
        if not match:
            if link.get("href", "").startswith("https://paypayfleamarket.yahoo.co.jp/item/"):
                continue  # Yahoo mixes flea-market listings into its results.
            raise ValueError("Yahoo listing URL is missing or unrecognized")
        prices = {}
        for paragraph in link.find_all("p"):
            # Join adjacent spans without spaces (17,170<span>円</span>).
            price = re.fullmatch(r"(現在|即決)\s*([\d,]+)円", paragraph.get_text("", strip=True))
            if price:
                prices[price[1]] = int(price[2].replace(",", ""))
        if not prices or any(value <= 0 for value in prices.values()):
            raise ValueError("Yahoo listing price is missing or invalid")
        end = re.search(r";end:(\d+);", link.get("data-cl-params", ""))
        items[match[1]] = {
            "id": match[1], "title": title.get_text(strip=True), "url": link["href"],
            "price": prices.get("現在", prices.get("即決")), "currency": "JPY",
            "buying_option": "AUCTION" if "現在" in prices else "FIXED_PRICE",
            "buyout_price": prices.get("即決"),
            "ends_at": datetime.fromtimestamp(int(end[1]), timezone.utc).isoformat() if end else None,
        }
    return list(items.values()), total, end_position + 1


def search_yahoo(api_key: str, search: dict, max_results: int) -> list[dict]:
    items = {}
    offset = 1
    total = None
    # Yahoo's supported page sizes are 20, 50 and 100. Use 50, then cap locally.
    while offset <= max_results:
        response = requests.post(
            FIRECRAWL_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": search_url(search, n=50, b=offset, s1="new", o1="d"),
                  "formats": ["html"], "location": {"country": "JP", "languages": ["ja-JP"]},
                  "maxAge": 0},
            timeout=120,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise ValueError("Firecrawl returned an invalid response")
        data = result.get("data") or {}
        if not result.get("success") or data.get("metadata", {}).get("statusCode", 200) != 200:
            raise ValueError("Firecrawl did not return a successful Yahoo page")
        if not isinstance(data.get("html"), str):
            raise ValueError("Firecrawl returned no Yahoo HTML")
        page_items, total, next_offset = parse_page(data.get("html", ""))
        if next_offset <= offset and total:
            raise ValueError("Yahoo pagination repeated a page; cache was not updated")
        items.update((item["id"], item) for item in page_items)
        if total == 0 or next_offset > total:
            break
        offset = next_offset
    fetched = list(items.values())[:max_results]
    search["_coverage"] = {"limited": total > max_results, "total": total, "fetched": len(fetched)}
    print(f"[{search['id']}] fetched {len(fetched)}/{total} Yahoo listing(s)")
    if total > len(fetched):
        print(f"[{search['id']}] WARNING: Yahoo results capped at MAX_RESULTS={max_results}; prices describe the newest fetched listings", file=sys.stderr)
    excludes = [word.lower() for word in search.get("exclude_keywords", [])]
    # Retain an affordable bid even when its optional buyout is above the ceiling.
    return [item for item in fetched
            if not any(word in item["title"].lower() for word in excludes)
            and (search.get("max_price") is None or item["price"] <= search["max_price"])]
