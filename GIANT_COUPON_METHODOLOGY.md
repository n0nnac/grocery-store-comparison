# Giant Coupon Methodology

This document describes how `giant_coupon_search.py` discovers and aggregates Giant Food digital coupons, and how `meal_price_tool.py` surfaces them in the cross-store cart view.

## Why a Hybrid Source

Giant exposes two endpoints that surface coupon data, and each has trade-offs.

### v7 Storewide Search

```text
POST /api/v7.0/coupons/users/{user_id}/prism/service-locations/{loc}/coupons/search
     ?fullDocument=true&unwrap=true
```

`paging.total` reports ~3,005 coupons in the catalog, but in practice the endpoint always returns the same first ~20 coupons regardless of pagination params. None of the following changed the response in our probes:

- Body params: `start`, `offset`, `from`, `page`, `pageSize`, `rows`, `size`, `paging.start`
- Filter params: `categoryTreeId`, `categoryTreeIds`, `filters`, `facetFilters`, `refinements`, `keywords`, `text`, `keyword`
- Query string variants of the above
- Different user IDs and service location IDs
- The `v8.0` variant of the endpoint (returns 254 entries with the same first-page-only behavior)

The savings page may use a different mechanism (per-category fetches, GraphQL, or a server-rendered first page), but for shell automation this endpoint behaves as a "top ~20 storewide promotions" snapshot rather than a full catalog mirror.

### v5 Per-Product Display Coupons

The product detail endpoint we already use for `giant_refresh_prices.py` carries an `availableDisplayCoupons` array per product:

```text
GET /api/v5.0/products/info/{user}/{loc}/{prodId}?extendedInfo=true&flags=true&substitute=true
```

Each saved Giant product carries 4–5 coupons directly relevant to it (often "meal bundle" offers like "Save $3 when you buy steak & eggs"). These records have a thinner schema than the v7 search response — no `productIds`/`categoryTreeIds`/`brandIds` scope arrays — but they include the source system, validity dates, max discount, clipping requirement, and per-user account state.

Walking the Giant product IDs already saved in `meal_prices.json` therefore gives us coupons that are **demonstrably relevant** to our planning catalog without paginating an unpaginatable storewide endpoint.

## Aggregation

`giant_coupon_search.py fetch` combines both sources into a single deduplicated catalog written to `giant_coupons.json`:

1. Pull the v7 storewide first page (configurable up to `--max-pages` calls).
2. For each saved Giant product ID in `meal_prices.json`, GET the product detail and harvest `availableDisplayCoupons`.
3. Dedupe by coupon `id`. When the same coupon appears in both sources, the richer v7 record wins for fields like `productIds` and `legalText`, while the per-product source contributes back-references.
4. Annotate each coupon with `matched_meal_keys` and `matched_product_ids` listing which saved items surfaced it.

Per-user `clipped`/`loaded`/`loadable` state is sanitized out of the tracked `giant_coupons.json` and written instead to `giant_coupon_account_state.local.json` (gitignored), mirroring the Safeway public/local split.

## Subcommands

```bash
python3 giant_coupon_search.py fetch --write
python3 giant_coupon_search.py fetch --no-storewide --only "eggs" "shredded cheese"
python3 giant_coupon_search.py search --query "meal bundle"
python3 giant_coupon_search.py search --category "Breakfast" --limit 10
python3 giant_coupon_search.py match --min-score 0.4 --keep 2
```

`fetch` runs both sources by default. `--no-storewide` skips the v7 step (useful when only product-relevant coupons matter); `--no-per-product` skips the v5 walk.

`match` scores active coupons against `meal_prices.json` items. The strongest signal is a `matched_meal_keys` back-reference (added during the per-product harvest); secondary signals include token overlap with the meal key and brand match.

## Cart Integration

`meal_price_tool.py cart --compare-stores` surfaces applicable Giant coupons after the cross-store totals:

- Loads `giant_coupons.json` (and merges account state from the gitignored local file when present).
- For each cart line, finds active coupons via the meal-key back-reference.
- Reports unique coupon count, line coverage, and aggregate max discount **if all bundle conditions are met**.
- With `--verbose`, lists each per-line coupon with its discount, clipping requirement, account state, and end date.

The aggregate max discount is intentionally **not** subtracted from the Giant subtotal. Most Giant coupons we have catalogued are bundle conditions (e.g. "Save $3 when you buy ground beef + tortillas + cheese"). Modelling whether a planned cart actually meets each bundle's product set is a future task; for now, the discount column is informational so the user can decide whether a bundle is worth completing.

## What This Methodology Does Not Do

- Mirror the full ~3,005-coupon Giant catalog. Only the first ~20 storewide coupons plus the union of per-product display coupons across saved meal items are captured.
- Auto-apply coupon discounts to Giant cart subtotals. Bundle conditions need to be modelled before the savings can be claimed reliably.
- Track clipping. The pipeline can read `clipped`/`loaded` state when present, but does not currently invoke a clip endpoint. This mirrors the read-only stance the Safeway coupon pipeline takes by default.

## Source Type and Confidence

- Source type: `giant_coupon_v7_api` (catalog file metadata)
- Per-coupon source system tags: `COP`, `ECI`, etc., preserved on each record
- Account state file: `giant_coupon_account_state.local.json` (gitignored)
