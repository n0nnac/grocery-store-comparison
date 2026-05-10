#!/usr/bin/env python3
"""Read Safeway clippable coupon/deal offers for the configured store.

The coupon gallery is a separate overlay layer from base product prices and
weekly ads. This tool only reads offer data; it does not clip or unclip offers.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from safeway_api_search import fetch_search, normalize_doc


ROOT = Path(__file__).parent
COUPONS_FILE = ROOT / "safeway_coupons.json"
MEAL_PRICES_FILE = ROOT / "meal_prices.json"

DEFAULT_STORE_ID = "923"
DEFAULT_BANNER = "safeway"
COUPON_SUBSCRIPTION_KEY = "7bad9afbb87043b28519c4443106db06"

COUPON_GALLERY_ENDPOINT = "https://www.safeway.com/abs/pub/xapi/offers/companiongalleryoffer"
COUPON_DETAIL_ENDPOINT = "https://www.safeway.com/abs/pub/xapi/offers"


def load_json(path):
    with path.open() as f:
        return json.load(f)


def write_json(path, data):
    with path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


def request_json(url, headers, method="GET", body=None, timeout=20.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def base_headers(banner):
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://www.{banner}.com/loyalty/coupons-deals",
        "Ocp-Apim-Subscription-Key": COUPON_SUBSCRIPTION_KEY,
        "x-swy-banner": banner,
        "x-swy-client-id": "web-portal",
    }


def fetch_gallery(store_id, banner, timeout):
    params = {
        "storeId": str(store_id),
        "rand": str(int(time.time() * 1000) % 1000000),
        "includeRedmBonusPathFPOffers": "true",
    }
    url = f"{COUPON_GALLERY_ENDPOINT}?{urllib.parse.urlencode(params)}"
    headers = base_headers(banner)
    headers.update({"X-SWY_API_KEY": "emju", "X-SWY_VERSION": "3.0"})
    return request_json(url, headers, timeout=timeout)


def fetch_offer_detail(offer_id, offer_program, store_id, banner, timeout):
    params = {
        "offerId": str(offer_id),
        "storeId": str(store_id),
        "offerPgm": str(offer_program),
        "includeUpc": "y",
    }
    url = f"{COUPON_DETAIL_ENDPOINT}?{urllib.parse.urlencode(params)}"
    headers = base_headers(banner)
    headers.update(
        {
            "Content-Type": "application/json",
            "X-SWY-APPLICATION-TYPE": "web",
            "X-SWY_API_KEY": "emju",
            "X-SWY_VERSION": "3.0",
        }
    )
    return request_json(url, headers, timeout=timeout)


def epoch_ms_to_date(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def infer_discount(value_text):
    if not value_text:
        return {"kind": "unknown"}
    text = " ".join(str(value_text).split())
    points = re.search(r"(?:earn\s+)?([0-9]+)\s*x\s+points?", text, flags=re.IGNORECASE)
    if points:
        return {
            "kind": "points_multiplier",
            "multiplier": int(points.group(1)),
            "text": text,
        }

    amount = re.search(r"\$([0-9]+(?:\.[0-9]{1,2})?)", text)
    if not amount:
        return {"kind": "unknown", "text": text}

    value = float(amount.group(1))
    lowered = text.lower()
    if "per lb" in lowered or "/lb" in lowered or " per pound" in lowered:
        return {"kind": "fixed_unit_price", "price": value, "unit": "lb", "text": text}
    if "off" in lowered or "save" in lowered:
        return {"kind": "amount_off", "amount": value, "text": text}
    if "each" in lowered or " ea" in lowered or lowered.endswith("ea"):
        return {"kind": "fixed_price", "price": value, "text": text}
    return {"kind": "dollar_value", "amount": value, "text": text}


def infer_application(offer, discount):
    text = " ".join(
        str(value or "")
        for value in (
            offer.get("brand"),
            offer.get("name"),
            offer.get("description"),
            offer.get("ecomDescription"),
            offer.get("forUDescription"),
        )
    )
    compact = " ".join(text.split())
    lowered = compact.lower()
    threshold = infer_threshold_amount(compact)

    if discount.get("kind") == "points_multiplier":
        scope = offer.get("category")
        allocation = "requires_terms_check"
        if "gift card" in lowered:
            scope = "Gift Cards"
            allocation = "cart_level"
        elif "look for tags" in lowered or "participating" in lowered:
            allocation = "tagged_products"
        elif threshold is not None:
            allocation = "cart_level"
        return {
            "kind": "points_bonus",
            "scope": scope,
            "threshold_amount": threshold,
            "discount": discount,
            "allocation": allocation,
        }

    if "department purchase" in lowered:
        department = infer_department_scope(compact, offer.get("category"))
        return {
            "kind": "department_threshold",
            "scope": department,
            "threshold_amount": threshold,
            "discount": discount,
            "allocation": "cart_level",
        }

    if threshold is not None and (
        "participating product" in lowered
        or "participating item" in lowered
        or "when you purchase" in lowered
        or "when you buy" in lowered
    ):
        return {
            "kind": "basket_threshold",
            "scope": offer.get("category"),
            "threshold_amount": threshold,
            "discount": discount,
            "allocation": "cart_level",
        }

    if discount.get("kind") in {"fixed_unit_price", "fixed_price"}:
        return {
            "kind": "item_or_product_price",
            "scope": offer.get("category"),
            "allocation": "line_item",
        }

    if discount.get("kind") == "amount_off":
        return {
            "kind": "item_or_basket_discount",
            "scope": offer.get("category"),
            "allocation": "requires_terms_check",
        }

    return {
        "kind": "unknown",
        "scope": offer.get("category"),
        "allocation": "requires_terms_check",
    }


def infer_threshold_amount(text):
    patterns = [
        r"\$([0-9]+(?:\.[0-9]{1,2})?)\s+or\s+more",
        r"buy\s+any\s+\$([0-9]+(?:\.[0-9]{1,2})?)",
        r"purchase\s+\$([0-9]+(?:\.[0-9]{1,2})?)",
        r"when\s+you\s+purchase\s+\$([0-9]+(?:\.[0-9]{1,2})?)",
    ]
    lowered = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return float(match.group(1))
    return None


def infer_department_scope(text, fallback):
    match = re.search(r"any\s+(.+?)\s+department\s+purchase", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return fallback


def normalize_offer(offer):
    value_text = (
        offer.get("offerPrice")
        or offer.get("forUDescription")
        or offer.get("ecomDescription")
        or ""
    )
    normalized = {
        "offer_id": str(offer.get("offerId")),
        "offer_program": offer.get("offerPgm"),
        "offer_sub_program": offer.get("offerSubPgm"),
        "brand": offer.get("brand") or offer.get("name"),
        "name": offer.get("name") or offer.get("brand"),
        "category": offer.get("category") or offer.get("categoryType"),
        "events": offer.get("events") or offer.get("hierarchies", {}).get("events") or [],
        "value_text": value_text,
        "ecom_description": offer.get("ecomDescription"),
        "for_u_description": offer.get("forUDescription"),
        "description": offer.get("description"),
        "disclaimer": offer.get("disclaimer"),
        "status": offer.get("status"),
        "is_clippable": offer.get("isClippable"),
        "is_displayable": offer.get("isDisplayable"),
        "usage_type": offer.get("usageType"),
        "limits": offer.get("limits"),
        "purchase_rank": parse_int(offer.get("purchaseRank")),
        "arrival_rank": parse_int(offer.get("arrivalRank")),
        "expiry_rank": parse_int(offer.get("expiryRank")),
        "start_date": epoch_ms_to_date(offer.get("startDate")),
        "end_date": epoch_ms_to_date(offer.get("endDate")),
        "image": offer.get("image"),
        "image_id": offer.get("imageId"),
        "external_offer_id": offer.get("extlOfferId"),
        "discount": infer_discount(value_text),
        "source_type": "coupon_gallery_api",
        "endpoint": COUPON_GALLERY_ENDPOINT,
        "account_state": default_account_state("unauthenticated_gallery"),
    }
    normalized["application"] = infer_application(offer, normalized["discount"])
    return normalized


def default_account_state(source_type):
    return {
        "source_type": source_type,
        "clipped": None,
        "clip_status_confirmed_on": None,
        "household_specific": None,
    }


def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def add_detail(offer, detail):
    offer["detail_endpoint"] = COUPON_DETAIL_ENDPOINT
    offer["upc_list"] = detail.get("upcList") or []
    offer["offer_end_date"] = detail.get("offerEndDate")
    offer["offer_program_type"] = detail.get("offerProgramType")
    offer["offer_proto_type"] = detail.get("offerProtoType")
    detail_obj = detail.get("offerDetail") or {}
    if detail_obj:
        offer["detail_name"] = detail_obj.get("name")
        offer["detail_description"] = detail_obj.get("description")
        offer["detail_offer_price"] = detail_obj.get("offerPrice")
        offer["detail_for_u_description"] = detail_obj.get("forUDescription")
        offer["detail_primary_category"] = detail_obj.get("primaryCategoryNM")
    return offer


def add_resolved_products(offer, store_id, banner, timeout, max_upcs=None):
    products = []
    seen = set()
    upcs = offer.get("upc_list") or []
    selected_upcs = upcs if max_upcs is None else upcs[:max_upcs]
    for upc in selected_upcs:
        try:
            payload = fetch_search(str(upc), store_id, 5, 0, banner, "pickup", timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        for doc in payload.get("response", {}).get("docs", []):
            product = normalize_doc(doc)
            key = product.get("pid") or product.get("upc")
            if key and key not in seen:
                seen.add(key)
                products.append(product)
    offer["resolved_products"] = products
    offer["upc_resolution"] = {
        "available_upc_count": len(upcs),
        "resolved_upc_count": len(selected_upcs),
        "max_upcs": max_upcs,
    }
    return offer


def filter_offers(offers, query, category, clippable_only):
    filtered = offers
    if category:
        category_l = category.lower()
        filtered = [
            offer for offer in filtered
            if category_l in (offer.get("category") or "").lower()
        ]
    if query:
        query_l = query.lower()
        fields = ("brand", "name", "description", "value_text", "category")
        filtered = [
            offer for offer in filtered
            if any(query_l in str(offer.get(field) or "").lower() for field in fields)
        ]
    if clippable_only:
        filtered = [offer for offer in filtered if offer.get("is_clippable") is not False]
    return filtered


def meal_upc_index():
    prices = load_json(MEAL_PRICES_FILE)
    index = {}
    for item_name, item in prices.get("items", {}).items():
        source = item.get("price_sources", {}).get("Safeway", {})
        upc = source.get("upc")
        if upc:
            index.setdefault(str(upc), []).append(item_name)
    return index


def print_table(offers, show_matches=False):
    print(f"{'Offer ID':<10} {'Pgm':<4} {'End':<10} {'Value':<18} {'Category':<24} Brand / Description")
    print("-" * 128)
    for offer in offers:
        value = (offer.get("value_text") or "")[:18]
        category = (offer.get("category") or "")[:24]
        brand = offer.get("brand") or ""
        desc = offer.get("description") or ""
        print(
            f"{offer['offer_id']:<10} {offer.get('offer_program') or '':<4} "
            f"{offer.get('end_date') or offer.get('offer_end_date') or '':<10} "
            f"{value:<18} {category:<24} {brand} - {desc[:90]}"
        )
        if show_matches and offer.get("matched_items"):
            print(f"{'':<10} {'':<4} {'':<10} {'':<18} {'':<24} matches: {', '.join(offer['matched_items'])}")
        if offer.get("resolved_products"):
            for product in offer["resolved_products"][:5]:
                current = product.get("price")
                base = product.get("base_price")
                per = product.get("price_per")
                unit = (product.get("unit_quantity") or product.get("unit_of_measure") or "unit").strip().lower()
                current_text = "" if current is None else f"${current:.2f}"
                base_text = "" if base is None else f"${base:.2f}"
                per_text = "" if per is None else f"${per:.2f}/{unit}"
                print(
                    f"{'':<10} {'':<4} {'':<10} {'':<18} {'':<24} "
                    f"product {product.get('pid')}: {current_text} current, {base_text} base, {per_text} - {product.get('name')}"
                )


def main():
    parser = argparse.ArgumentParser(description="Search Safeway clippable coupons/deals.")
    parser.add_argument("query", nargs="?", help="Text to search in offer brand/name/description")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--banner", default=DEFAULT_BANNER, help="Banner, usually safeway")
    parser.add_argument("--category", help="Category substring, e.g. 'Meat & Seafood'")
    parser.add_argument("--limit", type=int, default=25, help="Rows to print or enrich")
    parser.add_argument("--all", action="store_true", help="Do not limit printed/written offers")
    parser.add_argument("--with-details", action="store_true", help="Fetch offer detail and UPC list for selected offers")
    parser.add_argument("--resolve-upcs", action="store_true", help="Resolve detailed offer UPCs through the Safeway product API")
    parser.add_argument("--max-upcs", type=int, help="Limit UPCs resolved per offer; omit for all")
    parser.add_argument("--match-meal-prices", action="store_true", help="Match detailed coupon UPCs to meal_prices Safeway UPCs")
    parser.add_argument("--include-unclippable", action="store_true", help="Keep offers marked unclippable")
    parser.add_argument("--write", action="store_true", help=f"Write normalized results to {COUPONS_FILE.name}")
    parser.add_argument("--json", action="store_true", help="Print normalized JSON")
    parser.add_argument("--timeout", type=float, default=20.0, help="Request timeout in seconds")
    args = parser.parse_args()

    try:
        payload = fetch_gallery(args.store_id, args.banner, args.timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body[:500]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Request failed: {exc}")

    raw_offers = payload.get("companionGalleryOffer", {})
    offers = [normalize_offer(offer) for offer in raw_offers.values()]
    offers.sort(key=lambda offer: (offer.get("purchase_rank") is None, offer.get("purchase_rank") or 999999))
    offers = filter_offers(offers, args.query, args.category, not args.include_unclippable)

    selected = offers if args.all else offers[: args.limit]

    if args.with_details or args.match_meal_prices or args.resolve_upcs:
        for offer in selected:
            try:
                detail = fetch_offer_detail(
                    offer["offer_id"],
                    offer.get("offer_program") or "",
                    args.store_id,
                    args.banner,
                    args.timeout,
                )
                add_detail(offer, detail)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                offer["detail_error"] = str(exc)
            if args.resolve_upcs and offer.get("upc_list"):
                add_resolved_products(offer, args.store_id, args.banner, args.timeout, args.max_upcs)

    if args.match_meal_prices:
        upc_index = meal_upc_index()
        for offer in selected:
            matched = []
            for upc in offer.get("upc_list") or []:
                matched.extend(upc_index.get(str(upc), []))
            if matched:
                offer["matched_items"] = sorted(set(matched))

    output = {
        "metadata": {
            "source_url": f"https://www.{args.banner}.com/loyalty/coupons-deals",
            "store_id": str(args.store_id),
            "observed_on": datetime.now(timezone.utc).date().isoformat(),
            "source_type": "coupon_gallery_api",
            "gallery_endpoint": COUPON_GALLERY_ENDPOINT,
            "detail_endpoint": COUPON_DETAIL_ENDPOINT,
            "total_gallery_offers": len(raw_offers),
            "filtered_offers": len(offers),
            "selected_offers": len(selected),
            "details_fetched": bool(args.with_details or args.match_meal_prices or args.resolve_upcs),
            "upcs_resolved": bool(args.resolve_upcs),
        },
        "offers": selected,
    }

    if args.write:
        write_json(COUPONS_FILE, output)
        print(f"Wrote {COUPONS_FILE.name}: {len(selected)} offers")

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(
            f"Safeway store {args.store_id}: {len(offers)} matching coupon/deal offers "
            f"({len(raw_offers)} total gallery offers)"
        )
        print_table(selected, show_matches=args.match_meal_prices)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
