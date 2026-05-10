# Giant API Research Log

This is the research log for finding a cleaner Giant Food price source, parallel to the Safeway API work.

## Date And Store Context

- Research date: 2026-05-09
- Primary store: Giant Food `#0378`, 1345 Park Road, NW, Washington, DC 20010
- User ZIP context: `20037` / nearby Columbia Heights store
- Banner/opco seen in static assets: `GNTL`

## Executive Summary

A browser-session live Giant product-price API path is now confirmed.

Plain shell requests to Giant's live app/API are still DataDome-protected, but a normal Chrome session that can browse Giant prices can make same-origin API calls through the web app's own JSON endpoints. The reusable workflow is captured in `giant_browser_api_probe.py`.

What is confirmed:

1. The user's store is Giant Food store `#0378`.
2. Giant's live shopping app and historical Peapod-style API routes are protected by DataDome from shell requests.
3. Giant exposes reachable static grocery catalog pages under `/groceries/...`.
4. Those static pages contain `schema.org` JSON-LD with product names, product URLs, images, offer prices, availability, and `priceValidUntil`.
5. Product detail pages also expose `window.appConfig.productId`, which can be captured as a Giant product identifier.
6. The Giant Android app uses Apollo GraphQL for live catalog, service-location, coupon, weekly-ad, cart, and rewards-like flows.
7. The Android app's live GraphQL endpoint is `https://core.pdl.giantfood.com/prod/apollo/graphql`.
8. Direct shell requests to that endpoint are also DataDome-protected, even with recovered app headers.
9. Giant's web app uses reachable same-origin API routes under `/api/v5.0/...` once called from a validated browser session.
10. Park Road store `#0378` resolves to browser API service location `50000732` for service type `B`.

The browser-session `/api/v5.0` path should be the preferred Giant source for current Park Road prices. The static catalog remains useful as a fallback/product-discovery layer, but it is not the primary live-price path.

## Official Store Identity

Official locator page:

```text
https://stores.giantfood.com/dc/washington/1345-park-road%2C-nw
```

Observed on the page:

- Address: 1345 Park Road, NW, Washington, DC 20010
- Phone: (202) 777-1077
- Store Number: `#0378`

## Live API Surface Tested

### Historical Peapod API Pattern

An old open-source library for the unofficial Peapod API used this base URL:

```text
https://www.peapod.com/api/
```

With endpoints such as:

```text
GET /api/v2.0/user/products?keywords=milk&rows=5&start=0
GET /api/v3.0/user/cart
```

This was useful as a historical clue because Giant's product images and static catalog still use `peapod.com` image hosts and IDs.

Current result from shell:

```text
403 DataDome challenge
```

### Giant API Guesses

Tested examples:

```text
GET https://giantfood.com/api/v2.0/user/products?keywords=milk&rows=5&start=0
GET https://giantfood.com/api/v3.0/user/products?keywords=milk&rows=5&start=0
GET https://giantfood.com/api/v3.0/user/locations?search=20010
GET https://giantfood.com/api/v3.0/user/stores?zip=20010
GET https://giantfood.com/apis/
```

Current result from shell:

```text
403 DataDome challenge
```

The top-level homepage and `/product-search/...` app routes also return DataDome from shell. This means we should not build a production pipeline around plain `curl` access to the live app/API unless we find an officially reachable API host or a browser-session workflow that is stable and acceptable.

### Browser-Session Web API

The normal Giant web app calls JSON endpoints under the `giantfood.com` origin. Direct shell calls to the same URLs return a DataDome challenge, but calling them with `fetch()` from a validated Giant browser tab succeeds.

Confirmed store-resolution endpoint:

```text
GET /api/v5.0/serviceLocation/stores/378?serviceType=B
```

Observed Park Road result:

```text
location id: 50000732
address: 1345 Park Road N.W., Washington, DC 20010
locationNumber: 378
serviceType: B
priceZone: 1534
pickupLocationId / pupId: 10734
```

Confirmed product detail endpoint:

```text
GET /api/v5.0/products/info/{userId}/{serviceLocationId}/{productId}
  ?extendedInfo=true
  &flags=true
  &nutrition=true
  &substitute=true
  &categoryInfo=true
```

Confirmed product search endpoint:

```text
GET /api/v5.0/products/{userId}/{serviceLocationId}
  ?keywords={query}
  &sort=bestMatch+asc,+name+asc
  &rows=12
  &start=0
  &flags=true
  &facet=nutrition
  &hkInclude=true
  &facetExcludeFilter=true
```

Useful returned fields include:

```text
prodId, name, brand, size, price, regularPrice, unitPrice, unitMeasure, upc,
flags.sale, flags.outOfStock, hasCoupon, coupon, availableDisplayCoupons,
bmsm, bmsmTiers
```

This is not cookie scraping. The CLI opens or connects to a normal Chrome profile with Chrome DevTools enabled and asks that browser to perform same-origin `fetch()` calls. Cookies stay inside Chrome and are not printed or persisted by the project.

### Android App Apollo GraphQL

The Giant Android package `com.giantfood.mobile.droid`, version `9.0.1` / version code `6096`, contains generated Apollo query classes under:

```text
com.peapoddigitallabs.squishedpea
```

Important recovered BuildConfig values:

```text
applicationId: com.giantfood.mobile.droid
opco/build flavor: gntl
web URL: https://giantfood.com/
static content URL: https://static.giantfood.com/site/
digital content URL: https://digitalcontent1.giantfood.com
GraphQL URL: https://core.pdl.giantfood.com/prod/apollo/graphql
Apollo client name: com.giantfood.mobile.droid-apollo-android
Apollo client version: 9.0.1-6096
```

The app builds the Apollo client with a DataDome OkHttp interceptor:

```text
DataDomeSDK.with(application, SecureConfig.dataDomeAppKey(), "9.0.1")
new DataDomeInterceptor(application, dataDomeSDKBuilder)
ApolloClient.Builder().serverUrl(EnvironmentConfigHelper.awaitGraphqlUrl())
```

Recovered request headers added by the app include:

```text
User-Agent
Authorization
opco: GNTL
env: prod
basket-id
service-location-id
current-order-id
GQL-Platform-Origin: android
X-Correlation-Id
X-Device-Token
X-Glassbox-Session-Url
X-APOLLO-OPERATION-NAME
X-APOLLO-OPERATION-ID
apollographql-client-name
apollographql-client-version
```

For unauthenticated product lookup, the most relevant query operations are:

```text
getServiceLocations
operation id: 88920e787158b1f9ab621d4d50152861b1a5c6f91c3cd19bae8ef1e754309b20
variables: zip, customerType, serviceType
enum values observed:
  ShortCustomerType: C, M
  ServiceType: B, D, P

getProducts
operation id: 4c9c74591cdaaaa294d3b143260860d90f0a715d336ebde307668cce70b5410c
variables: keywords, start, limit, filter, sort, includeSponsors, serviceLocationId, adPositions
returns: product names, IDs, UPCs, price, regularPrice, unitPrice, sale flags, coupon display data, BMSM tiers, variable-weight metadata
```

Other useful generated operations exist and should be explored later:

```text
GetProductByIdQuery
GetProductByUPCQuery
GetProductSpecialsQuery
GetFilteredProductsQuery
GetStoreByNumberQuery
GetServiceLocationsByIdQuery
GetWeeklyCircularDealsQuery
GetWeeklyCircularDealsV2Query
GetCouponsQuery
GetCouponProductsQuery
CreateCartMutation
CartByCartIdQuery
```

Direct probe result on 2026-05-09:

```text
POST https://core.pdl.giantfood.com/prod/apollo/graphql
operation: getServiceLocations
headers: recovered app-style Apollo/opco/platform headers
result: HTTP 403 with x-datadome: protected and geo.captcha-delivery.com challenge URL
```

TLS/browser impersonation using `curl_cffi` with Chrome/Safari impersonation still returned DataDome 403. This suggests a valid DataDome browser/app session is needed, not just a better user-agent string.

## Robots.txt Signal

Giant's `robots.txt` is reachable:

```text
https://giantfood.com/robots.txt
```

Relevant lines:

```text
Disallow: /api/*
Disallow: /apis/*
Sitemap: https://giantfood.com/groceries/sitemap.xml
```

The grocery sitemap was DataDome-protected from shell during testing, but individual static grocery pages were reachable.

## Working Static Catalog Source

### Category Pages

Example:

```text
https://giantfood.com/groceries/dairy-eggs/milk.html
```

Returns normal HTML from shell and includes a large `application/ld+json` block:

```text
@type: ItemList
itemListElement[].item.@type: Product
itemListElement[].item.offers.price
itemListElement[].item.offers.priceValidUntil
```

Example observed fields:

- product name
- brand
- URL
- image URL
- price
- price currency
- price valid-until date
- availability

### Product Pages

Example:

```text
https://giantfood.com/groceries/dairy-eggs/milk/whole-milk/whole-milk-gallon/giant-vitamin-d-whole-milk-1-gallon.html
```

Returns a single `Product` JSON-LD block and a small app config:

```text
window.appConfig = {
  apiKey: 341,
  opcoTheme: "GNTL",
  productId: 58049
}
```

The `apiKey` appears to identify the static SEO/catalog app, not a confirmed callable API key. Do not treat it as a secret.

## Implementation Added

`giant_catalog_search.py` searches the reachable static catalog pages.

Example commands:

```bash
python3 giant_catalog_search.py "ground beef"
python3 giant_catalog_search.py "rice" --json
python3 giant_catalog_search.py "milk" --rows 8 --max-pages 8 --detail-rows 5
```

The script:

1. Starts from `/groceries/index.html` and query-relevant category hints.
2. Crawls a bounded number of static `/groceries/*.html` pages.
3. Parses JSON-LD product offers.
4. Optionally fetches product detail pages for top matches to capture `productId`.
5. Marks price freshness from `priceValidUntil`.
6. Emits store context for `#0378`, while explicitly labeling the source as static SEO catalog data.

`giant_graphql_probe.py` is a live GraphQL harness for the recovered Android app endpoint.

Example commands:

```bash
python3 giant_graphql_probe.py service-locations --zip 20010
GIANT_COOKIE_HEADER='datadome=...; __cf_bm=...' python3 giant_graphql_probe.py service-locations --zip 20010
python3 giant_graphql_probe.py search "milk" --service-location-id '<confirmed-service-location-id>'
```

The script:

1. Uses the recovered GraphQL endpoint and query documents.
2. Adds app-style Apollo/opco/platform headers.
3. Reads optional session cookies from `GIANT_COOKIE_HEADER` or `GIANT_DATADOME_COOKIE`.
4. Fails loudly on DataDome instead of pretending stale static prices are live prices.
5. Does not attempt to solve or bypass DataDome.

`giant_browser_api_probe.py` is the confirmed live web API harness.

Example commands:

```bash
python3 giant_browser_api_probe.py launch
python3 giant_browser_api_probe.py store
python3 giant_browser_api_probe.py search "milk" --rows 5
python3 giant_browser_api_probe.py product 151854
```

The script:

1. Launches or connects to a dedicated Chrome session with DevTools enabled.
2. Keeps Giant cookies/session state inside Chrome.
3. Resolves Park Road store `#0378` to service location `50000732`.
4. Queries live `/api/v5.0` product search/detail endpoints from inside the browser session.
5. Emits normalized product fields suitable for a future Giant price refresh pipeline.

## Sample Findings

The static catalog can resolve likely base products, for example:

- Giant Long Grain White Rice 5 lb bag: `$4.19`, product ID `56453`
- Giant 2% Reduced Fat Milk 1 gallon: `$3.99`, product ID `362945`
- Giant All Natural 80% Lean 20% Fat Ground Beef Fresh approx. 1.2 lb: `$8.75`, product ID `59367`

All of those test observations were marked stale because `priceValidUntil` was `2026-04-21` when researched on `2026-05-09`.

## Current Confidence

### Browser-Session API Good For

- Same-day live price lookup through Giant's current web app session.
- Store-specific Park Road pricing when using service location `50000732`.
- Current sale/member-style pricing exposed as `price` versus `regularPrice`.
- Product IDs, UPCs, package sizes, stock flags, unit prices, and coupon summaries.

### Static Catalog Good For

- Discovering Giant product names, URLs, images, and product IDs.
- Getting a rough base-price candidate when we lack better data.
- Building a local product index for matching meal-plan ingredients to Giant products.
- Finding item/package variants, especially for meats and store-brand staples.

### Not Yet Good For

- Plain unauthenticated shell refreshes, because `/api/v5.0` is DataDome-protected outside a browser session.
- Final cart total modeling, until cart fees, substitutions, random-weight handling, and coupons are reconciled.
- Account-specific coupon clipped state, unless a future logged-in capture layer is added.

## Flipp Weekly Circular API (confirmed 2026-05-09)

The Flipp digital flyer platform indexes Giant Food's weekly circular and exposes it through a public, unauthenticated JSON API. This is a clean shell-friendly source for Giant's current deal prices.

### Flipp API Surface

Giant Food merchant ID: `2520`

List current flyers near a zip code:

```text
GET https://backflipp.wishabi.com/flipp/flyers?locale=en-us&postal_code=20010
```

Returns flyers for all merchants near the zip. Filter client-side by `merchant_id == 2520`. Response includes `id`, `name`, `valid_from`, `valid_to`, and `categories_csv` per flyer.

Fetch all items from a specific flyer:

```text
GET https://backflipp.wishabi.com/flipp/flyers/{flyer_id}
```

Returns a complete item list with: `name`, `brand`, `price` (string), `valid_from`, `valid_to`, `cutout_image_url`, `pre_price_text` (for multi-buy deals like `"2/"`), `post_price_text`, `id`, `flyer_item_id`.

Search across all local flyers by keyword:

```text
GET https://backflipp.wishabi.com/flipp/items/search?locale=en-us&postal_code=20010&q={query}
```

Returns items from all merchants. Filter by `merchant_name == "Giant Food"`. Each item includes `current_price`, `original_price`, `pre_price_text`, `post_price_text`, `sale_story`, `valid_from`, `valid_to`, `flyer_id`, and `merchant_id`.

### Observed Flyer Data (2026-05-09)

Giant Food flyer `7914175` ("Weekly Ad"), valid 2026-05-08 to 2026-05-14, contained 207 items. Selected grocery-relevant prices:

| Item | Flyer Price | Notes |
|------|-------------|-------|
| Giant Grade A Large White Eggs | $3.00 | vs $5.29 in old manual data |
| Giant Butter | $3.49 | vs $5.49 in old manual data |
| Broccoli | $1.99/lb | |
| Assorted Pork Chops | $3.99 | |
| San Giorgio Pasta | 4/$5 ($1.25 each) | vs $1.99 in old manual data |
| Giant Jasmine Rice | $5.99 | 5 lb bag |
| Salmon Fillets | $8.99 | weekend deal, until 5/10 |
| Boneless NY Strip Steak | $7.99 | weekend deal, until 5/10 |
| Extra Jumbo Raw EZ Peel Shrimp | $6.99 | weekend deal, until 5/10 |
| Giant 93% Lean Ground Beef | $7.99 | or Nature's Promise 99% Lean Ground Turkey |
| White Mushrooms | $3.00 | |
| Premio Fresh Dinner Sausage | $3.99 | |
| Sargento Shredded Cheese | $4.00 | |
| Giant Rotisserie Chicken | $5.99 | |

### Flipp API Strengths

- Public, unauthenticated JSON. No DataDome, no browser session, no cookies.
- Returns the full weekly circular with 200+ items per cycle.
- Refreshes weekly with new flyer IDs.
- Covers sale/deal prices with exact valid-from/to dates.
- Works from plain `curl` or `requests`.

### Flipp API Limitations

- Only contains weekly circular/deal items, not the full catalog.
- No regular/base shelf prices — only promotional pricing.
- Price format varies: `"7.99"`, `"4/5"` (multi-buy), `"1.99/lb."` — requires parsing `pre_price_text` and `post_price_text`.
- Some items have `null` price (display-only circular items like "McCormick Assorted Spices").
- Not store-specific within the Giant DC market — the flyer covers the region, not one store.

## Instacart Giant Storefront (explored 2026-05-09)

Giant Food DC is on Instacart at:

```text
https://www.instacart.com/store/giant/storefront
```

(Note: `/store/giant-food-stores/` is the separate Pennsylvania chain.)

### What Works

The storefront page is server-side rendered (~2 MB HTML) and contains about 30 featured products with prices embedded in accessibility markup:

```text
<span class="screen-reader-only">Current price: $4.49</span>
<p aria-hidden="true" class="...">reg. $4.49</p>
```

### What Does Not Work

- **Search results** (`/store/giant/search/{query}`) return 200 but are fully client-rendered. No product data in the raw HTML — zero SSR prices.
- **Instacart's v3 REST API** (`/v3/containers/giant/search?q=milk`) returns `401 Unauthorized`.
- **Instacart's GraphQL API** (`/graphql`) requires persisted query IDs (`PersistedQueryNotSupported` on freeform queries).
- No product page links are exposed in the storefront HTML — only 4 navigation links.

### Instacart Verdict

Not viable as a shell-based price source for targeted product lookups. The storefront gives ~30 featured items, but we cannot search or browse the full catalog without a browser session. The same browser-session constraint we already have with Giant's own site would apply here, with no additional benefit.

## Giant Static Catalog Freshness (re-checked 2026-05-09)

Both category pages and product detail pages still show `priceValidUntil: 2026-04-21`, which is 18 days stale. The static SEO catalog has not refreshed since the original research. This confirms the static catalog is a product-discovery layer, not a live price source.

## Recommended Pipeline Position (updated 2026-05-09)

1. **Flipp weekly circular API** for Giant deal prices. Shell-friendly, no auth, refreshes weekly. Best for items currently on sale.
2. **Giant browser-session `/api/v5.0`** for live base/regular prices when a Chrome session is available. Best for items not in the flyer.
3. Recent user-observed Giant receipt/shelf/cart prices.
4. Giant static SEO catalog for product discovery and stale base-price estimates.

## Unexplored Base-Price Research Angles

The browser-session `/api/v5.0` path remains the cleanest live base-price source, but a few shell-friendly angles have not yet been probed. None are guaranteed, but each is cheap to test if the browser path becomes inconvenient.

### Peapod.com

Giant's static catalog already uses `peapod.com` image hosts, and the Peapod brand is the underlying Ahold Delhaize delivery service. The historical Peapod API base was:

```text
https://www.peapod.com/api/
```

with endpoints like `/api/v2.0/user/products`. Direct shell calls to `peapod.com/api/...` returned DataDome on initial research, but the Peapod brand itself has its own storefront and may carry a less aggressive bot configuration than `giantfood.com`. Worth probing the Peapod web app's own `/api/v3.0` or `/api/v5.0` routes from shell.

### Apollo Persisted-Query GET Pattern

The Android app uses Apollo with operation IDs like:

```text
getProducts: 4c9c74591cdaaaa294d3b143260860d90f0a715d336ebde307668cce70b5410c
getServiceLocations: 88920e787158b1f9ab621d4d50152861b1a5c6f91c3cd19bae8ef1e754309b20
```

The standard Apollo persisted-query GET pattern is:

```text
GET https://core.pdl.giantfood.com/prod/apollo/graphql
  ?operationName=getProducts
  &variables={"keywords":"milk", ...}
  &extensions={"persistedQuery":{"version":1,"sha256Hash":"4c9c74..."}}
```

Initial research only tested `POST` with freeform queries (got 403). The persisted-query `GET` form sometimes routes through different infrastructure that bypasses DataDome challenges for read-only operations. Worth testing on `getServiceLocations`, `getProducts`, and `GetWeeklyCircularDealsQuery`.

### Mobile and Legacy Subdomains

Possible alternative hosts not yet tested:

```text
m.giantfood.com
delivery.giantfood.com
pickup.giantfood.com
shop.giantfood.com
www.giantfood.com  (vs giantfood.com)
```

Some retailers run a legacy or mobile subdomain with looser bot protection. Quick HEAD requests would confirm whether any return 200 from shell.

### Static Catalog Freshness Watcher

`priceValidUntil` is uniformly stale (`2026-04-21`) across both category and product pages, suggesting a single bulk regeneration rather than per-page drift. A passive watcher could re-check a sentinel page weekly and flag when the catalog refreshes — the static catalog would then become a viable shell-friendly base-price source again, at least until it goes stale.

### Sitemap-Driven Product ID Harvest

The grocery sitemap is referenced in `robots.txt`:

```text
Sitemap: https://giantfood.com/groceries/sitemap.xml
```

It returned DataDome from shell during initial research, but if a working path is found later, it would let us harvest the full set of Giant product IDs for use against the browser-session `/api/v5.0/products/info/...` endpoint.

## Next Research Steps

1. Build `giant_flipp_deals.py` to pull the current Giant weekly circular from Flipp and match items against `meal_prices.json`.
2. Build `giant_refresh_prices.py` around `giant_browser_api_probe.py` for base/regular price refreshes.
3. Add high-confidence Giant matches to `meal_prices.json` without overwriting fresher manual/cart observations.
4. Compare Flipp deal prices against browser API prices to validate consistency.
5. Explore `availableDisplayCoupons`, `bmsm`, and `bmsmTiers` in the `/api/v5.0` product response.
6. Probe the unexplored angles above (Peapod, Apollo persisted-query GET, subdomains) when convenient.
7. Keep Android GraphQL documented as a useful secondary path, but deprioritize while web API and Flipp work.
