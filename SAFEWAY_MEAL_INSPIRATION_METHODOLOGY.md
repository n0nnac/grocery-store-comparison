# Safeway Meal Inspiration Methodology

This document describes how the project turns weekly ad data into meal ideas. It is intentionally product-agnostic so the method stays useful as weekly ad items change.

## Goal

The meal inspiration layer answers a different question than the cart optimizer:

- Cart optimizer: What will this planned cart likely cost?
- Meal inspiration: What current deals are worth building meals around this week?

The inspiration layer should favor sale proteins, produce, freezer-friendly items, and ingredients that make it easier to try something new. It should not simply choose the absolute cheapest possible meal.

## Inputs

Use these data sources:

- Active `weekly_deals*.json` file selected by the dated validity window.
- `normalized_deals` from the weekly ad file.
- Optional `safeway_weekly_deal_base_observations_YYYY-MM-DD.json` API enrichment.
- `meal_prices.json` for saved Safeway base prices and known Trader Joe's support-staple comparisons.

Do not write new base prices into `meal_prices.json` inside this workflow. Missing saved base prices can be supplemented by API enrichment, but the enrichment stays in its own observation file.

## Deal Classification

Each weekly deal is classified into a planning role:

- `protein`
- `produce`
- `pantry`
- `beverage`
- `other`

Classification is keyword-based and should remain conservative. If a deal is a canned, boxed, or shelf-stable product, prefer pantry classification even when the product name includes a produce word.

## Base-Price Comparison

When possible, compare the weekly ad sale price against the saved Safeway base price.

A comparison is considered safe when:

- the deal can be matched to a saved ingredient by exact name, curated alias, or high-confidence fuzzy match
- the sale unit and saved base-price unit are compatible enough for meal-planning use

If a saved base price is unavailable or not comparable, the weekly deal enrichment pipeline may query Safeway's product search API for a likely matching product. API enrichment should be used for scoring only when:

- the product search result has high or medium confidence
- the department is compatible with the deal role
- the API result exposes a base price in a comparable unit
- package, each, and per-pound units are not mixed

Low-confidence API observations should remain visible in the observation file but should not drive meal inspiration scoring.

If the units are not safely comparable, keep the base price visible but do not compute savings. This avoids treating package prices, per-pound prices, and portion prices as interchangeable.

## Inspiration Scoring

The score is a planning heuristic, not a financial truth.

The score should reward:

- proteins and produce more than pantry or beverages
- unusually low sale prices for the item role
- known savings against a comparable saved base price or high/medium confidence API base observation
- larger percentage discounts, not only absolute dollar savings
- freezer-friendly items
- ingredients that support experimentation or less routine meals
- deals that combine well with other current weekly ad items

The score may lightly penalize:

- required clipping
- multi-buy conditions
- weak or missing comparability to saved base prices

Required clipping should be recorded explicitly. It should not be treated as impossible, but any generated shopping guidance must surface it.

Multi-buy promotions should be modeled as hard pricing constraints. For mix-and-match deals such as "when you buy 5+ participating items," the returned plan should either:

- include enough participating units across the whole shopping plan to meet the threshold
- or explicitly warn that the advertised weekly price may not apply

The local estimator evaluates these thresholds at the full plan/cart level, not recipe-by-recipe.

## Meal Idea Generation

Meal ideas are deterministic recipe seeds built from:

- one sale anchor, usually a protein
- one or more current sale produce or pantry components
- support ingredients that may come from Safeway, Trader Joe's, pantry inventory, or a future store comparison

Meal ideas should include:

- sale ingredients used
- support ingredients needed
- why the idea is interesting this week
- Trader Joe's notes only when saved data says Trader Joe's is cheaper and no current Safeway sale beats that saved comparison
- clipping notes when relevant
- freezer notes when relevant

## Chat Context

The context command emits JSON for a future chat/LLM workflow. The context should include:

- source file and validity metadata
- ranked deal candidates
- meal idea seeds
- source assumptions
- warnings about missing base prices, unconfirmed coupons, and limited Trader Joe's coverage

The chat layer should use this context as grounded input, not as permission to invent prices.

## External Prompt Contract

The prompt command emits a paste-ready Markdown prompt for another chat instance. That prompt includes:

- ranked weekly deals
- discount and clipping context
- a saved pricing catalog
- allowed `price_key` values
- a strict return schema

The external chat should return valid JSON with version `meal_inspiration_plan_v1`. Every priced ingredient must include:

- `name`
- `price_key`
- `source`
- `quantity`
- `unit`

The `price_key` should exactly match either a saved ingredient key or a weekly deal key from the exported catalog when one exists. Optional, pantry, or non-purchased items that should not be resolved locally should be returned under `unpriced_items`.

For purchased Safeway ingredients that are needed but missing from the exported catalog, the external response may use:

- `source`: `Safeway`
- `price_key`: `resolve:<snake_case_name>`
- `needs_price_resolution`: `true`
- a practical `quantity` and `unit`

The local estimator will query Safeway's product API for those ingredients during `estimate-plan`. It will also refresh stale saved Safeway prices for returned plans when the saved observation is older than the configured stale threshold.

Auto-resolution is read-only by default. Add `--write-resolved` to promote high/medium-confidence API matches into `meal_prices.json` and `safeway_price_observations.json`.

When a returned `price_key` is prefixed with `resolve:`, the durable catalog key should be the cleaned ingredient name, not the `resolve:` key itself. For example, `resolve:cilantro` becomes `cilantro` after promotion.

Returned JSON can be estimated locally with:

```bash
python3 meal_price_tool.py estimate-plan returned_meal_plan.json
```

The estimator can also read a Markdown response if it contains a fenced JSON block.

If the exported catalog includes a `promotion` object on any weekly deal, the external response should include `promotion_checks` showing planned quantity against the threshold. The local estimator still independently checks the promotion and will fall back to a base/API-base price when the threshold is not met.

## Use-Up Prompt Contract

The use-up prompt command exports the same weekly deal and pricing context, plus ingredients the user already owns.

Owned ingredients should:

- appear in `pricing_catalog.owned_items`
- use a `price_key` prefixed with `owned:`
- be returned as recipe ingredients with `source` set to `owned`
- be treated as zero incremental cost by the estimator

The external chat should build around the owned ingredients first, then use weekly deal ingredients as supporting add-ons when they make the dish more interesting or economical. If an owned ingredient is skipped, the response should explain why in `owned_ingredient_usage` or `shopping_notes`.

Example:

```bash
python3 safeway_meal_inspiration.py use-up-prompt "ground beef" "artichokes" --write
python3 meal_price_tool.py estimate-plan returned_use_up_plan.json
```

## Maintenance Rules

- Keep weekly deals separate from base prices.
- Keep this methodology product-agnostic.
- Add aliases only when a recurring deal maps cleanly to a saved ingredient.
- Do not hide uncertainty. If a base comparison is unavailable, say so.
- Do not model coupon discounts here unless coupon state and eligibility are available from the coupon overlay layer.
