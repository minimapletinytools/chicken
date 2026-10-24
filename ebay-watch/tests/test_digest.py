from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_watch import SEARCH, auction, watch, chart
from listing_digest import render_digest, update_discoveries
from search_config import result_limit, search_type_of, validate_searches

GENERIC = {**SEARCH, "search_type": "generic", "tags": ["japanese-tools"]}
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


class DigestTests(unittest.TestCase):
    def test_config_defaults_validation_and_seven_tool_searches(self):
        self.assertEqual(search_type_of(SEARCH), "specific")
        self.assertEqual(result_limit(GENERIC, 50), 500)
        self.assertEqual(result_limit({**GENERIC, "max_results": 1000}, 50), 1000)
        for value in [{**GENERIC, "search_type": "both"}, {**GENERIC, "max_results": 0},
                      {**GENERIC, "max_results": True}, {**GENERIC, "source": "ebay", "max_results": 201}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_searches([value])
        searches = watch.yaml.safe_load(watch.CONFIG_PATH.read_text())
        tools = [s for s in searches if s.get("tags") == ["japanese-tools"]]
        self.assertEqual(len(tools), 7)
        self.assertTrue(all(search_type_of(s) == "generic" for s in tools))

    def test_baseline_all_listing_types_and_same_day_merge(self):
        cache = {}
        update_discoveries(cache, [auction()], NOW, {})
        self.assertEqual(cache["discovery_days"]["2026-10-24"]["items"], [])
        fixed = {**auction("fixed"), "buying_option": "FIXED_PRICE"}
        update_discoveries(cache, [auction(), auction("new-bid"), fixed], NOW + timedelta(hours=1), {})
        record = cache["discovery_days"]["2026-10-24"]
        self.assertEqual({i["id"] for i in record["items"]}, {"new-bid", "fixed"})
        update_discoveries(cache, [auction()], NOW + timedelta(hours=2), {})
        self.assertEqual(len(record["items"]), 2)  # Reruns do not erase the daily digest.

    def test_retention_and_large_seen_set(self):
        cache = {}
        items = [auction(str(n)) for n in range(700)]
        update_discoveries(cache, items, NOW, {})
        update_discoveries(cache, items, NOW + timedelta(days=1), {})
        self.assertEqual(len(cache["seen_listings"]), 700)
        self.assertEqual(cache["discovery_days"]["2026-10-25"]["items"], [])
        update_discoveries(cache, items, NOW + timedelta(days=8), {})
        self.assertNotIn("2026-10-24", cache["discovery_days"])
        self.assertEqual(len(cache["seen_listings"]), 700)

    def test_limit_warning_survives_same_day_rerun(self):
        cache = {}
        update_discoveries(cache, [], NOW, {"limited": True})
        update_discoveries(cache, [], NOW + timedelta(hours=1), {"limited": False})
        self.assertTrue(cache["discovery_days"]["2026-10-24"]["limited"])

    def test_generic_processing_bypasses_price_alerts(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(watch, "CACHE_DIR", Path(temp)), \
                patch.object(watch, "search_yahoo", return_value=[auction(buyout=10000)]) as yahoo:
            cache, alerts = watch.process_search(None, GENERIC, "test")
            yahoo.assert_called_with("test", GENERIC, 500)
            self.assertEqual(alerts, [])
            self.assertEqual(cache["history"], [])
            with patch.object(watch, "search_yahoo", return_value=[auction("new", buyout=100)]):
                cache, alerts = watch.process_search(None, GENERIC, "test")
            self.assertEqual(alerts, [])
            record = next(iter(cache["discovery_days"].values()))
            self.assertEqual([i["id"] for i in record["items"]], ["new"])

    def test_switch_from_specific_seeds_generic_independently(self):
        cache = {"search_type": "specific", "last_checked": NOW.isoformat(), "history": [{"bin_min": 5000}]}
        update_discoveries(cache, [auction()], NOW, {})
        self.assertEqual(cache["discovery_days"]["2026-10-24"]["items"], [])
        self.assertEqual(cache["history"], [{"bin_min": 5000}])

    def test_render_links_escaping_staleness_and_exclusion_from_chart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = {"source": "yahoo_auctions", "currency": "JPY"}
            update_discoveries(cache, [], NOW - timedelta(days=1), {})
            update_discoveries(cache, [{**auction(), "title": "<script>unsafe</script>"}], NOW, {"limited": True})
            (root / "yahoo-test.json").write_text(json.dumps(cache))
            html = render_digest([GENERIC], root, NOW)
            self.assertIn("1 new matches today", html)
            self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", html)
            self.assertNotIn("<script>unsafe", html)
            self.assertIn(auction()["url"], html)
            self.assertIn("Fetch limit reached", html)
            self.assertIn("JPY 1,000 bid", html)
            stale = render_digest([GENERIC], root, NOW + timedelta(days=1))
            self.assertIn("Not updated today", stale)
            config = root / "searches.yml"
            config.write_text(json.dumps([GENERIC]))
            with patch.object(chart, "CONFIG_PATH", config), patch.object(chart, "CACHE_DIR", root):
                self.assertEqual(chart.build_dataset(), [])

    def test_ebay_generic_uses_newest_for_both_calls(self):
        search = {**GENERIC, "source": "ebay"}
        with patch.object(watch, "fetch_ebay_page", return_value=([], 0)) as fetch:
            watch.search_ebay("token", search)
        self.assertEqual([call.args[3] for call in fetch.call_args_list], ["newlyListed", "newlyListed"])

    def test_generic_run_to_published_artifacts_without_issue_alerts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "searches.yml"
            config.write_text(json.dumps([GENERIC]))
            summary = root / "summary.md"
            output = root / "docs" / "index.html"
            with patch.object(watch, "CONFIG_PATH", config), patch.object(watch, "CACHE_DIR", root), \
                    patch.object(watch, "search_yahoo", side_effect=[[auction()], [auction(), auction("new")]]), \
                    patch.object(watch, "upsert_issue") as issue, \
                    patch.dict("os.environ", {"FIRECRAWL_KEY": "test", "GITHUB_TOKEN": "test", "GITHUB_STEP_SUMMARY": str(summary)}, clear=True):
                watch.main()
                watch.main()
                issue.assert_not_called()
            self.assertIn("1 new matches in daily digest", summary.read_text())
            with patch.object(chart, "CONFIG_PATH", config), patch.object(chart, "CACHE_DIR", root), \
                    patch.object(chart, "OUTPUT_PATH", output):
                chart.main()
            self.assertIn("new-listings.html", output.read_text())
            digest = output.with_name("new-listings.html").read_text()
            self.assertIn(auction("new")["url"], digest)
            self.assertIn("japanese-tools", digest)


if __name__ == "__main__":
    unittest.main()
