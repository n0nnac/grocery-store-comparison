# Giant Flipp Weekly Circular Methodology

This document describes how `giant_flipp_deals.py` pulls Giant Food's current weekly circular from the public Flipp flyer API, parses the prices, and matches them against `meal_prices.json` for meal planning.

## Why Flipp

Giant Food's own `/api/v5.0` web API is DataDome-protected from plain shell requests, and the static SEO catalog at `/groceries/...` carries stale `priceValidUntil` dates. The Flipp digital flyer platform indexes Giant's weekly circular at the regional level and exposes it as unauthenticated JSON. This is the cleanest shell-friendly source for Giant's dated deal prices.

Flipp data is dated. It is not a base-price source. It complements the browser-session V5 API, which remains the preferred path for live regular prices.

## Source Identity

- Merchant name: `Giant Food`
- Flipp merchant ID: `2520`
- Flipp host: `https://backflipp.wishabi.com/flipp`
- DC zip used: `20010`
- Park Road store context: `#0378`, 1345 Park Road N.W., Washington, DC 20010

The Pennsylvania Giant Food Stores chain is a separate company (different merchant ID under `/store/giant-food-stores`); do not substitute it for the DC Giant Food chain.

## Flipp Endpoints Used

### List flyers near a postal code

```text
GET https://backflipp.wishabi.com/flipp/flyers
  ?locale=en-us
  &postal_code=20010
```

Returns flyers for all merchants near the zip. Filter client-side by `merchant_id == 2520`.

### Fetch all items in a flyer

```text
GET https://backflipp.wishabi.com/flipp/flyers/{flyer_id}
```

Returns the master list of items: `id`, `name`, `brand`, `price`, `valid_from`, `valid_to`, `cutout_image_url`, optional `description`.

This endpoint omits `pre_price_text` and `post_price_text`, so multi-buy and per-pound deals are not visible from this response alone.

### Per-item detail

```text
GET https://backflipp.wishabi.com/flipp/items/{item_id}
```

Returns the full price metadata, including `pre_price_text` (e.g. `"4/"`) and `description` (e.g. `"5 lb. bag"`). The pipeline calls this once per flyer item to reconstruct multi-buy unit pricing.

### Free-text search across local flyers

```text
GET https://backflipp.wishabi.com/flipp/items/search
  ?locale=en-us
  &postal_code=20010
  &q={query}
```

Returns items from all local merchants. Filter by `merchant_name == "Giant Food"`. Search results carry both `pre_price_text` and `post_price_text` directly.

## Price Format Handling

Flipp packages price information across three fields:

- `current_price` — numeric value, may be string or number
- `pre_price_text` — prefix like `"2/"` for multi-buy deals
- `post_price_text` — suffix like `"/lb."` or `"/ea."` (search endpoint only)

`parse_price()` handles four kinds of pricing:

| Form | Example fields | Unit price |
|------|----------------|------------|
| Single | `current_price=3.49`, no pre/post | `$3.49` |
| Multi-buy | `current_price=5`, `pre="4/"` | `5 / 4 = $1.25 each` |
| Per pound | `current_price=1.99`, `post="/lb."` | `$1.99/lb` |
| Per each | `current_price=8.99`, `post="/ea."` | `$8.99/ea` |

Items without a parseable price (e.g. promotional copy with no number) get `current_price=None` and are dropped under `--only-priced`.

## Pipeline Stages

### Stage 1 — Discover the active flyer

`fetch_giant_flyers()` lists all flyers near zip `20010` and filters for `merchant_id=2520`. The most recent flyer by `valid_to` is selected unless `--flyer-id` overrides.

### Stage 2 — Fetch the master item list

`fetch_flyer_items(flyer_id)` returns ~200 items with name, brand, raw price, and description.

### Stage 3 — Enrich with multi-buy info

For each flyer item, `fetch_item_detail(item_id)` is called to recover `pre_price_text`. This step adds ~10 to 20 seconds per refresh but is needed to correctly distinguish a flat-price deal from a multi-buy deal. Without this enrichment, a `4 for $5` pasta deal would be recorded as `$5.00` instead of the correct `$1.25` per box.

`--no-enrich` skips this step for fast previews.

### Stage 4 — Normalize

`normalize_flipp_item()` produces a stable record:

```json
{
  "flipp_id": 1010725058,
  "flipp_flyer_id": 7914175,
  "name": "San Giorgio Pasta",
  "brand": "San Giorgio",
  "raw_price": "5",
  "pre_price_text": "4/",
  "post_price_text": null,
  "description": "Selected Varieties and Sizes",
  "current_price": 1.25,
  "original_price": null,
  "unit_kind": "multi_buy",
  "multi_buy_qty": 4,
  "price_display": "4 for $5.00",
  "valid_from": "2026-05-08",
  "valid_to": "2026-05-14",
  "image_url": "...",
  "shop_url": "..."
}
```

`current_price` is the per-unit price after multi-buy decomposition. `price_display` is the human-readable form for previews.

### Stage 5 — Persist

The normalized payload is written to `giant_weekly_deals_<valid_from>.json` so each week's flyer becomes a separate dated file, parallel to the Safeway weekly ad files.

### Stage 6 — Match against meal items

`command_match()` compares each `meal_prices.json` key against the flyer items using the same guarded matcher used by `meal_price_tool.py`:

- Tokenize the meal key and the flyer item name + brand + description (drop stopwords like `select`, `fresh`, `pack`, etc.).
- Compute `len(overlap) / len(meal_tokens)`.
- Apply a category-based negative penalty when the flyer item contains tokens that are inconsistent with the meal item's category (e.g. a "frozen" + "chicken" item is penalized for matching a "frozen spinach" meal item).
- Hard-reject form mismatches that token overlap tends to miss, such as `butter` vs croissants, fresh mushrooms vs canned mushrooms, fresh/raw protein vs cooked/breaded/prepped protein, and lean-ratio mismatches such as `80/20` vs `93% lean`.
- Apply anchor-token requirements for distinctive terms like `tortillas`, `spinach`, `teriyaki`, `tenderloin`, `raw`, and `sweet`.
- Apply package-size compatibility scoring so a `5 lb bag` item does not silently match a much smaller package unless the downstream price is explicitly normalized.
- Add a small bonus when the flyer item's brand is `Giant`.

The match command outputs a side-by-side table with the meal key, the flyer name, the parsed price, the description, and the score. The user reviews matches and decides whether to promote any to a price layer; `--write` saves the structured match summary.

## What This Pipeline Does Not Do

- It does not write Flipp prices into `meal_prices.json` `base_prices`. Flipp data is a dated overlay, like Safeway's weekly ad.
- It does not log in or capture cookies. Every call is unauthenticated.
- It does not bypass DataDome or any bot protection. The Flipp flyer endpoints are public.
- It does not promise per-store accuracy. A Giant Food regional flyer applies to the regional flyer footprint; the Park Road store may have additional shelf adjustments not visible in the circular.
- It does not infer base/regular prices from Flipp data. The `original_price` field is usually `null` in Giant's circular records.

## Refresh Cadence

Giant's flyer rolls over weekly. A Sunday or Monday refresh of `python3 giant_flipp_deals.py fetch --write --only-priced` is enough to keep the deal layer current.

## Example Commands

```bash
python3 giant_flipp_deals.py fetch --write --only-priced
python3 giant_flipp_deals.py fetch --no-enrich  # quick preview without multi-buy parsing
python3 giant_flipp_deals.py search "ground beef"
python3 giant_flipp_deals.py match --min-score 0.4
python3 giant_flipp_deals.py match --only "eggs" --only "rice"
python3 giant_flipp_deals.py varieties --meal-key "shredded cheese"
python3 giant_flipp_deals.py varieties --name "Chobani Flip" --json
```

## Expanding Selected Varieties Into Live SKUs

Many flyer items advertise "Selected Varieties" without listing the qualifying SKUs. The `varieties` subcommand expands a single flyer item into the live Giant SKUs that currently qualify, by querying the browser-session V5 API and filtering on:

- Brand match (with normalization for punctuation, and a special case for Giant store brand which the API tags as `"Our Brand"` while the product name begins with `"Giant"`)
- Sale status (`flags.sale == true`)
- Per-unit price within tolerance of the flyer's per-unit price (relaxed for multi-buy deals, since Giant's API exposes the at-pop price rather than the deal-threshold price)
- Package size matches the flyer's `5-8 oz pkg`-style size range when one is parseable

Output includes per-SKU `prodId`, name, brand, size, current sale price, regular price, unit price, and UPC. The `--json` flag emits a structured payload that the meal-inspiration prompt can consume directly so the AI knows which specific flavors and sizes are part of the deal.

The `varieties` subcommand requires a Chrome session launched via `giant_browser_api_probe.py launch`. It is the only Flipp subcommand that needs a browser session; `fetch`, `search`, and `match` work from plain shell.

## Integration With Meal Planning

Once a `giant_weekly_deals_<valid_from>.json` file is on disk, `meal_price_tool.py` exposes the matched view through its own subcommand:

```bash
python3 meal_price_tool.py giant-deals
python3 meal_price_tool.py giant-deals --matched-only --all
python3 meal_price_tool.py giant-deals --min-score 0.4
```

This view loads the most recent active flyer file, applies the guarded matcher, and prints each meal item alongside the matched flyer item, package description, expiration date, and per-store base prices. Items without a Giant base price fall back to comparing against the Safeway base, which surfaces cross-store switching opportunities. When the flyer price is for a package that differs from the saved planning unit, the displayed sale price is normalized to the planning unit and the raw flyer display is shown in parentheses.

The integration is read-only with respect to base prices. `meal_price_tool.py` does not promote Flipp deal prices into `meal_prices.json` `base_prices`. The `estimate --compare-stores` and `cart --compare-stores` flows may use Flipp as a dated sale overlay, but only after planning-unit normalization and only when multi-buy thresholds are satisfied by the selected lines. Promotion of any Flipp deal into the base price layers requires manual review.

## Source Type and Confidence

Per `DATA_SOURCE_POLICY.md`:

- Source type: `giant_flipp_circular_api`
- Confidence label: `giant_flipp_dated_deal`

These are dated overlays and must not overwrite a fresher live Giant browser-session API observation or a verified shelf/receipt observation.
