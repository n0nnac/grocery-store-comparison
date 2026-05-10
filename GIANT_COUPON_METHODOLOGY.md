# Giant Coupon Methodology

This document describes how `giant_coupon_search.py` discovers and aggregates Giant Food digital coupons, and how `meal_price_tool.py` surfaces them in the cross-store cart view.

## Why a Hybrid Source

Giant exposes three endpoints that surface coupon data, each with different strengths. The catalog mirror combines all three into `giant_coupons.json`.

### v7 Storewide Search

```text
POST /api/v7.0/coupons/users/{user_id}/prism/service-locations/{loc}/coupons/search
     ?fullDocument=true&unwrap=true
```

The endpoint **does** paginate, once you send the body shape the savings page actually uses. The page sends:

```json
{
  "query": {"start": 0, "size": 60},
  "filter": {
    "sourceSystems": ["QUO", "COP", "INM"],
    "loadable": true,
    "loaded": false
  },
  "copientQuotientTargetingEnabled": true,
  "sorts": [{"targeted": "desc"}]
}
```

The trick we missed at first is that `start` and `size` are nested inside a `query` object — top-level `start` / `rows` / `offset` / `page` are silently ignored, which is what made the endpoint look broken in early probes. We confirmed this by capturing the live request body via Chrome DevTools Protocol's `Network.requestWillBeSent` event during a fresh navigation to `/savings/coupons/browse`.

What we now know about the body fields:

- `query.start`, `query.size`: real pagination. Server caps `size` at 90 even when a larger value is requested.
- `filter.sourceSystems`: restrict to specific source-system tags (`COP`, `ECI`, `INM`, `QUO`, `PHX`, etc.).
- `filter.loadable`, `filter.loaded`: per-account scope. The page defaults to `loadable: true, loaded: false`, which yields ~257 coupons (loadable for this account but not yet loaded).
- `copientQuotientTargetingEnabled: true`: enables targeted/personalized matching, which dramatically narrows the result set.
- `sorts: [{targeted: "desc"}]`: sorts targeted-first; safe to include without restricting scope.

For a meal-planning catalog mirror we want the **full** ~3,051-coupon catalog, so the script defaults to no filter and `targeting_enabled=False`. The savings page's narrow ~257-coupon view can be reproduced with `--targeting-only` plus `--loadable-only --unloaded-only`.

### v5 Per-Product Display Coupons

The product detail endpoint we already use for `giant_refresh_prices.py` carries an `availableDisplayCoupons` array per product:

```text
GET /api/v5.0/products/info/{user}/{loc}/{prodId}?extendedInfo=true&flags=true&substitute=true
```

Each saved Giant product carries 4–5 coupons directly relevant to it (often "meal bundle" offers like "Save $3 when you buy steak & eggs"). These records have a thinner schema than the v7 search response — no `productIds`/`categoryTreeIds`/`brandIds` scope arrays — but they include the source system, validity dates, max discount, clipping requirement, and per-user account state.

Walking the Giant product IDs already saved in `meal_prices.json` adds the `matched_meal_keys` and `matched_product_ids` back-references onto each coupon record, which is the strongest signal the meal-item match scorer uses.

### v5 Per-Coupon Qualifying Products (scope resolution)

The savings-page `View Coupon Details` modal renders a "Qualifying Products" grid (e.g. 29 SKUs for the Cheez-It $2.49 coupon). We discovered the call that powers it by capturing the modal's network traffic via Chrome DevTools Protocol's `Network.requestWillBeSent` event:

```text
GET /api/v5.0/products/{user_id}/{service_location_id}
    ?couponId={coupon_id}&start=0&rows=200&sort=bestMatch+asc&flags=true
```

This is the standard products-search endpoint with a `couponId=` filter pivoting it from "products matching keywords" to "products this coupon applies to." The response is shaped like any other product search:

```json
{
  "response": {
    "products": [{"prodId": 2214, "name": "Cheez-It Original Baked Cheese Crackers", "brand": "Cheez-It", "size": "12.4 OZ BOX", ...}, ...],
    "pagination": {"start": 0, "rows": 200, "total": 29}
  }
}
```

This matters because the v7 search endpoint **never populates `productIds`** in its response — empirically 0 of the ~3,051 returned coupons carry a populated scope array. The v5-by-couponId call is the only authoritative source for per-coupon SKU scope. Without it, name-based matching is the only way to associate a coupon with a meal item, and that produces false positives ("ground beef" matching "Our Brand K-Cups or Ground Coffee" via single-token overlap, or cross-category leaks like "rice" matching "Pasta or Grain" coupons).

`giant_coupon_search.py scope` walks active ITEM-target coupons and back-fills their `product_ids` arrays from this endpoint. Resolution is rate-limited (~0.10 s default) and supports `--skip-resolved` for incremental runs. ORDER/cart-target coupons are skipped (they have basket conditions, not product scope).

## Aggregation

`giant_coupon_search.py fetch` combines the storewide + per-product sources into a deduplicated catalog written to `giant_coupons.json`. The `scope` subcommand then enriches it with authoritative SKU lists:

1. Paginate the v7 storewide endpoint with the real body shape (`{query: {start, size}, ...}`) until the catalog is exhausted (~35 pages at `size=90`).
2. For each saved Giant product ID in `meal_prices.json`, GET the product detail and harvest `availableDisplayCoupons`.
3. Dedupe by coupon `id`. When the same coupon appears in both sources, the per-product source contributes the back-references; the v7 record wins for `legalText` and other catalog fields.
4. Annotate each coupon with `matched_meal_keys` and `matched_product_ids` listing which saved items surfaced it.
5. Run `giant_coupon_search.py scope --write` to back-fill `product_ids` from the per-coupon qualifying-products endpoint for every active ITEM-target coupon. Each resolved coupon also gets `scope_total` and `scope_resolved_on` annotations.

Per-user `clipped`/`loaded`/`loadable` state is sanitized out of the tracked `giant_coupons.json` and written instead to `giant_coupon_account_state.local.json` (gitignored), mirroring the Safeway public/local split.

## Cart Application

`meal_price_tool.py cart --compare-stores` now subtracts item-scope coupon savings from the Giant subtotal. The matcher in `best_giant_coupon_for_item()`:

1. Loads `giant_coupons.json` and merges per-user clip state from the gitignored `giant_coupon_account_state.local.json`.
2. Filters to active, simple item coupons via `giant_coupon_is_simple_item()`:
   - `coupon_reward_target == "ITEM"`
   - `multi_qty != true`
   - Name does not contain `"bundle"`
   - Description does not contain bundle phrases (`"when you buy"`, `"with $X purchase"`, etc.)
3. Scores each coupon against each meal item via `giant_coupon_meal_score()`. Listed in priority order:
   - **1.5** if the coupon's `matched_meal_keys` back-reference (per-product harvest) includes this meal key — Giant told us this coupon applies to this saved product.
   - **1.4** if the coupon's authoritative `product_ids` (populated by `scope` resolution) includes the meal item's saved Giant `product_id` — Giant's qualifying-products list includes our SKU.
   - **0.0 (definitive non-match)** when `scope_resolved_on` is set, we have a saved giant `product_id`, and that ID is **not** in the coupon's `product_ids`. The token fallback is **skipped** for this coupon — Giant has explicitly told us its qualifying-products list, and our SKU is not on it. This is what eliminates the "ground beef → Our Brand Ground Coffee" class of false positives.
   - **1.0** when every "anchor" token of the meal key (non-descriptor tokens like `beef`, `cheese`, `rice`) appears in the coupon's name+description. Descriptor tokens like `ground`, `fresh`, `boneless` are not required. Used as a fallback when scope is unresolved for that coupon, or when the meal item has no saved Giant `product_id` to compare against.
   - **+0.20 store-brand boost** when both sides are Giant store brand (`Our Brand` / `Giant`-name match on the meal product, and `Our Brand` / `Giant ` in the coupon text).
4. Default `--giant-coupon-min-score 1.2` requires the store-brand alignment boost or stronger when relying on the token path. Either authoritative path (1.5 or 1.4) clears 1.2 on its own, so post-scope the floor is effectively governed by the authoritative signals.
5. Coupons that require clipping are blocked unless `--assume-giant-clipped` is passed or the local account-state file confirms the clipped status.

The Giant final subtotal in the cross-store summary now reads:

```
Giant pre-coupon subtotal: $37.71  (Flipp deals + Giant base)
Giant item-scope coupon savings: -$1.50  (2 lines applied)
Giant final (with item coupons): $36.21
```

This matches Safeway's pre-coupon/with-coupon split, so the cross-store comparison is symmetric for item-level discounts. **Cart-level Safeway coupons** still apply only on the Safeway side, and **bundle-condition Giant deals** are surfaced informationally below the summary but not auto-applied (their conditions on cart contents are not modeled yet).

## Subcommands

```bash
python3 giant_coupon_search.py fetch --write
python3 giant_coupon_search.py fetch --targeting-only --loadable-only --unloaded-only --write
python3 giant_coupon_search.py fetch --source-systems COP --source-systems ECI --write
python3 giant_coupon_search.py fetch --no-storewide --only "eggs" "shredded cheese"
python3 giant_coupon_search.py scope --write
python3 giant_coupon_search.py scope --skip-resolved --write
python3 giant_coupon_search.py scope --coupon-ids COP_7444913 --verbose
python3 giant_coupon_search.py search --query "meal bundle"
python3 giant_coupon_search.py search --category "Breakfast" --limit 10
python3 giant_coupon_search.py match --min-score 0.4 --keep 2
```

`fetch` runs both sources by default. `--no-storewide` skips the v7 step (useful when only product-relevant coupons matter); `--no-per-product` skips the v5 walk. `--targeting-only`, `--loadable-only`, `--unloaded-only`, and `--source-systems` map directly onto the v7 body fields the savings page uses.

`scope` resolves authoritative `product_ids` for each active ITEM-target coupon by querying the same `/api/v5.0/products?couponId=...` endpoint the savings-page modal uses. Use `--skip-resolved` for incremental runs and `--coupon-ids` / `--source-systems` / `--only` to scope a smaller batch. Use `--relevant-only` to filter the candidate pool down to coupons whose name/description shares anchor tokens with at least one tracked meal item — narrows ~2,800 active ITEM coupons to ~700.

DataDome (Giant's WAF) protects the per-coupon endpoint, so the sweep needs to behave more like a human session than a tight loop. Two resilience knobs help:

- `--warmup` triggers a `Page.reload` of the savings tab over CDP and waits 8s before the sweep starts, putting the browser in the same state DataDome sees during normal browsing.
- `--no-preflight` (default off; preflight is on by default) makes ONE call to the v5 scope endpoint with a known coupon ID before the sweep. If it 403s with a DataDome challenge, the script exits cleanly with a helpful message — no backoff cycles wasted. Resume later with `--skip-resolved` when DataDome clears.

The default sweep rate is `--sleep 1.0` (1 request per second). The empirical bad number was 0.10s (10 req/sec), which DataDome flagged after ~9 seconds. If a sweep does start triggering 403s mid-run, `--backoff` and `--max-retries` control the per-coupon recovery; `--abort-after-403s` (default 10) caps how long the script will keep banging on a blocked session before giving up. Once the catalog is fully scoped, weekly maintenance runs only need to resolve the new coupons (`--skip-resolved` skips the rest), which keeps each run small enough to stay under any rate threshold.

`match` scores active coupons against `meal_prices.json` items. The strongest signal is the authoritative `product_ids` overlap with the meal item's saved Giant SKU; next is a `matched_meal_keys` back-reference; secondary signals include token overlap with the meal key and brand match.

## Cart Integration

`meal_price_tool.py cart --compare-stores` surfaces applicable Giant coupons after the cross-store totals:

- Loads `giant_coupons.json` (and merges account state from the gitignored local file when present).
- For each cart line, finds active coupons via the meal-key back-reference.
- Reports unique coupon count, line coverage, and aggregate max discount **if all bundle conditions are met**.
- With `--verbose`, lists each per-line coupon with its discount, clipping requirement, account state, and end date.

The aggregate max discount is intentionally **not** subtracted from the Giant subtotal. Most Giant coupons we have catalogued are bundle conditions (e.g. "Save $3 when you buy ground beef + tortillas + cheese"). Modelling whether a planned cart actually meets each bundle's product set is a future task; for now, the discount column is informational so the user can decide whether a bundle is worth completing.

## What This Methodology Does Not Do

- Auto-apply bundle-condition Giant coupons to cart subtotals. Most "Save $3 when you buy [bundle]" coupons need their bundle conditions modelled against planned cart contents before the savings can be claimed reliably; for now they are surfaced informationally.
- Track clipping. The pipeline can read `clipped`/`loaded` state when present, but does not currently invoke a clip endpoint. This mirrors the read-only stance the Safeway coupon pipeline takes by default.
- Scope-resolve ORDER/cart-target coupons. Those have basket conditions (e.g. `$5 off $30 order`) rather than product scope, so the qualifying-products endpoint returns nothing meaningful for them and we skip them in the `scope` walk.

## Source Type and Confidence

- Source type: `giant_coupon_v7_api` (catalog file metadata)
- Per-coupon source system tags: `COP`, `ECI`, etc., preserved on each record
- Account state file: `giant_coupon_account_state.local.json` (gitignored)
