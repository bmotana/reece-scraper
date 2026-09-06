"""Fast unit tests for scraper helpers; no browser or network access required."""

import importlib.util
import os
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

# Load the standalone scraper module directly so these tests work with
# `unittest discover` as well as direct execution.
scaper_path = Path(__file__).resolve().parents[1] / "scraper.py"
spec = importlib.util.spec_from_file_location("scraper", scaper_path)
scaper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaper)

clean_image_urls = scaper.clean_image_urls
extract_search_category = scaper.extract_search_category
scrape_product = scaper.scrape_product
set_postcode = scaper.set_postcode


class TestScraperHelpers(unittest.TestCase):
    def test_extract_search_category(self):
        url = "https://www.reece.com.au/search?query=hot-water-systems"

        self.assertEqual(extract_search_category(url), "Hot Water Systems")

    def test_clean_image_urls_keeps_product_assets_and_drops_noise(self):
        urls = [
            "https://digitalassets.reecegroup.com.au/products/mixer.jpg",
            "https://images.ctfassets.net/catalogue/product.jpg",
            "https://digitalassets.reecegroup.com.au/brand-logo.jpg",
            "https://example.com/untrusted-image.jpg",
        ]

        result = clean_image_urls(urls, "Example Mixer", "12345")

        self.assertIn(
            "https://digitalassets.reecegroup.com.au/products/mixer.jpg", result
        )
        self.assertIn("https://images.ctfassets.net/catalogue/product.jpg", result)
        self.assertNotIn(
            "https://digitalassets.reecegroup.com.au/brand-logo.jpg", result
        )
        self.assertNotIn("https://example.com/untrusted-image.jpg", result)


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_TESTS") == "1",
    "Live Reece test skipped; run with RUN_LIVE_TESTS=1 to enable it.",
)
class TestLivePostcode(unittest.TestCase):
    """Checks the real postcode modal without writing CSV or checkpoint files."""

    def test_postcode_enables_a_product_price(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            try:
                set_postcode(page, "3000")
                product = scrape_product(
                    page,
                    scaper.POSTCODE_SETUP_URL,
                    "Kitchen And Laundry",
                )
            finally:
                browser.close()

        self.assertNotEqual(product["price"], "Postcode dependent")
        self.assertRegex(product["price"], r"\d")


if __name__ == "__main__":
    unittest.main()
