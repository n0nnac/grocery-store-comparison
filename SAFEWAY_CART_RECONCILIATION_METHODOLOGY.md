# Safeway Cart Reconciliation Methodology

This document describes the repeatable process for comparing the local cart model with Safeway's cart or checkout totals. The goal is to identify gaps in price, coupon, reward, tax, fee, and item-matching logic without mutating the Safeway account.

## Scope

Cart reconciliation answers:

- what the local model expects the cart to cost
- which weekly-deal file and coupon/reward overlays were used
- what Safeway actually shows in cart or checkout
- which line items, savings, points, taxes, or fees disagree

This workflow does not place an order, redeem rewards, add items to the cart, clip coupons, or change account state.

## Expected Cart

Build the expected cart from saved recipes:

```bash
python3 safeway_cart_reconcile.py expected ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner
```

The expected model uses:

- `meal_prices.json`
- the active dated `weekly_deals*.json` file
- `safeway_coupons.json`
- `safeway_coupon_overrides.json`
- `safeway_rewards.json`

The active-deals selector prevents expired weekly ads from being used after their validity window.

## Observed Cart Template

Create a template for Safeway cart or checkout observations:

```bash
python3 safeway_cart_reconcile.py template ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner --output safeway_cart_observed_template.json
```

Fill in fields that Safeway exposes:

- item subtotal
- coupon savings
- rewards savings
- tax
- fees
- estimated total
- points earned
- observed line totals when visible

Leave unknown fields as `null`.

## Browser Capture

When a logged-in Safeway cart or checkout page is open in a Chrome session with remote debugging, capture the visible page text without clicking or changing account state:

```bash
python3 safeway_cart_capture.py capture --cdp-url http://127.0.0.1:9223 --output safeway_cart_capture_2026-05-08.json
```

Parse the raw capture into the observed-cart schema:

```bash
python3 safeway_cart_capture.py parse safeway_cart_capture_2026-05-08.json --template safeway_cart_observed_template.json --output safeway_cart_observed_from_capture_2026-05-08.json
```

The capture file is evidence. The parsed observed-cart file is what the reconciler consumes.

If Safeway shows a service/problem page, empty cart, or no visible totals, the parser records that in `metadata.page_problem` and leaves unknown values as `null`.

## Compare

Compare the expected model to the observed Safeway data:

```bash
python3 safeway_cart_reconcile.py compare ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner --observed safeway_cart_observed_template.json
python3 safeway_cart_reconcile.py compare ground_beef_lunch_bowls chicken_lunch_bowls salmon_dinner --observed safeway_cart_observed_from_capture_2026-05-08.json
```

The report marks each row as:

- `ok`: observed value matches within tolerance
- `diff`: observed value differs from the model
- `missing`: observed value was not supplied

Use `--summary-only` when only subtotal, discounts, tax, fees, total, and points matter.

## Data Hygiene

- Keep observed cart files separate from base prices.
- Do not overwrite product prices from a checkout discrepancy without investigating the source.
- Treat random-weight meat/produce discrepancies differently from fixed-package discrepancies.
- Treat taxes and fees as checkout layers, not item-price layers.
- Do not count rewards future value as a same-transaction discount unless Safeway explicitly applies a reward redemption to the observed cart.

## Current Known Gaps

- Observed Safeway cart entry is manual/template-based.
- Browser/cart extraction is text-based and depends on Safeway exposing totals on the current cart/checkout page.
- Safeway online cart requires a delivery or pickup fulfillment context. The 1701 Corcoran in-store context may not expose a normal online cart; selecting another pickup store would change the store/pricing context and should not be done silently.
- Exact tax and fee rules are only reconciled after observation.
- Safeway substitutions and random-weight package selections can still move final totals.

## Next Automation Hook

The next durable improvement is a read-only cart capture tool:

1. Build or manually prepare the cart in Safeway.
2. Read cart/checkout line items, discounts, taxes, fees, and points from the logged-in page.
3. Write an observed cart JSON file.
4. Run `safeway_cart_reconcile.py compare`.

The capture tool should remain read-only. Any future add-to-cart or reward-redemption automation should be a separate opt-in command.
