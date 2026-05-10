# Safeway Rewards Methodology

This document describes the repeatable process for modeling Safeway rewards points. Product-specific reward tiles belong in `safeway_rewards.json`, not in this methodology document.

## Scope

Rewards are separate from prices, weekly ad deals, and coupons.

Use this layer to answer:

- how many points a cart should earn
- which clippable offers add bonus points
- what points are worth under known redemption options
- whether redeeming points for a product is better than paying the current product price

Do not write points value into item base prices or weekly deal prices.

## Official Program Rules

Primary sources:

- Rewards dashboard: `https://www.safeway.com/rewards/dashboard`
- Public program FAQ: `https://www.safeway.com/faq/foru.html`

Current planning assumptions stored in `safeway_rewards.json`:

- Eligible grocery purchases earn 1 point per whole dollar.
- Eligible gift card purchases earn 2 points per whole dollar.
- Qualifying pharmacy purchases earn 1 point per whole dollar.
- Some categories are excluded from points, including alcohol, tobacco, fuel, selected services, taxes, fees, bottle deposits, and fluid dairy.
- Points can be redeemed for grocery discounts, participating fuel discounts, automatic cash off, or selected product rewards.
- Product reward options are account/dashboard-specific and should be refreshed separately from public coupon data.

## Data Files

Rewards rules and redemption options live in:

```bash
safeway_rewards.json
```

Account-specific state such as current points balance and auto-cash-off status
lives in ignored local overlay data:

```bash
safeway_rewards_account_state.local.json
```

Captured dashboard tab text can be kept as evidence in dated files such as:

```bash
safeway_rewards_dashboard_capture_2026-05-07.json
safeway_rewards_adjacent_capture_2026-05-07.json
```

Point multiplier offers from the coupon/deal gallery live in:

```bash
safeway_coupons.json
```

This split matters because an "Earn 3X Points" offer behaves like a clippable deal, while "$20 off groceries for 1200 points" behaves like a redemption choice. Public rewards valuation should be commit-safe; account balance state should stay local.

## Public Point Offers

The coupon gallery can expose point multiplier offers with values like:

- `Earn 2X Points`
- `Earn 3X Points`
- `Earn 10X Points`

The coupon parser records these as:

```json
{
  "discount": {
    "kind": "points_multiplier",
    "multiplier": 3
  },
  "application": {
    "kind": "points_bonus",
    "allocation": "tagged_products"
  }
}
```

Application types:

- `cart_level`: safe to apply when the cart subtotal and scope satisfy the terms.
- `tagged_products`: visible offer exists, but eligible products require detail UPCs, dashboard data, or in-store tag confirmation.
- `requires_terms_check`: do not apply automatically.

## Account-Specific Dashboard Data

The dashboard is the future source for:

- current points balance
- selected or redeemed rewards
- product reward tiles
- reward expiration
- whether automatic cash off is enabled

These facts should be captured as account state. A dashboard read should not mutate the account, clip offers, redeem points, or add items to the cart.

Recommended schema for product rewards:

```json
{
  "id": "dashboard_reward_id",
  "name": "Reward display name",
  "type": "product_reward",
  "point_cost": 100,
  "estimated_value": 4.99,
  "product_id": "Safeway product id when known",
  "upc": "UPC when known",
  "source_type": "rewards_dashboard",
  "observed_on": "YYYY-MM-DD",
  "expires_on": "YYYY-MM-DD"
}
```

Compare `estimated_value / point_cost` against the default cash redemption value before recommending redemption.

## Cart Math

Cart estimates should calculate rewards after item prices and coupons:

1. Price line items using base/current/weekly/coupon overlays.
2. Apply cart-level coupons.
3. Estimate eligible grocery spend after cart-level coupon savings.
4. Award base points using whole eligible dollars.
5. Add confirmed applicable point multiplier bonuses.
6. Report rewards as future value, not as a same-transaction discount.

The default planning value is currently the user's best observed grocery redemption:

```text
1200 points = $20
```

This is equivalent to about `$1.67` per 100 points. Fuel redemptions can be theoretically higher when a full eligible fill-up is available, but those should be shown separately because they depend on fuel station access and gallons purchased.

## Commands

Show known redemption options:

```bash
python3 meal_price_tool.py rewards --affordable --limit 25 --only-valued
```

Show point multiplier offers from saved coupon data:

```bash
python3 meal_price_tool.py point-offers
```

Estimate a cart with rewards value:

```bash
python3 meal_price_tool.py cart ground_beef_lunch_bowls --verbose
```

Import a dashboard capture:

```bash
python3 safeway_rewards_import.py safeway_rewards_dashboard_capture_2026-05-07.json --resolve-prices --write
python3 safeway_rewards_import.py safeway_rewards_dashboard_capture_2026-05-07.json --adjacent-capture safeway_rewards_adjacent_capture_2026-05-07.json --resolve-prices --write
```

The importer parses all Grocery Rewards point tabs, can include adjacent tabs like `More ways to use`, merges dashboard rewards into `safeway_rewards.json`, and optionally estimates product reward values through the Safeway product search API. Product matches are marked with resolution confidence and should be treated as planning estimates until confirmed.

## Open Work

- Discover a read-only Rewards dashboard endpoint for current point balance and reward tiles.
- Replace text-capture import with endpoint-backed dashboard refresh if a stable endpoint is found.
- Resolve point multiplier UPC lists when available.
- Add a cart mode that evaluates whether spending points on a reward beats saving those points for the 1200-point grocery redemption.
