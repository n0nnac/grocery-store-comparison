# Research / archival scripts

These scripts aren't part of the day-to-day meal-planning workflow. They're preserved here so the repo carries the discovery history without cluttering the root.

Each one is still runnable from this directory; the ones that import root utilities have a `sys.path` shim at the top so `python3 research/<script>.py …` Just Works.

## What's in here

### `giant_graphql_probe.py`
First-pass discovery of Giant's mobile GraphQL endpoint. Used early to understand the schema. Superseded by the v5 browser API; kept as a record of the GraphQL surface in case it becomes useful again.

### `giant_catalog_search.py`
Static catalog fallback for Giant — searches the unauthenticated `https://giantfood.com/api/v6.0/...` catalog when the CDP browser session isn't available. Useful for ad-hoc product lookups but not part of any pipeline.

### `giant_coupon_detail_probe.py`
The CDP probe that captured the savings-page "View Coupon Details" modal traffic and revealed the per-coupon qualifying-products endpoint (`/api/v5.0/products?couponId=...`). Did its job; preserved as documentation of how the discovery happened, in case a similar investigation is needed later.

### `grocery_compare.py`
Earliest tool in the repo — a fuzzy-match CLI over a hand-maintained `prices.json` file. Pre-dates `meal_price_tool.py` and the structured pipeline. Kept for historical curiosity.

### `safeway_cart_capture.py`
Bridge from the Safeway cart/checkout page (via CDP) to a structured observed-cart JSON. Used together with `safeway_cart_reconcile.py` to verify that modeled cart totals match what Safeway charges. Audit tooling, not planning tooling.

### `safeway_cart_reconcile.py`
Compares an observed Safeway cart capture against the modeled cart estimate in `meal_price_tool.py`. Useful for catching modeling drift after price refreshes; not part of the planning loop.

## Why these moved

The repo grew a lot of one-time discovery scripts and parallel audit tools. To keep the root focused on the active planning pipeline (`plan.py`, the refreshers, `safeway_meal_inspiration.py`, `meal_price_tool.py`), anything that wasn't load-bearing for end-to-end weekly meal planning landed here.

If you need to revive any of these into the main flow, just move it back up. The shims expect to live under `research/`, so update the path arithmetic if you do.
