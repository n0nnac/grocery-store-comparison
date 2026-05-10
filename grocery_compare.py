#!/usr/bin/env python3
"""Grocery Store Price Comparison Tool for zip code 20037.

Compares prices across Trader Joe's, Safeway, and Giant
to find the best overall deal for your shopping list.
"""

import json
import sys
from pathlib import Path
from difflib import get_close_matches

PRICES_FILE = Path(__file__).parent / "prices.json"
STORES = ["Trader Joe's", "Safeway", "Giant"]


def load_prices():
    with open(PRICES_FILE) as f:
        return json.load(f)


def save_prices(data):
    with open(PRICES_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print("Prices saved.")


def find_item(query, items):
    """Find an item by exact or fuzzy match. Returns (matched_name, prices) or (None, None)."""
    query_lower = query.strip().lower()

    # Exact match
    for name in items:
        if name.lower() == query_lower:
            return name, items[name]

    # Substring match
    for name in items:
        if query_lower in name.lower() or name.lower() in query_lower:
            return name, items[name]

    # Fuzzy match
    matches = get_close_matches(query_lower, [n.lower() for n in items], n=1, cutoff=0.5)
    if matches:
        for name in items:
            if name.lower() == matches[0]:
                return name, items[name]

    return None, None


def compare(shopping_list):
    """Compare prices for a shopping list across all stores."""
    data = load_prices()
    items = data["items"]

    store_totals = {s: 0.0 for s in STORES}
    found_items = []
    missing_items = []

    for query in shopping_list:
        name, prices = find_item(query, items)
        if name:
            found_items.append((name, prices))
            for store in STORES:
                store_totals[store] += prices.get(store, 0)
        else:
            missing_items.append(query)

    # Display results
    if not found_items:
        print("\nNo matching items found. Run with --list to see available items.")
        return

    print(f"\n{'─' * 70}")
    print(f"  GROCERY PRICE COMPARISON — ZIP {data['metadata']['zip_code']}")
    print(f"  Prices last updated: {data['metadata']['last_updated']}")
    print(f"{'─' * 70}")

    # Per-item breakdown
    header = f"  {'Item':<25}"
    for store in STORES:
        header += f" {store:>12}"
    print(header)
    print(f"  {'─' * 61}")

    for name, prices in found_items:
        cheapest = min(prices.values())
        row = f"  {name:<25}"
        for store in STORES:
            price = prices.get(store, 0)
            marker = " *" if price == cheapest else "  "
            row += f"  ${price:>9.2f}{marker}"
        print(row)

    # Totals
    print(f"  {'─' * 61}")
    cheapest_total = min(store_totals.values())
    row = f"  {'TOTAL':<25}"
    for store in STORES:
        total = store_totals[store]
        marker = " *" if total == cheapest_total else "  "
        row += f"  ${total:>9.2f}{marker}"
    print(row)

    # Winner
    winner = min(store_totals, key=store_totals.get)
    savings = {s: t - cheapest_total for s, t in store_totals.items() if s != winner}
    print(f"\n  BEST DEAL: {winner}")
    for store, diff in sorted(savings.items(), key=lambda x: x[1]):
        print(f"    Save ${diff:.2f} vs {store}")

    if missing_items:
        print(f"\n  Items not found in database:")
        for item in missing_items:
            print(f"    - {item}")
        print("  Use --add to add new items.")

    print(f"{'─' * 70}")
    print("  * = cheapest for that item\n")


def list_items():
    """List all items in the price database."""
    data = load_prices()
    items = data["items"]
    print(f"\nAvailable items ({len(items)}):")
    print(f"{'─' * 50}")
    for name in sorted(items):
        prices = items[name]
        cheapest_store = min(prices, key=prices.get)
        print(f"  {name:<30} (cheapest: {cheapest_store})")
    print()


def add_item():
    """Interactively add or update an item's prices."""
    data = load_prices()

    name = input("Item name (e.g., 'avocados (each)'): ").strip().lower()
    if not name:
        print("Cancelled.")
        return

    existing_name, existing_prices = find_item(name, data["items"])
    if existing_name:
        print(f"  Updating existing item: {existing_name}")
        print(f"  Current prices: {existing_prices}")
        name = existing_name

    prices = {}
    for store in STORES:
        while True:
            raw = input(f"  Price at {store} (or 'skip'): ").strip()
            if raw.lower() == "skip":
                break
            try:
                price = float(raw.replace("$", ""))
                prices[store] = price
                break
            except ValueError:
                print("  Enter a number like 3.99")

    if prices:
        if name in data["items"]:
            data["items"][name].update(prices)
        else:
            data["items"][name] = prices
        from datetime import date
        data["metadata"]["last_updated"] = str(date.today())
        save_prices(data)
        print(f"  Saved: {name} → {prices}")
    else:
        print("  No prices entered. Cancelled.")


def remove_item():
    """Remove an item from the database."""
    data = load_prices()
    name = input("Item name to remove: ").strip()
    matched, _ = find_item(name, data["items"])
    if matched:
        confirm = input(f"  Remove '{matched}'? (y/n): ").strip().lower()
        if confirm == "y":
            del data["items"][matched]
            save_prices(data)
            print(f"  Removed: {matched}")
    else:
        print(f"  '{name}' not found.")


def print_usage():
    print("""
Grocery Store Price Comparison Tool
====================================

Usage:
  python grocery_compare.py <item1>, <item2>, ...   Compare prices for items
  python grocery_compare.py --list                   Show all items in database
  python grocery_compare.py --add                    Add/update an item's prices
  python grocery_compare.py --remove                 Remove an item

Examples:
  python grocery_compare.py milk, eggs, bread, butter
  python grocery_compare.py chicken breast, rice, onions, garlic, olive oil
  python grocery_compare.py "ground beef", pasta, tomatoes, cheddar cheese
""")


def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    arg = sys.argv[1]

    if arg == "--list":
        list_items()
    elif arg == "--add":
        add_item()
    elif arg == "--remove":
        remove_item()
    elif arg == "--help":
        print_usage()
    else:
        # Join all args and split by comma
        raw = " ".join(sys.argv[1:])
        shopping_list = [item.strip() for item in raw.split(",") if item.strip()]
        compare(shopping_list)


if __name__ == "__main__":
    main()
