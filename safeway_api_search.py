#!/usr/bin/env python3
"""Probe Safeway's private product-search endpoint.

This uses Safeway/Albertsons' browser-facing search endpoint discovered in
public page assets. It is undocumented and may change, but it is much faster
and cleaner than reading rendered product pages.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SEARCH_ENDPOINT = "https://www.safeway.com/abs/pub/xapi/search/substitute"
SEARCH_SUBSCRIPTION_KEY = "e914eec9448c4d5eb672debf5011cf8f"
DEFAULT_STORE_ID = "923"
DEFAULT_BANNER = "safeway"
DEFAULT_CHANNEL = "pickup"


def build_url(query, store_id, rows, start, banner, channel):
    params = {
        "request-id": str(int(time.time() * 1000)),
        "url": f"https://www.{banner}.com",
        "pageurl": f"https://www.{banner}.com",
        "pagename": "search",
        "rows": str(rows),
        "start": str(start),
        "search-type": "keyword",
        "storeid": str(store_id),
        "featured": "true",
        "search-uid": "",
        "q": query,
        "channel": channel,
        "banner": banner,
    }
    return f"{SEARCH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def fetch_search(query, store_id, rows, start, banner, channel, timeout):
    url = build_url(query, store_id, rows, start, banner, channel)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://www.{banner}.com/shop/search-results.html",
            "Ocp-Apim-Subscription-Key": SEARCH_SUBSCRIPTION_KEY,
            "x-swy-banner": banner,
            "x-swy-client-id": "web-portal",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_doc(doc):
    return {
        "name": doc.get("name"),
        "pid": doc.get("pid") or doc.get("id"),
        "upc": doc.get("upc"),
        "store_id": doc.get("storeId"),
        "price": doc.get("price"),
        "base_price": doc.get("basePrice"),
        "price_per": doc.get("pricePer"),
        "base_price_per": doc.get("basePricePer"),
        "unit_quantity": doc.get("unitQuantity"),
        "unit_of_measure": doc.get("unitOfMeasure"),
        "item_size_qty": doc.get("itemSizeQty"),
        "item_package_qty": doc.get("itemPackageQty"),
        "sell_by_weight": doc.get("sellByWeight"),
        "display_unit_quantity_text": doc.get("displayUnitQuantityText"),
        "promo_end_date": doc.get("promoEndDate"),
        "inventory_available": doc.get("inventoryAvailable"),
        "department_name": doc.get("departmentName"),
        "aisle_name": doc.get("aisleName"),
    }


def print_table(docs):
    print(f"{'PID':<12} {'Price':>8} {'Base':>8} {'Per':>8} {'Inv':>4}  Name")
    print("-" * 100)
    for doc in docs:
        price = "" if doc["price"] is None else f"${doc['price']:.2f}"
        base = "" if doc["base_price"] is None else f"${doc['base_price']:.2f}"
        price_per = "" if doc["price_per"] is None else f"${doc['price_per']:.2f}"
        inv = doc["inventory_available"] or ""
        print(f"{doc['pid'] or '':<12} {price:>8} {base:>8} {price_per:>8} {inv:>4}  {doc['name']}")


def main():
    parser = argparse.ArgumentParser(description="Query Safeway's browser-facing product search endpoint.")
    parser.add_argument("query", help="Search text, e.g. 'milk' or 'teriyaki sauce'")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--rows", type=int, default=10, help="Number of product rows")
    parser.add_argument("--start", type=int, default=0, help="Result offset")
    parser.add_argument("--banner", default=DEFAULT_BANNER, help="Banner, usually safeway")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="pickup, delivery, or instore")
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print normalized JSON instead of a table")
    args = parser.parse_args()

    try:
        payload = fetch_search(
            args.query,
            args.store_id,
            args.rows,
            args.start,
            args.banner,
            args.channel,
            args.timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body[:500]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Request failed: {exc}")

    response = payload.get("response", {})
    docs = [normalize_doc(doc) for doc in response.get("docs", [])]
    if args.json:
        print(json.dumps({"num_found": response.get("numFound"), "docs": docs}, indent=2))
    else:
        print(f"Safeway store {args.store_id}: {args.query!r} ({response.get('numFound', len(docs))} found)")
        print_table(docs)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
