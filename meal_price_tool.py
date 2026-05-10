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
COUPON_ACCOUNT_STATE_FILE = ROOT / "safeway_coupon_account_state.local.json"
REWARDS_FILE = ROOT / "safeway_rewards.json"
REWARDS_ACCOUNT_STATE_FILE = ROOT / "safeway_rewards_account_state.local.json"
OBSERVATIONS_FILE = ROOT / "safeway_price_observations.json"
WEEKLY_DEAL_BASE_OBSERVATION_PREFIX = "safeway_weekly_deal_base_observations"
GIANT_FLIPP_DEALS_GLOB = "giant_weekly_deals_*.json"
GIANT_COUPONS_FILE = ROOT / "giant_coupons.json"
GIANT_COUPON_ACCOUNT_STATE_FILE = ROOT / "giant_coupon_account_state.local.json"

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


def choose_giant_flipp_deals_file(explicit=None):
    if explicit:
        return Path(explicit)
    candidates = []
    for path in ROOT.glob(GIANT_FLIPP_DEALS_GLOB):
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        metadata = data.get("metadata") or {}
        valid_from = parse_iso_date(metadata.get("valid_from"))
        valid_to = parse_iso_date(metadata.get("valid_to"))
        if valid_from is not None and valid_to is not None and valid_from <= date.today() <= valid_to:
            candidates.append((valid_from, path))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def load_giant_coupons(explicit=None):
    """Load the Giant coupon catalog if available, with merged account state.

    Returns (path, data) or (None, None) if no file is on disk. Account state
    from the gitignored local file is merged in when present.
    """
    path = Path(explicit) if explicit else GIANT_COUPONS_FILE
    if not path.exists():
        return None, None
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError):
        return None, None
    account = {}
    if GIANT_COUPON_ACCOUNT_STATE_FILE.exists():
        try:
            account_data = load_json(GIANT_COUPON_ACCOUNT_STATE_FILE)
            account = account_data.get("by_id") or {}
        except (OSError, json.JSONDecodeError):
            account = {}
    if account:
        for coupon in data.get("coupons", []):
            cid = coupon.get("id")
            if not cid or cid not in account:
                continue
            existing = coupon.setdefault("account_state", {})
            for key, value in account[cid].items():
                if value is not None:
                    existing[key] = value
    return path, data


def coupon_active(coupon, today=None):
    today = today or date.today().isoformat()
    end = (coupon.get("end_date") or "")[:10]
    start = (coupon.get("start_date") or "")[:10]
    if end and end < today:
        return False
    if start and start > today:
        return False
    return True


def giant_coupons_for_meal_key(coupons, meal_key):
    """Return active coupons whose matched_meal_keys include this key."""
    rows = []
    for coupon in coupons or []:
        if meal_key in (coupon.get("matched_meal_keys") or []):
            if coupon_active(coupon):
                rows.append(coupon)
    return rows


def load_giant_flipp_deals(explicit=None):
    path = choose_giant_flipp_deals_file(explicit)
    if path is None:
        return None, None
    return path, load_json(path)


GIANT_MATCH_STOPWORDS = {
    "and", "any", "or", "with", "the", "for", "fresh", "select", "ea",
    "each", "pack", "lb", "lbs", "oz", "ct", "count", "size", "family",
    "value", "store", "brand", "premium", "natural", "organic",
}

GIANT_CATEGORY_NEGATIVES = {
    "protein": {"oil", "flour", "cookie", "ice", "cream", "yogurt", "salad", "dressing", "sauce", "spread", "snack", "crackers"},
    "produce": {"oil", "frozen", "ice", "cream", "yogurt", "snack", "candy", "chocolate", "wine", "beer", "canned", "jarred"},
    "pantry": {"frozen", "ice", "cream", "wine", "beer", "spirits"},
    "frozen": {"oil", "wine", "beer", "spirits", "candy"},
    "dairy": {"oil", "frozen", "wine", "beer", "spirits", "candy", "chocolate", "wax", "croissant", "croissants"},
}

GIANT_REQUIRED_TOKEN_GROUPS = [
    ({"raw"}, {"raw"}),
    ({"sweet"}, {"sweet"}),
    ({"tortillas"}, {"tortilla", "tortillas"}),
    ({"spinach"}, {"spinach"}),
    ({"tomatoes"}, {"tomato", "tomatoes"}),
    ({"crushed"}, {"crushed"}),
    ({"tenderloin"}, {"tenderloin"}),
    ({"chops"}, {"chop", "chops"}),
    ({"butter"}, {"butter"}),
    ({"cheese"}, {"cheese"}),
    ({"mushrooms"}, {"mushroom", "mushrooms"}),
    ({"pepper", "peppers"}, {"pepper", "peppers"}),
    ({"teriyaki"}, {"teriyaki"}),
    ({"broccoli"}, {"broccoli"}),
    ({"rice"}, {"rice"}),
    ({"pasta"}, {"pasta"}),
    ({"avocados"}, {"avocado", "avocados"}),
    ({"potatoes"}, {"potato", "potatoes"}),
    ({"onions"}, {"onion", "onions"}),
    ({"beef"}, {"beef"}),
    ({"turkey"}, {"turkey"}),
    ({"chicken"}, {"chicken"}),
    ({"shrimp"}, {"shrimp"}),
    ({"salmon"}, {"salmon"}),
]

GIANT_FORM_CONFLICTS = {
    "butter": {"btl", "cellars", "croissant", "croissants", "cookie", "cookies", "popcorn", "wine"},
    "mushrooms": {"can", "canned", "jar", "jarred", "stems", "pieces"},
    "spinach": {"chicken", "dip"},
}

GIANT_PREPARED_PROTEIN_TOKENS = {
    "battered", "breaded", "cooked", "diced", "entree", "entrees",
    "fajita", "grilled", "shredded",
}


def giant_token_set(text):
    normalized = (text or "").lower()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return {t for t in tokens if len(t) > 2 and t not in GIANT_MATCH_STOPWORDS and not t.isdigit()}


def giant_item_tokens(item):
    return (
        giant_token_set(item.get("name", ""))
        | giant_token_set(item.get("brand", ""))
        | giant_token_set(item.get("description", ""))
    )


def giant_parse_size(text):
    if not text:
        return (None, None)
    t = str(text).lower()
    weight_match = re.search(r"(\d+(?:\.\d+)?)\s*(lbs?|pound)", t)
    if weight_match:
        return (float(weight_match.group(1)) * 16, "weight")
    fl_oz_match = re.search(r"(\d+(?:\.\d+)?)\s*fl\.?\s*oz", t)
    if fl_oz_match:
        return (float(fl_oz_match.group(1)), "weight")
    oz_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)", t)
    if oz_match:
        return (float(oz_match.group(1)), "weight")
    if re.search(r"\b(doz|dozen)", t):
        qty_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:doz|dozen)", t)
        qty = float(qty_match.group(1)) if qty_match else 1.0
        return (qty * 12, "count")
    count_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:ct|count|pack|piece|pk)\b", t)
    if count_match:
        return (float(count_match.group(1)), "count")
    if re.search(r"\b(lbs?|pound|sold\s+by\s+lb)\b", t):
        return (None, "weight")
    if re.search(r"\b(each|ea)\b", t):
        return (1.0, "count")
    return (None, None)


def giant_parse_size_range(text):
    if not text:
        return (None, None, None)
    t = str(text).lower()
    weight_range = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(lbs?|pound|oz|ounce)", t)
    if weight_range:
        low = float(weight_range.group(1))
        high = float(weight_range.group(2))
        unit = weight_range.group(3)
        if unit.startswith("lb") or unit.startswith("pound"):
            low *= 16
            high *= 16
        return (low, high, "weight")
    count_range = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:ct|count|pack|pk)\b", t)
    if count_range:
        return (float(count_range.group(1)), float(count_range.group(2)), "count")
    qty, kind = giant_parse_size(t)
    if kind is not None:
        return (qty, qty, kind)
    return (None, None, None)


def giant_size_score(meal_unit, item):
    meal_qty, meal_kind = giant_parse_size(meal_unit)
    low, high, item_kind = giant_parse_size_range(item.get("description") or "")
    if item_kind is None and item.get("unit_kind") == "per_lb":
        item_kind = "weight"
    if meal_kind is None or item_kind is None:
        return 0.0
    item_text = " ".join(str(item.get(key) or "").lower() for key in ("name", "description"))
    if meal_kind == "weight" and item_kind == "count" and "shrimp" in item_text:
        return 0.0
    if meal_kind != item_kind:
        return -0.60
    if meal_qty is None or low is None or high is None:
        return 0.05
    if low * 0.85 <= meal_qty <= high * 1.20:
        return 0.15
    if high < meal_qty * 0.5 or low > meal_qty * 2.0:
        return -0.60
    return 0.0


def giant_lean_ratios(text):
    ratios = set()
    text = str(text or "").lower()
    for lean, fat in re.findall(r"\b(\d{2})\s*/\s*(\d{1,2})\b", text):
        ratios.add((int(lean), int(fat)))
    for lean in re.findall(r"\b(\d{2})\s*%\s*lean\b", text):
        lean_i = int(lean)
        ratios.add((lean_i, 100 - lean_i))
    return ratios


def giant_reject_match(meal_key, meal_record, item, meal_tokens, item_tokens):
    for triggers, required in GIANT_REQUIRED_TOKEN_GROUPS:
        if meal_tokens & triggers and not (item_tokens & required):
            return True

    for trigger, conflicts in GIANT_FORM_CONFLICTS.items():
        if trigger in meal_tokens and item_tokens & conflicts:
            return True

    expected_ratios = giant_lean_ratios(meal_key)
    item_ratios = giant_lean_ratios(
        " ".join(str(item.get(key) or "") for key in ("name", "brand", "description"))
    )
    if expected_ratios and item_ratios and expected_ratios.isdisjoint(item_ratios):
        return True

    category = (meal_record.get("category") or "").lower()
    if (
        category == "protein"
        and item_tokens & GIANT_PREPARED_PROTEIN_TOKENS
        and not meal_tokens & GIANT_PREPARED_PROTEIN_TOKENS
    ):
        return True
    if category == "produce" and item_tokens & {"can", "canned", "jar", "jarred", "pickled", "dried"}:
        return True

    return False


def giant_match_score(meal_key, meal_record, item):
    meal_tokens = giant_token_set(meal_key)
    item_tokens = giant_item_tokens(item)
    if not meal_tokens:
        return 0.0
    if giant_reject_match(meal_key, meal_record, item, meal_tokens, item_tokens):
        return 0.0

    overlap = meal_tokens & item_tokens
    score = len(overlap) / max(1, len(meal_tokens))

    category = (meal_record.get("category") or "").lower()
    negatives = GIANT_CATEGORY_NEGATIVES.get(category, set())
    if negatives & item_tokens:
        score -= 0.25

    if (item.get("brand") or "").lower() == "giant":
        score += 0.05
    score += giant_size_score(meal_record.get("unit"), item)

    return round(max(score, 0.0), 3)


def match_giant_flipp_to_meal_items(meal_items, flipp_deals, min_score=0.5):
    """Map meal_prices keys to their best Flipp flyer match.

    Returns dict {meal_key: {"score": float, "item": flipp_item}} for matches
    at or above min_score.
    """
    if not flipp_deals:
        return {}
    flyer_items = [item for item in flipp_deals.get("items", []) if item.get("current_price") is not None]
    matches = {}
    for key, record in meal_items.items():
        best = None
        best_score = 0.0
        for item in flyer_items:
            score = giant_match_score(key, record, item)
            if score >= min_score and score > best_score:
                best = item
                best_score = score
        if best is not None:
            matches[key] = {"score": best_score, "item": best}
    return matches


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
    apply_local_coupon_account_state(offers)
    return offers


def apply_local_coupon_account_state(offers):
    if not COUPON_ACCOUNT_STATE_FILE.exists():
        return
    data = load_json(COUPON_ACCOUNT_STATE_FILE)
    by_id = data.get("account_state_by_offer_id") or {}
    for offer in offers:
        offer_id = str(offer.get("offer_id") or "")
        if offer_id in by_id:
            offer["account_state"] = by_id[offer_id]
    existing_ids = {str(offer.get("offer_id") or "") for offer in offers}
    for offer in data.get("account_only_offers") or []:
        offer_id = str(offer.get("offer_id") or "")
        if offer_id and offer_id not in existing_ids:
            offers.append(offer)


def load_rewards_config():
    if REWARDS_FILE.exists():
        config = load_json(REWARDS_FILE)
    else:
        config = {
            "earning_rules": {"grocery": {"points_per_dollar": 1}},
            "valuation": {"default_point_value": 0.0},
            "redemption_options": [],
            "product_rewards": [],
        }
    if REWARDS_ACCOUNT_STATE_FILE.exists():
        account_state = load_json(REWARDS_ACCOUNT_STATE_FILE)
        config["account_state"] = account_state.get("account_state", account_state)
    return config


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


def augment_lines_with_giant(lines, prices, flipp_matches):
    """Annotate each cart line with Giant base / Flipp deal pricing."""
    flipp_counts = giant_flipp_counts(
        {line["item"]: line.get("qty") for line in lines},
        flipp_matches,
    )
    for line in lines:
        item_name = line["item"]
        item = prices.get("items", {}).get(item_name) or {}
        gnt_price, gnt_label = best_giant_price(item_name, item, flipp_matches, flipp_counts)
        line["giant_unit_price"] = gnt_price
        line["giant_label"] = gnt_label or ""
        if gnt_price is None:
            line["giant_line_total"] = None
            line["cheaper_store"] = "n/a"
            continue
        line["giant_line_total"] = line["qty"] * gnt_price
        sw_price = line.get("unit_price")
        if sw_price is None:
            line["cheaper_store"] = "Giant only"
        elif abs(sw_price - gnt_price) < 0.005:
            line["cheaper_store"] = "tie"
        elif sw_price < gnt_price:
            line["cheaper_store"] = "Safeway"
        else:
            line["cheaper_store"] = "Giant"


def cart(args):
    prices = load_json(MEAL_PRICES_FILE)
    deals_path, deals = load_weekly_deals(args.deals_file)
    coupons = [] if args.no_coupons else load_coupon_overlays()
    rewards_config = None if args.no_rewards else load_rewards_config()

    keys = args.recipes or ["ground_beef_lunch_bowls"]
    recipe_refs, lines, servings = build_cart_lines(keys, prices, deals, coupons)

    flipp_matches = {}
    if getattr(args, "compare_stores", False):
        _flipp_path, flipp_data = load_giant_flipp_deals(getattr(args, "giant_deals_file", None))
        if flipp_data is not None:
            flipp_matches = match_giant_flipp_to_meal_items(
                prices.get("items", {}),
                flipp_data,
                min_score=getattr(args, "min_flipp_score", 0.5),
            )
        augment_lines_with_giant(lines, prices, flipp_matches)

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

    if getattr(args, "compare_stores", False):
        print(f"\n{'Item':<32} {'Qty':>5} {'Unit':<12} {'SW':>7} {'SW Line':>8} {'Giant':>7} {'Gnt Line':>9} {'Cheaper':<13} {'Source / Detail':<26}")
        print("-" * 138)
        for line in lines:
            clip = "clip" if line["requires_clip"] else ""
            detail = line["detail"][:24]
            clip_detail = detail if not clip else f"{clip}; {detail}"[:26]
            sw_price = money(line["unit_price"])
            sw_line = money(line["line_total"])
            gnt_price = money(line.get("giant_unit_price"))
            gnt_line = money(line.get("giant_line_total"))
            cheaper = line.get("cheaper_store") or "n/a"
            print(
                f"{line['item'][:32]:<32} {line['qty']:>5g} {line['unit'][:12]:<12} "
                f"{sw_price:>7} {sw_line:>8} {gnt_price:>7} {gnt_line:>9} {cheaper:<13} {clip_detail:<26}"
            )
            if args.verbose and line.get("giant_label"):
                print(f"{'':<32} {'':>5} {'':<12} {'Giant detail:':<25} {line['giant_label']}")
    else:
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

    if getattr(args, "compare_stores", False):
        priced_lines = [line for line in lines if line.get("line_total") is not None]
        giant_priced_lines = [line for line in lines if line.get("giant_line_total") is not None]
        safeway_subtotal = sum(line["line_total"] for line in priced_lines)
        giant_subtotal = sum(line.get("giant_line_total") or 0 for line in giant_priced_lines)
        best_subtotal = 0.0
        best_known = True
        unknown_giant = []
        for line in lines:
            sw_line = line.get("line_total")
            gnt_line = line.get("giant_line_total")
            if sw_line is None and gnt_line is None:
                best_known = False
                continue
            if sw_line is None:
                best_subtotal += gnt_line
                continue
            if gnt_line is None:
                best_subtotal += sw_line
                unknown_giant.append(line["item"])
                continue
            best_subtotal += min(sw_line, gnt_line)

        print("\nCross-store comparison")
        print(f"  Safeway pre-coupon subtotal: {money(safeway_subtotal)}")
        print(f"  Safeway final (with coupons): {money(final_total)}")
        print(f"  Giant subtotal: {money(giant_subtotal)} (Flipp deals + Giant base; coupons not modeled)")
        print(f"  Best-of-both subtotal: {money(best_subtotal) if best_known else 'partial (some lines missing both stores)'}")
        if best_known and final_total < best_subtotal:
            print(f"  Safeway-only with coupons still beats best-of-both pre-coupon by {money(best_subtotal - final_total)}.")
        elif best_known and best_subtotal < safeway_subtotal:
            print(f"  Cherry-picking Giant for cheaper lines saves {money(safeway_subtotal - best_subtotal)} vs Safeway-only pre-coupon.")
        if unknown_giant:
            print(f"  No Giant price for: {', '.join(unknown_giant[:6])}{' ...' if len(unknown_giant) > 6 else ''}")

        # Per-line Giant coupon visibility (informational; bundle conditions
        # not modeled, so we do not subtract from subtotal automatically).
        _coupons_path, coupon_data = load_giant_coupons()
        if coupon_data:
            all_coupons = coupon_data.get("coupons") or []
            applicable_rows = []
            seen_coupon_ids = set()
            potential_max = 0.0
            for line in lines:
                meal_key = line["item"]
                matches = giant_coupons_for_meal_key(all_coupons, meal_key)
                clipped_matches = [
                    c for c in matches
                    if (c.get("account_state") or {}).get("clipped") in (True, None)
                ]
                line_max = 0.0
                for coupon in matches:
                    cid = coupon.get("id")
                    if not cid or cid in seen_coupon_ids:
                        continue
                    seen_coupon_ids.add(cid)
                    discount = coupon.get("max_discount") or 0
                    try:
                        discount = float(discount)
                    except (TypeError, ValueError):
                        discount = 0.0
                    line_max = max(line_max, discount)
                    applicable_rows.append({
                        "meal_key": meal_key,
                        "coupon_id": cid,
                        "name": coupon.get("name"),
                        "max_discount": discount,
                        "end_date": (coupon.get("end_date") or "")[:10],
                        "clipping_required": coupon.get("clipping_required"),
                        "clipped": (coupon.get("account_state") or {}).get("clipped"),
                    })
                if line_max:
                    potential_max += line_max

            if applicable_rows:
                print(f"\nGiant coupon coverage (informational; bundle conditions not auto-applied)")
                print(f"  {len(applicable_rows)} unique applicable coupons across {len({r['meal_key'] for r in applicable_rows})} lines")
                if potential_max:
                    print(f"  Aggregate max discount if all bundles met: up to {money(potential_max)}")
                if args.verbose:
                    grouped = {}
                    for row in applicable_rows:
                        grouped.setdefault(row["meal_key"], []).append(row)
                    for meal_key, rows in grouped.items():
                        rows.sort(key=lambda r: -(r["max_discount"] or 0))
                        for row in rows:
                            clip = "clip needed" if row["clipping_required"] else "auto-apply"
                            state = "clipped" if row["clipped"] else "unclipped" if row["clipped"] is False else "state unknown"
                            print(
                                f"    {meal_key:<28} max {money(row['max_discount']):>6} "
                                f"{clip:<12} {state:<14} ends {row['end_date']:<10} {row['name']}"
                            )


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


def list_giant_deals(args):
    prices = load_json(MEAL_PRICES_FILE)
    deals_path, deals = load_giant_flipp_deals(args.deals_file)
    if deals is None:
        print("No active Giant Flipp flyer found.")
        print(f"Run: python3 giant_flipp_deals.py fetch --write --only-priced")
        return

    metadata = deals.get("metadata") or {}
    matches = match_giant_flipp_to_meal_items(prices.get("items", {}), deals, min_score=args.min_score)

    rows = []
    for meal_key, record in prices.get("items", {}).items():
        match = matches.get(meal_key)
        if not match:
            if args.matched_only or args.only_savings:
                continue
            rows.append((meal_key, record, None, None, None))
            continue

        item = match["item"]
        sale = giant_flipp_planning_price(record, item)
        safeway_base = record.get("base_prices", {}).get("Safeway")
        giant_base = record.get("base_prices", {}).get("Giant")
        compare_base = giant_base if giant_base is not None else safeway_base
        savings = None if compare_base is None or sale is None else compare_base - sale
        if args.only_savings and (savings is None or savings <= 0):
            continue
        rows.append((meal_key, record, match, savings, sale))

    rows.sort(key=lambda row: (row[3] is None, -(row[3] or 0), row[0]))

    print(f"\nGiant Food weekly circular deals (matched to meal_prices.json)")
    print(f"File {deals_path.name}; flyer {metadata.get('flyer_id')}; valid {metadata.get('valid_from')} to {metadata.get('valid_to')}")
    print(f"{len(matches)} of {len(prices.get('items', {}))} meal items have a Flipp match >= {args.min_score}")
    print()
    print(f"{'Meal item':<32} {'Sale':>14} {'SW Base':>9} {'Gnt Base':>9} {'Save':>8} {'Sc':>5}  Match (description)")
    print("-" * 140)
    for meal_key, record, match, savings, sale in rows:
        safeway_base = record.get("base_prices", {}).get("Safeway")
        giant_base = record.get("base_prices", {}).get("Giant")
        sw_base_text = "n/a" if safeway_base is None else money(safeway_base)
        gnt_base_text = "n/a" if giant_base is None else money(giant_base)
        save_text = "n/a" if savings is None else money(savings)

        if match is None:
            print(f"{meal_key[:32]:<32} {'(no match)':>14} {sw_base_text:>9} {gnt_base_text:>9} {save_text:>8} {'':>5}  -")
            continue

        item = match["item"]
        sale_text = money(sale)
        raw_display = item.get("price_display")
        if raw_display and item.get("current_price") != sale:
            sale_text = f"{sale_text} ({raw_display})"
        elif raw_display:
            sale_text = raw_display
        score = match["score"]
        flyer_name = item.get("name") or ""
        description = item.get("description") or ""
        unit_kind = item.get("unit_kind") or ""
        valid_to = item.get("valid_to") or metadata.get("valid_to") or ""

        match_label = flyer_name[:50]
        if description:
            match_label += f" ({description[:30]})"
        if unit_kind == "per_lb":
            match_label += " /lb"
        if valid_to:
            match_label += f" until {valid_to[-5:]}"

        print(f"{meal_key[:32]:<32} {sale_text:>14} {sw_base_text:>9} {gnt_base_text:>9} {save_text:>8} {score:>5.2f}  {match_label}")


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


def giant_flipp_group_id(flipp_item):
    if not flipp_item or not flipp_item.get("multi_buy_qty"):
        return None
    return f"giant_flipp:{flipp_item.get('flipp_id') or flipp_item.get('deal_name') or flipp_item.get('name')}"


def giant_flipp_counts(quantities, flipp_matches):
    counts = {}
    for item_name, qty in quantities.items():
        match = (flipp_matches or {}).get(item_name)
        flipp_item = (match or {}).get("item")
        group_id = giant_flipp_group_id(flipp_item)
        if not group_id:
            continue
        try:
            counts[group_id] = counts.get(group_id, 0.0) + float(qty or 0)
        except (TypeError, ValueError):
            continue
    return counts


def giant_flipp_planning_price(meal_item, flipp_item):
    price = flipp_item.get("current_price")
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None

    meal_qty, meal_kind = giant_parse_size(meal_item.get("unit"))
    low, high, item_kind = giant_parse_size_range(flipp_item.get("description") or "")

    if flipp_item.get("unit_kind") == "per_lb":
        return price
    if meal_kind == "weight" and meal_qty is None and item_kind == "weight" and low and high and abs(low - high) < 0.001:
        pounds = low / 16
        if pounds > 0:
            return round(price / pounds, 2)
    if (
        meal_kind is not None
        and item_kind == meal_kind
        and meal_qty is not None
        and low is not None
        and high is not None
        and abs(low - high) < 0.001
        and low > 0
    ):
        return round(price * (meal_qty / low), 2)
    return price


def best_giant_price(item_name, item, flipp_matches, flipp_counts=None):
    """Return (price, source_label) for the best available Giant price.

    Considers Giant base price (from price_sources.Giant) and the matched
    Flipp circular deal, picking the lower of the two when both exist.
    """
    base = item.get("base_prices", {}).get("Giant")
    deal_price = None
    deal_label = None
    deal_blocked = None
    match = (flipp_matches or {}).get(item_name)
    if match and match.get("item"):
        flipp_item = match["item"]
        group_id = giant_flipp_group_id(flipp_item)
        threshold = flipp_item.get("multi_buy_qty")
        if group_id and threshold:
            count = (flipp_counts or {}).get(group_id, 0.0)
            if count < float(threshold):
                deal_blocked = f"flipp unmet {count:g}/{float(threshold):g}"
            else:
                deal_price = giant_flipp_planning_price(item, flipp_item)
        else:
            deal_price = giant_flipp_planning_price(item, flipp_item)
        if deal_price is not None:
            deal_label = flipp_item.get("price_display") or money(deal_price)

    candidates = []
    if base is not None:
        candidates.append((base, "base" if not deal_blocked else f"base; {deal_blocked}"))
    if deal_price is not None:
        candidates.append((deal_price, f"flipp ({deal_label})"))

    if not candidates:
        return None, None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0]


def estimate_recipe_cross_store(recipe_key, prices, deals, flipp_matches):
    """Compute Safeway and Giant per-line totals side by side."""
    recipe = RECIPES[recipe_key]
    flipp_counts = giant_flipp_counts(recipe["ingredients"], flipp_matches)
    rows = []
    safeway_total = 0.0
    giant_total = 0.0
    safeway_total_known = True
    giant_total_known = True
    best_total = 0.0
    best_total_known = True

    for name, qty in recipe["ingredients"].items():
        sw_price, sw_type, _deal = effective_price(name, prices, deals)
        item = prices["items"].get(name, {})
        gnt_price, gnt_label = best_giant_price(name, item, flipp_matches, flipp_counts)

        sw_line = None if sw_price is None else qty * sw_price
        gnt_line = None if gnt_price is None else qty * gnt_price

        if sw_line is None:
            safeway_total_known = False
        else:
            safeway_total += sw_line

        if gnt_line is None:
            giant_total_known = False
        else:
            giant_total += gnt_line

        if sw_line is None and gnt_line is None:
            best_line = None
        elif sw_line is None:
            best_line = gnt_line
        elif gnt_line is None:
            best_line = sw_line
        else:
            best_line = min(sw_line, gnt_line)
        if best_line is None:
            best_total_known = False
        else:
            best_total += best_line

        cheaper = "tie"
        if sw_price is not None and gnt_price is not None:
            if abs(sw_price - gnt_price) < 0.005:
                cheaper = "tie"
            elif sw_price < gnt_price:
                cheaper = "Safeway"
            else:
                cheaper = "Giant"
        elif sw_price is not None:
            cheaper = "Safeway only"
        elif gnt_price is not None:
            cheaper = "Giant only"
        else:
            cheaper = "n/a"

        rows.append({
            "name": name,
            "qty": qty,
            "safeway_price": sw_price,
            "safeway_type": sw_type,
            "safeway_line": sw_line,
            "giant_price": gnt_price,
            "giant_label": gnt_label,
            "giant_line": gnt_line,
            "cheaper": cheaper,
        })

    return {
        "recipe": recipe,
        "rows": rows,
        "safeway_total": safeway_total if safeway_total_known else None,
        "giant_total": giant_total if giant_total_known else None,
        "best_total": best_total if best_total_known else None,
    }


def estimate(args):
    prices = load_json(MEAL_PRICES_FILE)
    _deals_path, deals = load_weekly_deals(args.deals_file)

    flipp_matches = {}
    if getattr(args, "compare_stores", False):
        _flipp_path, flipp_data = load_giant_flipp_deals(getattr(args, "giant_deals_file", None))
        if flipp_data is not None:
            flipp_matches = match_giant_flipp_to_meal_items(prices.get("items", {}), flipp_data, min_score=getattr(args, "min_flipp_score", 0.5))

    keys = args.recipes or list(RECIPES)
    grand_total = 0.0
    grand_safeway = 0.0
    grand_giant = 0.0
    grand_best = 0.0
    grand_safeway_known = True
    grand_giant_known = True
    grand_best_known = True

    for key in keys:
        if key not in RECIPES:
            raise SystemExit(f"Unknown recipe: {key}. Available: {', '.join(RECIPES)}")

        if getattr(args, "compare_stores", False):
            estimate_data = estimate_recipe_cross_store(key, prices, deals, flipp_matches)
            recipe = estimate_data["recipe"]
            rows = estimate_data["rows"]
            safeway_total = estimate_data["safeway_total"]
            giant_total = estimate_data["giant_total"]
            best_total = estimate_data["best_total"]

            print(f"\n{recipe['display']} ({recipe['servings']} serving{'s' if recipe['servings'] != 1 else ''})")
            print(recipe["notes"])
            print(f"{'Ingredient':<34} {'Qty':>5} {'SW':>8} {'SW Line':>8} {'Giant':>8} {'Gnt Line':>9} {'Cheaper':<14} Giant detail")
            print("-" * 120)
            for row in rows:
                sw_text = "n/a" if row["safeway_price"] is None else money(row["safeway_price"])
                sw_line = "n/a" if row["safeway_line"] is None else money(row["safeway_line"])
                gnt_text = "n/a" if row["giant_price"] is None else money(row["giant_price"])
                gnt_line_text = "n/a" if row["giant_line"] is None else money(row["giant_line"])
                gnt_detail = row["giant_label"] or ""
                print(
                    f"{row['name']:<34} {row['qty']:>5g} {sw_text:>8} {sw_line:>8} "
                    f"{gnt_text:>8} {gnt_line_text:>9} {row['cheaper']:<14} {gnt_detail}"
                )

            sw_total_text = "n/a" if safeway_total is None else money(safeway_total)
            gnt_total_text = "n/a" if giant_total is None else money(giant_total)
            best_total_text = "n/a" if best_total is None else money(best_total)
            print(f"\nSafeway total: {sw_total_text}   Giant total: {gnt_total_text}   Best-of-both: {best_total_text}")
            if safeway_total is not None and giant_total is not None:
                delta = giant_total - safeway_total
                if abs(delta) < 0.005:
                    print("Both stores tie on this recipe.")
                elif delta > 0:
                    print(f"Safeway is cheaper by {money(delta)}.")
                else:
                    print(f"Giant is cheaper by {money(-delta)}.")

            if safeway_total is None:
                grand_safeway_known = False
            else:
                grand_safeway += safeway_total
            if giant_total is None:
                grand_giant_known = False
            else:
                grand_giant += giant_total
            if best_total is None:
                grand_best_known = False
            else:
                grand_best += best_total
            total = safeway_total if safeway_total is not None else 0
            grand_total += total

            if safeway_total is not None:
                per_serving = safeway_total / recipe["servings"]
                print(f"Safeway per serving: {money(per_serving)}")
            continue

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
        if getattr(args, "compare_stores", False):
            sw_text = money(grand_safeway) if grand_safeway_known else "n/a"
            gnt_text = money(grand_giant) if grand_giant_known else "n/a"
            best_text = money(grand_best) if grand_best_known else "n/a"
            print(f"\nCombined Safeway: {sw_text}   Combined Giant: {gnt_text}   Combined best-of-both: {best_text}")
            if grand_safeway_known and grand_best_known and grand_safeway > grand_best + 0.005:
                print(f"Cherry-picking across stores would save {money(grand_safeway - grand_best)} vs Safeway-only.")
        else:
            print(f"\nCombined estimate: {money(grand_total)}")


def main():
    parser = argparse.ArgumentParser(description="Estimate meal-plan costs from base prices and weekly deals.")
    sub = parser.add_subparsers(dest="command", required=True)

    deals_parser = sub.add_parser("deals", help="List weekly deals and stock-up candidates")
    deals_parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    deals_parser.add_argument("--all", action="store_false", dest="only_savings", help="Show advertised items even when not cheaper than base")
    deals_parser.set_defaults(func=list_deals, only_savings=True)

    giant_deals_parser = sub.add_parser("giant-deals", help="List Giant Flipp weekly circular deals matched to meal items")
    giant_deals_parser.add_argument("--deals-file", help="Giant Flipp deals JSON file; defaults to active dated giant_weekly_deals_*.json")
    giant_deals_parser.add_argument("--all", action="store_false", dest="only_savings", help="Show matched items even when not cheaper than base")
    giant_deals_parser.add_argument("--matched-only", action="store_true", help="Hide meal items with no flyer match")
    giant_deals_parser.add_argument("--min-score", type=float, default=0.5, help="Minimum token-overlap score for a match")
    giant_deals_parser.set_defaults(func=list_giant_deals, only_savings=True)

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
    estimate_parser.add_argument("--compare-stores", action="store_true", help="Show Safeway and Giant price columns side by side")
    estimate_parser.add_argument("--giant-deals-file", help="Giant Flipp deals JSON file; defaults to active dated giant_weekly_deals_*.json")
    estimate_parser.add_argument("--min-flipp-score", type=float, default=0.5, help="Minimum Flipp match score to include a Giant deal")
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
    cart_parser.add_argument("--compare-stores", action="store_true", help="Show Safeway and Giant price columns side by side and a cross-store summary")
    cart_parser.add_argument("--giant-deals-file", help="Giant Flipp deals JSON file; defaults to active dated giant_weekly_deals_*.json")
    cart_parser.add_argument("--min-flipp-score", type=float, default=0.5, help="Minimum Flipp match score to include a Giant deal")
    cart_parser.set_defaults(func=cart)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
