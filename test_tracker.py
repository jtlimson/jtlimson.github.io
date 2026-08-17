import csv
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import tracker
import pi_collector
import publish_site


class TrackerTests(unittest.TestCase):
    def test_publish_manifest_excludes_private_log_and_homepage(self):
        self.assertNotIn("data/collector.log", publish_site.PUBLIC_FILES)
        self.assertNotIn("index.html", publish_site.PUBLIC_FILES)
        self.assertIn("dashboard.html", publish_site.PUBLIC_FILES)

    def test_mirror_parser_validates_identity_and_reads_market_data(self):
        card = {
            "expected_year": "2021",
            "expected_brand": "POKEMON JAPANESE SWORD & SHIELD EEVEE HEROES",
            "expected_subject": "FA/UMBREON V",
            "expected_card_number": "085",
            "expected_variety": "EEVEE HEROES",
        }
        body = """Item Grade GEM MT 10
PSA Estimate
$506.00
PSA Population
[12,275](https://example.test/pop)
Year 2021
Brand/Title POKEMON JAPANESE SWORD & SHIELD EEVEE HEROES
Subject FA/UMBREON V
Card Number 085
Variety/Pedigree EEVEE HEROES
![Image 2: Cert image 1](https://example.test/front.jpg)
"""
        population, estimate, image_url = pi_collector.parse_psa(body, card)
        self.assertEqual(population, 12275)
        self.assertEqual(estimate, 506.0)
        self.assertEqual(image_url, "https://example.test/front.jpg")

    def test_mirror_parser_rejects_identity_mismatch(self):
        card = {
            "expected_year": "2021", "expected_brand": "EXPECTED",
            "expected_subject": "UMBREON", "expected_card_number": "085",
            "expected_variety": "EEVEE HEROES",
        }
        body = """Item Grade GEM MT 10
PSA Population
[12,275](https://example.test/pop)
Year 2021
Brand/Title WRONG
Subject UMBREON
Card Number 085
Variety/Pedigree EEVEE HEROES
"""
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            pi_collector.parse_psa(body, card)

    def test_snkrdunk_summary_reads_raw_market(self):
        body = ':summary="{&#34;usedListingCount&#34;:12,&#34;usedMinPriceAmount&#34;:42000,&#34;usedMinPriceCurrency&#34;:&#34;JPY&#34;}\n"'
        self.assertEqual(pi_collector.parse_snkrdunk_summary(body), (12, 42000))

    def test_snkrdunk_psa10_parser_rejects_other_grades(self):
        valid = json.dumps({"usedListings": [
            {"condition": "PSA 10", "currency": "JPY", "priceAmount": 86000}
        ]})
        self.assertEqual(len(pi_collector.parse_snkrdunk_psa10_listings(valid)), 1)
        invalid = json.dumps({"usedListings": [
            {"condition": "PSA 9", "currency": "JPY", "priceAmount": 50000}
        ]})
        with self.assertRaisesRegex(ValueError, "invalid SNKRDUNK PSA 10"):
            pi_collector.parse_snkrdunk_psa10_listings(invalid)

    def test_snkrdunk_market_uses_same_currency_prices(self):
        summary = ':summary="{&#34;usedListingCount&#34;:20,&#34;usedMinPriceAmount&#34;:40000,&#34;usedMinPriceCurrency&#34;:&#34;JPY&#34;}"'
        page = json.dumps({"usedListings": [
            {"condition": "PSA 10", "currency": "JPY", "priceAmount": 80000},
            {"condition": "PSA 10", "currency": "JPY", "priceAmount": 90000},
        ]})
        with mock.patch.object(
            pi_collector, "fetch_snkrdunk", side_effect=[summary.encode(), page.encode()]
        ):
            listing_count, spread = pi_collector.read_snkrdunk_market(724996)
        self.assertEqual(listing_count, 2)
        self.assertEqual(spread, 100.0)

    def test_recorded_snapshot_message_includes_price(self):
        self.assertEqual(
            tracker.recorded_snapshot_message("Mega Gengar", 33099, 588.0),
            "Recorded Mega Gengar: PSA 10 population 33,099; PSA estimate $588.00 USD",
        )

    def test_recorded_snapshot_message_marks_missing_price(self):
        self.assertEqual(
            tracker.recorded_snapshot_message("Mega Gengar", 33099, None),
            "Recorded Mega Gengar: PSA 10 population 33,099; PSA estimate n/a",
        )

    def test_change_stats_uses_requested_window(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        rows = [
            {"observed_at": start.isoformat(), "psa10_population": "100"},
            {"observed_at": (start + timedelta(days=7)).isoformat(), "psa10_population": "114"},
        ]
        result = tracker.change_stats(rows, 7)
        self.assertEqual(result["delta"], 14)
        self.assertAlmostEqual(result["pct"], 14.0)
        self.assertAlmostEqual(result["daily"], 2.0)
        self.assertTrue(result["window_complete"])

    def test_change_stats_marks_short_history_as_since_baseline(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        rows = [
            {"observed_at": start.isoformat(), "psa10_population": "100"},
            {"observed_at": (start + timedelta(days=3)).isoformat(), "psa10_population": "106"},
        ]
        result = tracker.change_stats(rows, 30)
        self.assertEqual(result["delta"], 6)
        self.assertFalse(result["window_complete"])

    def test_parse_time_rejects_naive_timestamp(self):
        with self.assertRaises(ValueError):
            tracker.parse_time("2026-08-09T09:00:00")

    def test_estimate_stats_tracks_price_change(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        rows = [
            {"observed_at": start.isoformat(), "psa_estimate_usd": "100.00"},
            {"observed_at": (start + timedelta(days=7)).isoformat(), "psa_estimate_usd": "125.00"},
        ]
        result = tracker.estimate_stats(rows, 7)
        self.assertEqual(result["current"], 125.0)
        self.assertEqual(result["delta"], 25.0)
        self.assertEqual(result["pct"], 25.0)
        self.assertTrue(result["window_complete"])

    def test_estimate_stats_marks_short_history_as_since_baseline(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        rows = [
            {"observed_at": start.isoformat(), "psa_estimate_usd": "100.00"},
            {"observed_at": (start + timedelta(days=3)).isoformat(), "psa_estimate_usd": "105.00"},
        ]
        result = tracker.estimate_stats(rows, 30)
        self.assertEqual(result["delta"], 5.0)
        self.assertFalse(result["window_complete"])

    def test_record_rejects_unknown_card(self):
        args = mock.Mock(card="not-a-card", psa10_pop=1)
        with self.assertRaises(ValueError):
            tracker.record(args)

    def test_aps_normalizes_price_change_to_ten_percent_population_growth(self):
        self.assertAlmostEqual(tracker.calculate_aps(1.2, 10.4), 1.153846, places=6)
        self.assertIsNone(tracker.calculate_aps(-2, 0))
        self.assertIsNone(tracker.calculate_aps(-2, -1))

    def test_aps_regime_boundaries(self):
        cases = [
            (0.01, "Strong"),
            (0, "Healthy"),
            (-3, "Healthy"),
            (-3.01, "Moderate"),
            (-7, "Moderate"),
            (-7.01, "Weak"),
            (-12, "Weak"),
            (-12.01, "Very weak"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(tracker.aps_regime(value), expected)

    def test_asi_zones(self):
        cases = [
            (80, "BUY / HOLD"),
            (65, "ACCUMULATE"),
            (45, "WAIT"),
            (30, "AVOID"),
            (29.99, "WAIT FOR FLOOR"),
        ]
        for value, action in cases:
            with self.subTest(value=value):
                self.assertEqual(tracker.asi_zone(value)[0], action)

    def test_asi_presentation_shows_provisional_score_with_two_observations(self):
        score, action, phase = tracker.asi_presentation(20, True, 3)
        self.assertEqual(score, 20)
        self.assertEqual(action, "CAUTION")
        self.assertEqual(phase, "Provisional weakness")

    def test_asi_presentation_softens_provisional_actions(self):
        cases = [
            (80, "POSITIVE WATCH"),
            (50, "WATCH"),
            (20, "CAUTION"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                score, action, _ = tracker.asi_presentation(value, True, 14)
                self.assertEqual(score, value)
                self.assertEqual(action, expected)

    def test_asi_presentation_keeps_definitive_measured_action(self):
        score, action, phase = tracker.asi_presentation(35, False, None)
        self.assertEqual(score, 35)
        self.assertEqual(action, "AVOID")
        self.assertEqual(phase, "Distribution")

    def test_thirty_day_metrics_require_a_real_thirty_day_baseline(self):
        rows = [
            {"date": "2026-08-10", "card_id": "card", "psa10_price": "100", "psa10_pop": "100"},
            {"date": "2026-08-11", "card_id": "card", "psa10_price": "99", "psa10_pop": "101"},
        ]
        tracker.recompute_market_history(rows)
        self.assertEqual(rows[-1]["pop_change_30d"], "")
        self.assertEqual(rows[-1]["APS"], "")
        self.assertEqual(rows[-1]["ASI"], "")

    def test_short_real_history_produces_unpersisted_provisional_asi(self):
        rows = [
            {"date": "2026-08-09", "card_id": "card", "psa10_price": "100", "psa10_pop": "1000"},
            {"date": "2026-08-12", "card_id": "card", "psa10_price": "99", "psa10_pop": "1005"},
        ]
        tracker.recompute_market_history(rows)
        result = tracker.calculate_asi(rows[-1], rows, 1)
        self.assertTrue(result["provisional"])
        self.assertEqual(result["projection_days"], 3)
        self.assertAlmostEqual(result["available_weight"], 0.6)
        self.assertIsNotNone(result["score"])
        self.assertEqual(result["confidence"], "Low")
        self.assertEqual(rows[-1]["ASI"], "")

    def test_projected_window_change_scales_observed_change(self):
        rows = [
            {"date": "2026-08-01", "psa10_pop": "1000"},
            {"date": "2026-08-11", "psa10_pop": "1010"},
        ]
        change, elapsed = tracker.projected_window_change(rows, 1, 30, "psa10_pop")
        self.assertEqual(elapsed, 10)
        self.assertAlmostEqual(change, 3.0)

    def test_population_comparison_keeps_only_top_ten(self):
        items = [(f"Card {value}", value) for value in range(15)]
        result = tracker.top_population_items(items)
        self.assertEqual(len(result), 10)
        self.assertEqual(result[0], ("Card 14", 14))
        self.assertEqual(result[-1], ("Card 5", 5))

    def test_dashboard_renders_an_asi_panel_for_every_card(self):
        path = Path(__file__).parent / "data" / "_test_dashboard.html"
        about_path = Path(__file__).parent / "data" / "_test_about_asi.html"
        path.unlink(missing_ok=True)
        about_path.unlink(missing_ok=True)
        cards = {
            "card-a": {
                "name": "Card A",
                "psa_url": "https://example.test/a",
                "reference_rank": 1,
                "reference_total_graded": 222343,
            },
            "card-b": {"name": "Card B", "psa_url": "https://example.test/b"},
        }
        market_rows = [
            {
                "date": "2026-08-11",
                "card_id": card_id,
                "psa10_price": "100",
                "raw_price": "70",
                "psa10_pop": "1000",
                "pop_change_30d": "5",
                "sales_7d": "25",
                "sales_30d": "100",
                "lowest_listing": "98",
                "listing_count": "10",
                "raw_psa_spread": "42.86",
                "APS": "1",
                "ASI": "80",
            }
            for card_id in cards
        ]
        try:
            with (
                mock.patch.object(tracker, "DASHBOARD_PATH", path),
                mock.patch.object(tracker, "ABOUT_ASI_PATH", about_path),
                mock.patch.object(tracker, "migrate_population_history"),
                mock.patch.object(tracker, "load_cards", return_value=cards),
                mock.patch.object(tracker, "load_history", return_value=[]),
                mock.patch.object(tracker, "load_market_history", return_value=market_rows),
            ):
                tracker.dashboard(30)
            rendered = path.read_text(encoding="utf-8")
            about_rendered = about_path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)
            about_path.unlink(missing_ok=True)
        self.assertEqual(rendered.count('data-asi-card="true"'), len(cards))
        self.assertEqual(rendered.count('class="component-modal"'), len(cards))
        self.assertEqual(rendered.count('class="component-kpis"'), len(cards))
        self.assertNotIn('class="card-asi-metrics"', rendered)
        self.assertEqual(rendered.count('class="market-chart"'), len(cards))
        self.assertEqual(rendered.count('class="history-modal"'), len(cards))
        self.assertEqual(rendered.count('class="history-preview"'), len(cards))
        self.assertEqual(rendered.count('class="mini-market-chart"'), len(cards))
        self.assertNotIn(">Show graph</button>", rendered)
        self.assertNotIn('class="card-market-history"', rendered)
        self.assertNotIn("<details>", rendered)
        self.assertIn('class="comparison-toggle"', rendered)
        self.assertIn('aria-controls="population-comparison-content"', rendered)
        self.assertIn('comparisonStorageKey="psa-population-comparison-hidden-v2"', rendered)
        self.assertIn("savedComparisonState===null?true", rendered)
        self.assertEqual(rendered.count('class="bookmark-button"'), len(cards))
        self.assertIn('id="card-search"', rendered)
        self.assertIn('data-card-filter="bookmarked"', rendered)
        self.assertIn('id="card-sort"', rendered)
        self.assertIn('<option value="population">PSA 10 population</option>', rendered)
        self.assertIn('<option value="price">PSA estimate</option>', rendered)
        self.assertIn('<option value="asi">ASI</option>', rendered)
        self.assertIn('data-card-id="card-a"', rendered)
        self.assertIn('data-card-name="card a"', rendered)
        self.assertIn('data-asi="80.0"', rendered)
        self.assertIn('bookmarkStorageKey="psa-card-bookmarks-v1"', rendered)
        self.assertIn("bookmarkedCards.has(card.dataset.cardId)", rendered)
        self.assertIn("card.hidden=!(matchesSearch&&matchesFilter)", rendered)
        self.assertIn("cards.sort(compareCards)", rendered)
        self.assertIn("localStorage.setItem(bookmarkStorageKey", rendered)
        self.assertIn('id="card-empty" hidden', rendered)
        self.assertNotIn(">Open PSA record</a>", rendered)
        self.assertNotIn('class="reference-meta"', rendered)
        self.assertNotIn("PSA submissions in the 2020s reference period", rendered)
        self.assertIn('href="about-asi.html"', rendered)
        self.assertIn("What is ASI?", about_rendered)
        self.assertIn("Price vs POP absorption", about_rendered)
        self.assertIn("ASI = Σ(component score × component weight)", about_rendered)
        self.assertIn("Absorption per +10% Supply", about_rendered)
        self.assertIn("Confidence &amp; missing data", about_rendered)

    def test_asi_reweights_available_components_and_reports_low_confidence(self):
        rows = [
            {
                "date": "2026-08-01",
                "card_id": "mega-gengar-ex-240",
                "sales_7d": "25",
                "sales_30d": "100",
                "raw_psa_spread": "50",
                "APS": "",
                "pop_change_30d": "",
                "psa10_price": "",
            }
        ]
        result = tracker.calculate_asi(rows[0], rows, 0)
        # Sales pace scores 80 and a 50% raw premium scores 100.
        self.assertAlmostEqual(result["score"], (80 * 0.2 + 100 * 0.1) / 0.3)
        self.assertAlmostEqual(result["available_weight"], 0.3)
        self.assertEqual(result["confidence"], "Low")

    def test_accumulation_alert_requires_all_three_signals(self):
        rows = [
            {
                "date": "2026-07-01",
                "psa10_price": "100",
                "sales_7d": "20",
                "sales_30d": "80",
                "pop_change_30d": "",
            },
            {
                "date": "2026-08-01",
                "psa10_price": "101",
                "sales_7d": "20",
                "sales_30d": "80",
                "pop_change_30d": "10",
            },
        ]
        self.assertTrue(tracker.accumulation_alert(rows))
        rows[-1]["sales_7d"] = "10"
        self.assertFalse(tracker.accumulation_alert(rows))

    def test_market_history_upsert_persists_requested_daily_fields(self):
        path = Path(__file__).parent / "data" / "_test_market_history.csv"
        path.unlink(missing_ok=True)
        try:
            with mock.patch.object(tracker, "MARKET_HISTORY_PATH", path):
                tracker.upsert_market_snapshot(
                    "mega-gengar-ex-240",
                    "2026-08-11",
                    psa10_price=589,
                    raw_price=400,
                    psa10_pop=32984,
                    sales_7d=120,
                    sales_30d=480,
                    lowest_listing=575,
                    listing_count=31,
                )
                rows = tracker.load_market_history()
        finally:
            path.unlink(missing_ok=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["date"], "2026-08-11")
            self.assertEqual(rows[0]["psa10_pop"], "32984")
            self.assertEqual(rows[0]["raw_psa_spread"], "47.25")
            self.assertTrue(rows[0]["ASI"])
            self.assertEqual(set(rows[0]), set(tracker.MARKET_FIELDNAMES))

    def test_population_migration_is_idempotent_and_uses_last_daily_observation(self):
        root = Path(__file__).parent / "data"
        population_path = root / "_test_population_history.csv"
        market_path = root / "_test_market_history.csv"
        population_path.unlink(missing_ok=True)
        market_path.unlink(missing_ok=True)
        try:
            with population_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=tracker.FIELDNAMES)
                writer.writeheader()
                for hour, pop, price in [(9, 100, 50), (21, 101, 52)]:
                    writer.writerow(
                        {
                            "observed_at": f"2026-08-11T{hour:02d}:00:00+09:00",
                            "card_id": "mega-gengar-ex-240",
                            "psa10_population": pop,
                            "psa9_population": "",
                            "total_population": "",
                            "psa_estimate_usd": price,
                            "source_url": "https://example.test",
                            "verified_identity": "true",
                            "notes": "test",
                        }
                    )
            with (
                mock.patch.object(tracker, "HISTORY_PATH", population_path),
                mock.patch.object(tracker, "MARKET_HISTORY_PATH", market_path),
            ):
                tracker.migrate_population_history(quiet=True)
                tracker.migrate_population_history(quiet=True)
                rows = tracker.load_market_history()
        finally:
            population_path.unlink(missing_ok=True)
            market_path.unlink(missing_ok=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["psa10_pop"], "101")
            self.assertEqual(rows[0]["psa10_price"], "52")


if __name__ == "__main__":
    unittest.main()
