#!/usr/bin/env python3
"""Enrich saved Safeway coupon offers with detail, UPC, and product data.

This script starts from safeway_coupons.json instead of re-reading the whole
gallery. It is meant for targeted passes over high-value coupon slices.
"""

import argparse
import json
import sys
import time
import urllib.error
from datetime import datetime, timezone

from safeway_coupon_search import (
    COUPON_DETAIL_ENDPOINT,
    COUPONS_FILE,
    DEFAULT_BANNER,
    DEFAULT_STORE_ID,
    add_detail,
    add_resolved_products,
    default_account_state,
    fetch_offer_detail,
    load_json,
    write_json,
)


SEARCH_FIELDS = (
    "brand",
    "name",
    "description",
    "value_text",
    "category",
    "ecom_description",
    "for_u_description",
    "detail_name",
    "detail_description",
)


def ensure_account_state(offer):
    state = offer.setdefault("account_state", {})
    default = default_account_state("unauthenticated_gallery")
    for key, value in default.items():
        state.setdefault(key, value)
    return offer


def text_matches(offer, query):
    if not query:
        return True
    lowered = query.lower()
    return any(lowered in str(offer.get(field) or "").lower() for field in SEARCH_FIELDS)


def offer_matches(offer, args):
    if args.offer_id and str(offer.get("offer_id")) not in args.offer_id:
        return False
    if args.category and args.category.lower() not in str(offer.get("category") or "").lower():
        return False
    if args.application_kind:
        kind = (offer.get("application") or {}).get("kind")
        if kind != args.application_kind:
            return False
    if args.missing_details and offer.get("detail_endpoint"):
        return False
    return text_matches(offer, args.query)


def selected_offers(offers, args):
    matching = [offer for offer in offers if offer_matches(offer, args)]
    if args.all:
        return matching, matching
    return matching, matching[: args.limit]


def detail_needed(offer, force):
    if force:
        return True
    return not offer.get("detail_endpoint")


def resolve_needed(offer, force):
    if force:
        return bool(offer.get("upc_list"))
    return bool(offer.get("upc_list")) and not offer.get("resolved_products")


def enrich_offer(offer, args):
    result = {"offer_id": offer.get("offer_id"), "detail": "skipped", "products": "skipped"}
    if detail_needed(offer, args.force):
        try:
            detail = fetch_offer_detail(
                offer["offer_id"],
                offer.get("offer_program") or "",
                args.store_id,
                args.banner,
                args.timeout,
            )
            add_detail(offer, detail)
            offer.pop("detail_error", None)
            result["detail"] = f"ok ({len(offer.get('upc_list') or [])} upcs)"
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            offer["detail_error"] = str(exc)
            result["detail"] = f"error: {exc}"
    else:
        result["detail"] = f"cached ({len(offer.get('upc_list') or [])} upcs)"

    if args.resolve_upcs:
        if resolve_needed(offer, args.force):
            max_upcs = None if args.max_upcs <= 0 else args.max_upcs
            add_resolved_products(offer, args.store_id, args.banner, args.timeout, max_upcs)
            result["products"] = f"ok ({len(offer.get('resolved_products') or [])} products)"
        else:
            result["products"] = f"cached ({len(offer.get('resolved_products') or [])} products)"

    ensure_account_state(offer)
    return result


def update_metadata(data, args, matching_count, selected_count, wrote):
    metadata = data.setdefault("metadata", {})
    metadata.setdefault("store_id", str(args.store_id))
    metadata.setdefault("source_type", "coupon_gallery_api")
    metadata.setdefault("detail_endpoint", COUPON_DETAIL_ENDPOINT)
    metadata["last_enrichment_attempt_on"] = datetime.now(timezone.utc).date().isoformat()
    if wrote:
        metadata["last_enriched_on"] = metadata["last_enrichment_attempt_on"]
    metadata["last_enrichment_filter"] = {
        "query": args.query,
        "category": args.category,
        "application_kind": args.application_kind,
        "offer_id": args.offer_id,
        "missing_details": args.missing_details,
        "limit": None if args.all else args.limit,
    }
    metadata["last_enrichment_matching_offers"] = matching_count
    metadata["last_enrichment_selected_offers"] = selected_count
    metadata["details_fetched"] = any(offer.get("detail_endpoint") for offer in data.get("offers", []))
    metadata["upcs_resolved"] = any(offer.get("resolved_products") for offer in data.get("offers", []))
    metadata["account_state_model"] = {
        "source_type": "unauthenticated_gallery | logged_in_gallery | manual_account_coupon",
        "clipped": "true | false | null",
        "clip_status_confirmed_on": "YYYY-MM-DD or null",
        "household_specific": "true | false | null",
    }


def print_results(results, offers):
    print(f"{'Offer ID':<12} {'Pgm':<4} {'Category':<24} {'Value':<14} {'Detail':<18} Products")
    print("-" * 104)
    by_id = {result["offer_id"]: result for result in results}
    for offer in offers:
        result = by_id.get(offer.get("offer_id"), {})
        print(
            f"{str(offer.get('offer_id') or ''):<12} {str(offer.get('offer_program') or ''):<4} "
            f"{str(offer.get('category') or '')[:24]:<24} {str(offer.get('value_text') or '')[:14]:<14} "
            f"{str(result.get('detail') or '')[:18]:<18} {result.get('products') or ''}"
        )
        for product in (offer.get("resolved_products") or [])[:5]:
            price = product.get("price")
            base = product.get("base_price")
            price_text = "" if price is None else f"${price:.2f}"
            base_text = "" if base is None else f"${base:.2f}"
            print(
                f"{'':<12} {'':<4} {'':<24} {'':<14} {'':<18} "
                f"product {product.get('pid')}: {price_text} current, {base_text} base - {product.get('name')}"
            )


def main():
    parser = argparse.ArgumentParser(description="Enrich saved Safeway coupon offers.")
    parser.add_argument("query", nargs="?", help="Text to search in saved offers")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--banner", default=DEFAULT_BANNER, help="Banner, usually safeway")
    parser.add_argument("--category", help="Category substring, e.g. 'Meat & Seafood'")
    parser.add_argument("--application-kind", help="Application kind, e.g. department_threshold")
    parser.add_argument("--offer-id", action="append", help="Specific offer ID to enrich; can be repeated")
    parser.add_argument("--missing-details", action="store_true", help="Only select offers without saved detail data")
    parser.add_argument("--limit", type=int, default=25, help="Maximum offers to enrich unless --all is set")
    parser.add_argument("--all", action="store_true", help="Enrich all matching offers")
    parser.add_argument("--resolve-upcs", action="store_true", help="Resolve eligible UPCs through the product price API")
    parser.add_argument("--max-upcs", type=int, default=25, help="Maximum UPCs to resolve per offer; use 0 for all")
    parser.add_argument("--force", action="store_true", help="Refetch details and product resolutions even when cached")
    parser.add_argument("--sleep", type=float, default=0.05, help="Delay between offer detail requests")
    parser.add_argument("--write", action="store_true", help=f"Write enriched results back to {COUPONS_FILE.name}")
    parser.add_argument("--json", action="store_true", help="Print selected enriched offers as JSON")
    parser.add_argument("--timeout", type=float, default=20.0, help="Request timeout in seconds")
    args = parser.parse_args()

    data = load_json(COUPONS_FILE)
    offers = data.get("offers", [])
    for offer in offers:
        ensure_account_state(offer)

    matching, selected = selected_offers(offers, args)
    results = []
    for index, offer in enumerate(selected):
        results.append(enrich_offer(offer, args))
        if args.sleep and index < len(selected) - 1:
            time.sleep(args.sleep)

    update_metadata(data, args, len(matching), len(selected), args.write)

    if args.write:
        write_json(COUPONS_FILE, data)
        print(f"Wrote {COUPONS_FILE.name}: enriched {len(selected)} of {len(matching)} matching offers")
    else:
        print(f"Dry run: enriched {len(selected)} of {len(matching)} matching offers in memory")
        print("Add --write to persist these details.")

    if args.json:
        print(json.dumps({"metadata": data.get("metadata", {}), "offers": selected}, indent=2))
    else:
        print_results(results, selected)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
