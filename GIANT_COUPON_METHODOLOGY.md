# Giant Coupon Methodology

This document describes how `giant_coupon_search.py` discovers and aggregates Giant Food digital coupons, and how `meal_price_tool.py` surfaces them in the cross-store cart view.

## Why a Hybrid Source

Giant exposes two endpoints that surface coupon data, and each has different strengths.

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

## Aggregation

`giant_coupon_search.py fetch` combines both sources into a single deduplicated catalog written to `giant_coupons.json`:

1. Paginate the v7 storewide endpoint with the real body shape (`{query: {start, size}, ...}`) until the catalog is exhausted (~35 pages at `size=90`).
2. For each saved Giant product ID in `meal_prices.json`, GET the product detail and harvest `availableDisplayCoupons`.
3. Dedupe by coupon `id`. When the same coupon appears in both sources, the richer v7 record wins for fields like `productIds` and `legalText`, while the per-product source contributes the back-references.
4. Annotate each coupon with `matched_meal_keys` and `matched_product_ids` listing which saved items surfaced it.

Per-user `clipped`/`loaded`/`loadable` state is sanitized out of the tracked `giant_coupons.json` and written instead to `giant_coupon_account_state.local.json` (gitignored), mirroring the Safeway public/local split.

## Cart Application

`meal_price_tool.py cart --compare-stores` now subtracts item-scope coupon savings from the Giant subtotal. The matcher in `best_giant_coupon_for_item()`:

1. Loads `giant_coupons.json` and merges per-user clip state from the gitignored `giant_coupon_account_state.local.json`.
2. Filters to active, simple item coupons via `giant_coupon_is_simple_item()`:
   - `coupon_reward_target == "ITEM"`
   - `multi_qty != true`
   - Name does not contain `"bundle"`
   - Description does not contain bundle phrases (`"when you buy"`, `"with $X purchase"`, etc.)
3. Scores each coupon against each meal item via `giant_coupon_meal_score()`:
   - **1.5** if the coupon's `matched_meal_keys` back-reference (per-product harvest) includes this meal key.
   - **1.4** if the coupon's `product_ids` includes the meal item's saved Giant `product_id`.
   - **1.0** when every "anchor" token of the meal key (non-descriptor tokens like `beef`, `cheese`, `rice`) appears in the coupon's name+description. Descriptor tokens like `ground`, `fresh`, `boneless` are not required.
   - **+0.20 store-brand boost** when both sides are Giant store brand (`Our Brand` / `Giant`-name match on the meal product, and `Our Brand` / `Giant ` in the coupon text).
4. Default `--giant-coupon-min-score 1.2` requires the store-brand alignment boost or stronger. Drop to `1.0` to allow plain anchor-token matches (catches more, but also pulls in coupons for competing brands).
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
python3 giant_coupon_search.py search --query "meal bundle"
python3 giant_coupon_search.py search --category "Breakfast" --limit 10
python3 giant_coupon_search.py match --min-score 0.4 --keep 2
```

`fetch` runs both sources by default. `--no-storewide` skips the v7 step (useful when only product-relevant coupons matter); `--no-per-product` skips the v5 walk. `--targeting-only`, `--loadable-only`, `--unloaded-only`, and `--source-systems` map directly onto the v7 body fields the savings page uses.

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
