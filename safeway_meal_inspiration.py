#!/usr/bin/env python3
"""Suggest meal ideas from unusually useful Safeway weekly ad deals.

This is intentionally different from pure cart optimization. It ranks weekly
ad ingredients by how much they can inspire a meal this week, especially sale
proteins and produce, while keeping base-price uncertainty explicit.
"""

import argparse
import json
import math
import re
from datetime import date
from difflib import get_close_matches
from pathlib import Path

from meal_price_tool import (
    MEAL_PRICES_FILE,
    choose_deals_file,
    giant_flipp_planning_price,
    load_giant_flipp_deals,
    load_json,
    match_giant_flipp_to_meal_items,
    money,
)


ROOT = Path(__file__).parent
DEFAULT_CONTEXT_FILE = ROOT / f"meal_inspiration_context_{date.today().isoformat()}.json"
DEFAULT_PROMPT_FILE = ROOT / f"meal_inspiration_prompt_{date.today().isoformat()}.md"
DEFAULT_USE_UP_PROMPT_FILE = ROOT / f"meal_use_up_prompt_{date.today().isoformat()}.md"
DEAL_BASE_OBSERVATION_PREFIX = "safeway_weekly_deal_base_observations"

ROLE_KEYWORDS = {
    "protein": [
        "beef",
        "chicken",
        "pork",
        "salmon",
        "shrimp",
        "lobster",
        "ribs",
        "steak",
        "turkey",
        "eggs",
    ],
    "produce": [
        "avocado",
        "avocados",
        "berries",
        "blackberries",
        "blueberries",
        "brussels",
        "carrots",
        "cherries",
        "corn",
        "green beans",
        "pineapple",
        "potatoes",
        "salad",
        "spinach",
        "strawberries",
        "sweet potatoes",
        "tomatoes",
    ],
    "pantry": [
        "coffee",
        "pasta",
        "tortillas",
        "tomatoes",
    ],
    "beverage": [
        "orange",
        "juice",
        "coffee",
    ],
}

NOVELTY_KEYWORDS = {
    "lobster": 20,
    "shrimp": 14,
    "salmon": 12,
    "ribs": 12,
    "strip steak": 10,
    "steak": 9,
    "pork loin chops": 8,
    "pineapple": 8,
    "cherries": 8,
    "brussels": 7,
    "corn": 6,
    "green beans": 5,
    "sweet potatoes": 5,
    "avocados": 4,
    "snacking tomatoes": 4,
    "spinach": 4,
    "chicken": 2,
}

BASE_ALIASES = {
    "egglands best eggs": "eggs",
    "fresh atlantic salmon fillet": "salmon portion",
    "jumbo raw shrimp": "raw shrimp 26-30 ct",
    "mission flour tortillas": "flour tortillas",
    "signature select pasta": "pasta",
    "tuttorosso tomatoes": "crushed tomatoes",
}

MEAL_TEMPLATES = [
    {
        "id": "pineapple_pork_chops",
        "title": "Pork chops with pineapple-corn salsa",
        "protein": "pork loin chops",
        "sale_components": ["pork loin chops", "golden pineapple", "sweet corn"],
        "support_components": ["rice", "onions", "garlic", "lime or vinegar"],
        "tj_watchlist": ["rice", "onions"],
        "method": "Pan-sear or grill pork chops, char corn, and fold corn with diced pineapple and onion for a bright topping.",
    },
    {
        "id": "sheet_pan_chicken",
        "title": "Crispy sheet-pan chicken with sweet potatoes and green veg",
        "protein": "chicken drumsticks leg quarters thighs",
        "sale_components": [
            "chicken drumsticks leg quarters thighs",
            "green beans brussels sprouts",
            "sweet potatoes",
        ],
        "support_components": ["olive oil", "garlic", "mustard or vinegar"],
        "tj_watchlist": ["olive oil"],
        "method": "Roast the chicken hot on a sheet pan, add cubed sweet potatoes early, then green beans or brussels near the end.",
    },
    {
        "id": "shrimp_tacos",
        "title": "Shrimp tacos with avocado and tomato-pineapple salsa",
        "protein": "jumbo raw shrimp",
        "sale_components": [
            "jumbo raw shrimp",
            "mission flour tortillas",
            "avocados",
            "snacking tomatoes",
            "golden pineapple",
        ],
        "support_components": ["lime", "onion", "hot sauce", "cabbage if cheap"],
        "tj_watchlist": ["onions"],
        "method": "Quick-sear shrimp with chili powder or cumin, then build tacos with avocado and chopped tomato-pineapple salsa.",
    },
    {
        "id": "salmon_green_veg",
        "title": "Roasted salmon with brussels or green beans",
        "protein": "fresh atlantic salmon fillet",
        "sale_components": [
            "fresh atlantic salmon fillet",
            "green beans brussels sprouts",
            "sweet potatoes",
        ],
        "support_components": ["rice", "lemon", "olive oil"],
        "tj_watchlist": ["rice"],
        "method": "Roast salmon on one pan with green veg; serve over rice or alongside sweet potatoes.",
    },
    {
        "id": "ribs_corn_salad",
        "title": "Shortcut ribs with corn and salad",
        "protein": "pork back ribs st louis ribs",
        "sale_components": [
            "pork back ribs st louis ribs",
            "sweet corn",
            "bagged salad spinach",
        ],
        "support_components": ["barbecue sauce", "vinegar", "pickles if on hand"],
        "tj_watchlist": [],
        "method": "Slow-roast or pressure-cook ribs, finish under high heat, and keep the sides simple with corn and greens.",
    },
    {
        "id": "steak_salad",
        "title": "Strip steak salad with avocado and tomatoes",
        "protein": "beef strip steak",
        "sale_components": [
            "beef strip steak",
            "bagged salad spinach",
            "avocados",
            "snacking tomatoes",
        ],
        "support_components": ["vinegar", "mustard", "bread or potatoes"],
        "tj_watchlist": [],
        "method": "Sear steak hard, slice thin, and turn the sale produce into a substantial salad.",
    },
    {
        "id": "lobster_tomato_pasta",
        "title": "Lobster tomato pasta",
        "protein": "lobster tail",
        "sale_components": [
            "lobster tail",
            "signature select pasta",
            "tuttorosso tomatoes",
            "bagged salad spinach",
        ],
        "support_components": ["garlic", "butter", "chili flakes"],
        "tj_watchlist": ["pasta"],
        "method": "Use the lobster as a small luxury accent in tomato pasta rather than making it the whole meal.",
    },
]


def deal_rows(deals_file=None):
    path = choose_deals_file(deals_file)
    raw = load_json(path)
    metadata = raw.get("metadata", {})
    normalized = raw.get("normalized_deals") or raw.get("deals") or {}
    return path, metadata, normalized


def default_enrichment_path(metadata):
    valid_from = metadata.get("valid_from") or date.today().isoformat()
    return ROOT / f"{DEAL_BASE_OBSERVATION_PREFIX}_{valid_from}.json"


def load_deal_enrichment(metadata, enrichment_file=None, no_api_base=False):
    if no_api_base:
        return {}
    path = Path(enrichment_file) if enrichment_file else default_enrichment_path(metadata)
    if not path.exists():
        return {}
    data = load_json(path)
    return data.get("observations") or {}


def lowercase_blob(name, deal):
    return " ".join(
        str(value or "").lower()
        for value in [name, deal.get("source_label"), deal.get("condition"), deal.get("unit")]
    )


def classify_role(name, deal):
    blob = lowercase_blob(name, deal)
    if any(keyword in blob for keyword in ["tuttorosso", "pasta", "tortilla"]):
        return "pantry"
    if "coffee" in blob or "juice" in blob:
        return "beverage"

    matches = {
        role: sum(1 for keyword in keywords if keyword in blob)
        for role, keywords in ROLE_KEYWORDS.items()
    }
    if matches["protein"]:
        return "protein"
    if matches["produce"]:
        return "produce"
    if matches["pantry"]:
        return "pantry"
    return "other"


def match_base_item(name, prices):
    items = prices.get("items", {})
    if name in items:
        return name, items[name], "exact"

    alias = BASE_ALIASES.get(name)
    if alias in items:
        return alias, items[alias], "alias"

    close = get_close_matches(name, items.keys(), n=1, cutoff=0.86)
    if close:
        return close[0], items[close[0]], "fuzzy"

    return None, None, None


def comparable_unit(deal_unit, base_unit):
    deal_unit = str(deal_unit or "").lower()
    base_unit = str(base_unit or "").lower()
    if not deal_unit or not base_unit:
        return False
    if "lb" in deal_unit and ("lb" in base_unit or "pound" in base_unit):
        return True
    if "dozen" in deal_unit and "dozen" in base_unit:
        return True
    if "package" in deal_unit and any(term in base_unit for term in ["package", "box", "can"]):
        return True
    if "can" in deal_unit and "can" in base_unit:
        return True
    if "bag" in deal_unit and "bag" in base_unit:
        return True
    if "each" in deal_unit and any(term in base_unit for term in ["each", "bag", "package"]):
        return True
    return False


def price_signal_score(role, price, unit):
    if price is None:
        return 0

    unit = str(unit or "").lower()
    if role == "protein":
        if "lb" in unit:
            if price <= 1.5:
                return 24
            if price <= 2.5:
                return 20
            if price <= 4:
                return 14
            if price <= 8:
                return 8
        if price <= 7:
            return 10
        return 2

    if role == "produce":
        if price <= 2:
            return 20
        if price <= 3:
            return 16
        if price <= 4:
            return 8
        return 2

    if role == "pantry":
        if price <= 1:
            return 15
        if price <= 2:
            return 10
        if price <= 4:
            return 5
        return 1

    if role == "beverage":
        return 3 if price <= 4 else 1

    return 1


def novelty_score(name, deal):
    blob = lowercase_blob(name, deal)
    return max((score for keyword, score in NOVELTY_KEYWORDS.items() if keyword in blob), default=0)


def usable_api_base_observation(observation):
    if not observation:
        return None
    if observation.get("confidence") not in {"high", "medium"}:
        return None
    if observation.get("comparison_status") != "comparable":
        return None
    best = observation.get("best_match") or {}
    if best.get("api_base_price") is None:
        return None
    return best


def deal_score(name, deal, prices, api_observation=None):
    role = classify_role(name, deal)
    sale_price = deal.get("sale_price")
    unit = deal.get("unit")
    base_name, base_item, base_match = match_base_item(name, prices)
    base_price = None
    base_source = None
    comparable = False
    savings = None
    savings_pct = None
    reasons = []

    api_base = usable_api_base_observation(api_observation)
    if api_base:
        product = api_base.get("product") or {}
        base_name = product.get("name") or name
        base_match = f"api_{api_observation.get('confidence')}"
        base_source = "weekly_deal_api"
        base_price = api_base.get("api_base_price")
        comparable = True
        savings = base_price - sale_price if sale_price is not None else None
        savings_pct = savings / base_price if base_price else None
        if savings is not None and savings > 0:
            pct = "" if savings_pct is None else f" ({savings_pct * 100:.0f}% off)"
            reasons.append(f"{money(savings)} below API base{pct}")
        elif savings is not None and math.isclose(savings, 0, abs_tol=0.01):
            reasons.append("same as API base")
        elif savings is not None:
            reasons.append(f"{money(abs(savings))} above API base")
    elif base_item:
        base_source = "saved_base"
        base_price = (base_item.get("base_prices") or {}).get("Safeway")
        comparable = comparable_unit(unit, base_item.get("unit"))
        if base_price is not None and sale_price is not None and comparable:
            savings = base_price - sale_price
            savings_pct = savings / base_price if base_price else None
            if savings > 0:
                reasons.append(f"{money(savings)} below saved Safeway base")
            elif math.isclose(savings, 0, abs_tol=0.01):
                reasons.append("same as saved Safeway base")
            else:
                reasons.append(f"{money(abs(savings))} above saved Safeway base")
        elif base_price is not None:
            reasons.append("saved base exists but unit is not safely comparable")
    else:
        if api_observation:
            reasons.append(f"API base match {api_observation.get('confidence', 'unknown')} confidence; not used")
        else:
            reasons.append("no saved or API base price yet")

    role_weight = {
        "protein": 36,
        "produce": 30,
        "pantry": 12,
        "beverage": 4,
        "other": 3,
    }[role]
    score = role_weight + price_signal_score(role, sale_price, unit) + novelty_score(name, deal)

    if savings_pct is not None and savings_pct > 0:
        score += min(30, savings_pct * 55)
    if deal.get("freezer_friendly"):
        score += 5
        reasons.append("freezer friendly")
    if deal.get("requires_clip"):
        score -= 1
        reasons.append("requires clipping")

    label = str(deal.get("source_label") or deal.get("condition") or "")
    if "buy 5" in label.lower():
        score -= 2
        reasons.append("requires buy 5+ participating items")

    if role in {"protein", "produce"}:
        reasons.append(f"{role} can anchor a meal")

    return {
        "name": name,
        "role": role,
        "sale_price": sale_price,
        "unit": unit,
        "source_label": label,
        "requires_clip": bool(deal.get("requires_clip")),
        "freezer_friendly": bool(deal.get("freezer_friendly")),
        "promotion": deal.get("promotion"),
        "base_item": base_name,
        "base_match": base_match,
        "base_source": base_source,
        "base_price": base_price,
        "base_unit": base_item.get("unit") if base_item else None,
        "comparable_to_base": comparable,
        "savings": savings,
        "savings_pct": savings_pct,
        "api_base_observation": api_observation,
        "inspiration_score": round(score, 1),
        "why": reasons,
    }


def ranked_deals(deals_file=None, enrichment_file=None, no_api_base=False):
    path, metadata, deals = deal_rows(deals_file)
    prices = load_json(MEAL_PRICES_FILE)
    enrichment = load_deal_enrichment(metadata, enrichment_file, no_api_base)
    rows = [
        deal_score(name, deal, prices, enrichment.get(name))
        for name, deal in deals.items()
    ]
    rows.sort(
        key=lambda row: (
            -row["inspiration_score"],
            row["role"] != "protein",
            row["name"],
        )
    )
    return path, metadata, rows, prices


def deal_index(rows):
    return {row["name"]: row for row in rows}


def present_components(names, index):
    return [name for name in names if name in index]


def missing_components(names, index):
    return [name for name in names if name not in index]


def trader_joes_guidance(component_names, prices, ranked_rows):
    items = prices.get("items", {})
    guidance_rows = []
    for name in component_names:
        item = items.get(name)
        if not item:
            continue
        base = item.get("base_prices") or {}
        safeway = base.get("Safeway")
        tj = base.get("Trader Joe's")
        if safeway is None or tj is None or tj >= safeway:
            continue
        safeway_sale = [
            row for row in ranked_rows
            if row.get("base_item") == name
            and row.get("sale_price") is not None
            and row.get("comparable_to_base")
        ]
        if safeway_sale and min(row["sale_price"] for row in safeway_sale) <= tj:
            continue
        guidance_rows.append(
            {
                "item": name,
                "trader_joes": tj,
                "safeway": safeway,
                "savings": safeway - tj,
            }
        )
    return guidance_rows


def build_ideas(rows, prices, limit=None):
    index = deal_index(rows)
    ideas = []
    for template in MEAL_TEMPLATES:
        if template["protein"] not in index:
            continue
        sale_components = present_components(template["sale_components"], index)
        missing_sale_components = missing_components(template["sale_components"], index)
        score = index[template["protein"]]["inspiration_score"]
        score += 8 * max(0, len(sale_components) - 1)
        score += sum(index[name]["inspiration_score"] * 0.08 for name in sale_components[1:])
        clip_items = [name for name in sale_components if index[name]["requires_clip"]]
        freezer_items = [name for name in sale_components if index[name]["freezer_friendly"]]
        tj_guidance = trader_joes_guidance(template["tj_watchlist"], prices, rows)
        why = [
            f"anchored by {index[template['protein']]['source_label']}",
            f"{len(sale_components)} sale components available this week",
        ]
        if clip_items:
            why.append("clip required for " + ", ".join(clip_items))
        if freezer_items:
            why.append("freezer-friendly sale item: " + ", ".join(freezer_items))
        if tj_guidance:
            why.append("Trader Joe's may be cheaper for " + ", ".join(row["item"] for row in tj_guidance))

        ideas.append(
            {
                "id": template["id"],
                "title": template["title"],
                "inspiration_score": round(score, 1),
                "sale_components": sale_components,
                "missing_sale_components": missing_sale_components,
                "support_components": template["support_components"],
                "trader_joes_watchlist": tj_guidance,
                "method": template["method"],
                "why_this_week": why,
            }
        )

    ideas.sort(key=lambda row: (-row["inspiration_score"], row["title"]))
    if limit:
        return ideas[:limit]
    return ideas


def print_deals(args):
    path, metadata, rows, _prices = ranked_deals(
        args.deals_file,
        args.enrichment_file,
        args.no_api_base,
    )
    if args.role:
        rows = [row for row in rows if row["role"] == args.role]
    if args.limit:
        rows = rows[: args.limit]

    print(f"\nMeal-inspiration deal ranking from {path.name}")
    if metadata.get("valid_from") or metadata.get("valid_to"):
        print(f"Valid: {metadata.get('valid_from', '?')} to {metadata.get('valid_to', '?')}")
    print(
        f"{'Score':>6} {'Role':<8} {'Deal':<40} {'Sale':>8} {'Base':>8} {'Src':<5} Notes"
    )
    print("-" * 112)
    for row in rows:
        sale = money(row["sale_price"])
        if row["base_price"] is None:
            base = "n/a"
        elif row["comparable_to_base"]:
            base = money(row["base_price"])
        else:
            base = "unit n/a"
        notes = []
        if row["requires_clip"]:
            notes.append("clip")
        if row["freezer_friendly"]:
            notes.append("freezer")
        if row["savings"] is not None and row["savings"] > 0:
            notes.append(f"save {money(row['savings'])}")
        if row["base_item"] and row["base_item"] != row["name"]:
            notes.append(f"base: {row['base_item']}")
        notes.extend(row["why"][:2])
        source = "api" if row.get("base_source") == "weekly_deal_api" else "saved" if row.get("base_source") else ""
        print(
            f"{row['inspiration_score']:>6.1f} {row['role']:<8} "
            f"{row['name'][:40]:<40} {sale:>8} {base:>8} {source:<5} "
            f"{'; '.join(notes)}"
        )


def print_ideas(args):
    path, metadata, rows, prices = ranked_deals(
        args.deals_file,
        args.enrichment_file,
        args.no_api_base,
    )
    ideas = build_ideas(rows, prices, args.limit)
    print(f"\nMeal ideas from {path.name}")
    if metadata.get("valid_from") or metadata.get("valid_to"):
        print(f"Valid: {metadata.get('valid_from', '?')} to {metadata.get('valid_to', '?')}")
    for idea in ideas:
        print(f"\n{idea['title']}  [{idea['inspiration_score']:.1f}]")
        print("Sale ingredients: " + ", ".join(idea["sale_components"]))
        if idea["support_components"]:
            print("Support ingredients: " + ", ".join(idea["support_components"]))
        if idea["trader_joes_watchlist"]:
            parts = [
                f"{row['item']} TJ {money(row['trader_joes'])} vs Safeway {money(row['safeway'])}"
                for row in idea["trader_joes_watchlist"]
            ]
            print("TJ price notes: " + "; ".join(parts))
        print("Method: " + idea["method"])
        print("Why this week: " + "; ".join(idea["why_this_week"]))


def context_payload(args):
    path, metadata, rows, prices = ranked_deals(
        args.deals_file,
        args.enrichment_file,
        args.no_api_base,
    )
    top_deals = rows[: args.limit]
    ideas = build_ideas(rows, prices, args.ideas)
    giant = giant_circular_context(args, prices)
    payload = {
        "generated_on": date.today().isoformat(),
        "source_file": str(path),
        "metadata": metadata,
        "purpose": (
            "Meal inspiration from this week's Safeway deals. Rank sale proteins "
            "and produce by usefulness and novelty, not just absolute cheapest price."
        ),
        "selection_guidance": {
            "prefer": [
                "proteins or produce with high inspiration_score",
                "sale items that are freezer-friendly if not used immediately",
                "recipes that use multiple current weekly ad components",
                "Trader Joe's for support staples when saved prices show it is cheaper",
                "Giant Flipp deals when giant_circular has a stronger match than the Safeway base or weekly price",
            ],
            "do_not_assume": [
                "base savings when comparable base_price is null",
                "low-confidence API matches are valid base comparisons",
                "coupon discounts unless requires_clip/account state is separately confirmed",
                "Trader Joe's prices beyond saved observations",
                "Giant Flipp deals apply if multi_buy_qty thresholds are not met across the cart",
            ],
        },
        "ranked_deals": top_deals,
        "meal_idea_seeds": ideas,
    }
    if giant is not None:
        payload["giant_circular"] = giant
    return payload


def pricing_catalog(prices, rows, deal_limit):
    saved_items = []
    for name, item in sorted(prices.get("items", {}).items()):
        base_prices = item.get("base_prices") or {}
        saved_items.append(
            {
                "price_key": name,
                "kind": "saved_item",
                "category": item.get("category"),
                "planning_unit": item.get("unit"),
                "meal_tags": item.get("meal_tags") or [],
                "freezer_friendly": bool(item.get("freezer_friendly")),
                "known_store_prices": {
                    store: price
                    for store, price in base_prices.items()
                    if price is not None
                },
            }
        )

    deal_items = []
    for row in rows[:deal_limit]:
        deal_items.append(
            {
                "price_key": row["name"],
                "kind": "weekly_deal",
                "category": row["role"],
                "planning_unit": row["unit"],
                "safeway_sale_price": row["sale_price"],
                "requires_clip": row["requires_clip"],
                "freezer_friendly": row["freezer_friendly"],
                "promotion": row.get("promotion"),
                "inspiration_score": row["inspiration_score"],
                "base_price": row["base_price"] if row["comparable_to_base"] else None,
                "base_source": row["base_source"],
                "savings": row["savings"],
                "savings_pct": row["savings_pct"],
                "source_label": row["source_label"],
            }
        )

    return {
        "saved_items": saved_items,
        "weekly_deal_items": deal_items,
        "allowed_price_keys": sorted(
            {item["price_key"] for item in saved_items}
            | {item["price_key"] for item in deal_items}
        ),
    }


def slim_deal_row(row):
    return {
        "price_key": row["name"],
        "role": row["role"],
        "sale_price": row["sale_price"],
        "unit": row["unit"],
        "requires_clip": row["requires_clip"],
        "freezer_friendly": row["freezer_friendly"],
        "promotion": row.get("promotion"),
        "inspiration_score": row["inspiration_score"],
        "base_price": row["base_price"] if row["comparable_to_base"] else None,
        "base_source": row["base_source"],
        "savings": row["savings"],
        "savings_pct": row["savings_pct"],
        "source_label": row["source_label"],
        "why": row["why"],
    }


def giant_circular_context(args, prices):
    """Build Giant Flipp circular context for the inspiration prompt.

    Loads the most recent active Flipp flyer file and matches against the
    meal_prices items. When --expand-varieties is set, also calls Giant's
    live V5 search API through the browser session to expand each matched
    deal into the qualifying SKUs (specific flavors, sizes, prodIds).

    Returns None when:
    - --no-giant-deals is set
    - no active Flipp flyer file is on disk
    - the file exists but no deals match meal_prices items
    """
    if getattr(args, "no_giant_deals", False):
        return None

    deals_file = getattr(args, "giant_deals_file", None)
    deals_path, deals = load_giant_flipp_deals(deals_file)
    if deals is None:
        return None

    metadata = deals.get("metadata") or {}
    matches = match_giant_flipp_to_meal_items(
        prices.get("items", {}),
        deals,
        min_score=getattr(args, "giant_min_score", 0.5),
    )
    if not matches:
        return None

    expand = getattr(args, "expand_varieties", False)
    variety_limit = getattr(args, "variety_limit", 8)
    expansion_error = None
    expansion_helpers = None
    if expand:
        try:
            from giant_browser_api_probe import (
                browser_fetch,
                product_rows,
                summarize_product,
                wait_for_devtools,
            )
            from giant_flipp_deals import (
                build_giant_search_url,
                filter_qualifying_skus,
                summarize_qualifying_product,
                variety_query_for,
            )
            wait_for_devtools(getattr(args, "giant_port", 9227))
            expansion_helpers = {
                "browser_fetch": browser_fetch,
                "product_rows": product_rows,
                "summarize_product": summarize_product,
                "build_giant_search_url": build_giant_search_url,
                "filter_qualifying_skus": filter_qualifying_skus,
                "summarize_qualifying_product": summarize_qualifying_product,
                "variety_query_for": variety_query_for,
            }
        except Exception as exc:  # noqa: BLE001 - we want to skip gracefully
            expansion_error = str(exc)

    matched_deals = []
    for meal_key, match in matches.items():
        item = match["item"]
        meal_item = prices.get("items", {}).get(meal_key, {})
        deal_entry = {
            "meal_key": meal_key,
            "deal_name": item.get("name"),
            "brand": item.get("brand"),
            "description": item.get("description"),
            "price_display": item.get("price_display"),
            "current_price": item.get("current_price"),
            "planning_price": giant_flipp_planning_price(meal_item, item),
            "planning_unit": meal_item.get("unit"),
            "unit_kind": item.get("unit_kind"),
            "multi_buy_qty": item.get("multi_buy_qty"),
            "valid_from": item.get("valid_from"),
            "valid_to": item.get("valid_to"),
            "match_score": match.get("score"),
            "flipp_id": item.get("flipp_id"),
        }

        if expansion_helpers is not None:
            try:
                query = expansion_helpers["variety_query_for"](item)
                url = expansion_helpers["build_giant_search_url"](
                    query,
                    getattr(args, "giant_service_location_id", "50000732"),
                    getattr(args, "giant_user_id", "2"),
                    getattr(args, "giant_search_rows", 24),
                )
                response = expansion_helpers["browser_fetch"](
                    getattr(args, "giant_port", 9227), [url]
                )
                result = response["results"][0]
                if result.get("ok"):
                    raw_products = expansion_helpers["product_rows"](result.get("payload") or {})
                    products = [expansion_helpers["summarize_product"](p) for p in raw_products]
                    qualifying, _excluded = expansion_helpers["filter_qualifying_skus"](
                        products,
                        item,
                        getattr(args, "giant_price_tolerance", 0.10),
                    )
                    deal_entry["qualifying_count"] = len(qualifying)
                    deal_entry["qualifying_skus"] = [
                        expansion_helpers["summarize_qualifying_product"](p)
                        for p in qualifying[:variety_limit]
                    ]
                    deal_entry["search_query"] = query
                else:
                    deal_entry["expansion_error"] = (
                        f"browser fetch failed: status={result.get('status')}"
                    )
            except Exception as exc:  # noqa: BLE001
                deal_entry["expansion_error"] = str(exc)

        matched_deals.append(deal_entry)

    matched_deals.sort(key=lambda row: -(row.get("match_score") or 0))

    context = {
        "store": metadata.get("store_context") or {},
        "flyer_id": metadata.get("flyer_id"),
        "flyer_name": metadata.get("flyer_name"),
        "valid_from": metadata.get("valid_from"),
        "valid_to": metadata.get("valid_to"),
        "source_file": str(deals_path),
        "matched_deal_count": len(matched_deals),
        "matched_deals": matched_deals,
        "varieties_expanded": expand and expansion_helpers is not None,
    }
    if expansion_error:
        context["expansion_error"] = expansion_error
    return context


def prompt_payload(args):
    path, metadata, rows, prices = ranked_deals(
        args.deals_file,
        args.enrichment_file,
        args.no_api_base,
    )
    top_deals = [slim_deal_row(row) for row in rows[: args.limit]]
    giant = giant_circular_context(args, prices)
    payload = {
        "generated_on": date.today().isoformat(),
        "source_file": str(path),
        "metadata": metadata,
        "ranked_deals": top_deals,
        "pricing_catalog": pricing_catalog(prices, rows, args.catalog_limit),
        "pricing_return_contract": {
            "version": "meal_inspiration_plan_v1",
            "rules": [
                "Return valid JSON.",
                "pricing_catalog.allowed_price_keys is a price hint, not a menu. Any ingredient is allowed.",
                "When an ingredient IS in allowed_price_keys, prefer that exact price_key (it has a saved price).",
                "When an ingredient ISN'T in allowed_price_keys, use price_key=resolve:<snake_case_name>, source=Safeway, needs_price_resolution=true, plus quantity and unit. The local estimator resolves these live via Safeway's API.",
                "Use source Safeway for weekly_deal price_keys.",
                "Use source Trader Joe's only when known_store_prices includes Trader Joe's for that saved_item.",
                "Use source pantry for assumed on-hand seasonings, oil, vinegar, spices, and condiments; pantry items are not priced.",
                "Use source shared when an ingredient is bought once but split across multiple recipes (e.g. one 1 lb onion bag used in two dishes); the FIRST recipe lists it as source: Safeway with the full purchase quantity, and SUBSEQUENT recipes list it as source: shared with the local consumption quantity. Shared lines are documented in the recipe but billed once in the consolidated shopping list. Do not use quantity=0 as a workaround.",
                "Quantities must be in the planning_unit for the chosen price_key (or a sensible unit when resolving live).",
                "Use unpriced_items only for optional or truly unpriced items that should not be purchased or locally resolved.",
                "Treat weekly_deal promotion objects as hard pricing constraints.",
                "For promotion.type mix_and_match_min_count, the total quantity across all returned ingredients with the same promotion.group_id should be at least threshold_count, or the plan should explicitly say the deal may not apply.",
                "Lean toward dishes that aren't an obvious repeat of recently-suggested meals; novelty and exploration are valued over absolute lowest cost.",
            ],
            "schema": {
                "version": "meal_inspiration_plan_v1",
                "objective": "short description of the cooking direction",
                "recipes": [
                    {
                        "id": "snake_case_id",
                        "title": "Recipe title",
                        "meal_role": "dinner | lunch_prep | flexible",
                        "servings": 1,
                        "why_this_week": [
                            "Use specific deal names and discount signals from ranked_deals"
                        ],
                        "ingredients": [
                            {
                                "name": "Human ingredient label",
                                "price_key": "exact key from allowed_price_keys",
                                "source": "Safeway | Trader Joe's | Giant | pantry",
                                "quantity": 1.0,
                                "unit": "planning_unit copied from catalog",
                                "needs_price_resolution": False,
                                "notes": "optional"
                            }
                        ],
                        "unpriced_items": [
                            {
                                "name": "Ingredient not in catalog",
                                "reason": "why it is needed"
                            }
                        ],
                        "clip_required": [
                            "weekly deal price_keys that require clipping"
                        ],
                        "method_summary": [
                            "short practical cooking steps"
                        ],
                    }
                ],
                "shopping_notes": [
                    "store split, freezer notes, substitutions, or clipping reminders"
                ],
                "promotion_checks": [
                    {
                        "group_id": "promotion group id from weekly deal catalog",
                        "threshold_count": 5,
                        "planned_count": 5,
                        "status": "met | short | not_applicable",
                        "notes": "required for any mix-and-match multi-buy deals used"
                    }
                ],
            },
        },
    }
    if giant is not None:
        payload["giant_circular"] = giant
        contract_rules = payload["pricing_return_contract"]["rules"]
        contract_rules.append(
            "When a Giant Flipp deal in giant_circular.matched_deals fits a recipe better "
            "than the Safeway base/weekly price, you may use source Giant with price_key "
            "set to the matched meal_key. Use planning_price in the matching planning_unit, "
            "not raw current_price; respect multi_buy_qty thresholds the same way as Safeway "
            "weekly deals. Cite the qualifying SKU brand+size from qualifying_skus when "
            "specifying which variety to buy."
        )
    return payload


def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return slug or "ingredient"


def owned_ingredient_rows(args):
    rows = []
    if args.owned_json:
        raw = json.loads(Path(args.owned_json).read_text())
        if not isinstance(raw, list):
            raise SystemExit("--owned-json must contain a JSON list")
        for item in raw:
            if not isinstance(item, dict) or not item.get("name"):
                raise SystemExit("--owned-json entries must be objects with at least a name")
            name = item["name"]
            rows.append(
                {
                    "name": name,
                    "price_key": item.get("price_key") or f"owned:{slugify(name)}",
                    "quantity": item.get("quantity"),
                    "unit": item.get("unit"),
                    "notes": item.get("notes"),
                    "source": "owned",
                }
            )

    positional = list(args.ingredients or [])
    quantity = getattr(args, "quantity", None)
    unit = getattr(args, "unit", None)
    if (quantity is not None or unit) and len(positional) != 1:
        raise SystemExit("--quantity / --unit only apply when exactly one positional ingredient is given; use --owned-json for multi-ingredient quantities")

    for name in positional:
        rows.append(
            {
                "name": name,
                "price_key": f"owned:{slugify(name)}",
                "quantity": quantity,
                "unit": unit,
                "notes": None,
                "source": "owned",
            }
        )

    if not rows:
        raise SystemExit("Provide at least one owned ingredient")
    return rows


def use_up_prompt_payload(args):
    payload = prompt_payload(args)
    owned = owned_ingredient_rows(args)
    payload["purpose"] = (
        "Use the provided owned ingredients as the starting point, then use "
        "weekly deal inspiration rules to choose useful add-on ingredients."
    )
    payload["owned_ingredients"] = owned
    payload["pricing_catalog"]["owned_items"] = owned
    payload["pricing_catalog"]["allowed_price_keys"] = sorted(
        set(payload["pricing_catalog"]["allowed_price_keys"])
        | {item["price_key"] for item in owned}
    )
    rules = payload["pricing_return_contract"]["rules"]
    rules.insert(1, "Use every owned ingredient meaningfully unless it would make the dish incoherent; explain any skipped owned ingredient in shopping_notes.")
    rules.insert(2, "Owned ingredients must use source owned and an exact price_key from pricing_catalog.owned_items.")
    rules.insert(3, "Owned ingredients are already paid for and will be estimated at zero incremental cost.")
    ingredient_schema = payload["pricing_return_contract"]["schema"]["recipes"][0]["ingredients"][0]
    ingredient_schema["source"] = "Safeway | Trader Joe's | Giant | pantry | owned"
    payload["pricing_return_contract"]["schema"]["owned_ingredient_usage"] = [
        {
            "price_key": "owned ingredient price_key",
            "used_in_recipe_ids": ["recipe id"],
            "status": "used | partly_used | skipped",
            "notes": "how the owned ingredient was used or why skipped"
        }
    ]
    return payload


def prompt_markdown(args):
    payload = prompt_payload(args)
    return (
        "# Meal Inspiration Prompt\n\n"
        "You are helping plan meals from grounded grocery pricing data. "
        "Do not browse and do not invent prices. Use only the JSON input below.\n\n"
        "Goal: suggest meals that are interesting because this week's deals make "
        "specific proteins or produce unusually attractive, not meals that are "
        "only the absolute cheapest possible. Lean toward dishes I haven't seen "
        "before — the saved catalog is a price hint, NOT a constraint on what to cook.\n\n"
        "Return one JSON object matching `pricing_return_contract.schema`. "
        "The returned JSON must be directly usable by my local pricing estimator. "
        "Any ingredient is fair game — `pricing_catalog.allowed_price_keys` lists "
        "items I already have saved prices for, but you should treat it as a hint "
        "of what's known, not a menu of what to use. If a dish wants something "
        "that isn't in the allowlist, just include it in `ingredients` with "
        "`source: Safeway`, `price_key: resolve:<snake_case_name>`, "
        "`needs_price_resolution: true`, and a practical quantity/unit. The local "
        "estimator will resolve the price live via the Safeway search API at "
        "run time, so unknown ingredients are not a problem.\n\n"
        "Use `unpriced_items` only for pantry/optional items that should not be "
        "purchased or resolved. Surface any required clipping explicitly. "
        "Treat multi-buy promotions as hard constraints: if you use a weekly deal "
        "with a `promotion`, make sure the total planned quantity across all recipes "
        "meets the threshold, or call out that the deal may not apply.\n\n"
        "Prefer meals that use multiple high-ranked weekly deals, lean on novelty, "
        "and feel like a good excuse to try something this week. Use Trader Joe's "
        "for support ingredients only when the saved catalog says Trader Joe's "
        "has a known price advantage.\n\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
        "```\n"
    )


def use_up_prompt_markdown(args):
    payload = use_up_prompt_payload(args)

    owned_phrases = []
    for item in payload["owned_ingredients"]:
        qty, unit, name = item.get("quantity"), item.get("unit"), item["name"]
        if qty is not None and unit:
            qty_str = f"{int(qty)}" if isinstance(qty, (int, float)) and float(qty).is_integer() else f"{qty}"
            owned_phrases.append(f"{qty_str} {unit} of {name}")
        elif qty is not None:
            owned_phrases.append(f"{qty} {name}")
        else:
            owned_phrases.append(name)
    owned_phrase = ", ".join(owned_phrases)

    portions = getattr(args, "portions", None)
    single_dish = getattr(args, "single_dish", False)
    meal_prep = getattr(args, "meal_prep", False)

    portions_clause = f" yielding exactly {portions} portions" if portions else ""
    dish_clause = "Build one coherent dish" if single_dish else "Build one or more coherent dishes"
    prep_clause = (
        " The dish must be meal-preppable: cooked once, refrigerator-stable for the week, "
        "and reheat-friendly. Avoid raw salads or texture-fragile components that wilt."
        if meal_prep else ""
    )

    return (
        "# Use-Up Meal Inspiration Prompt\n\n"
        "You are helping plan a dish from grounded grocery pricing data and "
        "ingredients I already have. Do not browse and do not invent prices. "
        "Use only the JSON input below.\n\n"
        f"Goal: use up these owned ingredients: {owned_phrase}. "
        f"{dish_clause} around them{portions_clause}, using this week's unusually strong "
        "Safeway deals as add-ons where they make the dish better or more interesting."
        f"{prep_clause}\n\n"
        "Return one JSON object matching `pricing_return_contract.schema`. "
        "Every owned ingredient must appear as an ingredient with `source` set "
        "to `owned` and an exact `price_key` from `pricing_catalog.owned_items`, "
        "unless you explicitly mark it skipped in `owned_ingredient_usage`. "
        "Owned ingredients are zero incremental cost. For purchased ingredients, "
        "any item is fair game — `pricing_catalog.allowed_price_keys` lists "
        "items I already have saved prices for, but you should treat it as a hint "
        "of what's known, not a menu of what to use. If a dish wants something "
        "that isn't in the allowlist, include it in `ingredients` with "
        "`source: Safeway`, `price_key: resolve:<snake_case_name>`, "
        "`needs_price_resolution: true`, and quantity/unit. The local estimator "
        "will resolve the price live via Safeway's API at run time. "
        "Use `unpriced_items` only for pantry or optional items that should not "
        "be purchased or resolved. Surface clipping and multi-buy constraints "
        "explicitly.\n\n"
        "Prefer ideas that feel like a smart use-up meal rather than a generic "
        "weekly-deal meal, and lean toward dishes that aren't an obvious repeat. "
        "The owned ingredient should shape the dish.\n\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
        "```\n"
    )


def write_use_up_prompt(args):
    text = use_up_prompt_markdown(args)
    if args.write:
        output = Path(args.output) if args.output else DEFAULT_USE_UP_PROMPT_FILE
        output.write_text(text)
        print(f"Wrote {output}")
    else:
        print(text)


def write_prompt(args):
    text = prompt_markdown(args)
    if args.write:
        output = Path(args.output) if args.output else DEFAULT_PROMPT_FILE
        output.write_text(text)
        print(f"Wrote {output}")
    else:
        print(text)


def write_context(args):
    payload = context_payload(args)
    if args.write:
        output = Path(args.output) if args.output else DEFAULT_CONTEXT_FILE
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"Wrote {output}")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def add_source_args(parser):
    parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    parser.add_argument("--enrichment-file", help="API base-distance observation JSON; defaults to matching safeway_weekly_deal_base_observations_YYYY-MM-DD.json")
    parser.add_argument("--no-api-base", action="store_true", help="Ignore API base-distance observations and use saved meal_prices.json bases only")
    parser.add_argument("--giant-deals-file", help="Giant Flipp deals JSON file; defaults to active dated giant_weekly_deals_*.json")
    parser.add_argument("--no-giant-deals", action="store_true", help="Skip Giant Flipp circular context")
    parser.add_argument("--giant-min-score", type=float, default=0.5, help="Minimum token-overlap score for Giant deal matches")
    parser.add_argument("--expand-varieties", action="store_true", help="Expand each matched Giant deal into qualifying SKUs (requires browser session)")
    parser.add_argument("--variety-limit", type=int, default=8, help="Max qualifying SKUs to include per Giant deal when expanding")
    parser.add_argument("--giant-port", type=int, default=9227, help="Chrome DevTools port for variety expansion")
    parser.add_argument("--giant-service-location-id", default="50000732", help="Giant service location ID for variety expansion")
    parser.add_argument("--giant-user-id", default="2", help="Giant API user ID for variety expansion")
    parser.add_argument("--giant-search-rows", type=int, default=24, help="Giant API search rows for variety expansion")
    parser.add_argument("--giant-price-tolerance", type=float, default=0.10, help="Price tolerance for matching SKUs to the Giant deal price")


def cmd_varieties(args):
    """Enumerate stocked Safeway SKUs for a deal name (or free-form query).

    Used to answer "which specific Signature SELECT pasta shapes are
    actually available, and which qualify for the buy-5+ deal price?"
    without leaving the planning toolset. The deal-match column flags
    SKUs whose per-unit price matches the deal's sale_price within
    $0.05.
    """
    from safeway_api_search import (  # local import to keep top deps slim
        DEFAULT_BANNER as SW_BANNER,
        DEFAULT_CHANNEL as SW_CHANNEL,
        DEFAULT_STORE_ID as SW_STORE,
        fetch_search,
    )

    sale_price = None
    deal = None
    query = args.query
    deal_name_tokens = set()
    promo_gated = False
    if args.deal:
        path, _, normalized = deal_rows(args.deals_file)
        if args.deal in normalized:
            deal = normalized[args.deal]
            sale_price = deal.get("sale_price")
            promo_gated = bool(deal.get("promotion"))
            # For promo-gated deals (buy-5+, etc.), the deal price isn't
            # reflected in the SKU's pricePer (it triggers at checkout when
            # the threshold is met). Match on name tokens instead.
            deal_name_tokens = {
                t for t in re.findall(r"[a-z0-9]+", args.deal.lower())
                if len(t) > 2
            }
        else:
            print(f"Deal '{args.deal}' not in {path.name}.", file=sys.stderr)
            close = [k for k in normalized.keys() if args.deal.lower() in k.lower() or k.lower() in args.deal.lower()]
            if close:
                print(f"Did you mean: {', '.join(close)}", file=sys.stderr)
            return 2
        if not query:
            query = args.deal

    if not query:
        print("Provide --deal <name> or --query <text>.", file=sys.stderr)
        return 2

    try:
        payload = fetch_search(query, args.store_id or SW_STORE, args.rows, 0,
                               args.banner or SW_BANNER, args.channel or SW_CHANNEL, args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"Safeway API error: {exc}", file=sys.stderr)
        return 2

    docs = payload.get("response", {}).get("docs", []) or []
    if not docs:
        print(f"No SKUs returned for {query!r}")
        return 0

    print(f"Safeway varieties for {query!r} (store {args.store_id or SW_STORE})")
    if deal:
        print(f"  Deal: {args.deal}, sale ${sale_price}/{deal.get('unit')}; SKUs flagged DEAL match the deal price within $0.05")
    print(f"\n{'PID':<14} {'Price':>7} {'Per':>7} {'Unit':<6} {'Pack':<7} {'Inv':<4} {'Match':<6} Name")
    print("-" * 130)
    deal_match_count = 0
    in_stock_count = 0
    for doc in docs[: args.rows]:
        pid = str(doc.get("id") or doc.get("upc") or "")
        price = doc.get("price")
        per = doc.get("pricePer")
        unit = (doc.get("unitQuantity") or doc.get("unitOfMeasure") or "")[:6]
        avg = doc.get("averageWeight")
        avg_val = ""
        if isinstance(avg, list) and avg:
            try:
                avg_val = f"{float(avg[0]):g}"
            except (TypeError, ValueError):
                avg_val = ""
        inv = "yes" if str(doc.get("inventoryAvailable") or "").lower() == "1" or doc.get("inventoryAvailable") == 1 else "no"
        if inv == "yes":
            in_stock_count += 1
        match = ""
        # For promo-gated deals, flag SKUs whose name tokens cover the deal
        # name (these qualify for the deal price at checkout when the
        # promo threshold is met).
        if promo_gated and deal_name_tokens:
            sku_tokens = {
                t for t in re.findall(r"[a-z0-9]+", (doc.get("name") or "").lower())
                if len(t) > 2
            }
            if deal_name_tokens.issubset(sku_tokens):
                match = "DEAL"
                deal_match_count += 1
        # For weight-priced deals (e.g. pork chops $1.99/lb on a 4 lb
        # value pack), the deal price is reflected in pricePer.
        elif sale_price is not None and per is not None:
            try:
                if abs(float(per) - float(sale_price)) <= 0.05:
                    match = "DEAL"
                    deal_match_count += 1
            except (TypeError, ValueError):
                pass
        name = (doc.get("name") or "")[:60]
        price_str = f"${price:.2f}" if isinstance(price, (int, float)) else "?"
        per_str = f"${per:.2f}" if isinstance(per, (int, float)) else "?"
        print(f"{pid:<14} {price_str:>7} {per_str:>7} {unit:<6} {avg_val:<7} {inv:<4} {match:<6} {name}")

    if deal:
        print(f"\nDeal-priced SKUs: {deal_match_count} / {len(docs[:args.rows])} candidates ({in_stock_count} in stock)")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Rank Safeway weekly ad deals for meal inspiration."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    deals_parser = sub.add_parser("deals", help="Rank current deals by meal-inspiration value")
    add_source_args(deals_parser)
    deals_parser.add_argument("--limit", type=int, default=20)
    deals_parser.add_argument("--role", choices=["protein", "produce", "pantry", "beverage", "other"])
    deals_parser.set_defaults(func=print_deals)

    ideas_parser = sub.add_parser("ideas", help="Generate deterministic meal ideas from current deals")
    add_source_args(ideas_parser)
    ideas_parser.add_argument("--limit", type=int, default=8)
    ideas_parser.set_defaults(func=print_ideas)

    context_parser = sub.add_parser("context", help="Emit JSON context for a future chat/LLM workflow")
    add_source_args(context_parser)
    context_parser.add_argument("--limit", type=int, default=20, help="Number of ranked deals to include")
    context_parser.add_argument("--ideas", type=int, default=8, help="Number of meal ideas to include")
    context_parser.add_argument("--write", action="store_true", help="Write context JSON to disk")
    context_parser.add_argument("--output", help="Output path when using --write")
    context_parser.set_defaults(func=write_context)

    prompt_parser = sub.add_parser("prompt", help="Export a paste-ready prompt for another chat instance")
    add_source_args(prompt_parser)
    prompt_parser.add_argument("--limit", type=int, default=20, help="Number of ranked deals to include")
    prompt_parser.add_argument("--catalog-limit", type=int, default=24, help="Number of weekly deal price keys to expose")
    prompt_parser.add_argument("--write", action="store_true", help="Write prompt Markdown to disk")
    prompt_parser.add_argument("--output", help="Output path when using --write")
    prompt_parser.set_defaults(func=write_prompt)

    use_up_parser = sub.add_parser("use-up-prompt", help="Export a prompt that uses owned ingredients plus weekly deal inspiration")
    add_source_args(use_up_parser)
    use_up_parser.add_argument("ingredients", nargs="*", help="Owned ingredients to use up, e.g. 'ground beef' 'artichokes'")
    use_up_parser.add_argument("--owned-json", help="Optional JSON list of owned ingredient objects with name/quantity/unit")
    use_up_parser.add_argument("--quantity", type=float, help="Quantity for the (single) positional ingredient (use --owned-json for multi-ingredient quantities)")
    use_up_parser.add_argument("--unit", help="Unit for --quantity, e.g. 'lb', 'oz', 'package'")
    use_up_parser.add_argument("--portions", type=int, help="Portion count the dish should yield")
    use_up_parser.add_argument("--single-dish", action="store_true", help="Ask for one coherent dish rather than several alternatives")
    use_up_parser.add_argument("--meal-prep", action="store_true", help="Frame the dish as meal-preppable: refrigerator-stable, reheat-friendly")
    use_up_parser.add_argument("--limit", type=int, default=20, help="Number of ranked deals to include")
    use_up_parser.add_argument("--catalog-limit", type=int, default=24, help="Number of weekly deal price keys to expose")
    use_up_parser.add_argument("--write", action="store_true", help="Write prompt Markdown to disk")
    use_up_parser.add_argument("--output", help="Output path when using --write")
    use_up_parser.set_defaults(func=write_use_up_prompt)

    varieties_parser = sub.add_parser(
        "varieties",
        help="Enumerate stocked Safeway SKUs for a deal (or free-form query); flags SKUs that match the deal price",
    )
    varieties_parser.add_argument("--deal", help="Deal key from normalized_deals (e.g. 'signature select pasta'); auto-flags deal-priced SKUs")
    varieties_parser.add_argument("--query", help="Free-form search query (defaults to --deal name)")
    varieties_parser.add_argument("--deals-file", help="Weekly deals JSON file; defaults to active dated weekly_deals*.json")
    varieties_parser.add_argument("--rows", type=int, default=15)
    varieties_parser.add_argument("--store-id", default=None)
    varieties_parser.add_argument("--banner", default=None)
    varieties_parser.add_argument("--channel", default=None)
    varieties_parser.add_argument("--timeout", type=float, default=15.0)
    varieties_parser.set_defaults(func=cmd_varieties)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
