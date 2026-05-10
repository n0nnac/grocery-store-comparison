# Grocery Store Comparison

Personal grocery price and meal-planning tooling for comparing Safeway, Trader Joe's, and Giant around ZIP `20037`.

The goal is a small end-to-end pipeline: **scan base + deal prices across stores, identify exploration-leaning meal candidates, and return a full plan with recipes, shopping list, and pricing in a single round.** Everything else in the repo supports that.

## The end-to-end flow

```bash
# 1. Refresh data + emit cross-store planning context
python3 plan.py week

# 2. Read the context (or paste to an LLM) and produce a plan JSON
#    matching pricing_return_contract.schema in the emitted context.

# 3. Price the plan and get a shopping list with cross-store breakdown.
python3 plan.py price plan.json
```

`plan.py` is a thin wrapper that orchestrates the underlying refresh / context / pricing scripts; see `plan.py --help` for the subcommands. The "planning" step is intentionally outside the script — that's where a human or LLM produces the JSON the pricer round-trips.

The emitted context lists this week's standout deals from both stores plus base prices for known ingredients. The saved catalog is presented as a price hint, **not** as a constraint on what to cook — any ingredient is fair game. Unknown items get marked `needs_price_resolution: true` and are resolved live against Safeway's search API at price-time. The prompt language explicitly discourages defaulting to past-week dishes; ask for novelty if a returned plan feels too familiar.

## Lower-level commands

The pieces `plan.py` calls (and that you can run individually):

```bash
# Refresh saved Safeway base prices via the Safeway product API
python3 safeway_refresh_prices.py
python3 safeway_refresh_prices.py --write --fill-missing-only

# Refresh saved Giant base prices via a Chrome CDP browser session
python3 giant_browser_api_probe.py launch        # one-time per session
python3 giant_refresh_prices.py --write --fill-missing-only

# Pull Giant's current Flipp circular
python3 giant_flipp_deals.py fetch --write --only-priced

# Generate the planning context / paste-ready prompt
python3 safeway_meal_inspiration.py context --write
python3 safeway_meal_inspiration.py prompt --write
python3 safeway_meal_inspiration.py use-up-prompt "ground beef" --quantity 2 --unit lb --portions 4 --single-dish --meal-prep --write

# Price a returned plan JSON (estimate-plan resolves missing Safeway items live)
python3 meal_price_tool.py estimate-plan plan.json --write-resolved

# Cross-store cart estimate (saved meal recipes)
python3 meal_price_tool.py cart ground_beef_lunch_bowls --compare-stores --verbose
```

## Secondary accuracy layers

These improve pricing precision but aren't required for the E2E flow:

```bash
# Safeway clippable coupon gallery + per-account clip state
python3 safeway_coupon_pipeline.py --account-state --write

# Giant coupons (full catalog mirror + per-coupon authoritative SKU scope)
python3 giant_coupon_search.py fetch --write
python3 giant_coupon_search.py scope --relevant-only --skip-resolved --warmup --write

# Safeway rewards points dashboard import
python3 safeway_rewards_import.py safeway_rewards_dashboard_capture_*.json --resolve-prices --write
```

`giant_coupon_search.py scope` resolves authoritative `product_ids` for every active ITEM-target coupon by hitting the same per-coupon qualifying-products endpoint the Giant savings page uses internally. With scope resolved, the cart matcher treats the absence of a meal item's SKU from a coupon's `product_ids` as a definitive non-match, which kills cross-category false positives.

The cart `--compare-stores` mode gives a symmetric Safeway-vs-Giant view including each store's coupon savings.

## Files

Live data:
- `meal_prices.json` — normalized ingredient catalog (saved bases + price sources for both stores)
- `weekly_deals*.json`, `giant_weekly_deals_*.json` — current dated weekly circulars
- `safeway_coupons.json`, `giant_coupons.json` — coupon gallery mirrors (sanitized)
- `safeway_rewards.json` — earning / redemption rules

Sources of truth (single-store evidence files):
- `safeway_price_observations.json`, `giant_price_observations.json`
- `safeway_weekly_deal_base_observations_*.json`

Account-specific local files (gitignored):
- `safeway_coupon_account_state.local.json`
- `safeway_rewards_account_state.local.json`
- `giant_coupon_account_state.local.json`

Methodology docs:
- `SAFEWAY_SCRAPE_METHODOLOGY.md`, `SAFEWAY_COUPON_METHODOLOGY.md`, `SAFEWAY_REWARDS_METHODOLOGY.md`, `SAFEWAY_CART_RECONCILIATION_METHODOLOGY.md`, `SAFEWAY_MEAL_INSPIRATION_METHODOLOGY.md`
- `GIANT_FLIPP_METHODOLOGY.md`, `GIANT_COUPON_METHODOLOGY.md`
- `SAFEWAY_API_RESEARCH.md`, `GIANT_API_RESEARCH.md`
- `DATA_SOURCE_POLICY.md` — the contract for which observation type beats which

Research / archived discovery scripts: see `research/README.md`.

## Project context

- Default Safeway: store `923`, 1701 Corcoran St NW, Washington, DC 20009, `?loc=923` on product URLs
- Default Giant: store `#0378` (Park Road) → service location `50000732`, service type `B`
- Weekly ad prices are kept separate from regular/base prices; never overwrite a base with a deal
- Account-specific data (clip state, rewards balances, cart captures) lives in `*.local.json` files that are gitignored
- The repo treats Trader Joe's as a price-only fallback (no API access)
