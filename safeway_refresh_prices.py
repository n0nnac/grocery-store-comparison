#!/usr/bin/env python3
"""Refresh saved Safeway product prices from the product search API.

This script reads Safeway product IDs already stored in meal_prices.json,
queries the browser-facing product API by exact product ID, and optionally
updates meal_prices.json plus safeway_price_observations.json.

Dry-run is the default. Use --write to persist changes.
"""

import argparse
import copy
import json
import sys
import urllib.error
from datetime import date
from pathlib import Path

from safeway_api_search import (
    DEFAULT_BANNER,
    DEFAULT_CHANNEL,
    DEFAULT_STORE_ID,
    SEARCH_ENDPOINT,
    fetch_search,
    normalize_doc,
)


ROOT = Path(__file__).parent
MEAL_PRICES_FILE = ROOT / "meal_prices.json"
OBSERVATIONS_FILE = ROOT / "safeway_price_observations.json"


def load_json(path):
    with path.open() as f:
        return json.load(f)


def write_json(path, data):
    with path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


def money(value):
    if value is None:
        return "n/a"
    return f"${value:.2f}"


def safeway_source(item):
    return item.get("price_sources", {}).get("Safeway", {})


def iter_saved_product_ids(meal_prices):
    for item_name, item in meal_prices.get("items", {}).items():
        source = safeway_source(item)
        product_id = source.get("product_id")
        if product_id:
            yield item_name, item, str(product_id)


def exact_product_doc(payload, product_id):
    docs = payload.get("response", {}).get("docs", [])
    normalized = [normalize_doc(doc) for doc in docs]
    for doc in normalized:
        if str(doc.get("pid")) == str(product_id):
            return doc
    return None


def planning_base_price(item, doc):
    """Return the price that matches the ingredient planning unit."""
    unit = (item.get("unit") or "").strip().lower()
    if unit in {"lb", "pound", "pounds"}:
        return planning_pound_price(item, doc, "base")
    if doc.get("base_price") is not None:
        return doc["base_price"]
    return doc.get("price")


def planning_current_price(item, doc):
    """Return the current price that matches the ingredient planning unit."""
    unit = (item.get("unit") or "").strip().lower()
    if unit in {"lb", "pound", "pounds"}:
        return planning_pound_price(item, doc, "current")
    return doc.get("price")


def parse_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def planning_pound_price(item, doc, price_kind):
    """Convert API package/unit data to a per-pound planning price when safe."""
    if price_kind == "base":
        package_price = doc.get("base_price")
        unit_price = doc.get("base_price_per")
    else:
        package_price = doc.get("price")
        unit_price = doc.get("price_per")

    api_unit = (
        doc.get("unit_quantity")
        or doc.get("unit_of_measure")
        or ""
    ).strip().upper()
    unit_of_measure = (doc.get("unit_of_measure") or "").strip().upper()
    item_size_qty = parse_float(doc.get("item_size_qty"))
    existing = item.get("base_prices", {}).get("Safeway")

    if unit_price is not None and api_unit in {"LB", "LBS", "POUND", "POUNDS"}:
        return unit_price

    if package_price is not None and item_size_qty is not None and unit_of_measure in {"OZ", "OUNCE", "OUNCES"}:
        pounds = item_size_qty / 16
        if pounds > 0:
            return round(package_price / pounds, 2)

    if unit_price is not None and api_unit in {"OZ", "OUNCE", "OUNCES"}:
        return round(unit_price * 16, 2)

    if price_kind == "base" and existing is not None:
        return existing
    return None


def make_product_url(product_id, store_id):
    return f"https://www.safeway.com/shop/product-details.{product_id}.html?loc={store_id}"


def make_observation(item, doc, store_id, observed_on):
    return {
        "product_name": doc.get("name"),
        "url": make_product_url(doc.get("pid"), store_id),
        "source_type": "search_substitute_api",
        "endpoint": SEARCH_ENDPOINT,
        "current_price": doc.get("price"),
        "regular_price": doc.get("base_price"),
        "current_unit_price": doc.get("price_per"),
        "regular_unit_price": doc.get("base_price_per"),
        "unit": item.get("unit"),
        "api_unit_quantity": doc.get("unit_quantity"),
        "api_unit_of_measure": doc.get("unit_of_measure"),
        "api_item_size_qty": doc.get("item_size_qty"),
        "api_item_package_qty": doc.get("item_package_qty"),
        "api_sell_by_weight": doc.get("sell_by_weight"),
        "api_display_unit_quantity_text": doc.get("display_unit_quantity_text"),
        "planning_base_price": planning_base_price(item, doc),
        "planning_current_price": planning_current_price(item, doc),
        "promo_end_date": doc.get("promo_end_date"),
        "inventory_available": doc.get("inventory_available"),
        "upc": doc.get("upc"),
        "department_name": doc.get("department_name"),
        "aisle_name": doc.get("aisle_name"),
        "store_id": str(store_id),
        "observed_on": observed_on,
        "confidence": "api_price_doc",
    }


def update_item_source(item, doc, store_id, observed_on):
    item.setdefault("price_sources", {}).setdefault("Safeway", {})
    source = item["price_sources"]["Safeway"]
    source.update(
        {
            "source_type": "search_substitute_api",
            "observed_on": observed_on,
            "store_id": str(store_id),
            "product_id": str(doc.get("pid")),
            "product_name": doc.get("name"),
            "endpoint": SEARCH_ENDPOINT,
            "url": make_product_url(doc.get("pid"), store_id),
            "confidence": "api_price_doc",
            "regular_price": doc.get("base_price"),
            "current_price": doc.get("price"),
            "regular_unit_price": doc.get("base_price_per"),
            "current_unit_price": doc.get("price_per"),
            "api_unit_quantity": doc.get("unit_quantity"),
            "api_unit_of_measure": doc.get("unit_of_measure"),
            "api_item_size_qty": doc.get("item_size_qty"),
            "api_item_package_qty": doc.get("item_package_qty"),
            "planning_base_price": planning_base_price(item, doc),
            "planning_current_price": planning_current_price(item, doc),
            "promo_end_date": doc.get("promo_end_date"),
            "inventory_available": doc.get("inventory_available"),
            "upc": doc.get("upc"),
        }
    )

    base_price = planning_base_price(item, doc)
    if base_price is not None:
        item.setdefault("base_prices", {})["Safeway"] = base_price


def refresh(args):
    meal_prices = load_json(MEAL_PRICES_FILE)
    observations = load_json(OBSERVATIONS_FILE)
    updated_meal_prices = copy.deepcopy(meal_prices)
    updated_observations = copy.deepcopy(observations)
    updated_observations.setdefault("observations", {})
    updated_observations.setdefault("not_yet_verified", [])

    candidates = list(iter_saved_product_ids(updated_meal_prices))
    if args.only:
        wanted = {name.lower() for name in args.only}
        candidates = [(name, item, pid) for name, item, pid in candidates if name.lower() in wanted]
    if args.limit is not None:
        candidates = candidates[: args.limit]

    observed_on = args.observed_on or date.today().isoformat()
    rows = []
    failures = []

    for item_name, item, product_id in candidates:
        old_base = item.get("base_prices", {}).get("Safeway")
        try:
            payload = fetch_search(
                product_id,
                args.store_id,
                args.rows,
                0,
                args.banner,
                args.channel,
                args.timeout,
            )
        except urllib.error.HTTPError as exc:
            failures.append((item_name, product_id, f"HTTP {exc.code}"))
            continue
        except urllib.error.URLError as exc:
            failures.append((item_name, product_id, f"request failed: {exc}"))
            continue

        doc = exact_product_doc(payload, product_id)
        if not doc:
            failures.append((item_name, product_id, "no exact product-id match"))
            continue

        update_item_source(item, doc, args.store_id, observed_on)
        updated_observations["observations"][str(product_id)] = make_observation(
            item, doc, args.store_id, observed_on
        )

        new_base = item.get("base_prices", {}).get("Safeway")
        rows.append(
            {
                "item": item_name,
                "pid": product_id,
                "old_base": old_base,
                "new_base": new_base,
                "current": doc.get("price"),
                "regular": doc.get("base_price"),
                "current_unit": doc.get("price_per"),
                "regular_unit": doc.get("base_price_per"),
                "promo_end": doc.get("promo_end_date"),
                "inventory": doc.get("inventory_available"),
            }
        )

    print(f"Safeway store {args.store_id}: refreshed {len(rows)} saved product IDs")
    if not args.write:
        print("Mode: dry-run; no files written")
    print()
    print(
        f"{'Item':<34} {'PID':<12} {'Old Base':>9} {'New Base':>9} "
        f"{'Current':>9} {'Regular':>9} {'Inv':>4} Promo End"
    )
    print("-" * 118)
    for row in rows:
        print(
            f"{row['item']:<34} {row['pid']:<12} {money(row['old_base']):>9} "
            f"{money(row['new_base']):>9} {money(row['current']):>9} "
            f"{money(row['regular']):>9} {str(row['inventory'] or ''):>4} "
            f"{row['promo_end'] or ''}"
        )

    if failures:
        print("\nSkipped or failed:")
        for item_name, product_id, reason in failures:
            print(f"- {item_name} ({product_id}): {reason}")

    if args.write:
        write_json(MEAL_PRICES_FILE, updated_meal_prices)
        write_json(OBSERVATIONS_FILE, updated_observations)
        print(f"\nWrote {MEAL_PRICES_FILE.name} and {OBSERVATIONS_FILE.name}")

    return 0 if not failures else 1


def main():
    parser = argparse.ArgumentParser(
        description="Refresh known Safeway product prices from saved product IDs."
    )
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--banner", default=DEFAULT_BANNER, help="Banner, usually safeway")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="pickup, delivery, or instore")
    parser.add_argument("--rows", type=int, default=5, help="Rows to request for each product ID")
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout in seconds")
    parser.add_argument("--limit", type=int, help="Refresh only the first N saved product IDs")
    parser.add_argument("--only", nargs="*", help="Refresh only exact meal_prices item names")
    parser.add_argument("--observed-on", help="Override observed_on date, YYYY-MM-DD")
    parser.add_argument("--write", action="store_true", help="Write refreshed JSON files")
    parser.add_argument("--dry-run", action="store_true", help="Explicitly keep dry-run mode")
    args = parser.parse_args()

    if args.dry_run and args.write:
        raise SystemExit("Use either --dry-run or --write, not both.")

    return refresh(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
