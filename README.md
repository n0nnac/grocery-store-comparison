# Grocery Store Comparison

Personal grocery price and meal-planning tooling for comparing Safeway, Trader Joe's, and Giant around ZIP `20037`.

The current strongest automation path is Safeway store-specific product pricing for store `923`:

- Safeway, 1701 Corcoran St NW, Washington, DC 20009
- Product URLs use `?loc=923` when possible
- Weekly ad prices are kept separate from regular/base prices

## Quick Commands

Search Safeway's browser-facing product API:

```bash
python3 safeway_api_search.py "chicken thighs" --rows 5
python3 safeway_api_search.py "960139991" --rows 3 --json
```

Search Giant's reachable static grocery catalog fallback:

```bash
python3 giant_catalog_search.py "ground beef"
python3 giant_catalog_search.py "rice" --json
```

Giant catalog results include freshness metadata from `priceValidUntil`. Treat stale static catalog prices as product-discovery/base-candidate evidence, not same-day store pricing.

Launch and query Giant's live browser-session API:

```bash
python3 giant_browser_api_probe.py launch
python3 giant_browser_api_probe.py store
python3 giant_browser_api_probe.py search "milk" --rows 5
python3 giant_browser_api_probe.py product 151854
```

The browser API probe uses a dedicated Chrome session as the authorized transport, then calls Giant's same-origin `/api/v5.0` endpoints from inside the page. It does not print or store cookies. Park Road store `#0378` currently resolves to service location `50000732` for service type `B`.

Probe Giant's recovered mobile GraphQL endpoint:

```bash
python3 giant_graphql_probe.py service-locations --zip 20010
GIANT_COOKIE_HEADER='datadome=...; __cf_bm=...' python3 giant_graphql_probe.py service-locations --zip 20010
python3 giant_graphql_probe.py search "milk" --service-location-id '<confirmed-service-location-id>'
```

The GraphQL probe uses the Android app endpoint and query documents, but plain shell requests are currently DataDome-blocked. Keep it as a secondary research path; the browser-session `/api/v5.0` probe is now the preferred Giant live-price path.

Pull Giant's current weekly circular from the public Flipp flyer API:

```bash
python3 giant_flipp_deals.py fetch --write --only-priced
python3 giant_flipp_deals.py search "ground beef"
python3 giant_flipp_deals.py match --min-score 0.4
```

Flipp data is the cleanest shell-friendly source for Giant's dated deal prices. It complements the browser-session V5 API, which remains the preferred path for live regular prices. See `GIANT_FLIPP_METHODOLOGY.md` for details.

Refresh saved Safeway product IDs without writing files:

```bash
python3 safeway_refresh_prices.py --dry-run
```

Write refreshed Safeway base/current price observations:

```bash
python3 safeway_refresh_prices.py --write
```

Search Safeway clippable coupons/deals:

```bash
python3 safeway_coupon_search.py "ground beef" --with-details --resolve-upcs
python3 safeway_coupon_search.py --category "Meat & Seafood"
```

Run the reusable coupon refresh pipeline:

```bash
python3 safeway_coupon_pipeline.py --account-state --write
```

Lower-level coupon commands are still available for probing:

```bash
python3 safeway_coupon_enrich.py --category "Meat & Seafood" --resolve-upcs --write
python3 safeway_coupon_account_state.py --cdp-url http://127.0.0.1:9223 --add-new --write
```

List Safeway weekly deal overlays:

```bash
python3 meal_price_tool.py deals --all
```

List Giant Flipp weekly circular deals matched to saved meal items:

```bash
python3 meal_price_tool.py giant-deals
python3 meal_price_tool.py giant-deals --matched-only --all
python3 meal_price_tool.py giant-deals --min-score 0.4
```

The match column shows the flyer item name, package description, and the deal expiration day. Items without a Giant base price fall back to comparing against the Safeway base, which surfaces cross-store switching opportunities.

Rank weekly ad ingredients for meal inspiration rather than pure cheapest-cart optimization:

```bash
python3 safeway_weekly_deal_enrich.py --write
python3 safeway_meal_inspiration.py deals --limit 15
python3 safeway_meal_inspiration.py ideas
python3 safeway_meal_inspiration.py context --write
python3 safeway_meal_inspiration.py prompt --write
python3 meal_price_tool.py estimate-plan returned_meal_plan.json
```

`estimate-plan` automatically resolves missing or stale Safeway purchase ingredients through the Safeway product API. Returned plan JSON can use `source: Safeway`, `price_key: resolve:<name>`, and `needs_price_resolution: true` for ingredients that were not in the exported catalog.

Export a use-up prompt for ingredients already in the fridge/freezer/pantry:

```bash
python3 safeway_meal_inspiration.py use-up-prompt "ground beef" "artichokes" --write
python3 meal_price_tool.py estimate-plan returned_use_up_plan.json
```

Useful returned-plan resolution switches:

```bash
python3 meal_price_tool.py estimate-plan returned_meal_plan.json --no-resolve-missing
python3 meal_price_tool.py estimate-plan returned_meal_plan.json --stale-days 7
python3 meal_price_tool.py estimate-plan returned_meal_plan.json --write-resolved
```

Estimate the saved meal-plan recipes:

```bash
python3 meal_price_tool.py estimate
```

Estimate a cart with weekly prices and cart-level coupons:

```bash
python3 meal_price_tool.py cart ground_beef_lunch_bowls --verbose
python3 meal_price_tool.py cart ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner --verbose
```

Weekly ad mix-and-match thresholds are checked when estimating carts and returned meal-inspiration plans. If a deal requires buying 5+ participating items and the modeled plan only has 4, the estimator blocks the weekly price and uses a fallback where available.

Build and reconcile an expected cart against a Safeway cart/checkout observation:

```bash
python3 safeway_cart_reconcile.py expected ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner
python3 safeway_cart_reconcile.py template ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner --output safeway_cart_observed_template.json
python3 safeway_cart_capture.py capture --cdp-url http://127.0.0.1:9223 --output safeway_cart_capture_2026-05-08.json
python3 safeway_cart_capture.py parse safeway_cart_capture_2026-05-08.json --template safeway_cart_observed_template.json --output safeway_cart_observed_from_capture_2026-05-08.json
python3 safeway_cart_reconcile.py compare ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner --observed safeway_cart_observed_template.json
```

Show item-level coupon matches for saved ingredients:

```bash
python3 meal_price_tool.py coupon-matches --verbose
```

List Safeway rewards redemption values and point multiplier offers:

```bash
python3 meal_price_tool.py rewards --affordable --limit 25 --only-valued
python3 meal_price_tool.py point-offers
```

Import a captured Rewards dashboard point-tab snapshot:

```bash
python3 safeway_rewards_import.py safeway_rewards_dashboard_capture_2026-05-07.json --resolve-prices --write
python3 safeway_rewards_import.py safeway_rewards_dashboard_capture_2026-05-07.json --adjacent-capture safeway_rewards_adjacent_capture_2026-05-07.json --resolve-prices --write
```

Compare the original simple grocery list:

```bash
python3 grocery_compare.py milk eggs bread
```

## Files

- `meal_prices.json`: normalized ingredient prices for meal planning.
- `safeway_price_observations.json`: source observations by Safeway product ID.
- `weekly_deals.json`: current weekly ad sale overlays.
- `weekly_deals_preview_2026-05-08.json`: preview ad sale overlays when Safeway publishes the next ad early.
- `safeway_coupons.json`: current read-only coupon/deal gallery overlay.
- `safeway_coupon_overrides.json`: temporary manual/account-specific coupon overlay until logged-in coupon-state reads are automated.
- `safeway_rewards.json`: Safeway points earning rules, redemption valuation options, and future product reward captures.
- `safeway_rewards_dashboard_capture_2026-05-07.json`: account dashboard text capture for current point redemption tiles.
- `safeway_rewards_adjacent_capture_2026-05-07.json`: account dashboard text capture for adjacent Rewards tabs.
- `safeway_cart_observed_template.json`: fill-in template for observed Safeway cart/checkout totals.
- `safeway_cart_capture_2026-05-08.json`: raw read-only browser capture from the Safeway cart page.
- `safeway_cart_observed_from_capture_2026-05-08.json`: parsed observed-cart file from the raw capture.
- `meal_inspiration_context_YYYY-MM-DD.json`: generated context for future chat-based weekly meal idea workflows.
- `meal_inspiration_prompt_YYYY-MM-DD.md`: paste-ready prompt for an external chat instance to produce priceable meal ideas.
- `meal_use_up_prompt_YYYY-MM-DD.md`: paste-ready prompt for an external chat instance to use owned ingredients plus weekly deal inspiration.
- `safeway_weekly_deal_base_observations_YYYY-MM-DD.json`: API-backed base-price distance observations for weekly ad deals.
- `prices.json`: original simple three-store comparison dataset.
- `safeway_api_search.py`: focused CLI for probing Safeway product search.
- `giant_catalog_search.py`: focused CLI for probing Giant's reachable static grocery catalog fallback.
- `giant_browser_api_probe.py`: focused CLI for probing Giant's live browser-session `/api/v5.0` product API.
- `giant_graphql_probe.py`: focused CLI for probing the recovered Giant Android Apollo GraphQL endpoint.
- `safeway_refresh_prices.py`: repeatable refresh workflow for saved Safeway product IDs.
- `safeway_coupon_pipeline.py`: reusable end-to-end coupon refresh pipeline.
- `safeway_coupon_search.py`: read-only coupon/deal gallery scraper with optional UPC resolution.
- `safeway_coupon_enrich.py`: targeted enrichment workflow for saved coupon offers.
- `safeway_coupon_account_state.py`: read-only logged-in coupon clipped-state updater.
- `safeway_rewards_import.py`: importer for Rewards dashboard point-tab captures and optional product-value resolution.
- `safeway_cart_capture.py`: read-only Safeway cart/checkout page capture and parser.
- `safeway_cart_reconcile.py`: expected-cart builder and observed-cart reconciliation CLI.
- `safeway_weekly_deal_enrich.py`: uses Safeway product search to estimate regular-price distance for active weekly ad deals.
- `safeway_meal_inspiration.py`: weekly-ad meal inspiration scorer, context generator, and external prompt exporter.
- `meal_price_tool.py`: meal and weekly-deal reporting helper.
- `grocery_compare.py`: original grocery-list comparison CLI.
- `SAFEWAY_SCRAPE_METHODOLOGY.md`: clean methodology for collecting Safeway base prices.
- `SAFEWAY_COUPON_METHODOLOGY.md`: clean methodology for collecting coupon/deal overlays.
- `SAFEWAY_REWARDS_METHODOLOGY.md`: clean methodology for valuing and capturing Safeway rewards points.
- `SAFEWAY_CART_RECONCILIATION_METHODOLOGY.md`: clean methodology for comparing local cart estimates with Safeway cart/checkout.
- `SAFEWAY_MEAL_INSPIRATION_METHODOLOGY.md`: clean methodology for turning weekly ad deals into meal ideas.
- `SAFEWAY_API_RESEARCH.md`: experiment log for discovered Safeway endpoints.
- `GIANT_API_RESEARCH.md`: experiment log for Giant/Peapod API probing and static catalog fallback.
- `DATA_SOURCE_POLICY.md`: rules for base prices, current prices, weekly deals, and future coupons.

## Data Flow

1. Ingredient names and store prices live in `meal_prices.json`.
2. Safeway product IDs in `meal_prices.json` can be refreshed through `safeway_refresh_prices.py`.
3. Refreshes write detailed evidence to `safeway_price_observations.json`.
4. Base prices in `meal_prices.json` use regular prices where available.
5. Weekly ads and future coupons are separate dated overlays, not base-price replacements.
6. Coupon gallery data lives in `safeway_coupons.json`.
7. Account-specific/manual coupon observations live in `safeway_coupon_overrides.json`.
8. `safeway_coupon_pipeline.py` refreshes the public gallery, preserves enrichment/account fields, enriches targeted slices, and optionally refreshes logged-in account state.
9. Saved coupon offers can be enriched with detail records, eligible UPCs, and product matches.
10. Logged-in coupon state can mark account-specific clipped/unclipped state without mutating coupons.
11. Coupon account state is tracked separately from offer existence.
12. Safeway rewards rules and redemption values live in `safeway_rewards.json`.
13. Meal-planning estimates combine base prices with eligible weekly deal overlays.
14. Cart estimates apply cart-level coupons after line-item pricing and report clipping state.
15. Cart estimates report rewards points as future-value credits, not as same-transaction discounts.
16. Cart reconciliation compares the local estimate against an observed Safeway cart/checkout breakdown.
17. Weekly deal enrichment can query Safeway product search for API-backed base-price distance.
18. Meal inspiration exports grounded prompts for external recipe generation without mutating base prices.
19. Use-up inspiration exports grounded prompts that treat owned ingredients as zero incremental cost.
20. `meal_price_tool.py estimate-plan` prices returned recipe JSON from the external prompt contract.
21. Giant live pricing should prefer `giant_browser_api_probe.py` for store `#0378` / service location `50000732`.
22. Returned-plan estimates can auto-resolve missing or stale Safeway ingredients through the product API.
23. `--write-resolved` promotes high/medium-confidence resolved Safeway API prices into `meal_prices.json` and `safeway_price_observations.json`.
24. Giant static catalog search can discover product names, URLs, product IDs, and fallback price candidates.
25. Giant static catalog observations must remain below live API/cart/receipt/manual observations unless current store-specific pricing is verified.
26. Giant mobile GraphQL remains a secondary research path while the browser-session `/api/v5.0` path works.

## Current Scope

The current system is designed to answer:

- What is the regular/base Safeway price for known ingredients?
- What is the current Safeway price for known products?
- Which weekly ad items are worth using or freezing?
- Which clippable coupon/deal offers may reduce those prices further?
- How many Safeway points should a meal-plan cart earn, and what are those points worth under known redemption options?
- What should a one-person dinner plus lunch meal prep cost after weekly deals and clipped coupons?
- Where does Safeway checkout disagree with the local cart model?
- What current weekly ad ingredients are especially worth cooking around for experimentation?
- What can I cook to use up ingredients I already have while still taking advantage of this week's deals?
- What Safeway price should be used for a returned ingredient that was missing from the local catalog or has stale data?
- What Giant product/page should be used as a fallback match when no stronger Giant price observation exists?

Future authenticated coupon and rewards work should add confirmed clipped/account state, current rewards balance, and product reward tiles without changing the base-price fields.
