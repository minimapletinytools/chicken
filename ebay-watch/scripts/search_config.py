"""Shared watch configuration and marketplace links."""
import math
import re
from urllib.parse import urlencode


def source_of(search: dict) -> str:
    return search.get("source", "ebay")


def search_type_of(search: dict) -> str:
    return search.get("search_type", "specific")


def result_limit(search: dict, default: int) -> int:
    fallback = 500 if search_type_of(search) == "generic" and source_of(search) == "yahoo_auctions" else default
    return search.get("max_results", fallback)


def search_url(search: dict, **params) -> str:
    if source_of(search) == "yahoo_auctions":
        return "https://auctions.yahoo.co.jp/search/search?" + urlencode({"p": search["query"], **params})
    return "https://www.ebay.com/sch/i.html?" + urlencode({"_nkw": search["query"]})


def validate_searches(searches) -> None:
    if not isinstance(searches, list):
        raise ValueError("Search config must be a YAML list")
    ids = set()
    for search in searches:
        if not isinstance(search, dict):
            raise ValueError("Each search must be a mapping")
        search_id = search.get("id")
        if not isinstance(search_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", search_id):
            raise ValueError("Each search needs a safe, stable id (letters, numbers, hyphens, underscores)")
        if search_id in ids:
            raise ValueError(f"Duplicate search id: {search_id}")
        ids.add(search_id)
        if not isinstance(search.get("query"), str) or not search["query"].strip():
            raise ValueError(f"[{search_id}] query must be nonempty text")
        source = source_of(search)
        if source not in ("ebay", "yahoo_auctions"):
            raise ValueError(f"[{search_id}] source must be ebay or yahoo_auctions")
        if search_type_of(search) not in ("specific", "generic"):
            raise ValueError(f"[{search_id}] search_type must be specific or generic")
        if "max_results" in search:
            limit = search["max_results"]
            ceiling = 10000 if source == "yahoo_auctions" else 200
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ceiling:
                raise ValueError(f"[{search_id}] max_results must be an integer between 1 and {ceiling}")
        if source == "yahoo_auctions":
            for field in ("marketplace", "category_ids", "condition"):
                if field in search:
                    raise ValueError(f"[{search_id}] {field} is only supported for eBay")
        for field in ("max_price", "price_drop_threshold_pct"):
            if field in search:
                value = search[field]
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"[{search_id}] {field} must be a positive number")
        for field in ("tags", "exclude_keywords"):
            if field in search and (not isinstance(search[field], list) or
                                    any(not isinstance(v, str) for v in search[field])):
                raise ValueError(f"[{search_id}] {field} must be a list of strings")


def check_cache_source(cache: dict, search: dict) -> None:
    # Historical caches without metadata are eBay caches.
    if cache.get("source", "ebay") != source_of(search):
        raise ValueError(f"[{search['id']}] source differs from cached history; use a new id")
