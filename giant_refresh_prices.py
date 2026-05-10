#!/usr/bin/env python3
"""Refresh Giant Food product prices through the validated browser session.

For each meal_prices.json item:

- If a Giant product ID is already saved, call /api/v5.0/products/info by ID.
- Otherwise, search /api/v5.0/products by ingredient name, score candidates,
  and take the best match above a confidence threshold.

Updates meal_prices.json `base_prices.Giant` and `price_sources.Giant`, and
also appends to giant_price_observations.json. Dry-run is the default.

Requires a Chrome session launched by `giant_browser_api_probe.py launch`.
This script never reads, prints, or persists cookies — every API call runs
inside the browser context via Chrome DevTools Protocol.
"""

import argparse
import copy
import json
import re
import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path

from giant_browser_api_probe import (
    DEFAULT_PORT,
    DEFAULT_SERVICE_LOCATION_ID,
    DEFAULT_USER_ID,
    GiantBrowserError,
    PARK_ROAD_CONTEXT,
    browser_fetch,
    product_rows,
    summarize_product,
    wait_for_devtools,
)


ROOT = Path(__file__).parent
MEAL_PRICES_FILE = ROOT / "meal_prices.json"
OBSERVATIONS_FILE = ROOT / "giant_price_observations.json"

API_BASE = "/api/v5.0"
SOURCE_TYPE = "giant_browser_v5_api"
CONFIDENCE_LABEL = "giant_browser_live"

STOPWORDS = {
    "and", "any", "or", "with", "the", "for", "fresh", "select", "ea",
    "each", "pack", "lb", "lbs", "oz", "ct", "count", "size", "family",
    "value", "store", "brand", "premium", "natural", "organic",
}

CATEGORY_NEGATIVES = {
    "protein": {"oil", "flour", "cookie", "ice", "cream", "yogurt", "salad", "dressing", "sauce", "spread", "snack", "crackers"},
    "produce": {"oil", "frozen", "ice", "cream", "yogurt", "snack", "candy", "chocolate", "wine", "beer"},
    "pantry": {"frozen", "ice", "cream", "wine", "beer", "spirits", "ravioli"},
    "frozen": {"oil", "wine", "beer", "spirits", "candy"},
    "dairy": {"oil", "frozen", "wine", "beer", "spirits", "candy", "chocolate", "wax"},
}

# Products tagged with these tokens almost always indicate the meal item is
# being matched against a child-targeted SKU (baby food, toddler pouches, kids
# snacks). Reject these regardless of token overlap unless the meal item is
# explicitly marked for that audience.
GENERIC_REJECT_TOKENS = {"baby", "infant", "toddler", "tot", "kids", "kid"}

# When a meal item asks for a bulk staple (e.g. "5 lb bag" rice, "32 oz bag"
# shredded cheese), products with these tokens are usually portion-prep
# products that don't fit. The penalty is large enough to push them below the
# default score threshold even with a perfect token overlap.
BULK_UNIT_KEYWORDS = {"bag", "box", "can", "jar", "bottle", "package", "container", "pkg", "carton", "ctn", "roll"}
# Prefix-style tokens. "microwav" catches both "microwave" and "microwavable".
PORTION_PREFIXES = {"microwav", "instant", "ready", "pouch", "single-serve", "boil-in-bag"}

# Preservation/preparation styles that don't fit fresh produce. "Roasted",
# "pickled", "dried", and "jarred" peppers/onions/spinach are different
# products from the fresh equivalent, even when token overlap is perfect.
PRODUCE_PREP_REJECT_TOKENS = {"roasted", "pickled", "dried", "canned", "jarred", "marinated", "fermented"}

# Dietary/specialty variant tokens. When the meal item is generic (does not
# include any of these tokens), we penalize products that carry them, since a
# "fat free" or "lactose free" SKU is a specialized variant and not the
# expected baseline product.
DIETARY_VARIANT_TOKENS = {
    "fat", "free", "lowfat", "reduced", "lactose", "gluten",
    "diet", "skim", "nonfat",
}


def load_json(path):
    with path.open() as f:
        return json.load(f)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def money(value):
    if value is None:
        return "n/a"
    return f"${value:.2f}"


def giant_source(item):
    return item.get("price_sources", {}).get("Giant", {})


def parse_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def token_set(text):
    normalized = (text or "").lower()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return {t for t in tokens if len(t) > 2 and t not in STOPWORDS and not t.isdigit()}


def is_bulk_meal_unit(meal_unit):
    text = (meal_unit or "").lower()
    return any(keyword in text for keyword in BULK_UNIT_KEYWORDS)


def has_portion_signal(product):
    name = (product.get("name") or "").lower()
    size = (product.get("size") or "").lower()
    text = f"{name} {size}"
    return any(prefix in text for prefix in PORTION_PREFIXES)


def parse_size_oz(text):
    """Parse a size string into (qty_oz, kind) where kind is 'weight' or 'count'.

    Returns (None, kind) when the unit family is known but quantity is not
    (e.g. plain 'lb' for sell-by-weight). Returns (None, None) when nothing
    can be parsed.
    """
    if not text:
        return (None, None)
    t = text.lower()

    weight_match = re.search(r"(\d+(?:\.\d+)?)\s*(lbs?|pound)", t)
    if weight_match:
        return (float(weight_match.group(1)) * 16, "weight")

    fl_oz_match = re.search(r"(\d+(?:\.\d+)?)\s*fl\.?\s*oz", t)
    if fl_oz_match:
        return (float(fl_oz_match.group(1)), "weight")

    oz_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)", t)
    if oz_match:
        return (float(oz_match.group(1)), "weight")

    if re.search(r"\bdoz", t):
        qty_match = re.search(r"(\d+(?:\.\d+)?)\s*doz", t)
        qty = float(qty_match.group(1)) if qty_match else 1.0
        return (qty * 12, "count")

    count_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:ct|count|pack|piece|pk)\b", t)
    if count_match:
        return (float(count_match.group(1)), "count")

    if re.search(r"\b(lbs?|pound|sell\s*by\s*weight)\b", t):
        return (None, "weight")
    if re.search(r"\beach\b|\bea\b", t):
        return (1.0, "count")

    return (None, None)


def size_compatibility(meal_unit, product):
    """Return a score adjustment based on how well the package sizes line up."""
    product_size = product.get("size") or ""
    meal_qty, meal_kind = parse_size_oz(meal_unit)
    prod_qty, prod_kind = parse_size_oz(product_size)

    # When the API indicates the product is sold by the pound directly,
    # treat it as weight-kind even if the size string is empty.
    if prod_kind is None:
        unit_measure = (product.get("unitMeasure") or "").upper()
        if unit_measure in {"LB", "LBS", "POUND", "POUNDS"}:
            prod_kind = "weight"

    if meal_kind is None or prod_kind is None:
        return 0.0
    if meal_kind != prod_kind:
        return -0.30
    if meal_qty is None or prod_qty is None:
        return 0.05
    if meal_qty <= 0:
        return 0.0
    ratio = prod_qty / meal_qty
    if 0.85 <= ratio <= 1.20:
        return 0.20
    if 0.5 <= ratio <= 2.0:
        return 0.05
    if 0.20 <= ratio <= 5.0:
        return 0.0
    return -0.30


def candidate_score(meal_key, meal_record, product):
    meal_tokens = token_set(meal_key)
    product_tokens = token_set(product.get("name") or "") | token_set(product.get("brand") or "")
    if not meal_tokens:
        return 0.0

    if GENERIC_REJECT_TOKENS & product_tokens and not (GENERIC_REJECT_TOKENS & meal_tokens):
        return 0.0

    overlap = meal_tokens & product_tokens
    score = len(overlap) / max(1, len(meal_tokens))

    category = (meal_record.get("category") or "").lower()
    negatives = CATEGORY_NEGATIVES.get(category, set())
    if negatives & product_tokens:
        score -= 0.25

    # Reject portion-prep SKUs (microwave cups, single-serve pouches) when
    # the meal item is a bulk pantry staple. Restricted to pantry only because
    # produce items often legitimately carry "Ready to Eat" or "Microwavable"
    # markers without being a different product class (pre-washed broccoli,
    # microwave-friendly sweet potato bags, etc.).
    if (
        category == "pantry"
        and is_bulk_meal_unit(meal_record.get("unit"))
        and has_portion_signal(product)
    ):
        score -= 0.50

    # Fresh produce should not match jarred/pickled/dried/canned variants.
    if category == "produce":
        meal_lower = (meal_key or "").lower()
        if PRODUCE_PREP_REJECT_TOKENS & product_tokens and not any(
            token in meal_lower for token in PRODUCE_PREP_REJECT_TOKENS
        ):
            score -= 0.50

    score += size_compatibility(meal_record.get("unit"), product)

    # Penalize specialty dietary variants when the meal item does not ask
    # for them (e.g. plain "shredded cheese" should not match Kraft Fat Free).
    meal_lower = (meal_key or "").lower()
    meal_tokens_lower = set(re.findall(r"[a-z]+", meal_lower))
    variant_hits = DIETARY_VARIANT_TOKENS & product_tokens
    if variant_hits and not (variant_hits & meal_tokens_lower):
        score -= 0.20 * len(variant_hits)

    brand_lower = (product.get("brand") or "").lower()
    name_lower = (product.get("name") or "").lower()
    is_giant_store_brand = (
        brand_lower == "giant"
        or (brand_lower == "our brand" and name_lower.startswith("giant "))
    )
    if is_giant_store_brand:
        score += 0.10

    return round(max(score, 0.0), 3)


def confidence_label(score, exact_id_match=False):
    if exact_id_match:
        return "high"
    if score >= 0.75:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def planning_pound_price(item, product):
    """Convert API price to per-pound price when ingredient unit is in pounds.

    The browser API exposes per-unit pricing via `unitPrice`/`unitMeasure`,
    and a `price` for the package. When the ingredient unit is `lb`, prefer
    the per-lb unitPrice; otherwise infer from package price + size text.
    """
    unit = (item.get("unit") or "").strip().lower()
    if unit not in {"lb", "pound", "pounds"}:
        return None
    unit_price = parse_float(product.get("unitPrice"))
    unit_measure = (product.get("unitMeasure") or "").strip().upper()
    if unit_price is not None and unit_measure in {"LB", "LBS", "POUND", "POUNDS"}:
        return unit_price
    return None


def planning_base_price(item, product):
    """Best estimate of the regular Giant price matching the planning unit."""
    pound_price = planning_pound_price(item, product)
    if pound_price is not None:
        return pound_price
    regular = parse_float(product.get("regularPrice"))
    if regular is not None:
        return regular
    return parse_float(product.get("price"))


def planning_current_price(item, product):
    pound_price = planning_pound_price(item, product)
    if pound_price is not None:
        return pound_price
    return parse_float(product.get("price"))


def make_product_url(product_id):
    if not product_id:
        return None
    return f"https://giantfood.com/product/{product_id}"


def build_search_url(query, service_location_id, user_id, rows):
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
        f"{API_BASE}/products/{user_id}/{service_location_id}"
        f"?{urllib.parse.urlencode(params)}"
    )


def build_product_url(product_id, service_location_id, user_id):
    params = {
        "extendedInfo": "true",
        "flags": "true",
        "nutrition": "true",
        "substitute": "true",
        "categoryInfo": "true",
    }
    return (
        f"{API_BASE}/products/info/{user_id}/{service_location_id}/{product_id}"
        f"?{urllib.parse.urlencode(params)}"
    )


def fetch_one(port, url):
    response = browser_fetch(port, [url])
    result = response["results"][0]
    if not result.get("ok"):
        raise GiantBrowserError(
            f"Browser fetch failed: status={result.get('status')} url={url} body={result.get('text') or result.get('error')}"
        )
    return result.get("payload") or {}


def find_product_by_id(payload, product_id):
    products = product_rows(payload)
    for product in products:
        if str(product.get("prodId")) == str(product_id):
            return product
    if products:
        return products[0]
    return None


def best_search_candidate(meal_key, meal_record, payload, min_score):
    products = product_rows(payload)
    scored = []
    for product in products:
        score = candidate_score(meal_key, meal_record, product)
        scored.append((score, product))
    scored.sort(key=lambda pair: -pair[0])
    if not scored or scored[0][0] < min_score:
        return None, scored[: 5]
    return scored[0], scored[: 5]


def make_observation(meal_key, item, product, service_location_id, observed_on, source, confidence):
    return {
        "item_key": meal_key,
        "product_name": product.get("name"),
        "product_id": product.get("prodId"),
        "url": make_product_url(product.get("prodId")),
        "source_type": SOURCE_TYPE,
        "endpoint": API_BASE,
        "service_location_id": str(service_location_id),
        "store_number": PARK_ROAD_CONTEXT["store_number"],
        "store_address": PARK_ROAD_CONTEXT["store_address"],
        "current_price": parse_float(product.get("price")),
        "regular_price": parse_float(product.get("regularPrice")),
        "unit_price": parse_float(product.get("unitPrice")),
        "unit_measure": product.get("unitMeasure"),
        "size": product.get("size"),
        "brand": product.get("brand"),
        "upc": product.get("upc"),
        "sale": (product.get("flags") or {}).get("sale") if isinstance(product.get("flags"), dict) else None,
        "out_of_stock": (product.get("flags") or {}).get("outOfStock") if isinstance(product.get("flags"), dict) else None,
        "has_coupon": product.get("hasCoupon"),
        "planning_base_price": planning_base_price(item, product),
        "planning_current_price": planning_current_price(item, product),
        "match_source": source,
        "match_confidence": confidence,
        "observed_on": observed_on,
        "confidence": CONFIDENCE_LABEL,
    }


def update_item_source(meal_key, item, product, service_location_id, observed_on, source, confidence, fill_missing_only=False):
    item.setdefault("price_sources", {}).setdefault("Giant", {})
    target = item["price_sources"]["Giant"]
    target.update(
        {
            "source_type": SOURCE_TYPE,
            "observed_on": observed_on,
            "service_location_id": str(service_location_id),
            "store_number": PARK_ROAD_CONTEXT["store_number"],
            "store_address": PARK_ROAD_CONTEXT["store_address"],
            "product_id": product.get("prodId"),
            "product_name": product.get("name"),
            "url": make_product_url(product.get("prodId")),
            "endpoint": API_BASE,
            "confidence": CONFIDENCE_LABEL,
            "match_source": source,
            "match_confidence": confidence,
            "current_price": parse_float(product.get("price")),
            "regular_price": parse_float(product.get("regularPrice")),
            "unit_price": parse_float(product.get("unitPrice")),
            "unit_measure": product.get("unitMeasure"),
            "size": product.get("size"),
            "brand": product.get("brand"),
            "upc": product.get("upc"),
            "planning_base_price": planning_base_price(item, product),
            "planning_current_price": planning_current_price(item, product),
        }
    )

    base_price = planning_base_price(item, product)
    if base_price is None or confidence not in {"high", "medium"}:
        return
    base_prices = item.setdefault("base_prices", {})
    if fill_missing_only and base_prices.get("Giant") is not None:
        return
    base_prices["Giant"] = base_price


def iter_candidates(meal_prices, args):
    items = meal_prices.get("items", {})
    only = {key.lower() for key in (args.only or [])}
    selected = []
    for meal_key, item in items.items():
        if only and meal_key.lower() not in only:
            continue
        if args.with_id_only and not giant_source(item).get("product_id"):
            continue
        selected.append((meal_key, item))
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def load_observations():
    if OBSERVATIONS_FILE.exists():
        try:
            return load_json(OBSERVATIONS_FILE)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "metadata": {
            "store_number": PARK_ROAD_CONTEXT["store_number"],
            "store_address": PARK_ROAD_CONTEXT["store_address"],
            "service_location_id": DEFAULT_SERVICE_LOCATION_ID,
            "endpoint": API_BASE,
            "source_type": SOURCE_TYPE,
            "notes": [
                "Observations are written by giant_refresh_prices.py.",
                "Only high/medium-confidence matches update meal_prices.json base_prices.",
                "Low-confidence rows are kept here for audit and re-tuning.",
            ],
        },
        "observations": {},
    }


def refresh(args):
    wait_for_devtools(args.port)

    meal_prices = load_json(MEAL_PRICES_FILE)
    updated_meal_prices = copy.deepcopy(meal_prices)
    updated_observations = load_observations()
    updated_observations.setdefault("observations", {})

    candidates = iter_candidates(updated_meal_prices, args)
    observed_on = args.observed_on or date.today().isoformat()

    rows = []
    failures = []

    for meal_key, item in candidates:
        old_base = item.get("base_prices", {}).get("Giant")
        saved_pid = None if args.force_search else giant_source(item).get("product_id")

        try:
            if saved_pid:
                payload = fetch_one(
                    args.port,
                    build_product_url(saved_pid, args.service_location_id, args.user_id),
                )
                product = find_product_by_id(payload, saved_pid)
                source_kind = "product_id"
                score = 1.0
                top_candidates = [(score, product)] if product else []
            else:
                query = item.get("category") and f"{meal_key}" or meal_key
                payload = fetch_one(
                    args.port,
                    build_search_url(query, args.service_location_id, args.user_id, args.rows),
                )
                best, top_candidates = best_search_candidate(meal_key, item, payload, args.min_score)
                if best is None:
                    failures.append((meal_key, saved_pid, f"no candidate >= {args.min_score}"))
                    continue
                score, product = best
                source_kind = "search"
        except GiantBrowserError as exc:
            failures.append((meal_key, saved_pid, str(exc)))
            continue

        if product is None:
            failures.append((meal_key, saved_pid, "no product returned"))
            continue

        confidence = confidence_label(score, exact_id_match=bool(saved_pid))
        update_item_source(
            meal_key, item, product, args.service_location_id, observed_on, source_kind, confidence,
            fill_missing_only=args.fill_missing_only,
        )

        observation_key = str(product.get("prodId") or meal_key)
        updated_observations["observations"][observation_key] = make_observation(
            meal_key, item, product, args.service_location_id, observed_on, source_kind, confidence
        )

        new_base = item.get("base_prices", {}).get("Giant")
        rows.append(
            {
                "item": meal_key,
                "pid": product.get("prodId"),
                "name": (product.get("name") or "")[:50],
                "old_base": old_base,
                "new_base": new_base,
                "current": parse_float(product.get("price")),
                "regular": parse_float(product.get("regularPrice")),
                "unit_price": parse_float(product.get("unitPrice")),
                "unit_measure": product.get("unitMeasure"),
                "score": score,
                "confidence": confidence,
                "source_kind": source_kind,
            }
        )

        time.sleep(args.sleep)

    metadata = updated_observations.setdefault("metadata", {})
    metadata.update(
        {
            "service_location_id": str(args.service_location_id),
            "last_refreshed_on": observed_on,
        }
    )

    print(f"Giant store {PARK_ROAD_CONTEXT['store_number']} (service location {args.service_location_id})")
    print(f"Refreshed {len(rows)} ingredient lookups; {len(failures)} skipped")
    if not args.write:
        print("Mode: dry-run; no files written")
    print()
    print(f"{'Item':<32} {'PID':<10} {'Match':<7} {'Conf':<7} {'Score':>5} {'Old':>8} {'New':>8} {'Curr':>8} {'Reg':>8} Product")
    print("-" * 140)
    for row in rows:
        print(
            f"{row['item'][:32]:<32} {str(row['pid'] or '-'):<10} "
            f"{row['source_kind']:<7} {row['confidence']:<7} "
            f"{row['score']:>5.2f} {money(row['old_base']):>8} {money(row['new_base']):>8} "
            f"{money(row['current']):>8} {money(row['regular']):>8} {row['name']}"
        )

    if failures:
        print("\nSkipped or failed:")
        for meal_key, saved_pid, reason in failures:
            tag = f"pid={saved_pid}" if saved_pid else "no saved pid"
            print(f"- {meal_key} ({tag}): {reason}")

    if args.write:
        write_json(MEAL_PRICES_FILE, updated_meal_prices)
        write_json(OBSERVATIONS_FILE, updated_observations)
        print(f"\nWrote {MEAL_PRICES_FILE.name} and {OBSERVATIONS_FILE.name}")

    return 0 if not failures else 1


def main():
    parser = argparse.ArgumentParser(
        description="Refresh Giant Food prices through the validated browser session."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Chrome DevTools port")
    parser.add_argument("--service-location-id", default=DEFAULT_SERVICE_LOCATION_ID, help="Giant service location ID")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="Giant API user ID")
    parser.add_argument("--rows", type=int, default=8, help="Search rows per ingredient")
    parser.add_argument("--min-score", type=float, default=0.55, help="Minimum match score for search results")
    parser.add_argument("--limit", type=int, help="Refresh only the first N items")
    parser.add_argument("--only", nargs="*", help="Refresh only exact meal_prices item names")
    parser.add_argument("--with-id-only", action="store_true", help="Only refresh items that already have a saved Giant product ID")
    parser.add_argument("--fill-missing-only", action="store_true", help="Do not overwrite items that already have a Giant base price")
    parser.add_argument("--force-search", action="store_true", help="Ignore saved Giant product IDs and re-run the search/scoring path for every item")
    parser.add_argument("--observed-on", help="Override observed_on date, YYYY-MM-DD")
    parser.add_argument("--sleep", type=float, default=0.2, help="Sleep between API calls")
    parser.add_argument("--write", action="store_true", help="Write refreshed JSON files")
    parser.add_argument("--dry-run", action="store_true", help="Explicitly keep dry-run mode")
    args = parser.parse_args()

    if args.dry_run and args.write:
        raise SystemExit("Use either --dry-run or --write, not both.")

    try:
        return refresh(args)
    except GiantBrowserError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        print("\nHint: launch the browser session first:", file=sys.stderr)
        print("  python3 giant_browser_api_probe.py launch", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
