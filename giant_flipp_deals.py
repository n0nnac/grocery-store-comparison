#!/usr/bin/env python3
"""Pull Giant Food weekly circular deals from the public Flipp flyer API.

Flipp's `backflipp.wishabi.com` host indexes Giant Food's current weekly
circular and exposes it as unauthenticated JSON. This is the cleanest
shell-friendly source for Giant dated deal prices, complementing the
browser-session `/api/v5.0` path used for live base/regular prices.

Subcommands:

    fetch       Download the current Giant flyer and write a normalized JSON file.
    search      Search Flipp for a query and show only Giant Food results.
    match       Match flyer items against keys in meal_prices.json.
    varieties   Expand "Selected Varieties" deals into the qualifying Giant SKUs
                via the live browser session (requires `giant_browser_api_probe.py
                launch`).

This script does not log in, does not store cookies, and does not bypass
any bot protection. Flipp's flyer endpoints are public.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
MEAL_PRICES_FILE = ROOT / "meal_prices.json"
DEALS_PREFIX = "giant_weekly_deals"

GIANT_MERCHANT_ID = 2520
GIANT_MERCHANT_NAME = "Giant Food"
DEFAULT_POSTAL_CODE = "20010"
DEFAULT_LOCALE = "en-us"

PARK_ROAD_CONTEXT = {
    "store_number": "0378",
    "store_address": "1345 Park Road N.W., Washington, DC 20010",
    "postal_code": DEFAULT_POSTAL_CODE,
}

FLIPP_BASE = "https://backflipp.wishabi.com/flipp"
FLYERS_LIST_ENDPOINT = f"{FLIPP_BASE}/flyers"
FLYER_ITEMS_ENDPOINT = f"{FLIPP_BASE}/flyers/{{flyer_id}}"
ITEM_DETAIL_ENDPOINT = f"{FLIPP_BASE}/items/{{item_id}}"
SEARCH_ENDPOINT = f"{FLIPP_BASE}/items/search"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

STOPWORDS = {
    "and", "any", "or", "with", "the", "for", "fresh", "select", "ea",
    "each", "pack", "lb", "lbs", "oz", "ct", "count", "size", "family",
    "value", "store", "brand", "premium", "natural", "organic",
}


def http_get_json(url, timeout=15):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def load_json(path):
    with path.open() as f:
        return json.load(f)


def parse_price(current_price, pre_price_text, post_price_text):
    """Parse a Flipp price record into a per-unit price plus pricing kind.

    Flipp packages price information across three fields:

    - `current_price`: numeric value (may be string or number)
    - `pre_price_text`: prefix like "2/" for multi-buy deals
    - `post_price_text`: suffix like "/lb." or "/ea."

    Returns dict with `unit_price`, `unit_kind`, `multi_buy_qty`, and
    `display`. `unit_price` is None when the price cannot be parsed.
    """
    if current_price is None or current_price == "":
        return {
            "unit_price": None,
            "unit_kind": None,
            "multi_buy_qty": None,
            "display": None,
        }

    try:
        raw_price = float(current_price)
    except (TypeError, ValueError):
        return {
            "unit_price": None,
            "unit_kind": None,
            "multi_buy_qty": None,
            "display": str(current_price),
        }

    pre = (pre_price_text or "").strip()
    post = (post_price_text or "").strip()

    multi_match = re.match(r"^([0-9]+)\s*/\s*$", pre)
    if multi_match:
        qty = int(multi_match.group(1))
        unit_price = round(raw_price / qty, 4) if qty > 0 else None
        return {
            "unit_price": unit_price,
            "unit_kind": "multi_buy",
            "multi_buy_qty": qty,
            "display": f"{qty} for ${raw_price:.2f}",
        }

    post_lower = post.lower()
    if post_lower.startswith("/lb"):
        return {
            "unit_price": raw_price,
            "unit_kind": "per_lb",
            "multi_buy_qty": None,
            "display": f"${raw_price:.2f}/lb",
        }
    if post_lower.startswith("/ea"):
        return {
            "unit_price": raw_price,
            "unit_kind": "each",
            "multi_buy_qty": None,
            "display": f"${raw_price:.2f}/ea",
        }
    if post_lower.startswith("/oz") or post_lower.startswith("/fl"):
        return {
            "unit_price": raw_price,
            "unit_kind": post_lower.lstrip("/"),
            "multi_buy_qty": None,
            "display": f"${raw_price:.2f}{post}",
        }

    return {
        "unit_price": raw_price,
        "unit_kind": "single",
        "multi_buy_qty": None,
        "display": f"${raw_price:.2f}",
    }


def normalize_flipp_item(item, flyer_id=None):
    name = item.get("name") or ""
    brand = item.get("brand") or item.get("brand_name")
    raw_price = item.get("current_price", item.get("price"))
    pre = item.get("pre_price_text")
    post = item.get("post_price_text")
    pricing = parse_price(raw_price, pre, post)

    original_price = item.get("original_price")
    try:
        original_price = float(original_price) if original_price not in (None, "") else None
    except (TypeError, ValueError):
        original_price = None

    return {
        "flipp_id": item.get("id") or item.get("flyer_item_id"),
        "flipp_flyer_id": flyer_id or item.get("flyer_id"),
        "name": name,
        "brand": brand,
        "raw_price": str(raw_price) if raw_price is not None else None,
        "pre_price_text": pre,
        "post_price_text": post,
        "description": item.get("description"),
        "sale_story": item.get("sale_story"),
        "valid_from": (item.get("valid_from") or "")[:10] or None,
        "valid_to": (item.get("valid_to") or "")[:10] or None,
        "available_to": (item.get("available_to") or "")[:10] or None,
        "current_price": pricing["unit_price"],
        "original_price": original_price,
        "unit_kind": pricing["unit_kind"],
        "multi_buy_qty": pricing["multi_buy_qty"],
        "price_display": pricing["display"],
        "image_url": item.get("cutout_image_url") or item.get("clean_image_url") or item.get("image_url"),
        "shop_url": item.get("ttm_url"),
        "category_l1": item.get("_L1"),
        "category_l2": item.get("_L2"),
    }


def fetch_giant_flyers(postal_code, locale, timeout):
    url = f"{FLYERS_LIST_ENDPOINT}?{urllib.parse.urlencode({'locale': locale, 'postal_code': postal_code})}"
    payload = http_get_json(url, timeout=timeout)
    all_flyers = payload.get("flyers", [])
    return [f for f in all_flyers if f.get("merchant_id") == GIANT_MERCHANT_ID]


def fetch_flyer_items(flyer_id, timeout):
    url = FLYER_ITEMS_ENDPOINT.format(flyer_id=flyer_id)
    payload = http_get_json(url, timeout=timeout)
    return payload.get("items", [])


def fetch_item_detail(item_id, timeout):
    """Fetch a single Flipp item with its full price metadata.

    The per-flyer endpoint omits `pre_price_text` and `post_price_text`,
    so multi-buy and per-pound deals look like flat dollar amounts.
    The per-item detail endpoint preserves the full pricing fields.
    """
    url = ITEM_DETAIL_ENDPOINT.format(item_id=item_id)
    payload = http_get_json(url, timeout=timeout)
    return payload.get("item", payload)


def search_flipp_items(query, postal_code, locale, timeout):
    url = (
        f"{SEARCH_ENDPOINT}?"
        f"{urllib.parse.urlencode({'locale': locale, 'postal_code': postal_code, 'q': query})}"
    )
    payload = http_get_json(url, timeout=timeout)
    return payload.get("items", []), payload.get("flyers", [])


def output_path_for(metadata, explicit=None):
    if explicit:
        return Path(explicit)
    valid_from = metadata.get("valid_from") or date.today().isoformat()
    return ROOT / f"{DEALS_PREFIX}_{valid_from}.json"


def command_fetch(args):
    flyers = fetch_giant_flyers(args.postal_code, args.locale, args.timeout)
    if not flyers:
        print(json.dumps({"ok": False, "error": "No Giant Food flyers found at this postal code."}, indent=2), file=sys.stderr)
        return 2

    if args.flyer_id:
        flyers = [f for f in flyers if f.get("id") == args.flyer_id]
        if not flyers:
            print(json.dumps({"ok": False, "error": f"flyer_id {args.flyer_id} not found"}, indent=2), file=sys.stderr)
            return 2

    flyers.sort(key=lambda f: (f.get("valid_to") or ""), reverse=True)
    target = flyers[0]
    flyer_id = target["id"]

    raw_items = fetch_flyer_items(flyer_id, args.timeout)

    if args.no_enrich:
        items = [normalize_flipp_item(item, flyer_id=flyer_id) for item in raw_items]
    else:
        print(f"Enriching {len(raw_items)} items with per-item detail (for multi-buy parsing)...")
        items = []
        import time
        for i, raw in enumerate(raw_items):
            item_id = raw.get("id")
            if not item_id:
                items.append(normalize_flipp_item(raw, flyer_id=flyer_id))
                continue
            try:
                detail = fetch_item_detail(item_id, args.timeout)
                merged = {**raw, **detail}
                items.append(normalize_flipp_item(merged, flyer_id=flyer_id))
            except (urllib.error.HTTPError, urllib.error.URLError):
                items.append(normalize_flipp_item(raw, flyer_id=flyer_id))
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(raw_items)}...")
            time.sleep(args.sleep)

    if args.only_priced:
        items = [item for item in items if item.get("current_price") is not None]

    metadata = {
        "fetched_on": date.today().isoformat(),
        "source_type": "giant_flipp_circular_api",
        "merchant": GIANT_MERCHANT_NAME,
        "merchant_id": GIANT_MERCHANT_ID,
        "store_context": PARK_ROAD_CONTEXT,
        "endpoint_listing": FLYERS_LIST_ENDPOINT,
        "endpoint_items": FLYER_ITEMS_ENDPOINT.format(flyer_id=flyer_id),
        "flyer_id": flyer_id,
        "flyer_name": target.get("name"),
        "valid_from": (target.get("valid_from") or "")[:10],
        "valid_to": (target.get("valid_to") or "")[:10],
        "item_count": len(items),
        "available_flyers": [
            {
                "id": f["id"],
                "name": f.get("name"),
                "valid_from": (f.get("valid_from") or "")[:10],
                "valid_to": (f.get("valid_to") or "")[:10],
            }
            for f in flyers
        ],
    }

    payload = {"metadata": metadata, "items": items}

    print(f"Giant Food flyer {flyer_id} ({metadata['flyer_name']})")
    print(f"  Valid: {metadata['valid_from']} to {metadata['valid_to']}")
    print(f"  Items: {len(items)} ({sum(1 for i in items if i.get('current_price') is not None)} with prices)")
    print()

    sample = [item for item in items if item.get("current_price") is not None][:8]
    for item in sample:
        name = item.get("name") or ""
        brand = item.get("brand") or ""
        brand_str = f" ({brand})" if brand else ""
        print(f"  {item.get('price_display'):>14}  {name}{brand_str}")
    if len(items) > 8:
        print(f"  ... ({len(items) - 8} more)")

    if args.write:
        output = output_path_for(metadata, args.output)
        write_json(output, payload)
        print(f"\nWrote {output}")
    else:
        print("\nMode: dry-run; no files written. Pass --write to save.")

    return 0


def command_search(args):
    items, _flyers = search_flipp_items(args.query, args.postal_code, args.locale, args.timeout)
    giant = [item for item in items if item.get("merchant_name") == GIANT_MERCHANT_NAME]

    output = {
        "query": args.query,
        "postal_code": args.postal_code,
        "total_results": len(items),
        "giant_results": len(giant),
        "items": [normalize_flipp_item(item, flyer_id=item.get("flyer_id")) for item in giant],
    }

    if args.json:
        print(json.dumps(output, indent=2))
        return 0

    print(f"'{args.query}' -> {len(giant)} Giant Food results (of {len(items)} total)")
    for item in output["items"]:
        name = item.get("name") or ""
        brand = item.get("brand") or ""
        brand_str = f" ({brand})" if brand else ""
        valid_to = item.get("valid_to") or ""
        print(f"  {item.get('price_display'):>14}  {name}{brand_str}  | until {valid_to}")
    return 0


def token_set(text):
    normalized = (text or "").lower()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return {t for t in tokens if len(t) > 2 and t not in STOPWORDS and not t.isdigit()}


CATEGORY_NEGATIVES = {
    "protein": {"oil", "flour", "cookie", "ice", "cream", "yogurt", "salad", "dressing", "sauce", "spread", "snack", "crackers"},
    "produce": {"oil", "frozen", "ice", "cream", "yogurt", "snack", "candy", "chocolate", "wine", "beer"},
    "pantry": {"frozen", "ice", "cream", "wine", "beer", "spirits"},
    "frozen": {"oil", "wine", "beer", "spirits", "candy"},
    "dairy": {"oil", "frozen", "wine", "beer", "spirits", "candy", "chocolate", "wax"},
}


def match_score(meal_key, meal_record, item):
    meal_tokens = token_set(meal_key)
    item_tokens = token_set(item.get("name", "")) | token_set(item.get("brand", ""))
    if not meal_tokens:
        return 0

    overlap = meal_tokens & item_tokens
    score = len(overlap) / max(1, len(meal_tokens))

    category = (meal_record.get("category") or "").lower()
    negatives = CATEGORY_NEGATIVES.get(category, set())
    if negatives & item_tokens and not (overlap & set(meal_record.get("category", "").lower().split())):
        if item_tokens & negatives:
            score -= 0.25

    if (item.get("brand") or "").lower() == "giant":
        score += 0.05

    return round(max(score, 0), 3)


def command_match(args):
    meal_data = load_json(MEAL_PRICES_FILE)
    meal_items = meal_data.get("items", {})

    if args.deals_file:
        deals_path = Path(args.deals_file)
    else:
        candidates = sorted(ROOT.glob(f"{DEALS_PREFIX}_*.json"), reverse=True)
        if not candidates:
            print(json.dumps({"ok": False, "error": f"no {DEALS_PREFIX}_*.json files found; run fetch --write first"}, indent=2), file=sys.stderr)
            return 2
        deals_path = candidates[0]

    deals_data = load_json(deals_path)
    flyer_items = [i for i in deals_data.get("items", []) if i.get("current_price") is not None]

    metadata = deals_data.get("metadata", {})
    print(f"Matching against {deals_path.name} (flyer {metadata.get('flyer_id')}, {metadata.get('valid_from')} to {metadata.get('valid_to')})")
    print(f"Comparing {len(meal_items)} meal items vs {len(flyer_items)} priced flyer items")
    print()

    matches = {}
    selected_keys = list(meal_items.keys())
    if args.only:
        only = {key.lower() for key in args.only}
        selected_keys = [key for key in selected_keys if key.lower() in only]

    for key in selected_keys:
        meal_record = meal_items[key]
        scored = []
        for item in flyer_items:
            score = match_score(key, meal_record, item)
            if score >= args.min_score:
                scored.append((score, item))
        scored.sort(key=lambda pair: -pair[0])
        matches[key] = scored[: args.keep]

    print(f"{'Meal item':<32} {'Flyer match':<48} {'Price':>14}  {'Description':<32}  Score")
    print("-" * 140)
    for key, results in matches.items():
        if not results:
            print(f"{key[:32]:<32} {'(no match >= ' + str(args.min_score) + ')':<48}")
            continue
        for i, (score, item) in enumerate(results):
            name = item.get("name") or ""
            brand = item.get("brand") or ""
            brand_str = f" [{brand}]" if brand else ""
            display = item.get("price_display") or ""
            description = (item.get("description") or "")[:32]
            label = key if i == 0 else ""
            print(f"{label[:32]:<32} {(name + brand_str)[:48]:<48} {display:>14}  {description:<32}  {score:>5.2f}")

    if args.write:
        output_path = ROOT / f"giant_flipp_matches_{metadata.get('valid_from') or date.today().isoformat()}.json"
        payload = {
            "metadata": {
                "generated_on": date.today().isoformat(),
                "source_deals_file": str(deals_path),
                "flyer_id": metadata.get("flyer_id"),
                "valid_from": metadata.get("valid_from"),
                "valid_to": metadata.get("valid_to"),
                "min_score": args.min_score,
                "method": "token_overlap",
            },
            "matches": {
                key: [
                    {
                        "score": score,
                        "item": item,
                    }
                    for score, item in results
                ]
                for key, results in matches.items()
            },
        }
        write_json(output_path, payload)
        print(f"\nWrote {output_path}")
    return 0


def parse_size_range(description):
    """Extract a (min_oz, max_oz) range from a flyer description.

    Returns (None, None) if no oz-style size is present (e.g. counts,
    pounds, "Selected Varieties and Sizes").
    """
    if not description:
        return (None, None)
    text = description.lower()
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*oz", text)
    if range_match:
        return (float(range_match.group(1)), float(range_match.group(2)))
    single_match = re.search(r"(\d+(?:\.\d+)?)\s*oz", text)
    if single_match:
        value = float(single_match.group(1))
        return (value, value)
    return (None, None)


def product_size_oz(product):
    size_text = (product.get("size") or "").lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*oz", size_text)
    if match:
        return float(match.group(1))
    return None


GIANT_API_BASE = "/api/v5.0"


def build_giant_search_url(query, service_location_id, user_id, rows):
    params = {
        "keywords": query,
        "sort": "bestMatch asc, name asc",
        "rows": str(rows),
        "start": "0",
        "flags": "true",
        "facet": "nutrition",
        "hkInclude": "true",
        "facetExcludeFilter": "true",
    }
    return (
        f"{GIANT_API_BASE}/products/{user_id}/{service_location_id}"
        f"?{urllib.parse.urlencode(params)}"
    )


def find_flipp_item(deals, args):
    items = deals.get("items", [])
    if args.flipp_id:
        for item in items:
            if str(item.get("flipp_id")) == str(args.flipp_id):
                return item
        return None
    if args.name:
        target = args.name.lower()
        for item in items:
            if target in (item.get("name") or "").lower():
                return item
        return None
    if args.meal_key:
        try:
            from meal_price_tool import match_giant_flipp_to_meal_items
        except ImportError:
            return None
        meal_data = load_json(MEAL_PRICES_FILE)
        matches = match_giant_flipp_to_meal_items(
            {args.meal_key: meal_data.get("items", {}).get(args.meal_key, {})},
            deals,
            min_score=args.min_score,
        )
        match = matches.get(args.meal_key)
        if match:
            return match.get("item")
        return None
    return None


def normalize_brand(text):
    """Strip punctuation and whitespace so 'Land O'Lakes' and 'Land O Lakes' match."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def filter_qualifying_skus(products, flipp_item, price_tolerance):
    target_brand = normalize_brand(flipp_item.get("brand"))
    target_price = flipp_item.get("current_price")
    min_oz, max_oz = parse_size_range(flipp_item.get("description"))

    # Multi-buy deals advertise a per-unit price that only applies at the
    # threshold (e.g. "4 for $4" means $1 each when you buy 4). Giant's API
    # shows the at-pop price, not the deal-threshold price, so we cannot
    # match on exact dollar value for these — we trust brand+size+sale.
    is_multi_buy = flipp_item.get("unit_kind") == "multi_buy"
    if is_multi_buy:
        effective_tolerance = max(price_tolerance, 0.50)
    else:
        effective_tolerance = price_tolerance

    qualifying = []
    excluded = []

    for product in products:
        reasons = []
        product_brand = normalize_brand(product.get("brand"))
        product_name_normalized = normalize_brand((product.get("name") or "").split()[0] if product.get("name") else "")
        # Giant tags store-brand SKUs with brand "Our Brand" but the product
        # name begins with "Giant"; accept either the brand field or the
        # leading word of the name as a brand signal.
        brand_matches = (
            target_brand
            and (
                target_brand in product_brand
                or product_brand in target_brand
                or target_brand == product_name_normalized
            )
        )
        if target_brand and not brand_matches:
            reasons.append(f"brand mismatch ({product.get('brand')})")

        if not (product.get("sale") or product.get("flags", {}).get("sale")):
            reasons.append("not on sale")

        product_price = product.get("price")
        if target_price is not None and product_price is not None:
            distance = abs(float(product_price) - float(target_price))
            if distance > effective_tolerance:
                reasons.append(f"price ${product_price} not within ${effective_tolerance:.2f} of deal ${target_price}")

        if min_oz is not None and max_oz is not None:
            size_oz = product_size_oz(product)
            if size_oz is not None and not (min_oz - 0.5 <= size_oz <= max_oz + 0.5):
                reasons.append(f"size {size_oz}oz outside {min_oz}-{max_oz}oz")

        if reasons:
            excluded.append({"product": product, "reasons": reasons})
        else:
            qualifying.append(product)

    return qualifying, excluded


def variety_query_for(flipp_item):
    brand = (flipp_item.get("brand") or "").strip()
    name = (flipp_item.get("name") or "").strip()
    brand_tokens_lower = {token.lower() for token in re.findall(r"[A-Za-z]+", brand)}
    name_tokens = [
        token for token in re.findall(r"[A-Za-z]+", name)
        if token.lower() not in STOPWORDS
        and token.lower() not in brand_tokens_lower
        and len(token) > 2
    ]
    brand_clean = re.sub(r"[^A-Za-z0-9 ]+", "", brand).strip()
    if brand_clean:
        return f"{brand_clean} {' '.join(name_tokens[:3])}".strip()
    return " ".join(name_tokens[:3])


def summarize_qualifying_product(product):
    return {
        "prodId": product.get("prodId"),
        "name": product.get("name"),
        "brand": product.get("brand"),
        "size": product.get("size"),
        "price": product.get("price"),
        "regularPrice": product.get("regularPrice"),
        "unitPrice": product.get("unitPrice"),
        "unitMeasure": product.get("unitMeasure"),
        "upc": product.get("upc"),
        "outOfStock": product.get("outOfStock"),
        "hasCoupon": product.get("hasCoupon"),
    }


def command_varieties(args):
    try:
        from giant_browser_api_probe import (
            DEFAULT_PORT,
            DEFAULT_SERVICE_LOCATION_ID,
            DEFAULT_USER_ID,
            GiantBrowserError,
            browser_fetch,
            product_rows,
            summarize_product,
            wait_for_devtools,
        )
    except ImportError as exc:
        print(json.dumps({"ok": False, "error": f"giant_browser_api_probe import failed: {exc}"}, indent=2), file=sys.stderr)
        return 2

    if args.deals_file:
        deals = load_json(Path(args.deals_file))
    else:
        candidates = sorted(ROOT.glob(f"{DEALS_PREFIX}_*.json"), reverse=True)
        if not candidates:
            print(json.dumps({"ok": False, "error": f"no {DEALS_PREFIX}_*.json files found; run fetch --write first"}, indent=2), file=sys.stderr)
            return 2
        deals = load_json(candidates[0])

    flipp_item = find_flipp_item(deals, args)
    if not flipp_item:
        target = args.flipp_id or args.name or args.meal_key
        print(json.dumps({"ok": False, "error": f"no flyer item matched: {target}"}, indent=2), file=sys.stderr)
        return 2

    query = args.query or variety_query_for(flipp_item)

    try:
        wait_for_devtools(args.port)
        url = build_giant_search_url(query, args.service_location_id, args.user_id, args.rows)
        response = browser_fetch(args.port, [url])
    except GiantBrowserError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        print("\nHint: launch the browser session first:", file=sys.stderr)
        print("  python3 giant_browser_api_probe.py launch", file=sys.stderr)
        return 2

    result = response["results"][0]
    if not result.get("ok"):
        print(json.dumps({"ok": False, "error": f"browser fetch failed: status={result.get('status')}"}, indent=2), file=sys.stderr)
        return 2

    raw_products = product_rows(result.get("payload") or {})
    products = [summarize_product(product) for product in raw_products]
    qualifying, excluded = filter_qualifying_skus(products, flipp_item, args.price_tolerance)

    output = {
        "deal": {
            "name": flipp_item.get("name"),
            "brand": flipp_item.get("brand"),
            "description": flipp_item.get("description"),
            "price_display": flipp_item.get("price_display"),
            "current_price": flipp_item.get("current_price"),
            "valid_from": flipp_item.get("valid_from"),
            "valid_to": flipp_item.get("valid_to"),
            "flipp_id": flipp_item.get("flipp_id"),
            "flipp_flyer_id": flipp_item.get("flipp_flyer_id"),
        },
        "search_query": query,
        "qualifying_count": len(qualifying),
        "excluded_count": len(excluded),
        "qualifying_skus": [summarize_qualifying_product(product) for product in qualifying],
        "excluded_samples": [
            {
                "product": summarize_qualifying_product(item["product"]),
                "reasons": item["reasons"],
            }
            for item in excluded[:5]
        ],
    }

    if args.json:
        print(json.dumps(output, indent=2))
        return 0

    deal = output["deal"]
    print(f"Deal: {deal['name']}")
    if deal.get("brand"):
        print(f"  Brand: {deal['brand']}")
    print(f"  Description: {deal.get('description') or '(none)'}")
    print(f"  Price: {deal.get('price_display')}  | per-unit ${deal.get('current_price')}")
    print(f"  Valid: {deal.get('valid_from')} to {deal.get('valid_to')}")
    print(f"  Search: \"{query}\"")
    print()
    print(f"Qualifying SKUs ({len(qualifying)} of {len(products)} returned by Giant API):")
    if not qualifying:
        print("  (no products in the search results matched brand+size+sale)")
    for product in qualifying:
        size = product.get("size") or ""
        upc = product.get("upc") or ""
        print(
            f"  prodId={product.get('prodId'):<8} ${product.get('price'):>5.2f} "
            f"(reg ${product.get('regularPrice')}) {size:<14}  {product.get('name')}"
        )

    if excluded and args.show_excluded:
        print(f"\nExcluded products and reasons:")
        for entry in excluded:
            product = entry["product"]
            reasons = "; ".join(entry["reasons"])
            print(f"  prodId={product.get('prodId'):<8} {product.get('name')[:60]} -- {reasons}")

    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="Download the current Giant flyer.")
    fetch.add_argument("--postal-code", default=DEFAULT_POSTAL_CODE)
    fetch.add_argument("--locale", default=DEFAULT_LOCALE)
    fetch.add_argument("--flyer-id", type=int, help="Override flyer ID")
    fetch.add_argument("--timeout", type=float, default=15.0)
    fetch.add_argument("--sleep", type=float, default=0.05, help="Sleep between item-detail requests")
    fetch.add_argument("--no-enrich", action="store_true", help="Skip per-item detail enrichment (faster, but loses multi-buy parsing)")
    fetch.add_argument("--only-priced", action="store_true", help="Drop items without parseable prices")
    fetch.add_argument("--write", action="store_true")
    fetch.add_argument("--output", help="Output path when using --write")

    search = subparsers.add_parser("search", help="Search Flipp for Giant Food items.")
    search.add_argument("query")
    search.add_argument("--postal-code", default=DEFAULT_POSTAL_CODE)
    search.add_argument("--locale", default=DEFAULT_LOCALE)
    search.add_argument("--timeout", type=float, default=15.0)
    search.add_argument("--json", action="store_true")

    match = subparsers.add_parser("match", help="Match flyer items against meal_prices.json.")
    match.add_argument("--deals-file", help="Specific giant_weekly_deals_*.json; default: most recent")
    match.add_argument("--only", action="append", help="Match only the given meal item key (can repeat)")
    match.add_argument("--min-score", type=float, default=0.5, help="Minimum token-overlap score")
    match.add_argument("--keep", type=int, default=3, help="Top matches to keep per meal item")
    match.add_argument("--write", action="store_true", help="Write match summary to JSON")

    varieties = subparsers.add_parser(
        "varieties",
        help="Expand a Selected-Varieties flyer item into the qualifying live Giant SKUs.",
    )
    varieties.add_argument("--deals-file", help="Specific giant_weekly_deals_*.json; default: most recent")
    selector = varieties.add_mutually_exclusive_group(required=True)
    selector.add_argument("--flipp-id", type=int, help="Match the flyer item by Flipp ID")
    selector.add_argument("--name", help="Match the flyer item by name substring (case-insensitive)")
    selector.add_argument("--meal-key", help="Match the flyer item by best score against this meal_prices.json key")
    varieties.add_argument("--query", help="Override the Giant API search query (default: brand + first name tokens)")
    varieties.add_argument("--port", type=int, default=9227, help="Chrome DevTools port")
    varieties.add_argument("--service-location-id", default="50000732", help="Giant service location ID")
    varieties.add_argument("--user-id", default="2", help="Giant API user ID")
    varieties.add_argument("--rows", type=int, default=24, help="Giant API search rows")
    varieties.add_argument("--price-tolerance", type=float, default=0.10, help="Price tolerance for matching the deal price ($)")
    varieties.add_argument("--min-score", type=float, default=0.5, help="Minimum token-overlap score for --meal-key matching")
    varieties.add_argument("--show-excluded", action="store_true", help="Print products that did not qualify and the reasons")
    varieties.add_argument("--json", action="store_true", help="Emit JSON for the inspiration tool")

    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.command == "fetch":
            return command_fetch(args)
        if args.command == "search":
            return command_search(args)
        if args.command == "match":
            return command_match(args)
        if args.command == "varieties":
            return command_varieties(args)
        raise AssertionError(args.command)
    except urllib.error.HTTPError as exc:
        print(json.dumps({"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}, indent=2), file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"request failed: {exc}"}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
