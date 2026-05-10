# Safeway Price Scrape Methodology

This document describes the repeatable process for pulling Safeway prices for the configured local store. Product-specific observations belong in `safeway_price_observations.json`, not in this methodology document.

## Store Context

- Store: Safeway, 1701 Corcoran St NW, Washington, DC 20009
- Store ID: `923`
- User ZIP context: `20037`
- Product URLs should include `?loc=923` when possible:
  - `https://www.safeway.com/shop/product-details.<PRODUCT_ID>.html?loc=923`

## Current Goal

Populate `meal_prices.json` with Safeway base prices that are explicitly backed by scraped observations, not guesses.

The price file should distinguish:

- `search_substitute_api`: price observed from Safeway's browser-facing product search API
- `scraped_pdp`: price observed on a Safeway product detail page for store `923`
- `scraped_related_card`: price observed on a product card rendered in a PDP's similar/featured items section
- `weekly_ad`: advertised deal price from the weekly ad or preview ad
- `observed_manual`: previously observed/entered price without a fresh PDP scrape
- `estimate`: planning placeholder only; should not be treated as verified

## Sources Used

### Product Search API

Primary fast path:

```text
https://www.safeway.com/abs/pub/xapi/search/substitute
```

Despite the name, this endpoint returns normal product-search results with store-specific price fields. Use it before visible page scraping.

Useful fields:

- `pid`
- `upc`
- `name`
- `storeId`
- `price`
- `basePrice`
- `pricePer`
- `basePricePer`
- `promoEndDate`
- `inventoryAvailable`

Use `basePrice` as the regular/base price when available. Use `price` as the current price. If a known product ID exists, query it directly with `q=<product_id>`.

Implementation reference:

- `safeway_api_search.py`

### Product Detail Pages

Primary scrape target:

```text
https://www.safeway.com/shop/product-details.<PRODUCT_ID>.html?loc=923
```

What is useful:

- Product name from the page heading
- Product ID from the URL
- Current price when the buy box or product card renders
- Original price when the page/card exposes it
- Unit price when exposed, usually as `($X.XX / Lb)` or similar

Important caveat:

The mobile-rendered Safeway PDP sometimes does not show the active product's buy box at all, even after waiting and scrolling. In those cases, the page may still expose prices for similar/featured product cards. Those are usable only for the product shown in the card, not for the active PDP product.

### Safeway Category Pages

Category pages are useful for product IDs and product names, but not reliable for current local prices.

Category pages often show `(0)` and no products in the live in-app browser even when search-indexed HTML exposes product links. This makes them useful as a product-ID lookup source, but not as a price source.

### Weekly Ad / Preview Ad

Weekly ad data is stored separately in:

- `weekly_deals.json`
- `weekly_deals_preview_2026-05-08.json`
- `ad_downloads/safeway_weekly_ad_2026-05-08_to_2026-05-14.pdf`

Weekly ad prices are not base prices. They should be treated as sale overrides with:

- valid date range
- whether clipping is required
- limit/condition when present

### Official Recipe And Bundle Pages

Safeway recipe and bundle pages sometimes expose ingredient-level prices even when the PDP buy box does not render.

These are acceptable secondary sources when:

- The page is an official Safeway URL.
- The ingredient/product name is visible next to the price.
- The observation is labeled as `official_recipe_page` or `official_bundle_page`, not as a PDP scrape.

This source type should be treated as lower-confidence than `scraped_pdp` or `scraped_related_card`, but better than an estimate.

## Failed or Unreliable Approaches

### Other Direct Safeway APIs

Several private Albertsons/Safeway endpoints were tested from shell and browser context. Many were blocked, timed out, returned Incapsula/Imperva responses, or required authenticated/private browser state.

Current decision: use `search/substitute` for base-price lookup. Do not rely on `search/products`, `pgmsearch`, catalog lookup, or coupon APIs until separately proven.

### Search Results Pages

Search result URLs such as:

```text
https://www.safeway.com/shop/search-results.html?q=ground%20turkey&loc=923
```

often load header/footer content but stall on `Page is getting loaded`. They are not reliable enough for automated price scraping.

### Active PDP Buy Box

Some PDPs show the active item's price quickly; others show details only and omit the active buy box. When this happens, scrolling can reveal related cards, but those prices must be attributed to the related-card product only.

## Current Browser Scrape Workflow

1. Query the product search API with either ingredient text or known product ID.
2. If the API returns a high-confidence match, record `basePrice`, `price`, `pricePer`, product ID, and source metadata.
3. If the API cannot find or disambiguate the item, load a PDP with `?loc=923`.
4. Wait for `domcontentloaded`.
5. Wait a few seconds for client-rendered product pricing.
6. Scroll once to reveal similar/featured cards.
7. Read the browser DOM snapshot and/or body text.
8. Extract:
   - product name
   - product ID
   - current price
   - original price, if visible
   - unit price, if visible
   - whether the observation came from the active PDP or a related card
9. Store source metadata alongside `base_prices.Safeway`.

## Automated API Refresh Workflow

For products with known Safeway product IDs, use:

```bash
python3 safeway_refresh_prices.py --dry-run
python3 safeway_refresh_prices.py --write
```

The refresh script:

1. Reads known Safeway product IDs from `meal_prices.json`.
2. Queries `search/substitute` with `q=<PRODUCT_ID>`.
3. Requires an exact returned product-ID match.
4. Updates `safeway_price_observations.json` with the API evidence trail.
5. Updates `meal_prices.json` source metadata.
6. Uses API `basePrice` as the regular package price where appropriate.
7. Uses API `basePricePer` for ingredients whose meal-planning unit is pounds.

Dry-run is the default. Use `--write` only after reviewing the proposed changes.

## Data Hygiene Rules

- Do not overwrite a base price with a weekly ad sale price.
- If current price differs from original price, use original/regular as base when available.
- If only current price is visible, record it as current/member price and mark the base confidence accordingly.
- If the product is a weighted item, prefer unit price per lb for meal planning.
- If the price comes from a related card, identify the card product ID and name explicitly.
- If a product page fails to expose its own active price, leave that product as unverified instead of guessing.
- If a recipe or bundle page is used as a fallback, label it separately and keep the original Safeway URL.

## Next Refinements

- Per-item Safeway source metadata has been added to `meal_prices.json` for the items updated on May 7, 2026.
- Detailed observations are stored in `safeway_price_observations.json` so the raw source trail does not bloat the meal-planning file.
- Preserve `base_prices` for compatibility with `meal_price_tool.py`.
- Re-run key product pages periodically because Safeway pricing changes and sale/member pricing can replace regular shelf pricing in the rendered UI.
- Keep coupon and digital-offer data as a separate overlay layer when that phase starts.

## Files Updated From This Method

- `meal_prices.json`
  - Keeps the normalized meal-planning prices.
  - Adds `price_sources.Safeway` where a scraped source exists.

- `safeway_price_observations.json`
  - Stores the scrape observations by Safeway product ID.
  - Keeps active PDP observations and related-card observations separate.
  - Also records official Safeway recipe/bundle fallback observations when PDP prices fail.
