# Safeway Coupon Methodology

This document describes the repeatable process for reading Safeway clippable coupon and deal offers for the configured local store. Product-specific coupon observations belong in `safeway_coupons.json`, not in this methodology document.

## Store Context

- Store: Safeway, 1701 Corcoran St NW, Washington, DC 20009
- Store ID: `923`
- Coupon page: `https://www.safeway.com/loyalty/coupons-deals`

## Current Goal

Populate a coupon overlay that can be applied after base prices and weekly ad deals.

Coupon data should distinguish:

- the public gallery offer
- the offer's clippable status
- the offer value text
- inferred discount type when it is obvious
- valid date range
- UPC eligibility when available from offer detail
- whether the offer is already clipped, when authenticated state is available
- whether the offer awards bonus rewards points instead of reducing the price

## Sources Used

### Coupon Gallery API

Primary read-only gallery source:

```text
GET https://www.safeway.com/abs/pub/xapi/offers/companiongalleryoffer
```

Required parameters:

```text
storeId=923
rand=<random>
includeRedmBonusPathFPOffers=true
```

Useful fields:

- `offerId`
- `offerPgm`
- `brand`
- `name`
- `category`
- `events`
- `ecomDescription`
- `forUDescription`
- `description`
- `status`
- `isClippable`
- `startDate`
- `endDate`
- `usageType`
- `limits`

This endpoint returns the coupon gallery without requiring a cart mutation. In an unauthenticated context, all offer statuses may appear as unclipped.

### Coupon Detail API

Primary read-only detail source:

```text
GET https://www.safeway.com/abs/pub/xapi/offers
```

Required parameters:

```text
offerId=<OFFER_ID>
storeId=923
offerPgm=<OFFER_PROGRAM>
includeUpc=y
```

Useful fields:

- `offerDetail`
- `offerEndDate`
- `offerProgramType`
- `offerProtoType`
- `upcList`

This is the current bridge between coupons and product prices. If an offer exposes UPCs, those UPCs can be resolved through `safeway_api_search.py` and compared against product IDs, prices, and meal-planning ingredients.

## Current Workflow

Run the full reusable pipeline:

```bash
python3 safeway_coupon_pipeline.py --account-state --write
```

Default pipeline stages:

1. Refresh the public coupon gallery.
2. Merge public offer facts into `safeway_coupons.json` while preserving existing detail, UPC resolution, and account-state fields.
3. Retain account-only or recently missing saved offers unless `--no-keep-missing` is passed.
4. If `--account-state` is passed, launch a temporary copied Chrome profile, read logged-in clipped/unclipped state, then delete the copy.
5. Enrich default high-value slices: `Meat & Seafood`, `department_threshold`, `basket_threshold`, `points_bonus`, and clipped line-item coupons.
6. Validate duplicate IDs and account-state shape before writing.

The pipeline replaces `python3 safeway_coupon_search.py --all --write` for normal refreshes because the older command overwrites `safeway_coupons.json` instead of merging preserved fields.

Search coupon gallery:

```bash
python3 safeway_coupon_search.py bacon
python3 safeway_coupon_search.py --category "Meat & Seafood"
```

Fetch offer details and eligible UPCs:

```bash
python3 safeway_coupon_search.py bacon --with-details
```

Resolve eligible UPCs through the product-price API:

```bash
python3 safeway_coupon_search.py "ground beef" --with-details --resolve-upcs
```

Write the current coupon overlay:

```bash
python3 safeway_coupon_search.py --all --write
```

Use that overwrite command only for experiments or rebuilding from scratch. For normal updates, use the pipeline command above.

Enrich already-saved offers by high-value slice:

```bash
python3 safeway_coupon_enrich.py --category "Meat & Seafood" --resolve-upcs --write
python3 safeway_coupon_enrich.py --application-kind department_threshold --write
```

Use targeted enrichment before full-gallery enrichment. Offer details are quick, but manufacturer coupons can expose long UPC lists. Product resolution is capped by default in `safeway_coupon_enrich.py`; use `--max-upcs 0` only when full UPC coverage is worth the extra requests.

Refresh logged-in account clipped state:

```bash
python3 safeway_coupon_account_state.py --cdp-url http://127.0.0.1:9223 --add-new --write
```

This command expects a Chrome DevTools Protocol endpoint with a logged-in `safeway.com` page. The safest repeatable setup is a temporary copy of the Chrome profile, launched with remote debugging, so the real browser profile is not mutated. The account-state script only performs `GET`/same-origin `fetch` reads.

Example temporary-profile launch:

```bash
tmp="$(mktemp -d /tmp/safeway-chrome-profile.XXXXXX)"
mkdir -p "$tmp/Default"
cp "$HOME/Library/Application Support/Google/Chrome/Local State" "$tmp/Local State"
cp "$HOME/Library/Application Support/Google/Chrome/Default/Preferences" "$tmp/Default/Preferences"
cp "$HOME/Library/Application Support/Google/Chrome/Default/Cookies" "$tmp/Default/Cookies"
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --headless=new \
  --remote-debugging-port=9223 \
  --remote-allow-origins=http://127.0.0.1:9223 \
  --user-data-dir="$tmp" \
  --no-first-run \
  --disable-gpu \
  "https://www.safeway.com/loyalty/mylist"
```

Manually observed account-specific coupons can be stored in:

```text
safeway_coupon_overrides.json
```

Use that only for temporary account-specific coupons that are not visible in the unauthenticated gallery. Each manual coupon still needs the same `application` structure as a scraped coupon.

## Account And Clipping State

Every coupon offer should have an `account_state` object:

```json
{
  "source_type": "unauthenticated_gallery",
  "clipped": null,
  "clip_status_confirmed_on": null,
  "household_specific": null
}
```

Field rules:

- `source_type` records how the account state was observed, not where the offer itself came from.
- `clipped: null` means unknown. It does not mean unclipped.
- `clipped: true` should only be written from a logged-in read or explicit user confirmation.
- `clipped: false` should only be written from a logged-in read where the offer is available but not clipped.
- `household_specific` should remain `null` unless an authenticated source or manual account observation proves the offer is personalized.

Cart estimates may show a coupon as mathematically eligible while still flagging `clip unknown` or `clip needed`. That is intentional: eligibility and account state are separate facts.

## Logged-In Read-Only Account State

Working read-only endpoints observed from a logged-in Safeway page:

```text
GET /abs/pub/dce/offergallery/J4UProgram1/services/gallery/companion/clipped?storeId=923
GET /abs/pub/dce/offergallery/J4UProgram1/services/gallery/companion/v1/offers?storeId=923
```

Observed behavior:

- `companion/clipped` returns clipped offers and includes account clipping fields such as `clipId` and `clipTs`.
- `companion/v1/offers` returns the logged-in gallery visible to the account.
- Offers present in `companion/clipped` are stored as `account_state.clipped: true`.
- Offers present in the logged-in gallery but absent from `companion/clipped` are stored as `account_state.clipped: false`.
- Saved offers absent from the logged-in gallery are left unchanged rather than inferred unavailable.

Manual overrides should be marked inactive when a real account coupon supersedes them, to avoid double-counting cart-level savings.

## Data Hygiene Rules

- Do not write coupon prices into `base_prices`.
- Do not assume an offer is clipped just because it is clippable.
- Treat unauthenticated `status` values as gallery state, not confirmed account state.
- Use coupon UPCs for product matching when available.
- If a coupon applies to a different package size than the meal-planning base item, record it as a separate applicable product or coupon overlay.
- If a coupon applies to a department or basket threshold, record it as a cart-level overlay rather than assigning it to one product.
- If an offer awards points, keep the clippable offer in coupon data but value the earned points through `safeway_rewards.json`.
- Keep clipped/unclipped mutation endpoints out of read-only refresh scripts.

## Cart-Level And Department Threshold Coupons

Some coupons do not apply to a specific UPC. They apply to a cart subtotal, product set, or department subtotal. These should be modeled with:

- `application.kind`: `department_threshold` or `basket_threshold`
- `application.scope`: the eligible department, category, or offer scope
- `application.threshold_amount`: the minimum eligible spend
- `application.discount`: the amount or points earned
- `application.allocation`: `cart_level`

Calculation rule:

1. Price eligible line items first using base prices, weekly ads, and product-specific coupons.
2. Sum the eligible subtotal for the department or basket scope.
3. If the eligible subtotal meets the threshold and the coupon is clipped/usable, subtract the coupon as a cart-level savings line.
4. For per-serving estimates, either show the cart-level savings separately or allocate it proportionally across eligible line items. Do not mutate item base prices.

This prevents a department coupon from being incorrectly attached to a single package of meat or seafood.

Implementation reference:

```bash
python3 meal_price_tool.py cart ground_beef_lunch_bowls --verbose
```

## Known Limitations

- The gallery endpoint can show clippable offers, but unauthenticated reads do not prove account-specific clipped state.
- Direct "offers by UPC" lookup is authenticated and returned unauthorized without a valid session.
- Some offers expose value text that is easy to parse, while others require manual interpretation.
- Basket conditions, household-specific eligibility, and coupon stacking still need a logged-in read path.
