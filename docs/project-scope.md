# Original project brief

Scraped product catalogue work targeting Reece Australia.

## Scope of work

- Scrape product data across major categories
- Extract product name, SKU/product code, description, specifications, pricing (when available), category hierarchy, brand, images, documents, and related attributes
- Deliver structured output (CSV / analysis-ready)
- Handle pagination, dynamic content, and product variants
- Prioritise accuracy and completeness
- Support resumable runs and clear progress reporting

## Technical approach (implemented)

- **Playwright** for JavaScript-rendered pages
- Category search URLs as crawl seeds
- Checkpoint-based resume (`discovered` + `visited` product URLs)
- Postcode selection so location-dependent prices can surface
- CSV export with JSON-encoded nested fields (images, documents, specifications)
