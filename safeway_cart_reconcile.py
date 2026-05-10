#!/usr/bin/env python3
"""Reconcile modeled meal-plan carts against observed Safeway cart totals.

This script does not add items to Safeway, redeem rewards, or mutate the
account. It creates an expected cart model and compares it with an observed
cart breakdown copied from Safeway checkout/cart.
"""

import argparse
import json
from datetime import date
from pathlib import Path

from meal_price_tool import (
    COUPON_OVERRIDES_FILE,
    COUPONS_FILE,
    MEAL_PRICES_FILE,
    RECIPES,
    build_cart_lines,
    cart_level_coupon_rows,
    choose_deals_file,
    deals_validity,
    load_coupon_overlays,
    load_json,
    load_rewards_config,
    money,
    normalize_deals,
    point_offer_rows,
    rewards_summary,
    safeway_source,
)


ROOT = Path(__file__).parent
DEFAULT_OBSERVED_TEMPLATE = ROOT / "safeway_cart_observed_template.json"

def today():
    return date.today()


def write_json(path, data):
    with path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


def item_source_id(item_name, prices):
    item = prices.get("items", {}).get(item_name) or {}
    source = safeway_source(item)
    return {
        "product_id": source.get("product_id") or source.get("pid"),
        "upc": source.get("upc"),
        "product_name": source.get("product_name"),
        "url": source.get("url"),
    }


def expected_cart(recipe_keys, deals_path=None, no_coupons=False, no_rewards=False):
    prices = load_json(MEAL_PRICES_FILE)
    selected_deals_path = choose_deals_file(deals_path)
    if not selected_deals_path:
        raise SystemExit("No weekly deals file found")
    deals = normalize_deals(load_json(selected_deals_path))
    coupons = [] if no_coupons else load_coupon_overlays()
    rewards_config = None if no_rewards else load_rewards_config()

    recipes = recipe_keys or ["ground_beef_lunch_bowls"]
    recipe_refs, lines, servings = build_cart_lines(recipes, prices, deals, coupons)
    line_subtotal = round(sum(line["line_total"] or 0 for line in lines), 2)
    coupon_rows = cart_level_coupon_rows(lines, coupons)
    coupon_total = round(sum(row["amount"] for row in coupon_rows if row["applied"]), 2)
    total_after_coupons = round(line_subtotal - coupon_total, 2)

    point_rows = []
    reward_summary = None
    if rewards_config:
        point_rows = point_offer_rows(lines, coupons, prices)
        reward_summary = rewards_summary(lines, coupon_total, point_rows, rewards_config)

    start, end = deals_validity(deals)
    return {
        "metadata": {
            "source_type": "local_cart_model",
            "observed_on": today().isoformat(),
            "recipes": recipes,
            "servings": servings,
            "meal_prices_file": str(MEAL_PRICES_FILE.name),
            "deals_file": str(selected_deals_path.name),
            "deals_valid_from": start.isoformat() if start else None,
            "deals_valid_to": end.isoformat() if end else None,
            "coupons_file": str(COUPONS_FILE.name),
            "coupon_overrides_file": str(COUPON_OVERRIDES_FILE.name),
        },
        "recipes": [
            {
                "key": key,
                "display": RECIPES[key]["display"],
                "servings": RECIPES[key]["servings"],
            }
            for key in recipes
        ],
        "items": [
            {
                "item": line["item"],
                "qty": line["qty"],
                "unit": line["unit"],
                "expected_unit_price": line["unit_price"],
                "expected_line_total": line["line_total"],
                "source": line["source"],
                "detail": line["detail"],
                "requires_clip": line.get("requires_clip"),
                "rewards_eligible": line.get("rewards_eligible"),
                **item_source_id(line["item"], prices),
            }
            for line in lines
        ],
        "cart_level_coupons": coupon_rows,
        "rewards": {
            "point_offers": point_rows,
            "summary": reward_summary,
        },
        "summary": {
            "line_subtotal": line_subtotal,
            "cart_level_coupon_savings": coupon_total,
            "estimated_product_total_after_coupons": total_after_coupons,
            "estimated_tax": None,
            "estimated_fees": None,
            "estimated_total": total_after_coupons,
            "estimated_points_earned": None if not reward_summary else reward_summary["total_points"],
            "estimated_rewards_future_value": None if not reward_summary else round(reward_summary["estimated_future_value"], 2),
        },
    }


def observed_template(expected):
    return {
        "metadata": {
            "source_type": "safeway_cart_manual",
            "observed_on": today().isoformat(),
            "store_id": "923",
            "notes": [
                "Fill this from Safeway cart/checkout. Leave unknown fields null.",
                "Use positive numbers for savings; the reconciler subtracts savings when needed.",
            ],
        },
        "summary": {
            "item_subtotal": None,
            "club_card_savings": None,
            "coupon_savings": None,
            "rewards_savings": None,
            "tax": None,
            "fees": None,
            "estimated_total": None,
            "points_earned": None,
        },
        "items": [
            {
                "matched_item": item["item"],
                "safeway_name": item.get("product_name"),
                "product_id": item.get("product_id"),
                "qty": item["qty"],
                "unit": item["unit"],
                "observed_unit_price": None,
                "observed_line_total": None,
                "observed_savings": None,
                "notes": None,
            }
            for item in expected["items"]
        ],
        "discounts": [
            {
                "name": row.get("name"),
                "type": "cart_level_coupon",
                "observed_amount": None,
                "expected_amount": row.get("amount"),
                "offer_id": row.get("offer_id"),
            }
            for row in expected.get("cart_level_coupons", [])
        ],
        "rewards": {
            "points_earned": None,
            "points_redeemed": None,
            "reward_savings": None,
        },
    }


def dollars(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def delta(expected, observed):
    expected_value = dollars(expected)
    observed_value = dollars(observed)
    if expected_value is None or observed_value is None:
        return None
    return round(observed_value - expected_value, 2)


def status_for_delta(value, tolerance):
    if value is None:
        return "missing"
    return "ok" if abs(value) <= tolerance else "diff"


def format_metric(name, value):
    if value is None:
        return "n/a"
    if "points" in name:
        return f"{float(value):.0f}"
    return money(value)


def observed_item_map(observed):
    rows = {}
    for item in observed.get("items", []):
        key = item.get("matched_item")
        if key:
            rows[key] = item
    return rows


def observed_discount_total(observed, field_name, discount_type=None):
    summary_value = dollars((observed.get("summary") or {}).get(field_name))
    if summary_value is not None:
        return summary_value
    total = 0.0
    matched = False
    for discount in observed.get("discounts", []):
        if discount_type and discount.get("type") != discount_type:
            continue
        amount = dollars(discount.get("observed_amount"))
        if amount is not None:
            total += amount
            matched = True
    return round(total, 2) if matched else None


def compare(expected, observed, tolerance=0.02):
    item_rows = []
    observed_items = observed_item_map(observed)
    for expected_item in expected["items"]:
        observed_item = observed_items.get(expected_item["item"], {})
        expected_total = expected_item.get("expected_line_total")
        observed_total = observed_item.get("observed_line_total")
        row_delta = delta(expected_total, observed_total)
        item_rows.append(
            {
                "item": expected_item["item"],
                "expected": dollars(expected_total),
                "observed": dollars(observed_total),
                "delta": row_delta,
                "status": status_for_delta(row_delta, tolerance),
                "source": expected_item.get("source"),
                "notes": observed_item.get("notes"),
            }
        )

    observed_summary = observed.get("summary") or {}
    expected_summary = expected["summary"]
    observed_item_subtotal = dollars(observed_summary.get("item_subtotal"))
    if observed_item_subtotal is None:
        item_totals = [dollars(item.get("observed_line_total")) for item in observed.get("items", [])]
        if any(value is not None for value in item_totals):
            observed_item_subtotal = round(sum(value or 0 for value in item_totals), 2)

    observed_coupon_savings = observed_discount_total(observed, "coupon_savings")
    observed_rewards_savings = observed_discount_total(observed, "rewards_savings")
    observed_tax = dollars(observed_summary.get("tax"))
    observed_fees = dollars(observed_summary.get("fees"))
    observed_total = dollars(observed_summary.get("estimated_total"))
    observed_product_after = None
    if observed_item_subtotal is not None:
        observed_product_after = round(
            observed_item_subtotal
            - (observed_coupon_savings or 0)
            - (observed_rewards_savings or 0),
            2,
        )

    summary_rows = [
        (
            "item_subtotal",
            expected_summary["line_subtotal"],
            observed_item_subtotal,
        ),
        (
            "coupon_savings",
            expected_summary["cart_level_coupon_savings"],
            observed_coupon_savings,
        ),
        (
            "rewards_savings",
            0.0,
            observed_rewards_savings,
        ),
        (
            "product_total_after_savings",
            expected_summary["estimated_product_total_after_coupons"],
            observed_product_after,
        ),
        (
            "tax",
            expected_summary.get("estimated_tax"),
            observed_tax,
        ),
        (
            "fees",
            expected_summary.get("estimated_fees"),
            observed_fees,
        ),
        (
            "estimated_total",
            expected_summary["estimated_total"],
            observed_total,
        ),
        (
            "points_earned",
            expected_summary.get("estimated_points_earned"),
            observed_summary.get("points_earned") or (observed.get("rewards") or {}).get("points_earned"),
        ),
    ]

    summary = []
    for name, expected_value, observed_value in summary_rows:
        row_delta = delta(expected_value, observed_value)
        summary.append(
            {
                "name": name,
                "expected": dollars(expected_value),
                "observed": dollars(observed_value),
                "delta": row_delta,
                "status": status_for_delta(row_delta, tolerance),
            }
        )

    return {
        "metadata": {
            "source_type": "cart_reconciliation",
            "observed_on": today().isoformat(),
            "tolerance": tolerance,
            "expected_deals_file": expected["metadata"].get("deals_file"),
            "observed_source_type": (observed.get("metadata") or {}).get("source_type"),
        },
        "summary": summary,
        "items": item_rows,
    }


def print_expected(expected):
    metadata = expected["metadata"]
    print("\nExpected Safeway cart")
    print(f"Deals file: {metadata['deals_file']} ({metadata.get('deals_valid_from')} to {metadata.get('deals_valid_to')})")
    print(f"{'Item':<36} {'Qty':>7} {'Unit':<14} {'Price':>8} {'Line':>8} Source")
    print("-" * 100)
    for item in expected["items"]:
        print(
            f"{item['item']:<36} {item['qty']:>7g} {item['unit']:<14} "
            f"{money(item['expected_unit_price']):>8} {money(item['expected_line_total']):>8} {item['source']}"
        )
    summary = expected["summary"]
    print(f"\nLine subtotal: {money(summary['line_subtotal'])}")
    print(f"Cart-level coupon savings: -{money(summary['cart_level_coupon_savings'])}")
    print(f"Estimated product total after coupons: {money(summary['estimated_product_total_after_coupons'])}")
    if summary.get("estimated_points_earned") is not None:
        print(f"Estimated points earned: {summary['estimated_points_earned']:.0f}")


def print_reconciliation(result, summary_only=False):
    print("\nCart reconciliation")
    print(f"Deals file: {result['metadata'].get('expected_deals_file')}")
    print(f"{'Summary row':<30} {'Expected':>10} {'Observed':>10} {'Delta':>10} Status")
    print("-" * 76)
    for row in result["summary"]:
        print(
            f"{row['name']:<30} {format_metric(row['name'], row['expected']):>10} "
            f"{format_metric(row['name'], row['observed']):>10} "
            f"{format_metric(row['name'], row['delta']):>10} {row['status']}"
        )

    if summary_only:
        return

    print(f"\n{'Item':<36} {'Expected':>10} {'Observed':>10} {'Delta':>10} Status")
    print("-" * 84)
    for row in result["items"]:
        print(
            f"{row['item']:<36} {money(row['expected']):>10} {money(row['observed']):>10} "
            f"{money(row['delta']):>10} {row['status']}"
        )


def cmd_expected(args):
    expected = expected_cart(args.recipes, args.deals_file, args.no_coupons, args.no_rewards)
    if args.output:
        write_json(Path(args.output), expected)
    if args.json:
        print(json.dumps(expected, indent=2))
    else:
        print_expected(expected)


def cmd_template(args):
    expected = expected_cart(args.recipes, args.deals_file, args.no_coupons, args.no_rewards)
    template = observed_template(expected)
    output = Path(args.output or DEFAULT_OBSERVED_TEMPLATE)
    write_json(output, template)
    print(f"Wrote observed cart template: {output}")
    print(f"Expected deals file: {expected['metadata']['deals_file']}")


def cmd_compare(args):
    expected = expected_cart(args.recipes, args.deals_file, args.no_coupons, args.no_rewards)
    observed = load_json(Path(args.observed))
    result = compare(expected, observed, args.tolerance)
    if args.output:
        write_json(Path(args.output), result)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_reconciliation(result, args.summary_only)


def main():
    parser = argparse.ArgumentParser(description="Reconcile local Safeway cart estimates against observed cart totals.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(command):
        command.add_argument("recipes", nargs="*", help=f"Recipe keys: {', '.join(RECIPES)}")
        command.add_argument("--deals-file", help="Weekly deals JSON file; defaults to the active dated weekly_deals*.json")
        command.add_argument("--no-coupons", action="store_true", help="Ignore coupon overlays")
        command.add_argument("--no-rewards", action="store_true", help="Ignore rewards point estimates")

    expected_parser = sub.add_parser("expected", help="Print or write the expected local cart model")
    add_common(expected_parser)
    expected_parser.add_argument("--output", help="Write expected model JSON")
    expected_parser.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    expected_parser.set_defaults(func=cmd_expected)

    template_parser = sub.add_parser("template", help="Write a Safeway observed-cart template for manual checkout entry")
    add_common(template_parser)
    template_parser.add_argument("--output", help=f"Template output path, default {DEFAULT_OBSERVED_TEMPLATE.name}")
    template_parser.set_defaults(func=cmd_template)

    compare_parser = sub.add_parser("compare", help="Compare expected model with an observed Safeway cart JSON")
    add_common(compare_parser)
    compare_parser.add_argument("--observed", required=True, help="Observed cart JSON file")
    compare_parser.add_argument("--output", help="Write reconciliation JSON")
    compare_parser.add_argument("--tolerance", type=float, default=0.02, help="Dollar tolerance before marking a difference")
    compare_parser.add_argument("--summary-only", action="store_true", help="Only print subtotal/total rows")
    compare_parser.add_argument("--json", action="store_true", help="Print reconciliation JSON")
    compare_parser.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
