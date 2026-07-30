import csv
import json
import re
import time
import urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_FILE = "products.csv"
CHECKPOINT_FILE = "checkpoint.json"
# Prices on Reece are location-dependent. 3000 is Melbourne CBD; replace this
# with a different valid Australian postcode when a different price zone is needed.
POSTCODE = "3000"
POSTCODE_SETUP_URL = (
    "https://www.reece.com.au/product/"
    "mizu-drift-gooseneck-sink-mixer-tap-square-2266766?query=kitchen-and-laundry"
)

START_URLS = [
    "https://www.reece.com.au/search?query=bathroom",
    "https://www.reece.com.au/search?query=kitchen-and-laundry",
    "https://www.reece.com.au/search?query=heating-and-cooling",
    "https://www.reece.com.au/search?query=hot-water-systems",
    "https://www.reece.com.au/search?query=pool-and-spa-care",
    "https://www.reece.com.au/search?query=garden",
    "https://www.reece.com.au/search?query=plumbing",
]

# Set to None for scraping everything, or integer for limit during test runs
MAX_PAGES_PER_CATEGORY = 1


def set_postcode(page, postcode=POSTCODE):
    """Set the Reece delivery postcode once for the current browser context.

    Reece stores this choice in the browser context, so all pages opened with the
    same context can subsequently expose their location-specific prices.
    """
    # The location control is rendered on product pages, not the generic homepage.
    page.goto(POSTCODE_SETUP_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)

    postcode_link = page.locator("a.postcode-link")
    if postcode_link.count() == 0:
        raise RuntimeError("Could not find the postcode selector on the Reece product page.")

    postcode_link.first.click()

    # Prefer explicitly named postcode inputs, but retain a form-scoped fallback
    # for small markup changes to the postcode modal.
    postcode_input = page.locator(
        "input[name='customerpostcode'], input.popup-postcode__input"
    )
    if postcode_input.count() == 0:
        postcode_input = page.locator(
            "form input[name*='postcode' i], form input[id*='postcode' i], "
            "form input[placeholder*='postcode' i]"
        ).last

    postcode_input.wait_for(state="visible", timeout=10000)
    postcode_input.fill(postcode)

    submit_button = page.locator(".popup-postcode__form .popup-postcode__button")
    if submit_button.count() == 0:
        submit_button = page.locator(
            "form button[type='submit'], form button:has-text('Save'), "
            "form button:has-text('Apply'), form button:has-text('Continue'), "
            "form button:has-text('Submit')"
        )
    if submit_button.count() == 0:
        raise RuntimeError("Could not find the postcode form submit button.")

    submit_button.first.click()
    try:
        postcode_input.wait_for(state="hidden", timeout=10000)
    except Exception as e:
        raise RuntimeError(
            f"Postcode {postcode} was not accepted by the Reece form."
        ) from e

    print(f"Postcode set to {postcode}; location-specific prices are enabled.")


def load_checkpoint():
    """
    Loads checkpoint state from file. Backward compatible with old checkpoint files.
    """
    if Path(CHECKPOINT_FILE).exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "visited_products" not in data:
                        data["visited_products"] = []
                    if "discovered_products" not in data:
                        data["discovered_products"] = []
                    return data
        except Exception as e:
            print(f"Error loading checkpoint, starting fresh: {e}")

    return {"visited_products": [], "discovered_products": []}


def save_checkpoint(data):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def initialize_csv():
    if Path(OUTPUT_FILE).exists():
        return

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "product_name",
                "sku",
                "brand",
                "category",
                "description",
                "price",
                "image_urls",
                "document_urls",
                "specifications",
                "product_url",
            ]
        )


def append_product(product):
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                product.get("product_name"),
                product.get("sku"),
                product.get("brand"),
                product.get("category"),
                product.get("description"),
                product.get("price"),
                json.dumps(product.get("image_urls", [])),
                json.dumps(product.get("document_urls", [])),
                json.dumps(product.get("specifications", {})),
                product.get("product_url"),
            ]
        )


def discover_product_links_on_page(page):
    """
    Finds all links containing '/product/' on the current page.
    """
    links = set()
    try:
        anchors = page.locator("a").all()
        for anchor in anchors:
            try:
                href = anchor.get_attribute("href")
                if not href:
                    continue

                if "/product/" in href:
                    if href.startswith("/"):
                        href = "https://www.reece.com.au" + href
                    elif not href.startswith("http"):
                        href = "https://www.reece.com.au/" + href

                    links.add(href)
            except Exception:
                pass
    except Exception as e:
        print(f"Error discovering links on page: {e}")

    return list(links)


def clean_image_urls(image_urls, product_name, sku):
    """
    Filters out tracking pixels, icons, and non-product logos/images.
    """
    cleaned = []
    ignored_keywords = [
        "logo",
        "icon",
        "flag",
        "warranty",
        "delete-sign",
        "moodboard",
        "garden-bu",
        "kitchen-laundry-bu",
        "bathroom-bu",
        "hot-water-systems-bu",
    ]

    for url in image_urls:
        url_lower = url.lower()

        # We prefer digitalassets URLs which host the actual product photography
        if (
            "digitalassets.reecegroup.com.au" not in url_lower
            and "images.ctfassets.net" not in url_lower
        ):
            continue

        if any(kw in url_lower for kw in ignored_keywords):
            continue

        cleaned.append(url)

    return list(set(cleaned))


def extract_specifications(page):
    """
    Clicks the specification button (if present) to load specs, then parses key-value tables.
    """
    try:
        # Check if the "View Product Specifications" button is present and click it
        spec_btn = page.locator(
            "button:has-text('View Product Specifications'), a:has-text('View Product Specifications')"
        )
        if spec_btn.count() > 0:
            spec_btn.first.click()
            page.wait_for_timeout(1000)
    except Exception as e:
        print(f"Could not click specifications button (might be already visible): {e}")

    # Parse details tables
    return page.evaluate(
        """
        () => {
            const specs = {};
            const tables = [...document.querySelectorAll('table.details-table.details-table--2-col')];

            tables.forEach((table) => {
                table.querySelectorAll('tr').forEach((row) => {
                    const label = row.querySelector('th');
                    const value = row.querySelector('td');
                    if (!label || !value) {
                        return;
                    }

                    const key = label.textContent.trim();
                    const val = value.textContent.trim();
                    if (key && val) {
                        specs[key] = val;
                    }
                });
            });

            return specs;
        }
        """
    )


def extract_search_category(url):
    """
    Guesses general category from the query parameter in the search URL.
    """
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    q = params.get("query", [""])[0]
    return q.replace("-", " ").title() if q else "General"


def scrape_product(page, url, search_category=""):
    """
    Scrapes all attributes of a product page.
    """
    # Load the page and wait for the DOM to load
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    # Wait for dynamic rendering
    page.wait_for_timeout(2000)

    product = {
        "product_name": "",
        "sku": "",
        "brand": "",
        "category": "",
        "description": "",
        "price": "",
        "image_urls": [],
        "document_urls": [],
        "specifications": {},
        "product_url": url,
    }

    # 1. Product Name
    try:
        product["product_name"] = page.locator("h1").first.inner_text().strip()
    except Exception:
        pass

    # 2. Description
    try:
        desc = page.locator("meta[name='description']").get_attribute("content")
        if not desc:
            desc = page.locator("meta[property='og:description']").get_attribute(
                "content"
            )
        product["description"] = desc.strip() if desc else ""
    except Exception:
        pass

    # 3. Product Code / SKU
    try:
        product_code_el = page.locator(
            "[class*='product-code'], [class*='pdp-main-block__product-code']"
        ).first
        if product_code_el.count() > 0:
            product_code = product_code_el.inner_text()
            match = re.search(r"\d+", product_code)
            if match:
                product["sku"] = match.group()
    except Exception:
        pass

    # 4. Specifications
    try:
        product["specifications"] = extract_specifications(page)
    except Exception as e:
        print(f"Error extracting specifications: {e}")

    # Fallback for SKU from specifications
    if not product["sku"]:
        product["sku"] = product["specifications"].get("Product Code", "")

    # Fallback for SKU from URL if all else fails
    if not product["sku"]:
        match = re.search(r"-(\d+)(?:\?|$)", url)
        if match:
            product["sku"] = match.group(1)

    # 5. Brand (checks specifications, H1 name patterns, and first word of H1)
    brand = product["specifications"].get("Brand") or product["specifications"].get(
        "Manufacturer"
    )
    if not brand and product["product_name"]:
        name_upper = product["product_name"].upper()
        if name_upper.startswith("AMERICAN STANDARD"):
            brand = "American Standard"
        elif name_upper.startswith("HEATING & COOLING"):
            brand = "Reece"
        else:
            # Take the first word
            brand = product["product_name"].split()[0]

    product["brand"] = brand if brand else ""

    # 6. Category Hierarchy
    spec_type = product["specifications"].get("Product Type") or product[
        "specifications"
    ].get("Category")
    if search_category and spec_type:
        product["category"] = f"{search_category} > {spec_type}"
    elif spec_type:
        product["category"] = spec_type
    else:
        product["category"] = search_category or "General"

    # 7. Images (Filter and sanitize image sources)
    try:
        raw_images = page.locator("img").evaluate_all(
            "els => els.map(x => x.src).filter(Boolean)"
        )
        product["image_urls"] = clean_image_urls(
            raw_images, product["product_name"], product["sku"]
        )
    except Exception:
        pass

    # 8. Documents
    try:
        pdf_links = []
        for a in page.locator("a").all():
            try:
                href = a.get_attribute("href")
                if href and ".pdf" in href.lower():
                    if href.startswith("/"):
                        href = "https://www.reece.com.au" + href
                    pdf_links.append(href)
            except Exception:
                pass
        product["document_urls"] = list(set(pdf_links))
    except Exception:
        pass

    # 9. Price (Check if price element is available, otherwise postcode-dependent)
    try:
        price_el = page.locator("[class*='price'], [class*='Price']").first
        if price_el.count() > 0:
            price_text = price_el.inner_text().strip()
            if "postcode" in price_text.lower():
                product["price"] = "Postcode dependent"
            else:
                product["price"] = price_text
        else:
            product["price"] = "Postcode dependent"
    except Exception:
        product["price"] = "Postcode dependent"

    return product


def run_discovery(page, checkpoint):
    """
    Crawls search listing pages to discover and register all product links.
    """
    discovered_set = set(checkpoint["discovered_products"])
    print(f"Beginning link discovery across {len(START_URLS)} categories...")

    for start_url in START_URLS:
        category_name = extract_search_category(start_url)
        print(f"\nScanning category: {category_name} ({start_url})")

        current_url = start_url
        page_idx = 1

        while current_url:
            if MAX_PAGES_PER_CATEGORY and page_idx > MAX_PAGES_PER_CATEGORY:
                print(
                    f"Reached page limit ({MAX_PAGES_PER_CATEGORY}) for {category_name}"
                )
                break

            print(f"  [Page {page_idx}] Navigating to: {current_url}")
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)

                # Discover links
                page_links = discover_product_links_on_page(page)
                new_links_count = 0
                for link in page_links:
                    if link not in discovered_set:
                        discovered_set.add(link)
                        new_links_count += 1

                print(
                    f"    Found {len(page_links)} products on page ({new_links_count} new). Total discovered: {len(discovered_set)}"
                )

                # Save state periodically during discovery
                checkpoint["discovered_products"] = list(discovered_set)
                save_checkpoint(checkpoint)

                # Find the next page link
                next_el = page.locator("a.product-listing__pagination-control--next")
                if next_el.count() > 0:
                    next_href = next_el.first.get_attribute("href")
                    if next_href:
                        if next_href.startswith("/"):
                            current_url = "https://www.reece.com.au" + next_href
                        else:
                            current_url = next_href
                        page_idx += 1
                    else:
                        current_url = None
                else:
                    print("    No next page button found. Reached end of pagination.")
                    current_url = None

            except Exception as e:
                print(f"    Error during link discovery on page {page_idx}: {e}")
                current_url = None


def main():
    initialize_csv()
    checkpoint = load_checkpoint()

    visited_products = set(checkpoint["visited_products"])
    discovered_products = checkpoint["discovered_products"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # Select the delivery location before loading any search or product page.
        # The site's price data is postcode-dependent and the selection persists
        # within this browser context.
        set_postcode(page)

        # 1. Run link discovery if we have no discovered URLs or if we want to refresh
        if not discovered_products:
            run_discovery(page, checkpoint)
            discovered_products = checkpoint["discovered_products"]

        to_scrape = [
            link for link in discovered_products if link not in visited_products
        ]
        print(f"\n--- Scraping Queue ---")
        print(f"Total Discovered Links: {len(discovered_products)}")
        print(f"Already Visited:        {len(visited_products)}")
        print(f"Remaining to Scrape:    {len(to_scrape)}")

        # 2. Scrape details for each discovered product
        for idx, product_url in enumerate(to_scrape, start=1):
            category = "General"
            # Attempt to guess search category from query string in URL
            if "query=" in product_url:
                try:
                    parsed = urllib.parse.urlparse(product_url)
                    params = urllib.parse.parse_qs(parsed.query)
                    q = params.get("query", [""])[0]
                    if q:
                        category = q.replace("-", " ").title()
                except:
                    pass

            # Simple retry loop for network robustness
            success = False
            retries = 3
            while retries > 0 and not success:
                try:
                    print(
                        f"[{idx}/{len(to_scrape)}] Scraped details for: {product_url}"
                    )
                    product = scrape_product(page, product_url, category)

                    append_product(product)

                    visited_products.add(product_url)
                    checkpoint["visited_products"] = list(visited_products)
                    save_checkpoint(checkpoint)

                    success = True
                    # Compliant sleep between requests
                    time.sleep(1.5)
                except Exception as e:
                    retries -= 1
                    print(
                        f"  Attempt failed for: {product_url}. Retries remaining: {retries}. Error: {e}"
                    )
                    if retries > 0:
                        time.sleep(5)

            if not success:
                print(f"FAILED permanently to scrape details for: {product_url}")

        browser.close()

    print("\nScraping job completed successfully!")


if __name__ == "__main__":
    main()
