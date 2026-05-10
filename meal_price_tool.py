#!/usr/bin/env python3
"""Meal-planning price helper.

Uses normalized base prices plus weekly deal overrides to estimate meal costs
and identify stock-up buys.
"""

import argparse
import json
import math
import re
import urllib.error
from datetime import date, datetime
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
WEEKLY_DEALS_FILE = ROOT / "weekly_deals.json"
WEEKLY_DEAL_GLOB = "weekly_deals*.json"
COUPONS_FILE = ROOT / "safeway_coupons.json"
COUPON_OVERRIDES_FILE = ROOT / "safeway_coupon_overrides.json"
REWARDS_FILE = ROOT / "safeway_rewards.json"
OBSERVATIONS_FILE = ROOT / "safeway_price_observations.json"
WEEKLY_DEAL_BASE_OBSERVATION_PREFIX = "safeway_weekly_deal_base_observations"

WEEKLY_DEAL_BASE_ALIASES = {
    "egglands best eggs": "eggs",
    "fresh atlantic salmon fillet": "salmon portion",
    "jumbo raw shrimp": "raw shrimp 26-30 ct",
    "mission flour tortillas": "flour tortillas",
    "signature select pasta": "pasta",
    "tuttorosso tomatoes": "crushed tomatoes",
}


DEPARTMENT_SCOPES_BY_CATEGORY = {
    "protein": ["Meat & Seafood"],
    "dairy": ["Dairy, Eggs & Cheese"],
    "produce": ["Fruits & Vegetables"],
    "pantry": ["Pantry"],
    "frozen": ["Frozen"],
    "beverages": ["Beverages"],
    "household": ["Household"],
}

COUPON_STATUS_LABELS = {
    "C": "clipped",
    "U": "unclipped",
}

COUPON_LIMIT_LABELS = {
    "O": "one time use",
    "U": "unlimited use",
}


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


def parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def observed_age_days(value):
    observed = parse_iso_date(value)
    if not observed:
        return None
    return (date.today() - observed).days


def parse_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def planning_pound_price_from_doc(doc, price_kind):
    if price_kind == "base":
        package_price = doc.get("base_price")
        unit_price = doc.get("base_price_per")
    else:
        package_price = doc.get("price")
        unit_price = doc.get("price_per")

    api_unit = (doc.get("unit_quantity") or doc.get("unit_of_measure") or "").strip().upper()
    unit_of_measure = (doc.get("unit_of_measure") or "").strip().upper()
    item_size_qty = parse_float(doc.get("item_size_qty"))

    if unit_price is not None and api_unit in {"LB", "LBS", "POUND", "POUNDS"}:
        return unit_price
    if package_price is not None and item_size_qty is not None and unit_of_measure in {"OZ", "OUNCE", "OUNCES"}:
        pounds = item_size_qty / 16
        if pounds > 0:
            return round(package_price / pounds, 2)
    if unit_price is not None and api_unit in {"OZ", "OUNCE", "OUNCES"}:
        return round(unit_price * 16, 2)
    return None


def planning_price_from_doc(doc, unit, price_kind):
    unit = str(unit or "").strip().lower()
    if unit in {"lb", "pound", "pounds"}:
        return planning_pound_price_from_doc(doc, price_kind)
    if price_kind == "base":
        return doc.get("base_price") if doc.get("base_price") is not None else doc.get("price")
    return doc.get("price")


def promotion_from_condition(condition):
    text = str(condition or "")
    match = re.search(r"when\s+you\s+buy\s+(\d+)\+\s+participating\s+items", text, re.I)
    if not match:
        match = re.search(r"buy\s+(\d+)\+\s+participating\s+items", text, re.I)
    if not match:
        return None

    threshold = int(match.group(1))
    return {
        "type": "mix_and_match_min_count",
        "group_id": f"buy_{threshold}_participating_items",
        "threshold_count": threshold,
        "count_unit": "item",
        "text": text,
    }


def add_deal_promotions(deals):
    for deal in deals.get("deals", {}).values():
        if deal.get("promotion"):
            continue
        promotion = promotion_from_condition(deal.get("condition"))
        if promotion:
            deal["promotion"] = promotion
    return deals


def normalize_deals(raw):
    if "deals" in raw:
        return add_deal_promotions(raw)
    normalized = {}
    for item_name, deal in (raw.get("normalized_deals") or {}).items():
        condition = deal.get("condition") or deal.get("source_label")
        normalized[item_name] = {
            "sale_price": deal.get("sale_price"),
            "unit": deal.get("unit"),
            "condition": condition,
            "requires_clip": bool(deal.get("requires_clip")),
            "coupon_type": "Safeway for U digital coupon" if deal.get("requires_clip") else None,
            "limit": deal.get("limit"),
            "confidence": deal.get("confidence") or "normalized_ad_item",
            "promotion": deal.get("promotion") or promotion_from_condition(condition),
        }
    return add_deal_promotions({
        "metadata": raw.get("metadata", {}),
        "deals": normalized,
    })


def deals_validity(deals):
    metadata = deals.get("metadata") or {}
    return parse_iso_date(metadata.get("valid_from")), parse_iso_date(metadata.get("valid_to"))


def choose_deals_file(explicit=None):
    if explicit:
        return Path(explicit)

    active_candidates = []
    for path in sorted(ROOT.glob(WEEKLY_DEAL_GLOB)):
        try:
            deals = normalize_deals(load_json(path))
        except (OSError, json.JSONDecodeError):
            continue
        start, end = deals_validity(deals)
        if start is not None and end is not None and start <= date.today() <= end:
            active_candidates.append((start, path))

    if active_candidates:
        active_candidates.sort(reverse=True)
        return active_candidates[0][1]
    return WEEKLY_DEALS_FILE


def load_weekly_deals(explicit=None):
    path = choose_deals_file(explicit)
    return path, normalize_deals(load_json(path))


def effective_price(item_name, prices, deals):
    item = prices["items"][item_name]
    base = item.get("base_prices", {}).get("Safeway")
    deal = deals.get("deals", {}).get(item_name)
    if deal:
        return deal["sale_price"], "sale", deal
    return base, "base", None


def load_coupon_overlays():
    offers = []
    for path in (COUPONS_FILE, COUPON_OVERRIDES_FILE):
        if path.exists():
            data = load_json(path)
            offers.extend(
                offer for offer in data.get("offers", [])
                if offer.get("active", True) is not False
            )
    return offers


def load_rewards_config():
    if REWARDS_FILE.exists():
        return load_json(REWARDS_FILE)
    return {
        "earning_rules": {"grocery": {"points_per_dollar": 1}},
        "valuation": {"default_point_value": 0.0},
        "redemption_options": [],
        "product_rewards": [],
    }


def point_value(option):
    cost = option.get("point_cost") or 0
    value = option.get("estimated_value") or 0
    if cost <= 0:
        return 0.0
    return value / cost


def default_point_value(config):
    value = (config.get("valuation") or {}).get("default_point_value")
    if value is not None:
        return float(value)
    options = config.get("redemption_options") or []
    non_gas = [option for option in options if option.get("type") != "gas"]
    if not non_gas:
        return 0.0
    return max(point_value(option) for option in non_gas)


def best_redemption_option(config, include_gas=False):
    options = config.get("redemption_options") or []
    if not include_gas:
        options = [option for option in options if option.get("type") != "gas"]
    if not options:
        return None
    return max(options, key=point_value)


def safeway_source(item):
    return item.get("price_sources", {}).get("Safeway", {})


def item_product_ids(item):
    source = safeway_source(item)
    return {
        str(value)
        for value in (
            source.get("product_id"),
            source.get("pid"),
        )
        if value
    }


def item_upcs(item):
    source = safeway_source(item)
    return {str(source["upc"])} if source.get("upc") else set()


def best_non_coupon_price(candidates):
    usable = [
        candidate for candidate in candidates
        if candidate.get("price") is not None and candidate.get("type") != "coupon"
    ]
    if not usable:
        return None
    priority = {"weekly_ad": 0, "current": 1, "base": 2}
    return min(usable, key=lambda c: (c["price"], priority.get(c["type"], 9)))["price"]


def coupon_end_date(coupon):
    return parse_iso_date(coupon.get("end_date") or coupon.get("offer_end_date"))


def coupon_is_active(coupon):
    end = coupon_end_date(coupon)
    return end is None or end >= date.today()


def coupon_match(item, coupon):
    product_ids = item_product_ids(item)
    upcs = item_upcs(item)

    for product in coupon.get("resolved_products") or []:
        if product.get("pid") and str(product["pid"]) in product_ids:
            return "product_id"
        if product.get("upc") and str(product["upc"]) in upcs:
            return "upc"

    coupon_upcs = {str(upc) for upc in coupon.get("upc_list") or []}
    if upcs and upcs & coupon_upcs:
        return "upc"

    return None


def coupon_state_label(coupon):
    clipped = (coupon.get("account_state") or {}).get("clipped")
    if clipped is True:
        return "clipped"
    if clipped is False:
        return "needs clip"
    return "state unknown"


def item_coupon_price_candidates(item_name, item, coupons, candidates):
    rows = []
    if not coupons:
        return rows

    base_price = best_non_coupon_price(candidates)
    for coupon in coupons:
        application = coupon.get("application") or {}
        allocation = application.get("allocation")
        discount = application.get("discount") or coupon.get("discount") or {}
        match_type = coupon_match(item, coupon)
        if not match_type:
            continue
        if not coupon_is_active(coupon):
            continue

        clipped = (coupon.get("account_state") or {}).get("clipped")
        can_apply = clipped is True
        price = None
        detail = None
        kind = discount.get("kind")

        if allocation == "line_item" and kind in {"fixed_price", "fixed_unit_price"}:
            price = discount.get("price")
            detail = discount.get("text") or "Item coupon price"
        elif kind == "amount_off" and base_price is not None:
            amount = discount.get("amount")
            if amount is not None:
                price = max(0.0, base_price - amount)
                detail = discount.get("text") or "Item coupon savings"

        if price is None:
            continue

        state = coupon_state_label(coupon)
        if can_apply:
            label = "coupon"
            blocked_reason = None
        elif clipped is False:
            label = "coupon-needs-clip"
            blocked_reason = "coupon is not clipped"
        else:
            label = "coupon-unknown"
            blocked_reason = "coupon account state is unknown"

        rows.append(
            {
                "price": price,
                "type": "coupon",
                "label": label,
                "requires_clip": True,
                "can_apply": can_apply,
                "blocked_reason": blocked_reason,
                "detail": f"{detail}; {state}; offer {coupon.get('offer_id')}; match {match_type}",
                "coupon": coupon,
                "match_type": match_type,
                "item": item_name,
            }
        )
    return rows


def item_department_scopes(item):
    scopes = []
    category = item.get("category")
    if category:
        scopes.append(category)
        scopes.extend(DEPARTMENT_SCOPES_BY_CATEGORY.get(category, []))
    return sorted({scope for scope in scopes if scope})


def item_rewards_eligible(item):
    return item.get("rewards_eligible", True) is not False


def price_candidates(item_name, item, deals, coupons=None):
    candidates = []
    base = item.get("base_prices", {}).get("Safeway")
    if base is not None:
        candidates.append(
            {
                "price": base,
                "type": "base",
                "label": "base",
                "requires_clip": False,
                "detail": "Safeway base price",
            }
        )

    source = safeway_source(item)
    current = source.get("planning_current_price")
    if current is None and item.get("unit") not in {"lb", "pound", "pounds"}:
        current = source.get("current_price")
    promo_end = parse_iso_date(source.get("promo_end_date"))
    if promo_end is not None and promo_end < date.today():
        current = None
    if current is not None and (base is None or current < base):
        detail = "Safeway current price"
        if promo_end:
            detail += f"; promo ends {promo_end.isoformat()}"
        candidates.append(
            {
                "price": current,
                "type": "current",
                "label": "current",
                "requires_clip": False,
                "detail": detail,
            }
        )

    deal = deals.get("deals", {}).get(item_name)
    if deal:
        label = "weekly+clip" if deal.get("requires_clip") else "weekly"
        candidates.append(
            {
                "price": deal["sale_price"],
                "type": "weekly_ad",
                "label": label,
                "requires_clip": bool(deal.get("requires_clip")),
                "detail": deal.get("condition") or "Weekly ad",
                "deal": deal,
                "promotion": deal.get("promotion"),
            }
        )

    candidates.extend(item_coupon_price_candidates(item_name, item, coupons or [], candidates))

    candidates = [candidate for candidate in candidates if candidate["price"] is not None]
    selectable = [
        candidate for candidate in candidates
        if candidate.get("type") != "coupon" or candidate.get("can_apply")
    ]
    if not selectable:
        return None, []

    # Prefer a weekly-ad source on ties because it carries clip/limit context.
    priority = {"coupon": 0, "weekly_ad": 1, "current": 2, "base": 3}
    selected = min(selectable, key=lambda c: (c["price"], priority.get(c["type"], 9)))
    return selected, candidates


def weekly_deal_base_observation_path(deals):
    metadata = deals.get("metadata") or {}
    valid_from = metadata.get("valid_from") or date.today().isoformat()
    return ROOT / f"{WEEKLY_DEAL_BASE_OBSERVATION_PREFIX}_{valid_from}.json"


def load_weekly_deal_base_observations(deals):
    path = weekly_deal_base_observation_path(deals)
    if not path.exists():
        return {}
    try:
        return load_json(path).get("observations") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def weekly_deal_api_base_candidate(item_name, observations):
    observation = observations.get(item_name)
    if not observation:
        return None
    if observation.get("confidence") not in {"high", "medium"}:
        return None
    if observation.get("comparison_status") != "comparable":
        return None
    best = observation.get("best_match") or {}
    base_price = best.get("api_base_price")
    if base_price is None:
        return None
    product = best.get("product") or {}
    return {
        "price": base_price,
        "label": "api-base",
        "detail": f"API base fallback: {product.get('name') or item_name}",
        "requires_clip": False,
    }


def weekly_deal_alias_base_candidate(item_name, prices):
    alias = WEEKLY_DEAL_BASE_ALIASES.get(item_name)
    if not alias:
        return None
    item = prices.get("items", {}).get(alias)
    if not item:
        return None
    base_price = (item.get("base_prices") or {}).get("Safeway")
    if base_price is None:
        return None
    return {
        "price": base_price,
        "label": "base",
        "detail": f"Safeway base fallback via saved item: {alias}",
        "requires_clip": False,
    }


def fallback_candidate_for_unmet_promotion(line, prices, observations):
    candidates = [
        candidate for candidate in line.get("price_candidates", [])
        if candidate.get("type") != "weekly_ad"
        and (candidate.get("type") != "coupon" or candidate.get("can_apply"))
        and candidate.get("price") is not None
    ]
    if candidates:
        priority = {"coupon": 0, "current": 1, "base": 2}
        selected = min(candidates, key=lambda c: (c["price"], priority.get(c["type"], 9)))
        return {
            "price": selected["price"],
            "label": selected.get("label") or selected.get("type"),
            "detail": selected.get("detail") or "Fallback price",
            "requires_clip": bool(selected.get("requires_clip")),
        }

    item_name = line.get("item")
    item = prices.get("items", {}).get(item_name)
    if item:
        base_price = (item.get("base_prices") or {}).get("Safeway")
        if base_price is not None:
            return {
                "price": base_price,
                "label": "base",
                "detail": "Safeway base fallback",
                "requires_clip": False,
            }

    api_candidate = weekly_deal_api_base_candidate(item_name, observations)
    if api_candidate:
        return api_candidate

    return weekly_deal_alias_base_candidate(item_name, prices)


def promotion_count_quantity(line):
    try:
        return float(line.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def apply_multi_buy_promotions(lines, prices, deals):
    observations = load_weekly_deal_base_observations(deals)
    groups = {}
    for line in lines:
        promotion = line.get("promotion")
        if not promotion or promotion.get("type") != "mix_and_match_min_count":
            continue
        group_id = promotion.get("group_id")
        groups.setdefault(group_id, {"promotion": promotion, "lines": [], "count": 0.0})
        groups[group_id]["lines"].append(line)
        groups[group_id]["count"] += promotion_count_quantity(line)

    for group in groups.values():
        promotion = group["promotion"]
        threshold = float(promotion.get("threshold_count") or 0)
        count = group["count"]
        eligible = count >= threshold
        for line in group["lines"]:
            status = {
                "group_id": promotion.get("group_id"),
                "threshold_count": threshold,
                "current_count": count,
                "eligible": eligible,
                "shortfall": max(0.0, threshold - count),
                "text": promotion.get("text"),
            }
            line["promotion_status"] = status
            if eligible:
                continue

            fallback = fallback_candidate_for_unmet_promotion(line, prices, observations)
            original = {
                "unit_price": line.get("unit_price"),
                "line_total": line.get("line_total"),
                "source": line.get("source"),
                "detail": line.get("detail"),
            }
            line["promotion_blocked"] = True
            line["promotion_blocked_price"] = original
            prefix = (
                f"multi-buy unmet {count:g}/{threshold:g}; "
                f"short {max(0.0, threshold - count):g}; "
            )
            if fallback:
                line["unit_price"] = fallback["price"]
                line["line_total"] = line.get("qty", 0) * fallback["price"]
                line["source"] = fallback["label"]
                line["detail"] = prefix + fallback["detail"]
                line["requires_clip"] = bool(fallback.get("requires_clip"))
                line["priced"] = fallback["price"] is not None
            else:
                line["unit_price"] = None
                line["line_total"] = None
                line["source"] = "promo-unmet"
                line["detail"] = prefix + "no fallback price available"
                line["requires_clip"] = False
                line["priced"] = False
    return lines


def build_cart_lines(recipe_keys, prices, deals, coupons=None):
    recipe_refs = []
    ingredients = {}
    servings = 0
    for key in recipe_keys:
        if key not in RECIPES:
            raise SystemExit(f"Unknown recipe: {key}. Available: {', '.join(RECIPES)}")
        recipe = RECIPES[key]
        recipe_refs.append(recipe)
        servings += recipe["servings"]
        for item_name, qty in recipe["ingredients"].items():
            ingredients[item_name] = ingredients.get(item_name, 0) + qty

    lines = []
    for item_name, qty in ingredients.items():
        item = prices["items"].get(item_name)
        if not item:
            lines.append(
                {
                    "item": item_name,
                    "qty": qty,
                    "unit": "",
                    "unit_price": None,
                    "line_total": None,
                    "source": "missing",
                    "detail": "No item in meal_prices.json",
                    "scopes": [],
                    "requires_clip": False,
                    "rewards_eligible": False,
                }
            )
            continue

        selected, candidates = price_candidates(item_name, item, deals, coupons)
        unit_price = selected["price"] if selected else None
        line_total = None if unit_price is None else qty * unit_price
        lines.append(
            {
                "item": item_name,
                "qty": qty,
                "unit": item.get("unit", ""),
                "unit_price": unit_price,
                "line_total": line_total,
                "source": selected["label"] if selected else "missing",
                "detail": selected["detail"] if selected else "No Safeway price",
                "scopes": item_department_scopes(item),
                "requires_clip": bool(selected and selected.get("requires_clip")),
                "rewards_eligible": item_rewards_eligible(item),
                "price_candidates": candidates,
                "promotion": selected.get("promotion") if selected else None,
            }
        )
    apply_multi_buy_promotions(lines, prices, deals)
    return recipe_refs, lines, servings


def scope_matches(candidate_scope, line_scopes):
    if not candidate_scope:
        return True
    candidate = str(candidate_scope).lower()
    return any(candidate == str(scope).lower() for scope in line_scopes)


def eligible_subtotal(lines, scope):
    total = 0.0
    eligible_lines = []
    for line in lines:
        if line["line_total"] is None:
            continue
        if scope_matches(scope, line["scopes"]):
            total += line["line_total"]
            eligible_lines.append(line["item"])
    return total, eligible_lines


def coupon_clip_status(coupon):
    state = coupon.get("account_state") or {}
    clipped = state.get("clipped")
    if clipped is True:
        return "clip confirmed"
    if clipped is False:
        return "clip needed"
    if coupon.get("is_clippable"):
        return "clip unknown"
    return "no clip needed"


def coupon_status_label(value):
    if not value:
        return None
    return COUPON_STATUS_LABELS.get(value, value)


def coupon_limit_label(value):
    if not value:
        return None
    return COUPON_LIMIT_LABELS.get(value, value)


def cart_level_coupon_rows(lines, coupons, show_ineligible=False):
    rows = []
    for coupon in coupons:
        if not coupon_is_active(coupon):
            continue
        application = coupon.get("application", {})
        if application.get("allocation") != "cart_level":
            continue
        discount = application.get("discount") or coupon.get("discount") or {}
        if discount.get("kind") != "amount_off":
            continue

        scope = application.get("scope") or coupon.get("category")
        subtotal, eligible_lines = eligible_subtotal(lines, scope)
        if not eligible_lines and not show_ineligible:
            continue

        threshold = application.get("threshold_amount") or 0
        amount = discount.get("amount") or 0
        clipped = (coupon.get("account_state") or {}).get("clipped")
        applied = subtotal >= threshold and amount > 0 and clipped is not False
        if not applied and not show_ineligible and subtotal == 0:
            continue

        rows.append(
            {
                "offer_id": coupon.get("offer_id"),
                "name": coupon.get("name") or coupon.get("brand"),
                "scope": scope,
                "threshold": threshold,
                "eligible_subtotal": subtotal,
                "amount": min(amount, subtotal) if applied else 0,
                "potential_amount": amount,
                "applied": applied,
                "status": coupon.get("status"),
                "limits": coupon.get("limits"),
                "end_date": coupon.get("end_date"),
                "eligible_lines": eligible_lines,
                "source_type": coupon.get("source_type"),
                "clip_status": coupon_clip_status(coupon),
            }
        )
    rows.sort(key=lambda row: (not row["applied"], row["scope"] or "", row["offer_id"] or ""))
    return rows


def coupon_discount(coupon):
    application = coupon.get("application") or {}
    return application.get("discount") or coupon.get("discount") or {}


def rewards_base_points(lines, cart_level_coupon_total, config):
    rate = (
        (config.get("earning_rules") or {})
        .get("grocery", {})
        .get("points_per_dollar", 1)
    )
    eligible_subtotal = sum(
        line["line_total"] or 0
        for line in lines
        if line.get("rewards_eligible")
    )
    eligible_spend = max(0.0, eligible_subtotal - min(cart_level_coupon_total, eligible_subtotal))
    return {
        "eligible_subtotal": eligible_subtotal,
        "eligible_spend": eligible_spend,
        "rate": rate,
        "points": math.floor(eligible_spend) * rate,
    }


def point_offer_exact_lines(lines, coupon, prices):
    matched = []
    for line in lines:
        item = prices.get("items", {}).get(line["item"])
        if item and coupon_match(item, coupon):
            matched.append(line)
    return matched


def point_offer_scope_lines(lines, scope):
    return [
        line for line in lines
        if line.get("rewards_eligible") and scope_matches(scope, line.get("scopes", []))
    ]


def point_offer_rows(lines, coupons, prices, show_ineligible=False):
    rows = []
    for coupon in coupons:
        if not coupon_is_active(coupon):
            continue
        discount = coupon_discount(coupon)
        if discount.get("kind") != "points_multiplier":
            continue

        application = coupon.get("application") or {}
        multiplier = discount.get("multiplier")
        if not multiplier:
            continue

        exact_lines = point_offer_exact_lines(lines, coupon, prices)
        match_type = "product"
        eligible_lines = exact_lines
        if not eligible_lines and application.get("allocation") == "cart_level":
            eligible_lines = point_offer_scope_lines(lines, application.get("scope"))
            match_type = "scope"
        elif not eligible_lines:
            match_type = "terms"

        eligible_subtotal = sum(line["line_total"] or 0 for line in eligible_lines)
        threshold = application.get("threshold_amount") or 0
        clipped = (coupon.get("account_state") or {}).get("clipped")
        clip_ok = clipped is True or coupon.get("is_clippable") is False
        terms_ok = bool(eligible_lines) and (
            application.get("allocation") in {"cart_level", "line_item"}
            or bool(exact_lines)
        )
        applied = eligible_subtotal >= threshold and clip_ok and terms_ok
        whole_dollars = math.floor(eligible_subtotal)
        total_offer_points = whole_dollars * multiplier if applied else 0
        base_points = whole_dollars if applied else 0
        bonus_points = max(0, total_offer_points - base_points)

        if not applied and not show_ineligible and not eligible_lines:
            continue

        if not terms_ok:
            status = "needs exact product/terms"
        elif not clip_ok:
            status = coupon_clip_status(coupon)
        elif eligible_subtotal < threshold:
            status = "below threshold"
        else:
            status = "applied"

        rows.append(
            {
                "offer_id": coupon.get("offer_id"),
                "name": coupon.get("name") or coupon.get("brand"),
                "scope": application.get("scope") or coupon.get("category"),
                "threshold": threshold,
                "eligible_subtotal": eligible_subtotal,
                "multiplier": multiplier,
                "bonus_points": bonus_points,
                "total_offer_points": total_offer_points,
                "applied": applied,
                "status": status,
                "clip_status": coupon_clip_status(coupon),
                "match_type": match_type,
                "eligible_lines": [line["item"] for line in eligible_lines],
                "end_date": coupon.get("end_date") or coupon.get("offer_end_date"),
                "value_text": coupon.get("value_text"),
            }
        )
    rows.sort(key=lambda row: (not row["applied"], row["scope"] or "", row["offer_id"] or ""))
    return rows


def rewards_summary(lines, cart_level_coupon_total, point_rows, config):
    base = rewards_base_points(lines, cart_level_coupon_total, config)
    bonus_points = sum(row["bonus_points"] for row in point_rows if row["applied"])
    total_points = base["points"] + bonus_points
    value_per_point = default_point_value(config)
    best_non_gas = best_redemption_option(config, include_gas=False)
    best_with_gas = best_redemption_option(config, include_gas=True)
    return {
        "eligible_subtotal": base["eligible_subtotal"],
        "eligible_spend": base["eligible_spend"],
        "base_points": base["points"],
        "bonus_points": bonus_points,
        "total_points": total_points,
        "estimated_future_value": total_points * value_per_point,
        "value_per_point": value_per_point,
        "best_non_gas": best_non_gas,
        "best_with_gas": best_with_gas,
    }


def load_plan_json(path):
    text = Path(path).read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

    start = text.find("{")
    if start >= 0:
        decoder = json.JSONDecoder()
        try:
            data, _end = decoder.raw_decode(text[start:])
            return data
        except json.JSONDecodeError:
            pass
    raise SystemExit(f"Could not find valid JSON in {path}")


def external_plan_recipes(plan):
    recipes = plan.get("recipes")
    if not isinstance(recipes, list):
        raise SystemExit("Plan JSON must contain a recipes list")
    return recipes


def normalize_resolution_query(value):
    value = str(value or "")
    for prefix in ("resolve:", "unresolved:", "safeway:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    value = value.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", value).strip()


def token_set(value):
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 2 and not token.isdigit()
    }


def score_resolution_doc(query, doc):
    query_tokens = token_set(query)
    doc_tokens = token_set(doc.get("name"))
    overlap = query_tokens & doc_tokens
    overlap_score = 60 * len(overlap) / max(1, len(query_tokens))
    inventory_score = 8 if str(doc.get("inventory_available") or "") == "1" else 0
    price_score = 6 if doc.get("price") is not None else 0
    exact_score = 12 if str(query or "").lower() in str(doc.get("name") or "").lower() else 0
    starts_score = 20 if str(doc.get("name") or "").lower().startswith(str(query or "").lower()) else 0
    return round(overlap_score + inventory_score + price_score + exact_score + starts_score, 1)


def confidence_from_resolution_score(score):
    if score >= 68:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def resolve_safeway_doc(query, args, product_id=None):
    if not query and not product_id:
        return None
    search = str(product_id or query)
    try:
        payload = fetch_search(
            search,
            args.store_id,
            args.resolution_rows,
            0,
            args.banner,
            args.channel,
            args.timeout,
        )
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "query": search}
    except urllib.error.URLError as exc:
        return {"error": f"request failed: {exc}", "query": search}

    docs = [normalize_doc(doc) for doc in payload.get("response", {}).get("docs", [])]
    if product_id:
        for doc in docs:
            if str(doc.get("pid")) == str(product_id):
                return {
                    "query": search,
                    "confidence": "high",
                    "score": 100.0,
                    "doc": doc,
                    "match_type": "product_id",
                }

    if not docs:
        return {"query": search, "confidence": "none", "score": 0.0, "doc": None}

    scored = [
        {
            "query": search,
            "confidence": confidence_from_resolution_score(score_resolution_doc(query, doc)),
            "score": score_resolution_doc(query, doc),
            "doc": doc,
            "match_type": "search",
        }
        for doc in docs
    ]
    scored.sort(key=lambda row: (-row["score"], row["doc"].get("price") is None, row["doc"].get("price") or 999999, row["doc"].get("name") or ""))
    return scored[0]


def resolution_price_from_doc(doc, unit):
    current = planning_price_from_doc(doc, unit, "current")
    base = planning_price_from_doc(doc, unit, "base")
    if current is not None:
        return current, "api-current"
    if base is not None:
        return base, "api-base"
    return None, "api-missing"


def make_product_url(product_id, store_id):
    return f"https://www.safeway.com/shop/product-details.{product_id}.html?loc={store_id}"


def api_resolution_line(ingredient, key, source, qty, args, item=None):
    if not args or args.no_resolve_missing:
        return None

    product_id = None
    query = ingredient.get("name") or normalize_resolution_query(key)
    unit = ingredient.get("unit") or (item or {}).get("unit") or ""
    if item:
        safeway = safeway_source(item)
        product_id = safeway.get("product_id") or safeway.get("pid")
        age = observed_age_days(safeway.get("observed_on"))
        has_price = (item.get("base_prices") or {}).get("Safeway") is not None or safeway.get("planning_current_price") is not None
        if has_price and (age is None or age <= args.stale_days):
            return None
        query = safeway.get("product_name") or query

    result = resolve_safeway_doc(query, args, product_id=product_id)
    if not result or result.get("error"):
        return None
    doc = result.get("doc")
    if not doc or result.get("confidence") == "low":
        return None
    unit_price, label = resolution_price_from_doc(doc, unit)
    if unit_price is None:
        return None
    return {
        "item": key,
        "display_name": ingredient.get("name") or key,
        "qty": qty,
        "unit": unit or doc.get("display_unit_quantity_text") or "",
        "store": source,
        "unit_price": unit_price,
        "line_total": qty * unit_price,
        "source": label,
        "detail": (
            f"auto-resolved {result['confidence']} confidence via Safeway API: "
            f"{doc.get('name')} (pid {doc.get('pid')})"
        ),
        "requires_clip": False,
        "priced": True,
        "resolution": {
            "query": result.get("query"),
            "confidence": result.get("confidence"),
            "score": result.get("score"),
            "match_type": result.get("match_type"),
            "product_id": doc.get("pid"),
            "product_name": doc.get("name"),
            "regular_price": doc.get("base_price"),
            "current_price": doc.get("price"),
            "regular_unit_price": doc.get("base_price_per"),
            "current_unit_price": doc.get("price_per"),
            "api_unit_quantity": doc.get("unit_quantity"),
            "api_unit_of_measure": doc.get("unit_of_measure"),
            "api_item_size_qty": doc.get("item_size_qty"),
            "api_item_package_qty": doc.get("item_package_qty"),
            "api_sell_by_weight": doc.get("sell_by_weight"),
            "api_display_unit_quantity_text": doc.get("display_unit_quantity_text"),
            "promo_end_date": doc.get("promo_end_date"),
            "inventory_available": doc.get("inventory_available"),
            "upc": doc.get("upc"),
            "department_name": doc.get("department_name"),
            "aisle_name": doc.get("aisle_name"),
            "store_id": args.store_id,
            "observed_on": date.today().isoformat(),
            "source_type": "search_substitute_api",
            "endpoint": SEARCH_ENDPOINT,
        },
    }


def external_line_price(ingredient, prices, deals, coupons=None, args=None):
    key = ingredient.get("price_key") or ingredient.get("item") or ingredient.get("name")
    source = ingredient.get("source") or ingredient.get("store") or "Safeway"
    try:
        qty = float(ingredient.get("quantity", 0))
    except (TypeError, ValueError):
        qty = 0.0

    source_key = str(source).lower().replace("-", "_").replace(" ", "_")
    if source_key in {"pantry", "owned", "on_hand", "already_have"}:
        store = "owned" if source_key in {"owned", "on_hand", "already_have"} else "pantry"
        detail = (
            ingredient.get("notes")
            or ("Already owned / use-up ingredient" if store == "owned" else "Assumed pantry/on-hand item")
        )
        return {
            "item": key,
            "display_name": ingredient.get("name") or key,
            "qty": qty,
            "unit": ingredient.get("unit") or "",
            "store": store,
            "unit_price": 0.0,
            "line_total": 0.0,
            "source": "on hand",
            "detail": detail,
            "requires_clip": False,
            "priced": True,
        }

    item = prices.get("items", {}).get(key)
    if not item and source_key == "safeway":
        resolved_key = normalize_resolution_query(key).lower()
        if resolved_key in prices.get("items", {}):
            key = resolved_key
            item = prices["items"][key]
    deal = deals.get("deals", {}).get(key)

    if source_key == "safeway":
        if item:
            selected, candidates = price_candidates(key, item, deals, coupons or [])
            source_age = observed_age_days(safeway_source(item).get("observed_on"))
            stale_selected = (
                selected
                and selected.get("type") in {"base", "current"}
                and source_age is not None
                and source_age > (args.stale_days if args else 999999)
            )
            if selected and (selected.get("type") == "base" or stale_selected):
                resolved_line = api_resolution_line(ingredient, key, source, qty, args, item=item)
                if resolved_line:
                    resolved_line["price_candidates"] = candidates
                    return resolved_line
            unit_price = selected["price"] if selected else None
            if unit_price is None:
                resolved_line = api_resolution_line(ingredient, key, source, qty, args, item=item)
                if resolved_line:
                    resolved_line["price_candidates"] = candidates
                    return resolved_line
            return {
                "item": key,
                "display_name": ingredient.get("name") or key,
                "qty": qty,
                "unit": item.get("unit") or ingredient.get("unit") or "",
                "store": source,
                "unit_price": unit_price,
                "line_total": None if unit_price is None else qty * unit_price,
                "source": selected["label"] if selected else "missing",
                "detail": selected["detail"] if selected else "No Safeway price",
                "requires_clip": bool(selected and selected.get("requires_clip")),
                "priced": unit_price is not None,
                "price_candidates": candidates,
                "promotion": selected.get("promotion") if selected else None,
            }
        if deal:
            unit_price = deal.get("sale_price")
            return {
                "item": key,
                "display_name": ingredient.get("name") or key,
                "qty": qty,
                "unit": deal.get("unit") or ingredient.get("unit") or "",
                "store": source,
                "unit_price": unit_price,
                "line_total": None if unit_price is None else qty * unit_price,
                "source": "weekly",
                "detail": deal.get("condition") or deal.get("source_label") or "Weekly ad",
                "requires_clip": bool(deal.get("requires_clip")),
                "priced": unit_price is not None,
                "promotion": deal.get("promotion"),
            }

        resolved_line = api_resolution_line(ingredient, key, source, qty, args)
        if resolved_line:
            return resolved_line

    if item:
        unit_price = (item.get("base_prices") or {}).get(source)
        return {
            "item": key,
            "display_name": ingredient.get("name") or key,
            "qty": qty,
            "unit": item.get("unit") or ingredient.get("unit") or "",
            "store": source,
            "unit_price": unit_price,
            "line_total": None if unit_price is None else qty * unit_price,
            "source": "base",
            "detail": f"{source} saved base price" if unit_price is not None else f"No saved {source} price",
            "requires_clip": False,
            "priced": unit_price is not None,
        }

    return {
        "item": key,
        "display_name": ingredient.get("name") or key,
        "qty": qty,
        "unit": ingredient.get("unit") or "",
        "store": source,
        "unit_price": None,
        "line_total": None,
        "source": "missing",
        "detail": "No matching saved item or weekly deal",
        "requires_clip": False,
        "priced": False,
    }


def price_external_plan(plan, prices, deals, coupons=None, args=None):
    priced_recipes = []
    all_lines = []
    for recipe in external_plan_recipes(plan):
        lines = [
            external_line_price(ingredient, prices, deals, coupons, args=args)
            for ingredient in recipe.get("ingredients", [])
        ]
        all_lines.extend(lines)
        servings = recipe.get("servings") or 0
        priced_recipes.append(
            {
                "id": recipe.get("id"),
                "title": recipe.get("title"),
                "meal_role": recipe.get("meal_role"),
                "servings": servings,
                "why_this_week": recipe.get("why_this_week") or [],
                "unpriced_items": recipe.get("unpriced_items") or [],
                "clip_required": recipe.get("clip_required") or [],
                "method_summary": recipe.get("method_summary") or [],
                "lines": lines,
                "total": 0.0,
                "per_serving": None,
            }
        )
    apply_multi_buy_promotions(all_lines, prices, deals)
    for recipe in priced_recipes:
        total = sum(line["line_total"] or 0 for line in recipe["lines"])
        servings = recipe.get("servings") or 0
        recipe["total"] = total
        recipe["per_serving"] = (total / servings) if servings else None
    return {
        "version": plan.get("version"),
        "objective": plan.get("objective"),
        "recipes": priced_recipes,
        "shopping_notes": plan.get("shopping_notes") or [],
        "combined_total": sum(recipe["total"] for recipe in priced_recipes),
        "combined_servings": sum(recipe["servings"] or 0 for recipe in priced_recipes),
    }


def category_from_resolution(resolution):
    department = str(resolution.get("department_name") or "").lower()
    aisle = str(resolution.get("aisle_name") or "").lower()
    if "meat" in department or "seafood" in department or any(term in aisle for term in ["beef", "pork", "chicken", "seafood"]):
        return "protein"
    if "fruit" in department or "vegetable" in department or "produce" in department:
        return "produce"
    if "dairy" in department or "egg" in department or "cheese" in department:
        return "dairy"
    if "frozen" in department:
        return "frozen"
    if "beverage" in department:
        return "beverages"
    return "pantry"


def durable_key_for_resolved_line(line, prices):
    key = line.get("item")
    if key in prices.get("items", {}):
        return key
    cleaned = normalize_resolution_query(key)
    if cleaned:
        return cleaned.lower()
    return str(line.get("display_name") or key).strip().lower()


def item_from_resolution_line(line):
    resolution = line["resolution"]
    unit = line.get("unit") or resolution.get("api_display_unit_quantity_text") or ""
    base_price = planning_price_from_doc(
        {
            "base_price": resolution.get("regular_price"),
            "price": resolution.get("current_price"),
            "base_price_per": resolution.get("regular_unit_price"),
            "price_per": resolution.get("current_unit_price"),
            "unit_quantity": resolution.get("api_unit_quantity"),
            "unit_of_measure": resolution.get("api_unit_of_measure"),
            "item_size_qty": resolution.get("api_item_size_qty"),
        },
        unit,
        "base",
    )
    current_price = planning_price_from_doc(
        {
            "base_price": resolution.get("regular_price"),
            "price": resolution.get("current_price"),
            "base_price_per": resolution.get("regular_unit_price"),
            "price_per": resolution.get("current_unit_price"),
            "unit_quantity": resolution.get("api_unit_quantity"),
            "unit_of_measure": resolution.get("api_unit_of_measure"),
            "item_size_qty": resolution.get("api_item_size_qty"),
        },
        unit,
        "current",
    )
    if base_price is None:
        base_price = current_price

    return {
        "category": category_from_resolution(resolution),
        "unit": unit,
        "base_prices": {"Safeway": base_price} if base_price is not None else {},
        "price_sources": {
            "Safeway": {
                "source_type": "search_substitute_api",
                "observed_on": resolution.get("observed_on") or date.today().isoformat(),
                "store_id": str(resolution.get("store_id") or DEFAULT_STORE_ID),
                "product_id": str(resolution.get("product_id")),
                "product_name": resolution.get("product_name"),
                "endpoint": resolution.get("endpoint") or SEARCH_ENDPOINT,
                "url": make_product_url(resolution.get("product_id"), resolution.get("store_id") or DEFAULT_STORE_ID),
                "confidence": "api_price_doc",
                "match_confidence": resolution.get("confidence"),
                "regular_price": resolution.get("regular_price"),
                "current_price": resolution.get("current_price"),
                "regular_unit_price": resolution.get("regular_unit_price"),
                "current_unit_price": resolution.get("current_unit_price"),
                "api_unit_quantity": resolution.get("api_unit_quantity"),
                "api_unit_of_measure": resolution.get("api_unit_of_measure"),
                "api_item_size_qty": resolution.get("api_item_size_qty"),
                "api_item_package_qty": resolution.get("api_item_package_qty"),
                "planning_base_price": base_price,
                "planning_current_price": current_price,
                "promo_end_date": resolution.get("promo_end_date"),
                "inventory_available": resolution.get("inventory_available"),
                "upc": resolution.get("upc"),
                "department_name": resolution.get("department_name"),
                "aisle_name": resolution.get("aisle_name"),
            }
        },
        "meal_tags": [],
        "freezer_friendly": False,
    }


def observation_from_resolution_line(line):
    resolution = line["resolution"]
    item = item_from_resolution_line(line)
    source = item["price_sources"]["Safeway"]
    return {
        "product_name": resolution.get("product_name"),
        "url": source.get("url"),
        "source_type": "search_substitute_api",
        "endpoint": resolution.get("endpoint") or SEARCH_ENDPOINT,
        "current_price": resolution.get("current_price"),
        "regular_price": resolution.get("regular_price"),
        "current_unit_price": resolution.get("current_unit_price"),
        "regular_unit_price": resolution.get("regular_unit_price"),
        "unit": item.get("unit"),
        "api_unit_quantity": resolution.get("api_unit_quantity"),
        "api_unit_of_measure": resolution.get("api_unit_of_measure"),
        "api_item_size_qty": resolution.get("api_item_size_qty"),
        "api_item_package_qty": resolution.get("api_item_package_qty"),
        "api_sell_by_weight": resolution.get("api_sell_by_weight"),
        "api_display_unit_quantity_text": resolution.get("api_display_unit_quantity_text"),
        "planning_base_price": source.get("planning_base_price"),
        "planning_current_price": source.get("planning_current_price"),
        "promo_end_date": resolution.get("promo_end_date"),
        "inventory_available": resolution.get("inventory_available"),
        "upc": resolution.get("upc"),
        "department_name": resolution.get("department_name"),
        "aisle_name": resolution.get("aisle_name"),
        "store_id": str(resolution.get("store_id") or DEFAULT_STORE_ID),
        "observed_on": resolution.get("observed_on") or date.today().isoformat(),
        "confidence": "api_price_doc",
        "match_confidence": resolution.get("confidence"),
        "match_score": resolution.get("score"),
        "match_type": resolution.get("match_type"),
        "query": resolution.get("query"),
    }


def resolved_lines(priced):
    for recipe in priced.get("recipes", []):
        for line in recipe.get("lines", []):
            if line.get("resolution") and line.get("priced"):
                yield line


def write_resolved_prices(priced, prices):
    lines = list(resolved_lines(priced))
    if not lines:
        return []

    observations = load_json(OBSERVATIONS_FILE) if OBSERVATIONS_FILE.exists() else {"observations": {}, "not_yet_verified": []}
    observations.setdefault("observations", {})
    observations.setdefault("not_yet_verified", [])

    rows = []
    for line in lines:
        resolution = line["resolution"]
        if resolution.get("confidence") not in {"high", "medium"}:
            continue
        product_id = str(resolution.get("product_id") or "")
        if not product_id:
            continue

        durable_key = durable_key_for_resolved_line(line, prices)
        new_item = item_from_resolution_line(line)
        existing = prices.setdefault("items", {}).get(durable_key)
        action = "created"
        if existing:
            action = "updated"
            existing.setdefault("base_prices", {}).update(new_item.get("base_prices", {}))
            existing.setdefault("price_sources", {})["Safeway"] = new_item["price_sources"]["Safeway"]
            existing.setdefault("meal_tags", existing.get("meal_tags", []))
            existing.setdefault("freezer_friendly", existing.get("freezer_friendly", False))
            if not existing.get("category"):
                existing["category"] = new_item["category"]
            if not existing.get("unit"):
                existing["unit"] = new_item["unit"]
        else:
            prices["items"][durable_key] = new_item

        observations["observations"][product_id] = observation_from_resolution_line(line)
        rows.append(
            {
                "action": action,
                "item": durable_key,
                "product_id": product_id,
                "product_name": resolution.get("product_name"),
                "price": prices["items"][durable_key].get("base_prices", {}).get("Safeway"),
                "confidence": resolution.get("confidence"),
            }
        )

    if rows:
        write_json(MEAL_PRICES_FILE, prices)
        write_json(OBSERVATIONS_FILE, observations)
    return rows


def estimate_plan(args):
    prices = load_json(MEAL_PRICES_FILE)
    deals_path, deals = load_weekly_deals(args.deals_file)
    coupons = [] if args.no_coupons else load_coupon_overlays()
    plan = load_plan_json(args.plan_file)
    priced = price_external_plan(plan, prices, deals, coupons, args=args)
    write_rows = write_resolved_prices(priced, prices) if args.write_resolved else []

    if args.json:
        if write_rows:
            priced["written_resolved_prices"] = write_rows
        print(json.dumps(priced, indent=2, sort_keys=True))
        return

    print("\nExternal meal inspiration plan estimate")
    metadata = deals.get("metadata") or {}
    print(
        f"Deals file: {deals_path.name} "
        f"({metadata.get('valid_from') or '?'} to {metadata.get('valid_to') or '?'})"
    )
    if priced.get("objective"):
        print(f"Objective: {priced['objective']}")

    for recipe in priced["recipes"]:
        title = recipe.get("title") or recipe.get("id") or "Untitled recipe"
        servings = recipe.get("servings") or 0
        print(f"\n{title} ({servings:g} serving{'s' if servings != 1 else ''})")
        if recipe.get("why_this_week"):
            print("Why this week: " + "; ".join(recipe["why_this_week"]))
        print(f"{'Ingredient':<32} {'Store':<13} {'Qty':>7} {'Unit':<18} {'Price':>8} {'Line':>8} Detail")
        print("-" * 126)
        for line in recipe["lines"]:
            clip = "clip; " if line["requires_clip"] else ""
            print(
                f"{line['display_name'][:32]:<32} {line['store'][:13]:<13} "
                f"{line['qty']:>7g} {line['unit'][:18]:<18} "
                f"{money(line['unit_price']):>8} {money(line['line_total']):>8} "
                f"{clip}{line['source']}; {line['detail']}"
            )
        if recipe.get("unpriced_items"):
            print("Unpriced items: " + ", ".join(item.get("name", "") for item in recipe["unpriced_items"]))
        print(
            f"Recipe total: {money(recipe['total'])}"
            + (f"  |  Per serving: {money(recipe['per_serving'])}" if recipe.get("per_serving") is not None else "")
        )

    if priced.get("shopping_notes"):
        print("\nShopping notes")
        for note in priced["shopping_notes"]:
            print(f"- {note}")

    print(f"\nCombined total: {money(priced['combined_total'])}")
    if priced["combined_servings"]:
        print(f"Average per serving: {money(priced['combined_total'] / priced['combined_servings'])}")
    if write_rows:
        print("\nWrote resolved Safeway prices")
        for row in write_rows:
            print(
                f"- {row['action']} {row['item']}: {money(row['price'])} "
                f"from {row['product_name']} (pid {row['product_id']}; {row['confidence']})"
            )


def cart(args):
    prices = load_json(MEAL_PRICES_FILE)
    deals_path, deals = load_weekly_deals(args.deals_file)
    coupons = [] if args.no_coupons else load_coupon_overlays()
    rewards_config = None if args.no_rewards else load_rewards_config()

    keys = args.recipes or ["ground_beef_lunch_bowls"]
    recipe_refs, lines, servings = build_cart_lines(keys, prices, deals, coupons)
    subtotal = sum(line["line_total"] or 0 for line in lines)
    coupon_rows = cart_level_coupon_rows(lines, coupons, show_ineligible=args.show_ineligible_coupons)
    coupon_total = sum(row["amount"] for row in coupon_rows if row["applied"])
    final_total = subtotal - coupon_total
    reward_rows = []
    reward_summary_row = None
    if rewards_config:
        reward_rows = point_offer_rows(
            lines,
            coupons,
            prices,
            show_ineligible=args.show_ineligible_rewards,
        )
        reward_summary_row = rewards_summary(lines, coupon_total, reward_rows, rewards_config)

    print("\nCart estimate")
    metadata = deals.get("metadata") or {}
    if deals_path:
        print(
            f"Deals file: {deals_path.name} "
            f"({metadata.get('valid_from') or '?'} to {metadata.get('valid_to') or '?'})"
        )
    for recipe in recipe_refs:
        print(f"- {recipe['display']} ({recipe['servings']} serving{'s' if recipe['servings'] != 1 else ''})")

    print(f"\n{'Item':<34} {'Qty':>6} {'Unit':<14} {'Price':>8} {'Source':<12} {'Line':>8} Clip / Detail")
    print("-" * 126)
    for line in lines:
        clip = "clip" if line["requires_clip"] else ""
        detail = line["detail"]
        clip_detail = detail if not clip else f"{clip}; {detail}"
        print(
            f"{line['item']:<34} {line['qty']:>6g} {line['unit']:<14} "
            f"{money(line['unit_price']):>8} {line['source']:<12} {money(line['line_total']):>8} {clip_detail}"
        )
        if args.verbose:
            blocked = [
                candidate for candidate in line.get("price_candidates", [])
                if candidate.get("type") == "coupon" and not candidate.get("can_apply")
            ]
            for candidate in blocked:
                print(
                    f"{'':<34} {'':>6} {'':<14} "
                    f"{money(candidate['price']):>8} {candidate['label']:<12} {'':>8} "
                    f"not applied; {candidate['blocked_reason']}; {candidate['detail']}"
                )

    print(f"\nSubtotal before cart-level coupons: {money(subtotal)}")

    relevant_coupons = [
        row for row in coupon_rows
        if row["applied"] or row["eligible_subtotal"] > 0 or args.show_ineligible_coupons
    ]
    if relevant_coupons:
        print(f"\n{'Cart-level coupon':<42} {'Scope':<18} {'Eligible':>10} {'Min':>8} {'Save':>8} Status")
        print("-" * 112)
        for row in relevant_coupons:
            status = "applied" if row["applied"] else "not applied"
            raw_status = coupon_status_label(row["status"])
            raw_limit = coupon_limit_label(row["limits"])
            if raw_status and raw_status not in status:
                status += f"; {raw_status}"
            if raw_limit:
                status += f"; {raw_limit}"
            if row["end_date"]:
                status += f"; ends {row['end_date']}"
            status += f"; {row['clip_status']}"
            print(
                f"{(row['name'] or row['offer_id'] or '')[:42]:<42} {(row['scope'] or '')[:18]:<18} "
                f"{money(row['eligible_subtotal']):>10} {money(row['threshold']):>8} "
                f"{money(row['amount']):>8} {status}"
            )
            if args.verbose and row["eligible_lines"]:
                print(f"{'':<42} {'eligible items:':<18} {', '.join(row['eligible_lines'])}")
    else:
        print("\nCart-level coupons: none relevant to this cart")

    print(f"\nCart-level coupon savings: -{money(coupon_total)}")
    print(f"Estimated cart total: {money(final_total)}")
    if reward_summary_row:
        print("\nSafeway rewards points estimate")
        print(
            f"- Eligible grocery spend after cart-level coupons: "
            f"{money(reward_summary_row['eligible_spend'])}"
        )
        print(
            f"- Base points: {reward_summary_row['base_points']:.0f}; "
            f"bonus points: {reward_summary_row['bonus_points']:.0f}; "
            f"estimated total earned: {reward_summary_row['total_points']:.0f}"
        )
        print(
            f"- Future value at default redemption "
            f"({money(reward_summary_row['value_per_point'] * 1200)}/1200 pts): "
            f"{money(reward_summary_row['estimated_future_value'])}"
        )
        best = reward_summary_row.get("best_non_gas")
        if best:
            print(
                f"- Best known non-fuel option: {best['name']} "
                f"({money(point_value(best) * 100)} per 100 pts)"
            )
        best_gas = reward_summary_row.get("best_with_gas")
        if best_gas and best_gas is not best:
            print(
                f"- Best theoretical option including gas: {best_gas['name']} "
                f"({money(point_value(best_gas) * 100)} per 100 pts)"
            )

        relevant_rewards = [
            row for row in reward_rows
            if row["applied"] or row["eligible_subtotal"] > 0 or args.show_ineligible_rewards
        ]
        if relevant_rewards:
            print(f"\n{'Point offer':<42} {'Scope':<18} {'Eligible':>10} {'X':>4} {'Bonus':>7} Status")
            print("-" * 112)
            for row in relevant_rewards:
                status = row["status"]
                if row["end_date"]:
                    status += f"; ends {row['end_date']}"
                print(
                    f"{(row['name'] or row['offer_id'] or '')[:42]:<42} "
                    f"{(row['scope'] or '')[:18]:<18} {money(row['eligible_subtotal']):>10} "
                    f"{row['multiplier']:>4g} {row['bonus_points']:>7.0f} {status}"
                )
                if args.verbose and row["eligible_lines"]:
                    print(f"{'':<42} {'eligible items:':<18} {', '.join(row['eligible_lines'])}")
    if servings:
        print(f"Estimated per serving across selected recipes: {money(final_total / servings)}")


def list_deals(args):
    prices = load_json(MEAL_PRICES_FILE)
    deals_path, deals = load_weekly_deals(args.deals_file)

    rows = []
    for name, deal in deals["deals"].items():
        item = prices["items"].get(name, {})
        base = item.get("base_prices", {}).get("Safeway")
        sale = deal["sale_price"]
        savings = None if base is None else base - sale
        if args.only_savings and savings is not None and savings <= 0:
            continue
        freezer = "yes" if item.get("freezer_friendly") else "no"
        clip = "yes" if deal.get("requires_clip") else "no"
        limit = deal.get("limit") or ""
        rows.append((name, sale, deal["unit"], base, savings, freezer, clip, limit, deal.get("condition", "")))

    rows.sort(key=lambda row: (row[4] is None, -(row[4] or 0), row[0]))

    metadata = deals.get("metadata") or {}
    print(f"\nSafeway weekly deals: {metadata.get('address') or metadata.get('store') or 'Safeway'}")
    print(f"File {deals_path.name}; valid {metadata.get('valid_from')} to {metadata.get('valid_to')}\n")
    print(f"{'Item':<34} {'Sale':>8} {'Unit':<16} {'Base':>8} {'Save':>8} {'Freeze':<7} {'Clip':<5} Limit / Condition")
    print("-" * 140)
    for name, sale, unit, base, savings, freezer, clip, limit, condition in rows:
        base_text = "n/a" if base is None else money(base)
        save_text = "n/a" if savings is None else money(savings)
        detail = condition if not limit else f"{limit}; {condition}"
        print(f"{name:<34} {money(sale):>8} {unit:<16} {base_text:>8} {save_text:>8} {freezer:<7} {clip:<5} {detail}")


def coupon_matches(args):
    prices = load_json(MEAL_PRICES_FILE)
    _deals_path, deals = load_weekly_deals(args.deals_file)
    coupons = load_coupon_overlays()
    rows = []

    for item_name, item in prices.get("items", {}).items():
        selected, candidates = price_candidates(item_name, item, deals, coupons)
        for candidate in candidates:
            if candidate.get("type") != "coupon":
                continue
            if args.only_applicable and not candidate.get("can_apply"):
                continue
            coupon = candidate["coupon"]
            base = item.get("base_prices", {}).get("Safeway")
            current = safeway_source(item).get("planning_current_price") or safeway_source(item).get("current_price")
            weekly = (deals.get("deals", {}).get(item_name) or {}).get("sale_price")
            rows.append(
                {
                    "item": item_name,
                    "coupon_price": candidate["price"],
                    "base": base,
                    "current": current,
                    "weekly": weekly,
                    "selected": selected is candidate,
                    "can_apply": candidate.get("can_apply"),
                    "state": coupon_state_label(coupon),
                    "match": candidate.get("match_type"),
                    "offer_id": coupon.get("offer_id"),
                    "value": coupon.get("value_text"),
                    "brand": coupon.get("brand") or coupon.get("name"),
                    "detail": candidate.get("blocked_reason") or "applicable",
                }
            )

    rows.sort(key=lambda row: (not row["can_apply"], row["item"], row["coupon_price"]))

    print("\nSafeway item-level coupon matches")
    if not rows:
        print("No item-level coupon matches found for saved meal-planning ingredients.")
        return

    print(
        f"{'Item':<30} {'Coupon':>8} {'Base':>8} {'Current':>8} {'Weekly':>8} "
        f"{'State':<13} {'Match':<10} Offer / Brand"
    )
    print("-" * 132)
    for row in rows:
        marker = " *" if row["selected"] else ""
        weekly = "n/a" if row["weekly"] is None else money(row["weekly"])
        print(
            f"{(row['item'] + marker):<30} {money(row['coupon_price']):>8} "
            f"{money(row['base']):>8} {money(row['current']):>8} {weekly:>8} "
            f"{row['state']:<13} {row['match']:<10} {row['offer_id']} {row['value']} - {row['brand']}"
        )
        if args.verbose:
            print(f"{'':<30} {'':>8} {'':>8} {'':>8} {'':>8} {'':<13} {'':<10} {row['detail']}")


def list_rewards(args):
    config = load_rewards_config()
    options = config.get("redemption_options") or []
    dashboard_rewards = config.get("dashboard_rewards") or []
    product_rewards = config.get("product_rewards") or []
    account_state = config.get("account_state") or {}
    available_points = args.points
    if available_points is None:
        available_points = account_state.get("available_points")

    print("\nSafeway rewards redemption options")
    if available_points is not None:
        print(f"Available points: {available_points:g}")
        if account_state.get("freshpass_points_do_not_expire"):
            print("FreshPass: points do not expire")
        print(f"Auto cash off: {'on' if account_state.get('auto_cash_off_enabled') else 'off'}")
    print(f"{'Option':<34} {'Type':<18} {'Points':>8} {'Value':>8} {'$/100':>8} Clip / Notes")
    print("-" * 128)
    for option in sorted(options, key=lambda row: (-point_value(row), row.get("point_cost") or 0)):
        clip = "clip" if option.get("requires_clip") else ""
        notes = option.get("notes") or ""
        print(
            f"{option['name'][:34]:<34} {(option.get('type') or '')[:18]:<18} "
            f"{option.get('point_cost') or 0:>8g} {money(option.get('estimated_value')):>8} "
            f"{money(point_value(option) * 100):>8} {clip} {notes}"
        )

    print(f"\nDefault planning value: {money(default_point_value(config) * 100)} per 100 points")
    if dashboard_rewards:
        print("\nDashboard rewards")
        print(f"{'Reward':<54} {'Points':>8} {'Value':>8} {'$/100':>8} Type / Confidence")
        print("-" * 128)
        rows = dashboard_rewards
        if args.only_valued:
            rows = [reward for reward in rows if reward.get("estimated_value") is not None]
        if args.affordable:
            if available_points is None:
                raise SystemExit("--affordable requires --points or captured account_state.available_points")
            rows = [reward for reward in rows if (reward.get("point_cost") or 0) <= available_points]
        rows = sorted(
            rows,
            key=lambda reward: (
                reward.get("estimated_value") is None,
                -(
                    (reward.get("estimated_value") or 0)
                    / (reward.get("point_cost") or 1)
                ),
                reward.get("point_cost") or 0,
                reward.get("name") or "",
            ),
        )
        if args.limit:
            rows = rows[: args.limit]
        for reward in rows:
            value = reward.get("estimated_value")
            cost = reward.get("point_cost") or 0
            per_100 = (value / cost * 100) if value is not None and cost else 0
            confidence = ((reward.get("price_resolution") or {}).get("confidence") or reward.get("availability") or "")
            print(
                f"{(reward.get('display_value') + ' ' + (reward.get('name') or ''))[:54]:<54} "
                f"{cost:>8g} {money(value):>8} {money(per_100):>8} "
                f"{reward.get('type')}; {confidence}"
            )
    else:
        print("\nDashboard rewards: none captured yet from the account dashboard.")


def list_point_offers(args):
    coupons = load_coupon_overlays()
    rows = []
    for coupon in coupons:
        discount = coupon_discount(coupon)
        if discount.get("kind") != "points_multiplier":
            continue
        application = coupon.get("application") or {}
        rows.append(
            {
                "offer_id": coupon.get("offer_id"),
                "name": coupon.get("name") or coupon.get("brand"),
                "value": coupon.get("value_text"),
                "multiplier": discount.get("multiplier"),
                "scope": application.get("scope") or coupon.get("category"),
                "threshold": application.get("threshold_amount"),
                "allocation": application.get("allocation"),
                "state": coupon_state_label(coupon),
                "end_date": coupon.get("end_date") or coupon.get("offer_end_date"),
                "description": coupon.get("description") or "",
            }
        )

    rows.sort(key=lambda row: (row["scope"] or "", -(row["multiplier"] or 0), row["name"] or ""))
    print("\nSafeway point multiplier offers")
    if not rows:
        print("No point multiplier offers found in the saved coupon gallery.")
        return

    print(
        f"{'Offer ID':<10} {'X':>4} {'End':<10} {'Scope':<22} {'Min':>8} "
        f"{'State':<13} Allocation / Offer"
    )
    print("-" * 132)
    for row in rows:
        if args.only_clipped and row["state"] != "clipped":
            continue
        threshold = "" if row["threshold"] is None else money(row["threshold"])
        print(
            f"{row['offer_id']:<10} {row['multiplier'] or 0:>4g} "
            f"{row['end_date'] or '':<10} {(row['scope'] or '')[:22]:<22} {threshold:>8} "
            f"{row['state']:<13} {row['allocation']}; {row['value']} - {row['name']}"
        )
        if args.verbose and row["description"]:
            print(f"{'':<10} {'':>4} {'':<10} {'':<22} {'':>8} {'':<13} {row['description']}")


RECIPES = {
    "salmon_dinner": {
        "display": "Tonight: salmon, potatoes, broccoli",
        "servings": 1,
        "ingredients": {
            "salmon portion": 1,
            "potatoes": 1,
            "broccoli": 1,
        },
        "notes": "Uses one salmon portion plus sale potatoes/broccoli."
    },
    "ground_beef_lunch_bowls": {
        "display": "Mon/Tue/Thu lunch: ground beef taco rice bowls",
        "servings": 3,
        "ingredients": {
            "ground beef 80/20": 2,
            "rice": 1,
            "flour tortillas": 1,
            "shredded cheese": 1,
            "avocados": 1,
            "onions": 1,
        },
        "notes": "Uses the corrected $2.99/lb ground beef deal. Assumes pantry spices/salsa."
    },
    "chicken_lunch_bowls": {
        "display": "Mon/Tue/Thu lunch: honey-garlic chicken rice bowls",
        "servings": 3,
        "ingredients": {
            "boneless skinless chicken thighs": 1.5,
            "rice": 1,
            "broccoli": 1,
            "mushrooms": 1,
            "onions": 1,
        },
        "notes": "Chicken-thigh version from the earlier plan."
    }
}


def estimate_recipe(recipe_key, prices, deals):
    recipe = RECIPES[recipe_key]
    rows = []
    total = 0.0
    for name, qty in recipe["ingredients"].items():
        price, price_type, deal = effective_price(name, prices, deals)
        if price is None:
            rows.append((name, qty, None, price_type, None))
            continue
        line_total = qty * price
        total += line_total
        rows.append((name, qty, price, price_type, line_total))
    return recipe, rows, total


def estimate(args):
    prices = load_json(MEAL_PRICES_FILE)
    _deals_path, deals = load_weekly_deals(args.deals_file)

    keys = args.recipes or list(RECIPES)
    grand_total = 0.0
    for key in keys:
        if key not in RECIPES:
            raise SystemExit(f"Unknown recipe: {key}. Available: {', '.join(RECIPES)}")
        recipe, rows, total = estimate_recipe(key, prices, deals)
        grand_total += total
        print(f"\n{recipe['display']} ({recipe['servings']} serving{'s' if recipe['servings'] != 1 else ''})")
        print(recipe["notes"])
        print(f"{'Ingredient':<34} {'Qty':>6} {'Price':>8} {'Type':>7} {'Line':>8}")
        print("-" * 70)
        for name, qty, price, price_type, line_total in rows:
            price_text = "n/a" if price is None else money(price)
            line_text = "n/a" if line_total is None else money(line_total)
            print(f"{name:<34} {qty:>6g} {price_text:>8} {price_type:>7} {line_text:>8}")
        per_serving = total / recipe["servings"]
        print(f"Total: {money(total)}  |  Per serving: {money(per_serving)}")

    if len(keys) > 1:
        print(f"\nCombined estimate: {money(grand_total)}")


def main():
    parser = argparse.ArgumentParser(description="Estimate meal-plan costs from base prices and weekly deals.")
    sub = parser.add_subparsers(dest="command", required=True)

    deals_parser = sub.add_parser("deals", help="List weekly deals and stock-up candidates")
    deals_parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    deals_parser.add_argument("--all", action="store_false", dest="only_savings", help="Show advertised items even when not cheaper than base")
    deals_parser.set_defaults(func=list_deals, only_savings=True)

    coupons_parser = sub.add_parser("coupon-matches", help="List item-level coupon matches for saved ingredients")
    coupons_parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    coupons_parser.add_argument("--only-applicable", action="store_true", help="Only show clipped coupons that can currently be applied")
    coupons_parser.add_argument("--verbose", action="store_true", help="Show match/application detail")
    coupons_parser.set_defaults(func=coupon_matches)

    rewards_parser = sub.add_parser("rewards", help="List known Safeway rewards redemption options")
    rewards_parser.add_argument("--points", type=float, help="Available points for affordability filtering")
    rewards_parser.add_argument("--affordable", action="store_true", help="Only show dashboard rewards affordable with available points")
    rewards_parser.add_argument("--limit", type=int, help="Limit dashboard reward rows")
    rewards_parser.add_argument("--only-valued", action="store_true", help="Only show dashboard rewards with estimated dollar values")
    rewards_parser.set_defaults(func=list_rewards)

    point_offers_parser = sub.add_parser("point-offers", help="List Safeway point multiplier offers from coupons/deals")
    point_offers_parser.add_argument("--only-clipped", action="store_true", help="Only show offers confirmed clipped")
    point_offers_parser.add_argument("--verbose", action="store_true", help="Show offer descriptions")
    point_offers_parser.set_defaults(func=list_point_offers)

    estimate_parser = sub.add_parser("estimate", help="Estimate recipe costs")
    estimate_parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    estimate_parser.add_argument("recipes", nargs="*", help=f"Recipe keys: {', '.join(RECIPES)}")
    estimate_parser.set_defaults(func=estimate)

    plan_parser = sub.add_parser("estimate-plan", help="Estimate a recipe JSON returned from meal inspiration prompt")
    plan_parser.add_argument("plan_file", help="JSON file, or Markdown file containing a JSON block")
    plan_parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    plan_parser.add_argument("--no-coupons", action="store_true", help="Ignore coupon overlays")
    plan_parser.add_argument("--no-resolve-missing", action="store_true", help="Do not query Safeway API for missing or stale Safeway ingredients")
    plan_parser.add_argument("--stale-days", type=int, default=14, help="Refresh saved Safeway prices older than this many days")
    plan_parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID for API resolution")
    plan_parser.add_argument("--banner", default=DEFAULT_BANNER, help="Safeway banner for API resolution")
    plan_parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="Safeway channel for API resolution")
    plan_parser.add_argument("--resolution-rows", type=int, default=8, help="Search rows for missing ingredient resolution")
    plan_parser.add_argument("--timeout", type=float, default=15.0, help="Safeway API timeout")
    plan_parser.add_argument("--write-resolved", action="store_true", help="Write high/medium-confidence resolved Safeway prices into meal_prices.json and safeway_price_observations.json")
    plan_parser.add_argument("--json", action="store_true", help="Print priced plan JSON")
    plan_parser.set_defaults(func=estimate_plan)

    cart_parser = sub.add_parser("cart", help="Estimate a cart with line prices and cart-level coupons")
    cart_parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    cart_parser.add_argument("recipes", nargs="*", help=f"Recipe keys: {', '.join(RECIPES)}")
    cart_parser.add_argument("--no-coupons", action="store_true", help="Ignore coupon overlays")
    cart_parser.add_argument("--no-rewards", action="store_true", help="Ignore Safeway rewards point estimates")
    cart_parser.add_argument("--show-ineligible-coupons", action="store_true", help="Show cart-level coupons with no eligible subtotal")
    cart_parser.add_argument("--show-ineligible-rewards", action="store_true", help="Show point offers that need matching tags/products")
    cart_parser.add_argument("--verbose", action="store_true", help="Show extra coupon eligibility detail")
    cart_parser.set_defaults(func=cart)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
