"""Offline regression tests. Run: python -m unittest discover -s ebay-watch/tests."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ebay_watch as watch
import generate_chart as chart
from search_config import source_of, validate_searches
from yahoo_auctions import parse_page, search_yahoo

FIXTURE = (Path(__file__).parent / "fixtures" / "yahoo_results.html").read_text()
SEARCH = {"id": "yahoo-test", "source": "yahoo_auctions", "query": "Spyderco"}


def auction(item_id="auction-1", price=1000, buyout=None):
    return {"id": item_id, "title": "Test knife", "price": price, "currency": "JPY",
            "buying_option": "AUCTION", "buyout_price": buyout,
            "url": "https://auctions.yahoo.co.jp/jp/auction/" + item_id, "ends_at": None}


class ConfigTests(unittest.TestCase):
    def test_source_defaults_and_exclusive_values(self):
        validate_searches([{"id": "old-ebay", "query": "Dodo"}, SEARCH])
        self.assertEqual(source_of({}), "ebay")
        for source in ["both", ["ebay", "yahoo_auctions"], None]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_searches([{**SEARCH, "source": source}])

    def test_bad_filters_ids_and_duplicate_ids(self):
        for value in [{**SEARCH, "condition": "NEW"}, {**SEARCH, "id": "../outside"},
                      {**SEARCH, "max_price": "30000"}, {**SEARCH, "max_price": float("nan")},
                      {**SEARCH, "query": ""}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_searches([value])
        with self.assertRaises(ValueError):
            validate_searches([SEARCH, SEARCH])


class YahooTests(unittest.TestCase):
    def test_real_html_prices_tax_and_dual_format(self):
        items, total, offset = parse_page(FIXTURE)
        self.assertEqual((len(items), total, offset), (3, 254, 5))
        self.assertEqual(items[1]["price"], 26990)  # Displayed tax-inclusive price.
        self.assertEqual(items[0]["buying_option"], "FIXED_PRICE")
        self.assertEqual((items[2]["price"], items[2]["buyout_price"]), (9250, 27000))
        self.assertEqual(items[2]["buying_option"], "AUCTION")
        self.assertIsNotNone(items[2]["ends_at"])

    def test_blocked_incomplete_and_missing_prices_fail(self):
        for html in ["<h1>Access denied</h1>", "<h1>ログイン</h1>",
                     FIXTURE.replace("1〜4件", "1〜5件"), FIXTURE.replace("即決", "unknown")]:
            with self.subTest(html=html[:60]), self.assertRaises(ValueError):
                parse_page(html)

    def test_confirmed_empty(self):
        self.assertEqual(parse_page("<p>すべて 0件</p><p>条件に一致する商品は <br>見つかりませんでした</p>"), ([], 0, 1))

    @patch("yahoo_auctions.requests.post")
    def test_invalid_firecrawl_responses_fail(self, post):
        for result in [[], {"success": False}, {"success": True, "data": {"html": None}},
                       {"success": True, "data": {"html": FIXTURE, "metadata": {"statusCode": 403}}}]:
            post.return_value.json.return_value = result
            with self.subTest(result=result), self.assertRaises(ValueError):
                search_yahoo("test", SEARCH, 3)

    @patch("yahoo_auctions.requests.post")
    def test_firecrawl_request_and_filters(self, post):
        post.return_value.json.return_value = {"success": True, "data": {"html": FIXTURE, "metadata": {"statusCode": 200}}}
        items = search_yahoo("test-key", {**SEARCH, "max_price": 10000}, 3)
        self.assertEqual([item["price"] for item in items], [9250])
        request = post.call_args.kwargs
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(request["json"]["maxAge"], 0)
        self.assertIn("p=Spyderco", request["json"]["url"])
        self.assertEqual(search_yahoo("test-key", {**SEARCH, "exclude_keywords": ["SPYDERCO"]}, 3), [])

    @patch("yahoo_auctions.requests.post")
    def test_pagination_and_repeated_page(self, post):
        data = lambda html: {"success": True, "data": {"html": html}}
        second = FIXTURE.replace("1〜4件 / 254件", "5〜8件 / 8件").replace("1249555497", "1249555498")
        post.return_value.json.side_effect = [data(FIXTURE), data(second)]
        self.assertEqual(len(search_yahoo("test", SEARCH, 8)), 4)  # Deduplicates overlapping rows.
        self.assertIn("b=5", post.call_args.kwargs["json"]["url"])
        post.return_value.json.side_effect = [data(FIXTURE), data(FIXTURE)]
        with self.assertRaisesRegex(ValueError, "repeated"):
            search_yahoo("test", SEARCH, 8)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(watch, "CACHE_DIR", Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_yahoo(self, items, search=SEARCH):
        with patch.object(watch, "search_yahoo", return_value=items), patch.object(watch, "search_ebay") as ebay:
            result = watch.process_search(None, search, "test-key")
            ebay.assert_not_called()
            return result

    def test_seed_new_auction_once_without_bin_history(self):
        cache, alerts = self.run_yahoo([auction()])
        self.assertEqual(alerts, [])
        self.assertIsNone(cache["history"][-1]["bin_min"])
        cache, alerts = self.run_yahoo([auction(), auction("auction-2")])
        self.assertEqual([alert["kind"] for alert in alerts], ["yahoo_new_auction"])
        self.assertEqual(self.run_yahoo([auction(), auction("auction-2")])[1], [])
        self.assertEqual(cache["currency"], "JPY")
        self.assertIn("JPY 1,000", watch.format_alert(alerts[0]))

    def test_buyout_drop_and_ceiling_independent_of_bid(self):
        self.run_yahoo([auction(buyout=5000)])
        cache, alerts = self.run_yahoo([auction(buyout=3000)])
        self.assertEqual([alert["kind"] for alert in alerts], ["price_drop"])
        self.assertEqual(cache["history"][-1]["bin_min"], 3000)
        self.assertEqual(cache["history"][-1]["auction_min"], 1000)
        cache, _ = self.run_yahoo([auction(buyout=10000)], {**SEARCH, "max_price": 2000})
        self.assertIsNone(cache["history"][-1]["bin_min"])

    def test_scrape_failure_preserves_cache(self):
        self.run_yahoo([auction()])
        path = watch.CACHE_DIR / "yahoo-test.json"
        before = path.read_bytes()
        with patch.object(watch, "search_yahoo", side_effect=ValueError("blocked")), self.assertRaises(ValueError):
            watch.process_search(None, SEARCH, "test-key")
        self.assertEqual(path.read_bytes(), before)

    def test_source_and_currency_changes_rejected(self):
        cache, _ = self.run_yahoo([auction()])
        with self.assertRaisesRegex(ValueError, "source differs"):
            watch.process_search("token", {**SEARCH, "source": "ebay"})
        changed = auction()
        changed["currency"] = "USD"
        with self.assertRaisesRegex(ValueError, "currency differs"):
            self.run_yahoo([changed])
        self.assertEqual(watch.load_cache(SEARCH["id"]), cache)

    def test_old_ebay_cache_and_alerts_preserved(self):
        search = {"id": "old-ebay", "query": "Dodo"}
        cache = {"search_id": search["id"], "all_time_min_bin_price": 100,
                 "history": [], "seen_auction_ids": [], "last_checked": datetime.now(timezone.utc).isoformat()}
        watch.save_cache(search["id"], cache)
        item = {**auction(), "buying_option": "FIXED_PRICE", "price": 70, "currency": "USD"}
        with patch.object(watch, "search_ebay", return_value=[item]), patch.object(watch, "search_yahoo") as yahoo:
            cache, alerts = watch.process_search("token", search)
            yahoo.assert_not_called()
        self.assertEqual(alerts[0]["kind"], "price_drop")
        self.assertEqual(cache["all_time_min_bin_price"], 70)
        self.assertEqual(cache["source"], "ebay")

    def test_yahoo_only_main_never_authenticates_ebay(self):
        path = Path(self.temp.name) / "searches.yml"
        path.write_text(json.dumps([SEARCH]))
        with patch.object(watch, "CONFIG_PATH", path), patch.object(watch, "get_ebay_token") as token, \
                patch.object(watch, "search_yahoo", return_value=[]), \
                patch.dict("os.environ", {"FIRECRAWL_KEY": "test"}, clear=True):
            watch.main()
            token.assert_not_called()

    def test_ebay_failure_does_not_stop_yahoo(self):
        path = Path(self.temp.name) / "searches.yml"
        path.write_text(json.dumps([{"id": "ebay-test", "query": "Dodo"}, SEARCH]))
        with patch.object(watch, "CONFIG_PATH", path), \
                patch.object(watch, "get_ebay_token", side_effect=watch.requests.ConnectionError), \
                patch.object(watch, "search_yahoo", return_value=[]) as yahoo, \
                patch.dict("os.environ", {"FIRECRAWL_KEY": "test", "EBAY_CLIENT_ID": "test", "EBAY_CLIENT_SECRET": "test"}, clear=True):
            watch.main()
            yahoo.assert_called_once()
        self.assertTrue((watch.CACHE_DIR / "yahoo-test.json").exists())

    def test_ebay_auction_still_requires_bin_history(self):
        search = {"id": "ebay-test", "query": "Dodo"}
        fixed = {**auction("bin"), "buying_option": "FIXED_PRICE", "price": 100, "currency": "USD"}
        bid = {**auction(), "price": 50, "currency": "USD"}
        with patch.object(watch, "search_ebay", return_value=[fixed]):
            watch.process_search("token", search)
        with patch.object(watch, "search_ebay", return_value=[fixed, bid]):
            _, alerts = watch.process_search("token", search)
            self.assertEqual(alerts[0]["kind"], "new_auction")
            self.assertEqual(watch.process_search("token", search)[1], [])

    def test_even_empty_legacy_cache_cannot_change_source(self):
        watch.save_cache(SEARCH["id"], {"search_id": SEARCH["id"], "all_time_min_bin_price": None,
                                       "history": [], "last_checked": None})
        with self.assertRaisesRegex(ValueError, "source differs"):
            self.run_yahoo([])

    def test_chart_source_currency_links(self):
        self.run_yahoo([auction()])
        path = Path(self.temp.name) / "searches.yml"
        path.write_text(json.dumps([SEARCH]))
        with patch.object(chart, "CONFIG_PATH", path), patch.object(chart, "CACHE_DIR", watch.CACHE_DIR):
            data = chart.build_dataset()[0]
        self.assertEqual(data["currency"], "JPY")
        self.assertEqual(data["source_label"], "Yahoo Auctions")
        self.assertIn("auctions.yahoo.co.jp", data["search_url"])


if __name__ == "__main__":
    unittest.main()
