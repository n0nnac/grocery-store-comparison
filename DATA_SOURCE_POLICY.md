# Data Source Policy

This project keeps price layers separate so meal plans can distinguish regular prices, current store prices, weekly ad deals, and future clipped coupons.

## Pricing Layers

### Base Price

The regular price used for planning when no dated sale or coupon applies.

Use:

- Safeway API `basePrice` when present.
- Giant live API price if a future confirmed endpoint exposes a current regular/base price for the correct store.
- Giant static catalog price only as a fallback, and only with explicit freshness metadata.
- PDP/card regular price when the API cannot resolve the product.
- Current price only when no regular price is available, and mark the source accordingly.

Do not use weekly ad prices or clipped coupon prices as base prices.

### Current Price

The store's currently exposed price for a product. This may be a member price, temporary sale price, or shelf price.

Use Safeway API `price` as current price. If `price` differs from `basePrice`, preserve both.

### Weekly Ad Deal

A dated advertised override from Safeway's weekly ad or preview ad.

Weekly ad entries must include:

- valid date range
- sale price
- unit
- whether clipping is required
- limit or condition when visible
- multi-buy or mix-and-match threshold when visible

Weekly ad deals should live in weekly deal files, not in `base_prices`.

Multi-buy weekly ad prices should only be applied when the modeled cart reaches the advertised threshold. If the threshold is not met, use a non-weekly fallback price when available and clearly mark the weekly price as blocked.

### Weekly Deal Base-Distance Observation

An API-backed estimate of how far a weekly ad price is from a comparable regular product price.

Use:

- Safeway product search API results for the same store context.
- `basePrice` or `basePricePer` from a high/medium confidence product match.
- Current API price as a matching signal, not as the source of the weekly ad sale price.

Store these observations separately from both base prices and weekly ad prices. They are useful for scoring meal inspiration and deciding whether a deal is unusually exciting this week.

Do not use low-confidence matches for scoring. Keep them in the observation file for audit and refinement.

### Coupon Or Digital Offer

Coupon data is its own overlay layer because it may require:

- account login
- clipped/unclipped state
- household-specific eligibility
- one-time limits
- basket conditions

Coupon data should not overwrite base prices or weekly ad prices.

The current read-only implementation can capture public clippable offers and, when available, UPC eligibility. Confirmed clipped state remains account-specific.

Department or basket threshold coupons should be applied after line-item pricing as cart-level savings. They should not be assigned to a single item unless the offer detail explicitly restricts the offer to that item or UPC.

Coupon records should separate offer facts from account facts:

- offer facts: value text, description, category, validity window, UPC eligibility
- account facts: clipped state, household-specific eligibility, confirmation date

Unknown clipped state must be stored as `null`, not inferred from gallery status.

### Rewards Points

Rewards points are a value layer, not a price layer.

Use rewards data for:

- base points earned from eligible spend
- bonus points from point multiplier offers
- point redemption valuation
- product reward comparisons

Do not subtract rewards value from item prices. Report it as estimated future value unless a reward is actively redeemed against the current cart.

Point multiplier offers should stay in coupon/deal data when they come from the clippable offer gallery. Redemption rules and product reward options should stay in `safeway_rewards.json`.

### Meal Inspiration

Meal inspiration is a recommendation layer, not a price layer.

Use it for:

- ranking active weekly ad ingredients that are worth cooking around
- identifying sale proteins and produce that make this week different from a normal pantry plan
- generating recipe seeds and future chat context
- surfacing Trader Joe's support-staple notes only from saved price observations

Do not write inspiration scores, recipe ideas, or missing-price assumptions into base prices.

If a weekly ad item lacks a comparable saved base price, keep it eligible for inspiration but mark the base comparison as unavailable. A deal can be interesting without being fully quantified.

### Returned-Plan Price Resolution

Returned meal plans may include Safeway purchase ingredients that are missing from the local catalog or whose saved Safeway observation is stale.

Use Safeway product search API resolution for:

- `price_key` values prefixed with `resolve:`
- Safeway returned-plan ingredients missing from `meal_prices.json`
- saved Safeway product IDs older than the configured stale threshold

Returned-plan resolution is read-only by default. It can be used in the estimate output without mutating `meal_prices.json`.

When `--write-resolved` is used, high/medium-confidence API matches may be promoted into `meal_prices.json` and `safeway_price_observations.json`. Promotion should write the cleaned ingredient key for `resolve:` items, store full product ID evidence, and preserve base/current price distinction.

Low-confidence API matches should not be priced. Keep them missing rather than quietly using a questionable product.

### Giant Static Catalog Resolution

Giant's reachable `/groceries/...` static pages are a discovery and fallback price layer.

Use them for:

- product name and package variant discovery
- Giant product URL discovery
- Giant product ID discovery from product detail pages
- fallback base-price candidates when no fresher Giant source exists

Do not treat Giant static catalog data as same-day cart pricing unless its `priceValidUntil` is current on the observation date. If `priceValidUntil` is older than `observed_on`, mark the observation stale and do not overwrite a fresher manual, cart, receipt, or future live API observation.

Because store-specificity is not yet proven for the static catalog, these observations should carry the Park Road store context for the user's workflow but remain lower confidence than any source that verifies store `#0378` directly.

### Giant Live GraphQL Resolution

Giant's Android app exposes an Apollo GraphQL endpoint at:

```text
https://core.pdl.giantfood.com/prod/apollo/graphql
```

The recovered app queries include `getServiceLocations` and `getProducts`, which should be able to model store-specific product prices once a validated DataDome browser/app session is available.

Use Giant GraphQL observations only when:

- the request is made through a normal, user-authorized browser/app session or an explicitly supplied browser cookie header
- the response confirms the Park Road store by service-location/store metadata, not only by ZIP
- product records include live price fields from the GraphQL `products` result
- the observation stores the operation name, service location ID, product ID/UPC, observed date, and source type

Do not attempt to solve or bypass DataDome in code. A DataDome 403 is a source-access blocker, not a pricing result.

### Giant Flipp Weekly Circular Resolution

The Flipp digital flyer platform indexes Giant Food's current weekly circular and exposes it through a public, unauthenticated JSON API. This is the cleanest shell-friendly source for Giant's dated deal prices.

Use Flipp circular observations only when:

- the request is made against the public endpoints `https://backflipp.wishabi.com/flipp/flyers?locale=en-us&postal_code=<zip>` and `https://backflipp.wishabi.com/flipp/flyers/{flyer_id}`
- the flyer's `merchant_id` is `2520` (Giant Food); other Giant brands such as Giant Food Stores (PA) carry different merchant IDs and should not be substituted
- prices are parsed correctly across the observed Flipp formats: single price, multi-buy (`"2/"`, `"3/"`), per-pound (`"/lb."`), or per-each (`"/ea."`)
- the observation stores the Flipp flyer ID, item ID, valid-from and valid-to dates, raw `current_price`, `pre_price_text`, `post_price_text`, and source type

Flipp deal prices are dated overlays. They should live in `giant_weekly_deals*.json` files alongside Safeway weekly ad data, not in `base_prices`. They cover sale/promotional items only, not the full Giant catalog. They are not store-specific within the Giant DC market — the flyer applies to the regional flyer footprint, not solely to store `#0378`.

Do not attempt to use Flipp circular prices as base prices. Do not overwrite a fresher live Giant browser API observation with a Flipp circular price.

### Giant Browser V5 API Resolution

Giant's web app exposes live JSON product data under same-origin `/api/v5.0/...` endpoints when called from a normal validated browser session.

Use Giant browser API observations when:

- the request is made from a user-authorized Chrome session that can normally browse Giant prices
- the store is resolved through `/api/v5.0/serviceLocation/stores/378?serviceType=B`
- the response confirms Park Road service location `50000732` or another explicitly selected Giant service location
- product records include live `price`, `regularPrice`, `unitPrice`, `unitMeasure`, `prodId`, and `upc`
- observations store the endpoint path, service location ID, product ID/UPC, observed date, and source type

Do not export, log, or persist browser cookies. Browser-session API calls should run inside the browser context via Chrome DevTools Protocol.

### Cart Reconciliation

Observed Safeway cart or checkout data is an audit layer.

Use it to compare:

- expected line totals
- observed line totals
- coupon savings
- reward redemptions
- taxes
- fees
- final checkout total
- points earned

Do not write observed checkout totals into base prices without tracing the difference back to a stable source such as a product price, active sale, coupon, tax rule, fee, or random-weight item.

## Source Type Taxonomy

Use these source types consistently:

- `search_substitute_api`: Safeway browser-facing product search API.
- `scraped_pdp`: active product detail page price.
- `scraped_related_card`: related or similar product card on a Safeway page.
- `official_recipe_page`: official Safeway recipe ingredient source.
- `official_bundle_page`: official Safeway bundle source.
- `weekly_ad`: Safeway weekly ad or preview ad.
- `coupon_gallery_api`: Safeway coupon/deal gallery API.
- `coupon_account_gallery_api`: logged-in read-only coupon gallery API.
- `coupon_detail_api`: Safeway offer detail API with UPC eligibility.
- `official_rewards_faq`: Safeway/Albertsons official rewards program terms or FAQ.
- `rewards_dashboard`: logged-in Rewards dashboard account-state read.
- `rewards_dashboard_capture`: logged-in Rewards dashboard text capture transformed by the rewards importer.
- `giant_static_seo_catalog`: Giant `/groceries/...` static catalog JSON-LD product/offer source.
- `giant_browser_v5_api`: Giant live `/api/v5.0` product observation made from a validated browser session.
- `giant_flipp_circular_api`: Giant Food weekly circular item from the Flipp public flyer API.
- `safeway_cart_manual`: manually filled Safeway cart/checkout observation.
- `safeway_cart_browser_capture`: read-only browser capture of visible Safeway cart/checkout text.
- `local_cart_model`: local expected cart model built from saved pricing layers.
- `cart_reconciliation`: local comparison between model and observed Safeway cart.
- `observed_manual`: manually observed price where details are limited.
- `estimate`: planning placeholder only.

## Confidence Levels

Recommended confidence labels:

- `api_price_doc`: exact product ID returned by Safeway's product API.
- `scraped`: active product page price was visible.
- `scraped_card`: related-card product was visible and separately identified.
- `official_safeway_page`: official Safeway page, but not a product search/PDP source.
- `coupon_gallery`: coupon/deal gallery offer observed, but not confirmed clipped.
- `coupon_detail_with_upcs`: coupon detail observed with UPC eligibility.
- `official_rewards_rule`: public rewards earning or redemption rule observed from official program docs.
- `account_rewards_tile`: reward tile observed in the logged-in account dashboard.
- `giant_static_current`: Giant static catalog observation whose `priceValidUntil` is current on `observed_on`; still not final-cart confidence unless store context is verified.
- `giant_static_stale`: Giant static catalog observation whose `priceValidUntil` is older than `observed_on`.
- `giant_graphql_live`: Giant Android Apollo GraphQL product observation from a validated browser/app session.
- `giant_browser_live`: Giant `/api/v5.0` product observation from a validated browser session for the selected store.
- `giant_flipp_dated_deal`: Giant Food weekly circular deal from the Flipp public flyer API, valid only inside the flyer's date range.
- `manual_observation`: manually entered observation.
- `estimate`: not verified.

## Update Precedence

For base/current price refreshes:

1. Exact product-ID match from `search_substitute_api`.
2. Exact product-ID match from Giant browser `/api/v5.0` for store `#0378`.
3. Exact product-ID match from confirmed Giant live GraphQL for store `#0378`.
4. Active product detail page.
5. Related product card with explicit product ID and name.
6. Official Safeway recipe or bundle page.
7. Manual observation.
8. Current Giant static catalog observation.
9. Stale Giant static catalog observation.
10. Estimate.

Weekly ad deals and future coupons are not part of this precedence ladder because they are dated overlays.

## Store Rules

All Safeway prices should be tied to:

- `store_id`: `923`
- store address: 1701 Corcoran St NW, Washington, DC 20009
- product URL with `?loc=923` when possible

If a source cannot verify store context, mark it lower confidence and do not overwrite a stronger source.

All Giant prices should be tied to:

- store number: `#0378`
- store address: 1345 Park Road, NW, Washington, DC 20010
- browser API service location ID: `50000732` when using service type `B`
- source URL when available
- `priceValidUntil` when sourced from the static catalog

If a Giant source cannot verify store `#0378`, mark it lower confidence and do not overwrite a stronger source.

## Data Hygiene

- Preserve raw observation detail in `safeway_price_observations.json`.
- Keep `meal_prices.json` normalized for meal-planning use.
- Use `safeway_coupon_pipeline.py` for normal coupon refreshes so existing detail and account-state fields are merged, not overwritten.
- Keep account-specific or manually observed coupons in `safeway_coupon_overrides.json` until authenticated coupon-state reads are automated.
- Store coupon `account_state` separately from the public offer fields.
- Mark manual coupon overrides inactive when a logged-in account coupon supersedes them.
- Keep rewards earning and redemption valuation in `safeway_rewards.json`.
- Keep point multiplier offers in `safeway_coupons.json`; they are clippable offer overlays.
- Keep product rewards separate from product base prices and compare them against current product prices at redemption time.
- Treat dashboard product reward price resolutions as planning estimates unless exact product IDs/UPCs are confirmed.
- Keep meal inspiration scores and chat context separate from source price data.
- Keep weekly deal API base-distance observations in their own files unless a human promotes a high-confidence product to `meal_prices.json`.
- Keep Giant static catalog observations below live API, cart, receipt, and manual observations in precedence.
- Avoid product-specific methodology notes in methodology docs.
- Do not guess missing prices.
- Do not overwrite a stronger source with a weaker source.
- Do not write sale or coupon prices into `base_prices`.
- Record `observed_on` whenever price data is refreshed.
