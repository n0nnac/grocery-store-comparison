# Safeway API Research Log

This is the experiment log for finding a cleaner, faster path than visible-page scraping.

## Date And Store Context

- Research date: 2026-05-07
- Primary store: Safeway `923`, 1701 Corcoran St NW, Washington, DC 20009
- User ZIP: `20037`
- Banner: `safeway`

## Executive Summary

The best current approach is Safeway's browser-facing `search/substitute` endpoint:

```text
GET https://www.safeway.com/abs/pub/xapi/search/substitute
```

Despite the name, it returns normal product-search results with store-specific pricing fields:

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
- `departmentName`
- `aisleName`

This is currently much faster and cleaner than rendering product pages. It can also search directly by product ID.

## Working Product Search Endpoint

### Endpoint

```text
https://www.safeway.com/abs/pub/xapi/search/substitute
```

### Required Header

```text
Ocp-Apim-Subscription-Key: e914eec9448c4d5eb672debf5011cf8f
```

This key is exposed in Safeway's public browser assets. It is not a user secret, but the endpoint is private and undocumented.

Useful additional headers:

```text
Accept: application/json
User-Agent: Mozilla/5.0 ...
Referer: https://www.safeway.com/shop/search-results.html
x-swy-banner: safeway
x-swy-client-id: web-portal
```

### Query Parameters

The request only worked reliably after matching the browser's full parameter shape:

```text
request-id=<timestamp/random>
url=https://www.safeway.com
pageurl=https://www.safeway.com
pagename=search
rows=10
start=0
search-type=keyword
storeid=923
featured=true
search-uid=
q=<query-or-product-id>
channel=pickup
banner=safeway
```

### Example

```bash
python3 safeway_api_search.py "teriyaki sauce" --rows 5
python3 safeway_api_search.py "960017503" --rows 3 --json
```

### Notes

- `q` can be a normal search phrase.
- `q` can also be a Safeway product ID, which is very useful for refreshing a known item.
- `basePrice` should be treated as the regular/base price when present.
- `price` should be treated as the current shelf/member/sale price.
- `promoEndDate` tells us when a current discount expires.
- `inventoryAvailable` is a string, usually `"1"` or `"0"`.

## Working Store Resolver Endpoints

These are useful for store discovery and validating store context.

### ZIP To Shopping Availability

```text
GET /abs/pub/xapi/storeresolver/zipcodetoshopping?zipcode=20037&banner=safeway
```

Result: works with `Ocp-Apim-Subscription-Key: 7bad9afbb87043b28519c4443106db06`.

### Store Address By Store ID

```text
GET /abs/pub/xapi/storeresolver/storeaddress?storeid=923&banner=safeway
```

Result: works and returns the Corcoran St store address.

Important detail: parameter must be lowercase `storeid`; `storeId` returned a 400.

### Pickup/Delivery Store Lists

```text
GET /abs/pub/xapi/storeresolver/v2/all?zipcode=20037&banner=safeway
GET /abs/pub/xapi/storeresolver/v2/delivery?zipcode=20037&banner=safeway
GET /abs/pub/xapi/storeresolver/pickup?zipcode=20037&banner=safeway
```

These return large structured store lists. They are helpful for choosing a store, but they do not return product prices.

## Working Autosuggest Endpoint

```text
GET /abs/pub/xapi/search/autosuggest?q=milk&storeid=923&banner=safeway
```

Result: works with `Ocp-Apim-Subscription-Key: e914eec9448c4d5eb672debf5011cf8f`.

This returns search suggestions and filters, not prices. It may help normalize user ingredient terms before product search.

## Partially Working Endpoint

### Substitution API

```text
GET /abs/pub/xapi/pdreco/substitution?bpn=<pid>&storeId=923&hhid=<household-id>
```

Result: reachable, but requires a valid `hhid`. Without it, it returns a structured 422 error.

This is not a base-price source.

## Failed Or Unreliable Endpoints

### Product Search Variants That Hung

These timed out from a non-browser process even with browser-like headers and visible subscription keys:

```text
/abs/pub/xapi/pgmsearch/v1/search/products
/abs/pub/xapi/search/products
```

### Catalog Product Lookup

This endpoint family was discoverable in page assets:

```text
/abs/pub/xapi/catalog/products-by-bpn
/abs/pub/xapi/catalog/products-by-upc
/abs/pub/xapi/catalog/incrementPriceList
```

Attempts to POST likely request bodies from shell either timed out through `www.safeway.com` or failed DNS/fetch against the direct APIM host:

```text
prod.apim.azwestus.stratus.albertsons.com
```

These may work inside the real browser with full app state, but they are not the cleanest standalone path right now.

### Coupon Gallery

```text
/abs/pub/web/j4u/api/ecomgallery
```

Result: returns a structured 403:

```text
EMJO01000E Unauthorized Access
```

This remains relevant for clipped offers, not base product prices.

### Direct Product/Search Pages From Shell

Normal product/search/category page URLs return Imperva/Incapsula "Pardon Our Interruption" HTML from shell:

```text
/shop/product-details.<pid>.html?loc=923
/shop/search-results.html?q=<query>&loc=923
/shop/aisles/...
```

This confirms why browser rendering worked while shell page scraping did not.

## How The Endpoint Was Found

1. A guessed invalid API URL returned Safeway's real 404 app shell rather than the Imperva page.
2. The app shell exposed config blocks and JavaScript assets.
3. The large Angular bundle exposed environment configs, subscription keys, and endpoint names.
4. The `search/substitute` endpoint was reconstructed from the minified client code's full parameter builder.
5. The endpoint was tested with store `923` and returned priced product docs.

## Implementation Added

`safeway_api_search.py` is a small CLI proof of concept around the working endpoint.

Use it for quick probes:

```bash
python3 safeway_api_search.py "milk"
python3 safeway_api_search.py "960017503" --json
```

## Recommended Next Architecture

Use this tiered strategy:

1. Known product ID refresh:
   - Query `search/substitute` with `q=<pid>`.
   - If exactly one matching `pid` returns, update price from `basePrice`, `price`, and `pricePer`.

2. Unknown ingredient search:
   - Query `search/substitute` with ingredient text.
   - Filter to `inventoryAvailable == "1"` when possible.
   - Rank candidates by name match, store brand preference, package size, and unit price.

3. Weekly ad overlay:
   - Keep weekly ad deals separate.
   - Use weekly ad data as dated sale overrides, not as base prices.

4. Fallback:
   - Use visible PDP/card scraping only when the API cannot find or disambiguate the product.

## Coupon Research

Coupon and digital-offer data is separate from base/current product pricing.

The price API described above is useful for product prices, member prices, promo end dates, and inventory signals. It does not answer whether the account has:

- clipped a coupon
- become eligible for a personalized offer
- reached a basket condition
- hit a coupon limit

Coupon research should store coupon data as dated/account-state overlays rather than base-price replacements.

### Working Read-Only Coupon Gallery

```text
GET /abs/pub/xapi/offers/companiongalleryoffer?storeId=923&rand=<random>&includeRedmBonusPathFPOffers=true
```

Result: works and returns the coupon/deal gallery for store `923`.

Useful fields:

- `offerId`
- `offerPgm`
- `brand`
- `category`
- `events`
- `ecomDescription`
- `forUDescription`
- `description`
- `status`
- `isClippable`
- `startDate`
- `endDate`

Important caveat: without authenticated account state, `status` should be treated as gallery state, not confirmed clipped state.

### Working Read-Only Offer Detail

```text
GET /abs/pub/xapi/offers?offerId=<offerId>&storeId=923&offerPgm=<offerPgm>&includeUpc=y
```

Result: works for offer details and often returns `upcList`, which can be resolved through the Safeway product search API.

This is the current bridge from coupon offers to product prices.

Implementation note: `safeway_coupon_enrich.py` now starts from saved gallery offers and enriches targeted slices with this detail endpoint. This is faster than re-reading and resolving the full gallery when the immediate planning need is a department, category, or offer type.

### Partially Working Category Filter

```text
POST /abs/pub/dce/offergallery/anonymous/offers/filter?storeId=923
```

with a body like:

```json
{"primaryCategoryNm":["Meat & Seafood"]}
```

Result: works for anonymous category filtering. The full gallery endpoint is simpler for now.

### Auth-Gated Coupon Lookup

```text
POST /abs/pub/dce/offergallery/J4UProgram1/erums/services/gallery/offers?storeId=923&offerPgm=CC-PD
```

with a body like:

```json
["<upc>"]
```

Result: returns `EMJO01000E Unauthorized Access` without authenticated session state.

Candidate areas to investigate later:

- Confirmed clipped/unclipped state from the logged-in browser session.
- Coupon clip and unclip endpoints, only when explicitly requested.
- Whether coupon state can be read without performing cart mutations.
- Whether the auth-gated UPC-to-offer endpoint is more efficient than detail-per-offer lookup.

Recommended next authenticated read-only pass:

1. Capture the logged-in browser's coupon gallery/detail requests.
2. Identify which response fields represent account state versus public offer state.
3. Save only read-derived state fields such as `account_state.clipped`, `clip_status_confirmed_on`, and `household_specific`.
4. Keep clipping or unclipping actions in a separate explicit workflow because those mutate the user's account.

### Working Logged-In Coupon State

Using a Chrome DevTools Protocol page on `safeway.com`, these account-state reads worked:

```text
GET /abs/pub/dce/offergallery/J4UProgram1/services/gallery/companion/clipped?storeId=923
GET /abs/pub/dce/offergallery/J4UProgram1/services/gallery/companion/v1/offers?storeId=923
```

Observed result on 2026-05-07:

- `companion/clipped` returned 8 clipped account offers.
- `companion/v1/offers` returned 539 logged-in gallery offers.
- The account-state updater added 32 account-gallery offers not present in the unauthenticated gallery snapshot.
- The `$3 off any Meat & Seafood Department Purchase of $15 or more` coupon appeared as real account offer `72257330` and was clipped.
- The previously manual Meat & Seafood override was marked inactive after the real account coupon was captured.

Implementation reference:

```bash
python3 safeway_coupon_account_state.py --cdp-url http://127.0.0.1:9223 --add-new --write
```

This workflow is read-only. It does not call `Clipping1/services/clip/items`, `unclip/items`, `update/items`, or `delete/items`.

Reusable pipeline reference:

```bash
python3 safeway_coupon_pipeline.py --account-state --write
```

The pipeline launches a temporary copied Chrome profile for the account-state read when no `--cdp-url` is supplied. The copied profile is removed after the run. This makes the logged-in read repeatable without leaving browser cookies or SSO material in project files.

Known result so far:

```text
/abs/pub/web/j4u/api/ecomgallery
```

returned a structured unauthorized response outside the required authenticated context.

## Open Questions

- Whether the subscription key rotates.
- Whether `channel=pickup`, `delivery`, or `instore` changes prices materially for this store.
- Whether this endpoint has rate limits that matter for a small personal grocery tool.
- Whether product categories can be listed cleanly through an API, or whether product-ID search is enough for our workflow.
- Which authenticated coupon endpoints provide read-only offer state, and which actions mutate the user's account or cart.
