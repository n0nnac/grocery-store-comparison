#!/usr/bin/env python3
"""Probe Giant Food's coupon search API through the validated browser session.

Giant exposes its full digital coupon catalog through an authenticated
POST endpoint that returns ~3000 coupons paginated 20 at a time:

    POST /api/v7.0/coupons/users/{user_id}/prism/service-locations/{loc}/coupons/search
        ?fullDocument=true&unwrap=true

This script paginates through the catalog from the browser session, normalizes
each coupon to a stable shape, and writes to `giant_coupons.json`. Subcommands
let you search the saved catalog by keyword, category, or meal-item key.

Per-product coupon visibility (the `availableDisplayCoupons` array on a Giant
product detail response) is captured by giant_refresh_prices.py and stored in
`price_sources.Giant.available_coupons`. This script is the catalog-level
companion.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
from datetime import date
from pathlib import Path

from giant_browser_api_probe import (
    DEFAULT_PORT,
    DEFAULT_SERVICE_LOCATION_ID,
    DEFAULT_USER_ID,
    GiantBrowserError,
    PARK_ROAD_CONTEXT,
    evaluate_in_giant_page,
    wait_for_devtools,
)


ROOT = Path(__file__).parent
COUPONS_FILE = ROOT / "giant_coupons.json"
MEAL_PRICES_FILE = ROOT / "meal_prices.json"

API_BASE = "/api/v7.0"
SOURCE_TYPE = "giant_coupon_v7_api"


def load_json(path):
    with path.open() as f:
        return json.load(f)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def fetch_coupon_page(port, user_id, service_location_id, start, rows, timeout):
    """POST one page of the coupon catalog through the browser tab."""
    url = (
        f"{API_BASE}/coupons/users/{user_id}/prism"
        f"/service-locations/{service_location_id}"
        f"/coupons/search?fullDocument=true&unwrap=true"
    )
    body = {"start": start, "rows": rows}
    expression = f"""
    (async () => {{
      const response = await fetch({json.dumps(url)}, {{
        method: "POST",
        credentials: "include",
        headers: {{"Content-Type": "application/json"}},
        body: {json.dumps(json.dumps(body))}
      }});
      const text = await response.text();
      let payload = null;
      try {{ payload = JSON.parse(text); }} catch (e) {{}}
      return {{
        status: response.status,
        ok: response.ok,
        payload,
        text: payload ? null : text.slice(0, 600),
      }};
    }})()
    """
    return evaluate_in_giant_page(port, expression)


def normalize_coupon(raw):
    return {
        "id": raw.get("id"),
        "deal_tracking_id": raw.get("dealTrackingId"),
        "coupon_g_code": raw.get("couponGCode"),
        "source_system": raw.get("sourceSystem"),
        "source_system_id": raw.get("sourceSystemId"),
        "name": raw.get("name"),
        "title": (raw.get("title") or "").strip(),
        "description": raw.get("description"),
        "start_date": raw.get("startDate"),
        "end_date": raw.get("endDate"),
        "max_discount": raw.get("maxDiscount"),
        "promotion_type": raw.get("promotionType"),
        "coupon_type": raw.get("couponType"),
        "coupon_reward_target": raw.get("couponRewardTarget"),
        "promo_class_id": raw.get("promoClassId"),
        "multi_qty": raw.get("multiQty"),
        "manufacturer_coupon": raw.get("manufacturerCoupon"),
        "targeted": raw.get("targeted"),
        "personalized_offer": raw.get("personalizedOffer"),
        "clipping_required": raw.get("clippingRequired"),
        "category_tree_id": raw.get("categoryTreeId"),
        "category_tree_name": raw.get("categoryTreeName"),
        "top_category_tree_id": raw.get("topCategoryTreeId"),
        "top_category_tree_name": raw.get("topCategoryTreeName"),
        "category_tree_ids": raw.get("categoryTreeIds") or [],
        "product_ids": raw.get("productIds") or [],
        "brand_ids": raw.get("brandIds") or [],
        "pod_group_ids": raw.get("podGroupIds") or [],
        "consumer_category_id": raw.get("consumerCategoryId") or [],
        "coupon_channels": raw.get("couponChannels") or raw.get("channel") or [],
        "image_url": raw.get("imageUrl"),
        "external_image": raw.get("externalImage"),
        "legal_text": raw.get("legalText"),
        "badge_ids": raw.get("badgeIds") or [],
        "account_state": {
            "clipped": raw.get("clipped"),
            "loaded": raw.get("loaded"),
            "loadable": raw.get("loadable"),
        },
    }


def fetch_full_catalog(port, user_id, service_location_id, page_size, max_pages, timeout, sleep):
    coupons = []
    facets = None
    paging_total = None
    for page_index in range(max_pages):
        start = page_index * page_size
        try:
            result = fetch_coupon_page(port, user_id, service_location_id, start, page_size, timeout)
        except GiantBrowserError as exc:
            raise
        if not result.get("ok"):
            raise GiantBrowserError(
                f"Coupon search failed at start={start}: status={result.get('status')} "
                f"text={result.get('text') or '(no body)'}"
            )
        payload = result.get("payload") or {}
        page_coupons = payload.get("coupons") or []
        if facets is None:
            facets = payload.get("facets")
        if paging_total is None:
            paging_total = (payload.get("paging") or {}).get("total")
        coupons.extend(normalize_coupon(c) for c in page_coupons)
        if len(page_coupons) < page_size:
            break
        if paging_total is not None and len(coupons) >= paging_total:
            break
        if sleep:
            time.sleep(sleep)
    return coupons, facets, paging_total


def coupon_active(coupon, today=None):
    today = today or date.today().isoformat()
    end = (coupon.get("end_date") or "")[:10]
    start = (coupon.get("start_date") or "")[:10]
    if end and end < today:
        return False
    if start and start > today:
        return False
    return True


def coupon_text_blob(coupon):
    parts = [
        coupon.get("name") or "",
        coupon.get("description") or "",
        coupon.get("title") or "",
        coupon.get("category_tree_name") or "",
        coupon.get("top_category_tree_name") or "",
    ]
    return " ".join(part for part in parts if part).lower()


def keyword_matches(coupon, query):
    if not query:
        return True
    blob = coupon_text_blob(coupon)
    return all(token in blob for token in query.lower().split())


def category_matches(coupon, category):
    if not category:
        return True
    target = category.lower()
    return (
        target in (coupon.get("category_tree_name") or "").lower()
        or target in (coupon.get("top_category_tree_name") or "").lower()
    )


def command_fetch(args):
    wait_for_devtools(args.port)
    coupons, facets, paging_total = fetch_full_catalog(
        args.port,
        args.user_id,
        args.service_location_id,
        args.page_size,
        args.max_pages,
        args.timeout,
        args.sleep,
    )

    payload = {
        "metadata": {
            "fetched_on": date.today().isoformat(),
            "source_type": SOURCE_TYPE,
            "store_context": PARK_ROAD_CONTEXT,
            "service_location_id": str(args.service_location_id),
            "user_id": str(args.user_id),
            "endpoint": API_BASE + "/coupons/users/{user}/prism/service-locations/{loc}/coupons/search",
            "page_size": args.page_size,
            "fetched_count": len(coupons),
            "paging_total": paging_total,
            "notes": [
                "Catalog mirror of Giant's digital coupon search API.",
                "Account-state fields (clipped, loaded, loadable) reflect this user's account.",
                "End-dated coupons are kept for audit; meal-planning matching should call coupon_active().",
            ],
        },
        "facets": facets,
        "coupons": coupons,
    }

    print(f"Fetched {len(coupons)} coupons (catalog total={paging_total or '?'}) into memory")
    if args.write:
        write_json(COUPONS_FILE, payload)
        print(f"Wrote {COUPONS_FILE.name}")
    else:
        print("Mode: dry-run; pass --write to save")
    return 0


def command_search(args):
    if not COUPONS_FILE.exists():
        print(f"{COUPONS_FILE.name} not found; run fetch --write first.", file=sys.stderr)
        return 2
    data = load_json(COUPONS_FILE)
    today = date.today().isoformat()
    results = []
    for coupon in data.get("coupons", []):
        if args.active_only and not coupon_active(coupon, today):
            continue
        if not keyword_matches(coupon, args.query):
            continue
        if not category_matches(coupon, args.category):
            continue
        results.append(coupon)

    results.sort(key=lambda c: ((c.get("end_date") or "9999")[:10], c.get("name") or ""))

    if args.json:
        print(json.dumps(results[: args.limit], indent=2))
        return 0

    print(f"Matched {len(results)} coupon(s) (showing top {min(args.limit, len(results))})")
    print(f"{'End':<10} {'Discount':>9} {'Type':<8} {'Category':<22} Name / Description")
    print("-" * 130)
    for coupon in results[: args.limit]:
        end = (coupon.get("end_date") or "")[:10]
        max_d = coupon.get("max_discount")
        discount = f"${max_d:.2f}" if isinstance(max_d, (int, float)) else (coupon.get("title") or "")[:9]
        coupon_type = coupon.get("coupon_type") or ""
        category = (coupon.get("category_tree_name") or coupon.get("top_category_tree_name") or "")[:22]
        name = coupon.get("name") or ""
        description = coupon.get("description") or ""
        label = f"{name} — {description}" if description else name
        print(f"{end:<10} {discount:>9} {coupon_type:<8} {category:<22} {label[:80]}")
    return 0


def command_match(args):
    if not COUPONS_FILE.exists():
        print(f"{COUPONS_FILE.name} not found; run fetch --write first.", file=sys.stderr)
        return 2
    coupon_data = load_json(COUPONS_FILE)
    coupons = [c for c in coupon_data.get("coupons", []) if coupon_active(c)]

    meal_data = load_json(MEAL_PRICES_FILE)
    meal_items = meal_data.get("items", {})

    keys = list(meal_items.keys())
    if args.only:
        only = {k.lower() for k in args.only}
        keys = [k for k in keys if k.lower() in only]

    print(f"Matching {len(coupons)} active coupons against {len(keys)} meal items")
    print(f"{'Meal item':<30} {'End':<10} {'Discount':>9} {'Category':<24} Coupon")
    print("-" * 130)

    for meal_key in keys:
        item = meal_items[meal_key]
        meal_blob = " ".join(filter(None, [
            meal_key,
            item.get("category") or "",
            " ".join(item.get("meal_tags") or []),
            item.get("unit") or "",
        ])).lower()
        meal_tokens = {
            tok for tok in re.findall(r"[a-z]+", meal_blob)
            if len(tok) > 2
        }
        if not meal_tokens:
            continue

        scored = []
        giant_source = item.get("price_sources", {}).get("Giant", {}) or {}
        giant_pid = str(giant_source.get("product_id") or "")
        giant_brand = (giant_source.get("brand") or "").lower()

        for coupon in coupons:
            if coupon.get("category_tree_name", "").lower() in {"alcohol", "tobacco"} and "alcohol" not in meal_blob:
                continue

            score = 0
            blob = coupon_text_blob(coupon)
            blob_tokens = set(re.findall(r"[a-z]+", blob))
            overlap = meal_tokens & blob_tokens
            if not overlap:
                continue
            score += len(overlap) / max(1, len(meal_tokens))
            if giant_pid and giant_pid in {str(p) for p in coupon.get("product_ids") or []}:
                score += 1.0
            if giant_brand and giant_brand in blob:
                score += 0.10
            if score >= args.min_score:
                scored.append((score, coupon))

        scored.sort(key=lambda pair: -pair[0])
        for score, coupon in scored[: args.keep]:
            end = (coupon.get("end_date") or "")[:10]
            max_d = coupon.get("max_discount")
            discount = f"${max_d:.2f}" if isinstance(max_d, (int, float)) else "—"
            category = (coupon.get("category_tree_name") or "")[:24]
            name = coupon.get("name") or ""
            print(f"{meal_key[:30]:<30} {end:<10} {discount:>9} {category:<24} {name[:60]}")

        if not scored:
            if args.show_unmatched:
                print(f"{meal_key[:30]:<30} (no matches >= {args.min_score})")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Fetch the full Giant coupon catalog and save to giant_coupons.json.")
    fetch.add_argument("--port", type=int, default=DEFAULT_PORT)
    fetch.add_argument("--service-location-id", default=DEFAULT_SERVICE_LOCATION_ID)
    fetch.add_argument("--user-id", default=DEFAULT_USER_ID)
    fetch.add_argument("--page-size", type=int, default=200)
    fetch.add_argument("--max-pages", type=int, default=40)
    fetch.add_argument("--sleep", type=float, default=0.20)
    fetch.add_argument("--timeout", type=float, default=30.0)
    fetch.add_argument("--write", action="store_true")

    search = sub.add_parser("search", help="Search the saved coupon catalog by keyword/category.")
    search.add_argument("--query", help="Keyword text; matches name/description/category")
    search.add_argument("--category", help="Substring of category name (e.g. Dairy, Produce)")
    search.add_argument("--active-only", action="store_true", default=True)
    search.add_argument("--include-expired", dest="active_only", action="store_false")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")

    match = sub.add_parser("match", help="Match active coupons against meal_prices items.")
    match.add_argument("--only", action="append", help="Match only this meal_key (repeatable)")
    match.add_argument("--min-score", type=float, default=0.40)
    match.add_argument("--keep", type=int, default=3)
    match.add_argument("--show-unmatched", action="store_true")

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
        raise AssertionError(args.command)
    except GiantBrowserError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        print(json.dumps({"ok": False, "error": f"HTTP {exc.code}"}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
