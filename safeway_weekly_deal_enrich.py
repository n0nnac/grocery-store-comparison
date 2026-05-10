#!/usr/bin/env python3
"""Enrich weekly ad deals with API-backed base-price distance.

The weekly ad is still the source of sale prices. This script queries
Safeway's browser-facing product search API to find a likely matching product
and estimate the regular/base price for the same unit.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
from datetime import date
from pathlib import Path

from meal_price_tool import choose_deals_file, load_json, money, promotion_from_condition
from safeway_api_search import (
    DEFAULT_BANNER,
    DEFAULT_CHANNEL,
    DEFAULT_STORE_ID,
    SEARCH_ENDPOINT,
    fetch_search,
    normalize_doc,
)
from safeway_meal_inspiration import classify_role


ROOT = Path(__file__).parent
OBSERVATION_PREFIX = "safeway_weekly_deal_base_observations"

STOPWORDS = {
    "and",
    "any",
    "assorted",
    "best",
    "buy",
    "cold",
    "count",
    "ea",
    "each",
    "extra",
    "final",
    "fresh",
    "from",
    "grade",
    "large",
    "limit",
    "more",
    "pack",
    "package",
    "participating",
    "price",
    "raw",
    "sale",
    "savings",
    "select",
    "signature",
    "the",
    "waterfront",
    "when",
    "with",
}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def token_set(text):
    normalized = str(text or "").lower().replace("golden", "gold")
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) > 2 and token not in STOPWORDS and not token.isdigit()
    }


def clean_query(text):
    text = re.sub(r"\$[0-9]+(?:\.[0-9]+)?", " ", str(text or ""))
    text = re.sub(r"\b[0-9]+(?:\.[0-9]+)?\s*(?:lb|lbs|oz|ct|fl|ea)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:for|final price after|digital coupon savings|when you buy).*$", " ", text, flags=re.I)
    text = re.sub(r"[/(),;]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def candidate_queries(name, deal):
    queries = []
    for raw in [name, deal.get("source_label"), deal.get("condition")]:
        cleaned = clean_query(raw)
        if cleaned and cleaned.lower() not in {query.lower() for query in queries}:
            queries.append(cleaned)
    return queries or [name]


def sale_unit_kind(deal):
    unit = str(deal.get("unit") or "").lower()
    label = str(deal.get("source_label") or deal.get("condition") or "").lower()
    if "lb" in unit or re.search(r"\$[0-9]+(?:\.[0-9]+)?\s*lb\b", label):
        return "lb"
    if "dozen" in unit:
        return "package"
    if any(term in unit for term in ["package", "bag", "each", "can", "46 fl oz", "20 ct"]):
        return "package"
    return "unknown"


def to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def doc_price_for_unit(doc, kind):
    if kind == "lb":
        current = doc.get("price_per")
        base = doc.get("base_price_per")
        unit_quantity = str(doc.get("unit_quantity") or "").strip().upper()
        unit_measure = str(doc.get("unit_of_measure") or "").strip().upper()
        if current is not None and unit_quantity in {"LB", "LBS", "POUND", "POUNDS"}:
            return current, base, "lb"
        if current is not None and unit_measure in {"LB", "LBS", "POUND", "POUNDS"}:
            return current, base, "lb"

        item_size_qty = to_float(doc.get("item_size_qty"))
        package_current = doc.get("price")
        package_base = doc.get("base_price")
        if item_size_qty and unit_measure in {"OZ", "OUNCE", "OUNCES"}:
            pounds = item_size_qty / 16
            if pounds > 0:
                current = None if package_current is None else round(package_current / pounds, 2)
                base = None if package_base is None else round(package_base / pounds, 2)
                return current, base, "lb"

        return None, None, None

    if kind == "package":
        return doc.get("price"), doc.get("base_price"), "package"

    return None, None, None


def sale_price_match_score(sale_price, current_price):
    if sale_price is None or current_price is None:
        return 0, False
    distance = abs(float(sale_price) - float(current_price))
    if distance <= 0.03:
        return 35, True
    relative = distance / max(float(sale_price), 0.01)
    if relative <= 0.15:
        return 18, False
    if relative <= 0.35:
        return 6, False
    return -10, False


def candidate_score(name, deal, doc, query):
    deal_tokens = token_set(" ".join([name, deal.get("source_label") or "", query]))
    doc_tokens = token_set(doc.get("name"))
    overlap = deal_tokens & doc_tokens
    denominator = max(1, len(deal_tokens))
    overlap_score = 40 * len(overlap) / denominator

    kind = sale_unit_kind(deal)
    current_price, base_price, comparison_unit = doc_price_for_unit(doc, kind)
    unit_score = 18 if comparison_unit else -12
    price_score, exact_price_match = sale_price_match_score(deal.get("sale_price"), current_price)

    role = classify_role(name, deal)
    deal_blob = f"{name} {deal.get('source_label') or ''}".lower()
    doc_name = str(doc.get("name") or "").lower()
    department = str(doc.get("department_name") or "").lower()
    aisle = str(doc.get("aisle_name") or "").lower()
    department_score = 0
    department_mismatch = False
    if role == "protein" and (
        "meat" in department
        or "seafood" in department
        or ("egg" in deal_blob and ("egg" in department or "egg" in aisle or "dairy" in department))
        or any(term in aisle for term in ["beef", "pork", "chicken", "seafood"])
    ):
        department_score = 10
    elif role == "produce" and ("produce" in department or "fruits" in department or "vegetables" in department):
        department_score = 10
    elif role == "pantry" and ("pantry" in department or "pasta" in aisle or "canned" in aisle):
        department_score = 8
    elif role in {"protein", "produce"}:
        department_score = -30
        department_mismatch = True

    inventory_score = 4 if str(doc.get("inventory_available") or "") == "1" else 0
    base_score = 8 if base_price is not None else 0
    mismatch_score = 0
    if "tomatoes" in deal_blob and "paste" in doc_name and "tomatoes" not in doc_name:
        mismatch_score -= 24

    score = overlap_score + unit_score + price_score + department_score + inventory_score + base_score + mismatch_score
    return {
        "score": round(score, 1),
        "overlap_tokens": sorted(overlap),
        "comparison_unit": comparison_unit,
        "api_current_price": current_price,
        "api_base_price": base_price,
        "exact_price_match": exact_price_match,
        "department_mismatch": department_mismatch,
        "role": role,
    }


def confidence_label(score, comparison_unit, base_price, exact_price_match, department_mismatch):
    if not comparison_unit or base_price is None or department_mismatch:
        return "low"
    if exact_price_match and score >= 65:
        return "high"
    if score >= 58:
        return "medium"
    return "low"


def normalized_doc(doc):
    return {
        "pid": doc.get("pid"),
        "name": doc.get("name"),
        "upc": doc.get("upc"),
        "store_id": doc.get("store_id"),
        "price": doc.get("price"),
        "base_price": doc.get("base_price"),
        "price_per": doc.get("price_per"),
        "base_price_per": doc.get("base_price_per"),
        "unit_quantity": doc.get("unit_quantity"),
        "unit_of_measure": doc.get("unit_of_measure"),
        "item_size_qty": doc.get("item_size_qty"),
        "item_package_qty": doc.get("item_package_qty"),
        "sell_by_weight": doc.get("sell_by_weight"),
        "display_unit_quantity_text": doc.get("display_unit_quantity_text"),
        "promo_end_date": doc.get("promo_end_date"),
        "inventory_available": doc.get("inventory_available"),
        "department_name": doc.get("department_name"),
        "aisle_name": doc.get("aisle_name"),
    }


def fetch_candidates(name, deal, args):
    all_candidates = []
    failures = []
    seen = set()
    for query in candidate_queries(name, deal):
        try:
            payload = fetch_search(
                query,
                args.store_id,
                args.rows,
                0,
                args.banner,
                args.channel,
                args.timeout,
            )
        except urllib.error.HTTPError as exc:
            failures.append({"query": query, "error": f"HTTP {exc.code}"})
            continue
        except urllib.error.URLError as exc:
            failures.append({"query": query, "error": f"request failed: {exc}"})
            continue

        docs = [normalize_doc(doc) for doc in payload.get("response", {}).get("docs", [])]
        for doc in docs:
            pid = str(doc.get("pid") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            scored = candidate_score(name, deal, doc, query)
            all_candidates.append(
                {
                    "query": query,
                    "score": scored["score"],
                    "overlap_tokens": scored["overlap_tokens"],
                    "comparison_unit": scored["comparison_unit"],
                    "api_current_price": scored["api_current_price"],
                    "api_base_price": scored["api_base_price"],
                    "exact_price_match": scored["exact_price_match"],
                    "department_mismatch": scored["department_mismatch"],
                    "product": normalized_doc(doc),
                }
            )
        time.sleep(args.sleep)

    all_candidates.sort(key=lambda row: (-row["score"], row["product"].get("name") or ""))
    return all_candidates, failures


def enrich_deal(name, deal, args):
    candidates, failures = fetch_candidates(name, deal, args)
    best = candidates[0] if candidates else None
    confidence = "none"
    discount_amount = None
    discount_pct = None
    comparison_status = "no_candidate"

    if best:
        confidence = confidence_label(
            best["score"],
            best.get("comparison_unit"),
            best.get("api_base_price"),
            best.get("exact_price_match"),
            best.get("department_mismatch"),
        )
        if best.get("comparison_unit") and best.get("api_base_price") is not None:
            comparison_status = "comparable"
            discount_amount = round(best["api_base_price"] - deal.get("sale_price"), 2)
            discount_pct = (
                round(discount_amount / best["api_base_price"], 4)
                if best["api_base_price"]
                else None
            )
        else:
            comparison_status = "not_comparable"

    return {
        "deal_name": name,
        "role": classify_role(name, deal),
        "source_label": deal.get("source_label") or deal.get("condition"),
        "sale_price": deal.get("sale_price"),
        "sale_unit": deal.get("unit"),
        "sale_unit_kind": sale_unit_kind(deal),
        "requires_clip": bool(deal.get("requires_clip")),
        "freezer_friendly": bool(deal.get("freezer_friendly")),
        "best_match": best,
        "confidence": confidence,
        "comparison_status": comparison_status,
        "discount_amount": discount_amount,
        "discount_pct": discount_pct,
        "top_candidates": candidates[: args.keep_candidates],
        "failures": failures,
    }


_PROMO_STOPWORDS = {
    "buy", "more", "participating", "items", "when", "save", "free", "pick",
    "any", "off", "ea", "ct", "lb", "oz", "pkg", "pack", "bag", "box", "can",
    "with", "your", "next", "shopping", "order", "purchase", "and", "the",
    "from", "for", "ge", "select",  # 'select' alone (without 'signature') over-matches
}


def _label_product_part(label):
    """Return the product-name portion of an ad_items.label, dropping the
    promo boilerplate. Labels typically look like:

        "Coca-Cola, BUY 5 OR MORE, $4.99 ea WHEN YOU BUY 5 OR MORE PARTICIPATING ITEMS"

    so we take everything up to the first 'BUY', '$', or 'WHEN'.
    """
    text = str(label or "")
    cuts = []
    for marker in (r"\bBUY\b", r"\$", r"\bWHEN\b", r"\bSAVE\b"):
        m = re.search(marker, text, re.I)
        if m:
            cuts.append(m.start())
    if cuts:
        text = text[: min(cuts)]
    return text.strip(" ,")


def _meaningful_tokens(text):
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if len(tok) > 2 and tok not in _PROMO_STOPWORDS
    }


def backfill_promotions(args):
    """Parse promotion language out of normalized_deals.source_label and
    flyer ad_items.label, then write structured `promotion` fields back
    into the deals JSON in place.

    The promo metadata is consumed at runtime by meal_price_tool's cart
    and estimate-plan flows for multi-buy threshold enforcement, but
    consumers reading the JSON directly (LLMs, prompt context emission,
    manual inspection) only see the structured field if it's persisted.
    """
    deals_path = choose_deals_file(args.deals_file)
    raw = load_json(deals_path)
    deals = raw.get("normalized_deals") or {}
    ad_items = raw.get("ad_items") or []

    # Build a tokenized index of ad_item labels (product-name portion only)
    # so we can backfill deals whose source_label lacks the trigger phrase.
    flyer_promos = []
    for ad in ad_items:
        label = ad.get("label") or ""
        promo = promotion_from_condition(label)
        if not promo:
            continue
        product_part = _label_product_part(label)
        tokens = _meaningful_tokens(product_part)
        if not tokens:
            continue
        flyer_promos.append({
            "tokens": tokens,
            "promo": promo,
            "label": label,
            "product_part": product_part,
        })

    updated = []
    for name, deal in deals.items():
        if deal.get("promotion"):
            continue
        # First try the deal's own source_label / condition.
        promo = promotion_from_condition(deal.get("source_label") or deal.get("condition"))
        match_via = "source_label"
        match_label = deal.get("source_label") or deal.get("condition") or ""

        # Otherwise, try matching against ad_items by product-name token overlap.
        if not promo and flyer_promos:
            deal_tokens = _meaningful_tokens(name) | _meaningful_tokens(deal.get("source_label"))
            best_overlap = 0
            best_entry = None
            for entry in flyer_promos:
                overlap = len(deal_tokens & entry["tokens"])
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_entry = entry
            # Require >=2 product-name tokens overlap AND that overlap covers
            # at least half the deal's own tokens — guards against a single
            # generic word matching a totally different product.
            if (
                best_entry
                and best_overlap >= 2
                and best_overlap >= max(2, len(deal_tokens) // 2)
            ):
                promo = best_entry["promo"]
                match_via = "ad_items"
                match_label = best_entry["product_part"]

        if promo:
            deal["promotion"] = promo
            updated.append((name, promo["group_id"], match_via, match_label))

    print(f"Backfilled promo metadata onto {len(updated)} deal(s)")
    if updated:
        print(f"{'Deal':<32} {'Group':<32} Match via   Source label")
        print("-" * 130)
        for name, group_id, via, label in updated:
            print(f"{name[:32]:<32} {group_id:<32} {via:<11} {label[:55]}")

    if args.write:
        # Preserve original key order
        deals_path.write_text(json.dumps(raw, indent=2) + "\n")
        print(f"\nWrote {deals_path}")
    else:
        print("\nMode: dry-run; pass --write to save")
    return 0


def _normalize_weight(value):
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def verify_pack_mechanic(deal_name, deal, args):
    """Query Safeway's product search API for the deal item, identify SKUs
    whose per-unit price matches the deal's sale price, and infer the deal
    mechanic from those SKUs' pack sizes.

    Returns a dict like::

        {"deal_mechanic": "per_pack_only" | "per_unit_at_any_weight" | "uncertain",
         "pack_quantity": 4.0,        # only when per_pack_only
         "pack_unit": "lb",
         "verified_on": "2026-05-10",
         "evidence": [{"pid": ..., "name": ..., "price": 7.96,
                        "price_per": 1.99, "average_weight": 4.0, ...}, ...]}

    or ``None`` if the deal has no sale price.

    The mechanic is "per_pack_only" when ALL deal-priced SKUs share the
    same pack size (with weight > 1 unit). It's "per_unit_at_any_weight"
    when the deal price applies across multiple pack sizes. Otherwise
    "uncertain" — the caller should not assume.
    """
    sale_price = deal.get("sale_price")
    if sale_price is None:
        return None
    sale_unit = (deal.get("unit") or "").lower()

    query = deal_name
    try:
        payload = fetch_search(
            query, args.store_id, max(args.rows or 12, 12), 0,
            args.banner, args.channel, args.timeout,
        )
    except (urllib.error.HTTPError, urllib.error.URLError):
        return {"deal_mechanic": "uncertain", "reason": "api_error"}

    docs = payload.get("response", {}).get("docs", []) or []
    matches = []
    for doc in docs:
        price_per = doc.get("pricePer")
        if price_per is None:
            continue
        try:
            price_per_val = float(price_per)
        except (TypeError, ValueError):
            continue
        if abs(price_per_val - float(sale_price)) > 0.05:
            continue
        avg = _normalize_weight(doc.get("averageWeight"))
        min_w = _normalize_weight(doc.get("minWeight"))
        max_w = _normalize_weight(doc.get("maxWeight"))
        unit = (doc.get("unitQuantity") or doc.get("unitOfMeasure") or sale_unit).lower()
        matches.append({
            "pid": str(doc.get("id") or doc.get("upc") or ""),
            "name": doc.get("name"),
            "price": doc.get("price"),
            "price_per": price_per_val,
            "unit": unit,
            "average_weight": avg,
            "min_weight": min_w,
            "max_weight": max_w,
            "sell_by_weight": doc.get("sellByWeight"),
            "inventory_available": doc.get("inventoryAvailable"),
        })

    if not matches:
        return {
            "deal_mechanic": "uncertain",
            "reason": "no_matching_skus",
            "verified_on": date.today().isoformat(),
            "evidence": [],
        }

    weights = [m["average_weight"] for m in matches if m["average_weight"]]
    if weights:
        same_pack = max(weights) - min(weights) < 0.1
        substantial = min(weights) >= 1.0
        if same_pack and substantial:
            return {
                "deal_mechanic": "per_pack_only",
                "pack_quantity": round(min(weights), 2),
                "pack_unit": matches[0].get("unit") or sale_unit,
                "verified_on": date.today().isoformat(),
                "evidence": matches[:3],
            }
        if not same_pack:
            return {
                "deal_mechanic": "per_unit_at_any_weight",
                "verified_on": date.today().isoformat(),
                "evidence": matches[:3],
            }

    # No weight info on any match (e.g. count-based items like eggs).
    # Treat as uncertain unless all matches are clearly per-each.
    return {
        "deal_mechanic": "uncertain",
        "reason": "no_pack_size_info",
        "verified_on": date.today().isoformat(),
        "evidence": matches[:3],
    }


def verify_packs_command(args):
    """Walk every priced deal in the deals JSON, query the API, and persist
    a structured ``deal_mechanic`` (and ``pack_quantity`` when known) onto
    each deal record. Mutates the deals JSON in place when --write is set.
    """
    deals_path = choose_deals_file(args.deals_file)
    raw = load_json(deals_path)
    deals = raw.get("normalized_deals") or {}
    if not deals:
        print("No normalized_deals to verify.")
        return 0

    selected = list(deals.items())
    if args.role:
        selected = [(name, deal) for name, deal in selected if classify_role(name, deal) in args.role]
    if args.limit:
        selected = selected[: args.limit]

    print(f"Verifying pack mechanic for {len(selected)} deal(s) via Safeway API...\n")
    print(f"{'Deal':<32} {'Sale':>7} {'Mechanic':<24} {'Pack':>10} Top match")
    print("-" * 130)

    verified = 0
    for name, deal in selected:
        if not args.force and deal.get("deal_mechanic") and deal.get("verified_on"):
            top_evidence = (deal.get("verified_evidence") or [{}])[0].get("name") or ""
            print(f"{name[:32]:<32} {money(deal.get('sale_price')):>7} {(deal.get('deal_mechanic') or '?'):<24} "
                  f"{deal.get('pack_quantity') or '':>10} {top_evidence[:55]}  [cached]")
            continue

        result = verify_pack_mechanic(name, deal, args)
        if result is None:
            print(f"{name[:32]:<32} {money(deal.get('sale_price')):>7} {'(no sale price)':<24}")
            continue

        deal["deal_mechanic"] = result["deal_mechanic"]
        if "pack_quantity" in result:
            deal["pack_quantity"] = result["pack_quantity"]
            deal["pack_unit"] = result.get("pack_unit")
        deal["verified_on"] = result.get("verified_on")
        deal["verified_evidence"] = result.get("evidence", [])

        verified += 1
        top = (result.get("evidence") or [{}])[0]
        top_name = (top.get("name") or "")[:55]
        pack_str = ""
        if result.get("pack_quantity") is not None:
            pack_str = f"{result['pack_quantity']} {result.get('pack_unit') or ''}"
        print(f"{name[:32]:<32} {money(deal.get('sale_price')):>7} {(result['deal_mechanic'] or '?'):<24} "
              f"{pack_str:>10} {top_name}")

        if args.sleep:
            time.sleep(args.sleep)

    print(f"\nVerified {verified} deal(s)")
    if args.write:
        deals_path.write_text(json.dumps(raw, indent=2) + "\n")
        print(f"Wrote {deals_path}")
    else:
        print("Mode: dry-run; pass --write to save")
    return 0


def output_path_for(metadata, explicit=None):
    if explicit:
        return Path(explicit)
    valid_from = metadata.get("valid_from") or date.today().isoformat()
    return ROOT / f"{OBSERVATION_PREFIX}_{valid_from}.json"


def enrich(args):
    deals_path = choose_deals_file(args.deals_file)
    raw = load_json(deals_path)
    metadata = raw.get("metadata", {})
    deals = raw.get("normalized_deals") or raw.get("deals") or {}

    selected = list(deals.items())
    if args.role:
        selected = [(name, deal) for name, deal in selected if classify_role(name, deal) in args.role]
    if args.limit:
        selected = selected[: args.limit]

    observations = {}
    for name, deal in selected:
        observations[name] = enrich_deal(name, deal, args)

    payload = {
        "metadata": {
            "generated_on": date.today().isoformat(),
            "source_deals_file": str(deals_path),
            "store_id": str(args.store_id),
            "banner": args.banner,
            "channel": args.channel,
            "endpoint": SEARCH_ENDPOINT,
            "weekly_ad_valid_from": metadata.get("valid_from"),
            "weekly_ad_valid_to": metadata.get("valid_to"),
            "method": "search_substitute_api weekly deal base-price distance",
        },
        "observations": observations,
    }

    print(f"\nSafeway weekly deal API enrichment from {deals_path.name}")
    print(f"{'Deal':<40} {'Sale':>8} {'API Base':>9} {'Off':>8} {'Pct':>7} {'Conf':<7} Product")
    print("-" * 122)
    for name, row in observations.items():
        best = row.get("best_match") or {}
        product = (best.get("product") or {}).get("name") or ""
        api_base = best.get("api_base_price")
        discount_pct = row.get("discount_pct")
        pct = "" if discount_pct is None else f"{discount_pct * 100:.0f}%"
        print(
            f"{name[:40]:<40} {money(row.get('sale_price')):>8} {money(api_base):>9} "
            f"{money(row.get('discount_amount')):>8} {pct:>7} {row.get('confidence'):<7} "
            f"{product[:45]}"
        )

    if args.write:
        output = output_path_for(metadata, args.output)
        write_json(output, payload)
        print(f"\nWrote {output}")
    else:
        print("\nMode: dry-run; no files written")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Use the Safeway product API to estimate base-price distance for weekly ad deals."
    )
    parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--banner", default=DEFAULT_BANNER, help="Banner, usually safeway")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="pickup, delivery, or instore")
    parser.add_argument("--rows", type=int, default=8, help="Search rows per query")
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout")
    parser.add_argument("--sleep", type=float, default=0.15, help="Sleep between API queries")
    parser.add_argument("--limit", type=int, help="Limit number of weekly deals")
    parser.add_argument("--role", action="append", choices=["protein", "produce", "pantry", "beverage", "other"], help="Restrict to one or more roles")
    parser.add_argument("--keep-candidates", type=int, default=5, help="Candidate products to keep per deal")
    parser.add_argument("--write", action="store_true", help="Write observation JSON")
    parser.add_argument("--output", help="Output path when using --write")
    parser.add_argument("--backfill-promos", action="store_true", help="Skip API enrichment and instead parse promotion language out of deal source_label and ad_items.label, writing structured `promotion` fields back into the deals JSON in place. Use with --write to persist.")
    parser.add_argument("--verify-packs", action="store_true", help="Skip API enrichment and instead query Safeway product search for each deal, infer deal_mechanic (per_pack_only vs per_unit_at_any_weight) and pack_quantity from the matching SKUs' average weights, writing the result back into the deals JSON in place. Use with --write to persist.")
    parser.add_argument("--force", action="store_true", help="With --verify-packs: re-verify deals that already have a verified_on timestamp")
    args = parser.parse_args()
    if args.backfill_promos:
        return backfill_promotions(args)
    if args.verify_packs:
        return verify_packs_command(args)
    return enrich(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
