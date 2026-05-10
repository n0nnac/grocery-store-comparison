#!/usr/bin/env python3
"""Aggregate Giant Food coupons relevant to saved meal items.

Three sources combine into a single `giant_coupons.json` catalog:

1. The storewide v7 search endpoint paginates over the full ~3,000 catalog
   when given the right body shape:

       POST /api/v7.0/coupons/users/{user}/prism/service-locations/{loc}
            /coupons/search?fullDocument=true&unwrap=true
       body: {"query": {"start": N, "size": 90}, ...}

   `start`/`size` MUST be nested under `query` — top-level pagination keys
   are silently ignored. The server caps `size` at 90.

2. The per-product `availableDisplayCoupons` array on the v5 product detail
   endpoint, walked via `giant_refresh_prices.py` saved Giant product IDs.
   This is much richer: each saved meal-item product carries 4-5 coupons
   directly relevant to it, plus their full discount/scope metadata.

3. The `scope` subcommand back-fills authoritative qualifying-product SKUs
   for ITEM-target coupons via:

       GET /api/v5.0/products/{user}/{loc}?couponId={id}
           &start=0&rows=200&sort=bestMatch+asc&flags=true

   This is the same call the savings page's "View Coupon Details" modal
   uses to render its "Qualifying Products" grid. The v7 search response
   leaves `productIds` empty for every coupon; this endpoint is the one
   that surfaces the per-coupon SKU list.

The aggregated catalog is keyed by coupon id and back-references the meal
items the coupon applies to. Subcommands let you search the catalog,
resolve qualifying-product SKUs, or match coupons against `meal_prices.json`.
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
ACCOUNT_STATE_FILE = ROOT / "giant_coupon_account_state.local.json"
MEAL_PRICES_FILE = ROOT / "meal_prices.json"

API_BASE = "/api/v7.0"
SOURCE_TYPE = "giant_coupon_v7_api"

DEFAULT_ACCOUNT_STATE = {
    "clipped": None,
    "loaded": None,
    "loadable": None,
}


def load_json(path):
    with path.open() as f:
        return json.load(f)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def fetch_product_detail(port, user_id, service_location_id, product_id, timeout):
    """Fetch a single Giant product detail to capture its availableDisplayCoupons."""
    url = (
        f"/api/v5.0/products/info/{user_id}/{service_location_id}/{product_id}"
        "?extendedInfo=true&flags=true&substitute=true"
    )
    expression = f"""
    (async () => {{
      const response = await fetch({json.dumps(url)}, {{
        method: "GET",
        credentials: "include",
      }});
      const text = await response.text();
      let payload = null;
      try {{ payload = JSON.parse(text); }} catch (e) {{}}
      return {{
        status: response.status,
        ok: response.ok,
        payload,
        text: payload ? null : text.slice(0, 400),
      }};
    }})()
    """
    return evaluate_in_giant_page(port, expression)


def fetch_coupon_page(port, user_id, service_location_id, start, size, timeout, source_systems=None, loadable=None, loaded=None, sort_targeted=False, targeting_enabled=False):
    """POST one page of the coupon catalog through the browser tab.

    The Giant savings page uses a structured body where pagination params
    are nested under `query`. Top-level `start`/`rows` are silently ignored.
    The server caps `size` at 90 even when a larger value is requested.

    Two flags shape the result scope:

    - `targeting_enabled=True` (the page's default) restricts results to
      targeted/personalized coupons for the user; total reduces from
      ~3,051 to ~257.
    - `sort_targeted=True` sorts targeted-first; safe to include without
      restricting scope, but we leave it off by default to keep the
      response stable for catalog mirroring.
    """
    url = (
        f"{API_BASE}/coupons/users/{user_id}/prism"
        f"/service-locations/{service_location_id}"
        f"/coupons/search?fullDocument=true&unwrap=true"
    )
    body = {"query": {"start": start, "size": size}}
    if targeting_enabled:
        body["copientQuotientTargetingEnabled"] = True
    filter_block = {}
    if source_systems:
        filter_block["sourceSystems"] = list(source_systems)
    if loadable is not None:
        filter_block["loadable"] = loadable
    if loaded is not None:
        filter_block["loaded"] = loaded
    if filter_block:
        body["filter"] = filter_block
    if sort_targeted:
        body["sorts"] = [{"targeted": "desc"}]

    expression = f"""
    (async () => {{
      const response = await fetch({json.dumps(url)}, {{
        method: "POST",
        credentials: "include",
        headers: {{"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}},
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
        "account_state": dict(DEFAULT_ACCOUNT_STATE),
    }


def extract_account_state(raw):
    """Pull only the per-user clipped/loaded fields off a raw coupon."""
    return {
        "clipped": raw.get("clipped"),
        "loaded": raw.get("loaded"),
        "loadable": raw.get("loadable"),
    }


def normalize_display_coupon(raw):
    """Normalize a per-product `availableDisplayCoupons` entry.

    Per-product display coupons carry a thinner schema than the v7 search
    response — no productIds/categoryTreeIds — but they include source
    system, dates, max discount, and clipping metadata, which is the
    pricing info we need.
    """
    return {
        "id": raw.get("id"),
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
        "multi_qty": raw.get("multiQty"),
        "manufacturer_coupon": raw.get("manufacturerCoupon"),
        "targeted": raw.get("targeted"),
        "personalized_offer": raw.get("personalizedOffer"),
        "clipping_required": raw.get("clippingRequired"),
        "category_tree_id": raw.get("categoryTreeId"),
        "category_tree_name": raw.get("categoryTreeName"),
        "top_category_tree_id": raw.get("topCategoryTreeId"),
        "top_category_tree_name": raw.get("topCategoryTreeName"),
        "category_tree_ids": [],
        "product_ids": [],
        "brand_ids": [],
        "pod_group_ids": [],
        "consumer_category_id": [],
        "coupon_channels": [],
        "image_url": None,
        "external_image": raw.get("externalImage"),
        "legal_text": None,
        "badge_ids": [],
        "account_state": dict(DEFAULT_ACCOUNT_STATE),
    }


def merge_coupon_records(existing, incoming):
    """Merge a richer record over a thinner one without losing data."""
    if existing is None:
        return dict(incoming)
    merged = dict(existing)
    for key, value in incoming.items():
        # Prefer non-empty / non-null incoming values for fields that are
        # often missing on per-product display coupons.
        if value in (None, "", [], {}):
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def iter_giant_product_refs():
    """Yield (meal_key, product_id, brand) for saved Giant product IDs."""
    if not MEAL_PRICES_FILE.exists():
        return
    data = load_json(MEAL_PRICES_FILE)
    for meal_key, item in (data.get("items") or {}).items():
        source = (item.get("price_sources") or {}).get("Giant") or {}
        product_id = source.get("product_id")
        if not product_id:
            continue
        yield meal_key, str(product_id), source.get("brand")


def fetch_full_catalog(port, user_id, service_location_id, page_size, max_pages, timeout, sleep, source_systems=None, loadable=None, loaded=None, targeting_enabled=False):
    """Fetch the full coupon catalog by paginating with the real body shape.

    The page sends `{query: {start, size}, filter, sorts, ...}` rather than
    top-level `start`/`rows`. The server returns a real `paging.size` (often
    capped at 90 even when a larger size is requested). We increment `start`
    by the actual returned size each iteration.
    """
    coupons = []
    account_state = {}
    facets = None
    paging_total = None
    seen_ids = set()
    start = 0

    for _ in range(max_pages):
        result = fetch_coupon_page(
            port,
            user_id,
            service_location_id,
            start,
            page_size,
            timeout,
            source_systems=source_systems,
            loadable=loadable,
            loaded=loaded,
            targeting_enabled=targeting_enabled,
        )
        if not result.get("ok"):
            raise GiantBrowserError(
                f"Coupon search failed at start={start}: status={result.get('status')} "
                f"text={result.get('text') or '(no body)'}"
            )
        payload = result.get("payload") or {}
        page_coupons = payload.get("coupons") or []
        paging = payload.get("paging") or {}
        if facets is None:
            facets = payload.get("facets")
        if paging_total is None:
            paging_total = paging.get("total")
        returned = paging.get("size") or len(page_coupons)

        added = 0
        for raw in page_coupons:
            coupon_id = raw.get("id")
            if not coupon_id or coupon_id in seen_ids:
                continue
            seen_ids.add(coupon_id)
            coupons.append(normalize_coupon(raw))
            account_state[coupon_id] = extract_account_state(raw)
            added += 1

        if not page_coupons or added == 0:
            break
        if paging_total is not None and len(coupons) >= paging_total:
            break

        # Increment by the real returned page size rather than what we asked for.
        start += returned if returned else len(page_coupons)
        if sleep:
            time.sleep(sleep)

    return coupons, account_state, facets, paging_total


def collect_per_product_coupons(port, user_id, service_location_id, timeout, sleep, only_keys=None):
    """Walk saved Giant product IDs and harvest each product's display coupons."""
    refs = list(iter_giant_product_refs())
    if only_keys:
        only = {k.lower() for k in only_keys}
        refs = [r for r in refs if r[0].lower() in only]

    coupons_by_id = {}
    coupon_to_meal_keys = {}
    coupon_to_product_ids = {}
    account_state = {}
    failures = []

    for index, (meal_key, product_id, _brand) in enumerate(refs):
        try:
            result = fetch_product_detail(port, user_id, service_location_id, product_id, timeout)
        except GiantBrowserError as exc:
            failures.append({"meal_key": meal_key, "product_id": product_id, "error": str(exc)})
            continue
        if not result.get("ok"):
            failures.append({
                "meal_key": meal_key,
                "product_id": product_id,
                "status": result.get("status"),
                "text": result.get("text"),
            })
            continue
        payload = result.get("payload") or {}
        products = ((payload.get("response") or {}).get("products")) or []
        if not products:
            continue
        product = products[0]
        coupons = product.get("availableDisplayCoupons") or []
        for raw in coupons:
            cid = raw.get("id")
            if not cid:
                continue
            normalized = normalize_display_coupon(raw)
            coupons_by_id[cid] = merge_coupon_records(coupons_by_id.get(cid), normalized)
            coupon_to_meal_keys.setdefault(cid, set()).add(meal_key)
            coupon_to_product_ids.setdefault(cid, set()).add(str(product_id))
            account_state[cid] = extract_account_state(raw)
        if sleep and index < len(refs) - 1:
            time.sleep(sleep)

    return coupons_by_id, coupon_to_meal_keys, coupon_to_product_ids, account_state, failures


def fetch_coupon_scope_page(port, user_id, service_location_id, coupon_id, start, rows, timeout):
    """Fetch one page of qualifying products for a single coupon.

    Hits `/api/v5.0/products/{user}/{loc}?couponId={id}&start={start}&rows={rows}`,
    the same endpoint the savings-page "View Coupon Details" modal uses to
    render its qualifying-products grid. Returns the raw payload object the
    browser sees.
    """
    url = (
        f"/api/v5.0/products/{user_id}/{service_location_id}"
        f"?couponId={coupon_id}&start={start}&rows={rows}&sort=bestMatch+asc&flags=true"
    )
    expression = f"""
    (async () => {{
      const response = await fetch({json.dumps(url)}, {{
        method: "GET",
        credentials: "include",
      }});
      const text = await response.text();
      let payload = null;
      try {{ payload = JSON.parse(text); }} catch (e) {{}}
      return {{
        status: response.status,
        ok: response.ok,
        payload,
        text: payload ? null : text.slice(0, 400),
      }};
    }})()
    """
    return evaluate_in_giant_page(port, expression)


def fetch_coupon_scope_products(port, user_id, service_location_id, coupon_id, page_size, max_pages, timeout, sleep):
    """Walk paginated qualifying products for one coupon.

    Returns ``(products, total, error_or_None)``. Each product entry carries
    ``prod_id`` (string), ``name``, ``brand``, ``size``.
    """
    products = []
    total = None
    start = 0
    for _ in range(max_pages):
        result = fetch_coupon_scope_page(
            port, user_id, service_location_id, coupon_id, start, page_size, timeout
        )
        if not result.get("ok"):
            return products, total, {
                "status": result.get("status"),
                "text": result.get("text"),
                "start": start,
            }
        payload = result.get("payload") or {}
        resp = payload.get("response") or {}
        page_products = resp.get("products") or []
        for p in page_products:
            pid = p.get("prodId")
            if pid is None:
                continue
            products.append({
                "prod_id": str(pid),
                "name": p.get("name"),
                "brand": p.get("brand"),
                "size": p.get("size"),
            })
        pagination = resp.get("pagination") or {}
        total = pagination.get("total") if pagination.get("total") is not None else total
        if total is not None and len(products) >= total:
            break
        if not page_products:
            break
        start += page_size
        if sleep:
            time.sleep(sleep)
    return products, total, None


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

    # Source 1: storewide v7 search (capped at ~20 first results in practice).
    storewide_total = None
    storewide_count = 0
    coupons_by_id = {}
    coupon_meal_keys = {}
    coupon_product_ids = {}
    account_state = {}
    facets = None

    if not args.no_storewide:
        store_coupons, store_account_state, facets, paging_total = fetch_full_catalog(
            args.port,
            args.user_id,
            args.service_location_id,
            args.page_size,
            args.max_pages,
            args.timeout,
            args.sleep,
            source_systems=args.source_systems or None,
            loadable=args.loadable_only or None,
            loaded=False if args.unloaded_only else None,
            targeting_enabled=args.targeting_only,
        )
        storewide_total = paging_total
        seen = set()
        for coupon in store_coupons:
            cid = coupon.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            coupons_by_id[cid] = coupon
        storewide_count = len(seen)
        for cid, state in store_account_state.items():
            account_state[cid] = state

    # Source 2: per-product display coupons across saved Giant product IDs.
    per_product_count = 0
    per_product_failures = []
    if not args.no_per_product:
        product_coupons, p_meal_keys, p_product_ids, p_account_state, per_product_failures = (
            collect_per_product_coupons(
                args.port,
                args.user_id,
                args.service_location_id,
                args.timeout,
                args.sleep,
                only_keys=args.only,
            )
        )
        per_product_count = len(product_coupons)
        for cid, coupon in product_coupons.items():
            coupons_by_id[cid] = merge_coupon_records(coupons_by_id.get(cid), coupon)
            coupon_meal_keys.setdefault(cid, set()).update(p_meal_keys.get(cid, set()))
            coupon_product_ids.setdefault(cid, set()).update(p_product_ids.get(cid, set()))
        for cid, state in p_account_state.items():
            existing = account_state.get(cid) or {}
            merged_state = {key: state.get(key) if state.get(key) is not None else existing.get(key) for key in DEFAULT_ACCOUNT_STATE}
            account_state[cid] = merged_state

    # Sort coupons by end_date for stable output.
    coupons = list(coupons_by_id.values())
    coupons.sort(key=lambda c: ((c.get("end_date") or "9999")[:10], c.get("name") or ""))

    # Attach back-references to meal items / products that surfaced each coupon.
    for coupon in coupons:
        cid = coupon["id"]
        coupon["matched_meal_keys"] = sorted(coupon_meal_keys.get(cid, set()))
        coupon["matched_product_ids"] = sorted(coupon_product_ids.get(cid, set()))

    payload = {
        "metadata": {
            "fetched_on": date.today().isoformat(),
            "source_type": SOURCE_TYPE,
            "store_context": PARK_ROAD_CONTEXT,
            "service_location_id": str(args.service_location_id),
            "storewide_endpoint": API_BASE + "/coupons/users/{user}/prism/service-locations/{loc}/coupons/search",
            "storewide_paging_total": storewide_total,
            "storewide_returned": storewide_count,
            "per_product_count": per_product_count,
            "per_product_failures": per_product_failures,
            "fetched_count": len(coupons),
            "account_state_storage": (
                f"Account-specific clipped/loaded state belongs in ignored {ACCOUNT_STATE_FILE.name}."
            ),
            "notes": [
                "Combined catalog: storewide v7 search plus per-product display coupons.",
                "The v7 storewide search paginates correctly when query.start/size are nested under `query`; server caps size at 90.",
                "Per-product coupons are harvested by walking saved Giant product IDs in meal_prices.json.",
                "Each coupon record carries matched_meal_keys / matched_product_ids back-references when relevant.",
                "Authoritative product_ids for ITEM-target coupons come from the `scope` subcommand, which hits /api/v5.0/products?couponId=...",
                "Account-state fields are sanitized in this tracked file. Real per-account state lives in the local file.",
            ],
        },
        "facets": facets,
        "coupons": coupons,
    }

    clipped_count = sum(1 for state in account_state.values() if state.get("clipped"))
    loaded_count = sum(1 for state in account_state.values() if state.get("loaded"))
    account_payload = {
        "metadata": {
            "captured_on": date.today().isoformat(),
            "service_location_id": str(args.service_location_id),
            "user_id": str(args.user_id),
            "source_type": "giant_coupon_account_state_v7_plus_v5",
            "clipped_count": clipped_count,
            "loaded_count": loaded_count,
            "coupon_count": len(account_state),
            "notes": [
                "Per-user clipped/loaded/loadable state for Giant coupons.",
                "This file is gitignored; do not commit it.",
            ],
        },
        "by_id": account_state,
    }

    print(
        f"Storewide: {storewide_count} unique coupons (paging.total={storewide_total or '?'}; server caps page size at 90)"
    )
    print(f"Per-product: {per_product_count} unique coupons across saved Giant products")
    print(f"Combined: {len(coupons)} unique coupons total")
    if per_product_failures:
        print(f"Per-product failures: {len(per_product_failures)} (see metadata.per_product_failures)")
    print(f"Account state: {clipped_count} clipped, {loaded_count} loaded across {len(account_state)} entries")
    if args.write:
        write_json(COUPONS_FILE, payload)
        print(f"Wrote {COUPONS_FILE.name}")
        write_json(ACCOUNT_STATE_FILE, account_payload)
        print(f"Wrote {ACCOUNT_STATE_FILE.name} (gitignored)")
    else:
        print("Mode: dry-run; pass --write to save")
    return 0


DESCRIPTOR_TOKENS = {
    "boneless", "skinless", "raw", "fresh", "frozen", "ground", "diced",
    "shredded", "lean", "fat", "free", "low", "reduced", "natural",
}


def _tokens(text):
    return {t for t in re.findall(r"[a-z]+", (text or "").lower()) if len(t) > 2}


def _meal_anchor_tokens():
    """Build the union of anchor tokens across every meal_prices.json item.

    Anchor tokens are all alphabetic 3+-letter substrings of the meal key,
    category, and tags, minus generic descriptors (`fresh`, `lean`, etc.).
    Used by `--relevant-only` to pre-filter coupons whose name/description
    can plausibly apply to at least one tracked meal item.
    """
    if not MEAL_PRICES_FILE.exists():
        return set()
    data = load_json(MEAL_PRICES_FILE)
    items = data.get("items") or {}
    tokens = set()
    for meal_key, item in items.items():
        blob = " ".join([
            meal_key,
            item.get("category") or "",
            " ".join(item.get("meal_tags") or []),
            item.get("unit") or "",
        ])
        tokens.update(_tokens(blob))
    return tokens - DESCRIPTOR_TOKENS


def _coupon_is_relevant(coupon, meal_tokens):
    blob = " ".join(filter(None, [coupon.get("name"), coupon.get("description")]))
    return bool(_tokens(blob) & meal_tokens)


def scope_warmup(port, settle_seconds=8.0):
    """Reload the Giant savings page tab via CDP, then wait for it to settle.

    Mimics the natural "user opened savings page" flow before we start
    hammering the per-coupon scope endpoint. Returns (ok, message).
    """
    try:
        from giant_browser_api_probe import find_giant_page  # local import to avoid cycle
    except ImportError as exc:
        return False, f"could not import find_giant_page: {exc}"
    try:
        page = find_giant_page(port)
    except GiantBrowserError as exc:
        return False, str(exc)

    try:
        import websocket  # type: ignore
    except ImportError:
        return False, "websocket-client not installed"

    try:
        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"],
            timeout=15,
            origin=f"http://127.0.0.1:{port}",
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic preserved
        return False, f"websocket connect failed: {exc}"

    try:
        ws.send(json.dumps({"id": 1, "method": "Page.enable", "params": {}}))
        ws.send(json.dumps({"id": 2, "method": "Page.reload", "params": {"ignoreCache": True}}))
        ws.settimeout(2.0)
        try:
            for _ in range(6):
                ws.recv()
        except Exception:  # noqa: BLE001 - drain best-effort
            pass
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if settle_seconds:
        time.sleep(settle_seconds)
    return True, f"reloaded and waited {settle_seconds:.0f}s"


def scope_preflight(port, user_id, service_location_id, coupon_id, timeout):
    """Single test call to /api/v5.0/products?couponId=... to detect a DataDome
    block before starting the sweep. Returns (ok, message).

    `coupon_id` should be a real coupon ID; if the response carries products,
    the session is healthy.
    """
    try:
        result = fetch_coupon_scope_page(
            port, user_id, service_location_id, coupon_id,
            start=0, rows=1, timeout=timeout,
        )
    except GiantBrowserError as exc:
        return False, f"browser error: {exc}"
    if result.get("ok"):
        payload = result.get("payload") or {}
        resp = payload.get("response") or {}
        pagination = resp.get("pagination") or {}
        total = pagination.get("total")
        return True, f"v5 scope endpoint responded; total products for {coupon_id}: {total}"
    text = (result.get("text") or "")
    if result.get("status") == 403 or "Please enable JS" in text:
        return False, f"DataDome challenge (status {result.get('status')}); session is blocked. Try again later or refresh the savings tab manually."
    return False, f"unexpected status {result.get('status')}: {text[:200]}"


def command_scope(args):
    if not COUPONS_FILE.exists():
        print(f"{COUPONS_FILE.name} not found; run fetch --write first.", file=sys.stderr)
        return 2
    wait_for_devtools(args.port)

    catalog = load_json(COUPONS_FILE)
    coupons = catalog.get("coupons") or []
    today = date.today().isoformat()

    coupon_id_filter = set(args.coupon_ids or [])
    source_system_filter = set(args.source_systems or [])
    only_meal_keys = {k.lower() for k in (args.only or [])}
    relevant_tokens = _meal_anchor_tokens() if args.relevant_only else None

    candidates = []
    for coupon in coupons:
        if coupon.get("coupon_reward_target") != "ITEM":
            continue
        if not coupon_active(coupon, today):
            continue
        if coupon_id_filter and coupon.get("id") not in coupon_id_filter:
            continue
        if source_system_filter and coupon.get("source_system") not in source_system_filter:
            continue
        if only_meal_keys:
            mks = {(k or "").lower() for k in coupon.get("matched_meal_keys") or []}
            if not (mks & only_meal_keys):
                continue
        if args.skip_resolved and coupon.get("scope_resolved_on"):
            continue
        if relevant_tokens is not None and not _coupon_is_relevant(coupon, relevant_tokens):
            continue
        candidates.append(coupon)

    if args.limit:
        candidates = candidates[: args.limit]

    if args.warmup and candidates:
        print("Warming up: reloading the Giant savings tab to refresh the session...", file=sys.stderr)
        ok, message = scope_warmup(args.port, settle_seconds=args.warmup_settle)
        print(f"  {message}", file=sys.stderr)
        if not ok and not args.no_preflight:
            print("Warmup failed and preflight is enabled; aborting before any scope calls.", file=sys.stderr)
            return 2

    if not args.no_preflight and candidates:
        # Pick a probe coupon: prefer one already resolved (so we know what to expect),
        # otherwise the first candidate.
        already_resolved = next(
            (c for c in coupons if c.get("scope_resolved_on") and c.get("coupon_reward_target") == "ITEM"),
            None,
        )
        probe = already_resolved or candidates[0]
        probe_id = probe.get("id")
        ok, message = scope_preflight(
            args.port, args.user_id, args.service_location_id, probe_id, args.timeout,
        )
        if not ok:
            print(f"Preflight failed on {probe_id}: {message}", file=sys.stderr)
            print("Skipping sweep. Resume later with --skip-resolved when DataDome clears.", file=sys.stderr)
            return 2
        print(f"Preflight ok on {probe_id}: {message}", file=sys.stderr)

    print(f"Scope-resolving {len(candidates)} active ITEM-target coupons (rows={args.rows}, max-pages={args.max_pages}, sleep={args.sleep}s)")

    failures = []
    resolved_count = 0
    total_links = 0
    skipped_total = 0
    consecutive_403s = 0
    aborted = False
    write_every = max(args.write_every or 0, 0)

    def maybe_persist():
        if not args.write:
            return
        catalog.setdefault("metadata", {})
        catalog["metadata"]["scope_resolved_count"] = sum(
            1 for c in coupons if c.get("scope_resolved_on")
        )
        catalog["metadata"]["last_scope_resolved_on"] = today
        write_json(COUPONS_FILE, catalog)

    for index, coupon in enumerate(candidates):
        cid = coupon.get("id")
        attempt = 0
        products, total, error = [], None, None
        while attempt <= args.max_retries:
            try:
                products, total, error = fetch_coupon_scope_products(
                    args.port,
                    args.user_id,
                    args.service_location_id,
                    cid,
                    page_size=args.rows,
                    max_pages=args.max_pages,
                    timeout=args.timeout,
                    sleep=0,
                )
            except GiantBrowserError as exc:
                error = {"status": "browser-error", "text": str(exc)}
                products, total = [], None
            is_block = bool(error) and (
                error.get("status") == 403
                or "Please enable JS" in (error.get("text") or "")
            )
            if is_block and attempt < args.max_retries:
                attempt += 1
                backoff = args.backoff * (2 ** (attempt - 1))
                print(f"  [{index+1}/{len(candidates)}] {cid}: 403 challenge, backing off {backoff:.0f}s (attempt {attempt}/{args.max_retries})", file=sys.stderr)
                time.sleep(backoff)
                continue
            break  # success, non-block error, or retries exhausted

        if error:
            failures.append({"id": cid, "error": error})
            is_block = (
                error.get("status") == 403
                or "Please enable JS" in (error.get("text") or "")
            )
            if is_block:
                consecutive_403s += 1
                if consecutive_403s >= args.abort_after_403s:
                    print(f"\nAborting: {consecutive_403s} consecutive 403s — DataDome appears to be blocking the session.", file=sys.stderr)
                    aborted = True
                    break
            else:
                consecutive_403s = 0
            continue

        consecutive_403s = 0
        coupon["product_ids"] = sorted({p["prod_id"] for p in products})
        coupon["scope_total"] = total
        coupon["scope_resolved_on"] = today
        if total is not None and len(products) < total:
            skipped_total += total - len(products)
        resolved_count += 1
        total_links += len(products)
        if args.verbose and products:
            sample = ", ".join(p["name"] or "" for p in products[:3])
            print(f"  [{index+1}/{len(candidates)}] {cid}: {len(products)} products  {sample[:80]}")
        elif args.verbose:
            print(f"  [{index+1}/{len(candidates)}] {cid}: 0 products")

        if write_every and resolved_count % write_every == 0:
            maybe_persist()

        if args.sleep and index < len(candidates) - 1:
            time.sleep(args.sleep)

    print(f"\nResolved {resolved_count} / {len(candidates)} coupons; {total_links} total product links")
    if skipped_total:
        print(f"Note: {skipped_total} qualifying products beyond max-pages cap were not captured")
    if failures:
        print(f"Failures: {len(failures)}; first error: {failures[0]['error'] if isinstance(failures[0]['error'], str) else (failures[0]['error'].get('status') if isinstance(failures[0]['error'], dict) else failures[0]['error'])}")
    if aborted:
        print(f"Aborted after {consecutive_403s} consecutive 403s; resume later with --skip-resolved.")

    if args.write:
        maybe_persist()
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
            category_name = (coupon.get("category_tree_name") or "").lower()
            if category_name in {"alcohol", "tobacco"} and "alcohol" not in meal_blob:
                continue

            score = 0.0
            # Strongest signal: this coupon was discovered via this meal
            # item's saved Giant product (per-product harvest back-reference).
            if meal_key in (coupon.get("matched_meal_keys") or []):
                score += 1.5
            if giant_pid and giant_pid in {str(p) for p in coupon.get("matched_product_ids") or []}:
                score += 0.5

            # Fallback signal: text/category overlap.
            blob = coupon_text_blob(coupon)
            blob_tokens = set(re.findall(r"[a-z]+", blob))
            overlap = meal_tokens & blob_tokens
            if overlap:
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

    fetch = sub.add_parser("fetch", help="Fetch the combined Giant coupon catalog and save to giant_coupons.json.")
    fetch.add_argument("--port", type=int, default=DEFAULT_PORT)
    fetch.add_argument("--service-location-id", default=DEFAULT_SERVICE_LOCATION_ID)
    fetch.add_argument("--user-id", default=DEFAULT_USER_ID)
    fetch.add_argument("--page-size", type=int, default=90, help="Requested page size; server caps at 90")
    fetch.add_argument("--max-pages", type=int, default=50, help="Max storewide pages; ~35 covers the full ~3000 catalog at size 90")
    fetch.add_argument("--source-systems", action="append", help="Filter to specific source systems (COP, ECI, INM, QUO, etc.); repeatable")
    fetch.add_argument("--loadable-only", action="store_true", help="Restrict to loadable coupons via filter.loadable=true")
    fetch.add_argument("--unloaded-only", action="store_true", help="Restrict to unloaded coupons via filter.loaded=false")
    fetch.add_argument("--targeting-only", action="store_true", help="Restrict to targeted/personalized coupons via copientQuotientTargetingEnabled=true (mirrors the savings page default)")
    fetch.add_argument("--no-storewide", action="store_true", help="Skip the v7 storewide search step")
    fetch.add_argument("--no-per-product", action="store_true", help="Skip per-product display-coupon harvesting")
    fetch.add_argument("--only", action="append", help="Restrict per-product harvest to these meal_prices keys (repeatable)")
    fetch.add_argument("--sleep", type=float, default=0.10)
    fetch.add_argument("--timeout", type=float, default=30.0)
    fetch.add_argument("--write", action="store_true")

    scope = sub.add_parser(
        "scope",
        help="Resolve qualifying-product SKUs for ITEM-target coupons via /api/v5.0/products?couponId=...",
    )
    scope.add_argument("--port", type=int, default=DEFAULT_PORT)
    scope.add_argument("--service-location-id", default=DEFAULT_SERVICE_LOCATION_ID)
    scope.add_argument("--user-id", default=DEFAULT_USER_ID)
    scope.add_argument("--rows", type=int, default=200, help="Products per page (page used 40; we bump for fewer round-trips)")
    scope.add_argument("--max-pages", type=int, default=5, help="Cap on pagination per coupon")
    scope.add_argument("--limit", type=int, default=None, help="Resolve at most this many coupons in one run")
    scope.add_argument("--coupon-ids", nargs="*", default=None, help="Restrict to specific coupon IDs (e.g. COP_7444913)")
    scope.add_argument("--source-systems", nargs="*", default=None, help="Restrict to specific source systems")
    scope.add_argument("--only", nargs="*", default=None, help="Restrict to coupons whose matched_meal_keys overlap these")
    scope.add_argument("--skip-resolved", action="store_true", help="Skip coupons that already have scope_resolved_on set")
    scope.add_argument("--relevant-only", action="store_true", help="Only resolve coupons whose name+description shares anchor tokens with at least one tracked meal item (uses meal_prices.json)")
    scope.add_argument("--sleep", type=float, default=1.0, help="Delay between coupon requests (sec); too low triggers DataDome 403 challenges")
    scope.add_argument("--backoff", type=float, default=30.0, help="Seconds to sleep on a 403 challenge before retrying (doubled per attempt)")
    scope.add_argument("--max-retries", type=int, default=2, help="Retries per coupon when hitting 403 challenges")
    scope.add_argument("--abort-after-403s", type=int, default=10, help="Abort the whole sweep after this many consecutive 403s")
    scope.add_argument("--write-every", type=int, default=50, help="Persist progress to giant_coupons.json every N successful resolutions (0 disables)")
    scope.add_argument("--warmup", action="store_true", help="Reload the savings tab via CDP before the sweep, mimicking the natural page-load flow that DataDome expects")
    scope.add_argument("--warmup-settle", type=float, default=8.0, help="Seconds to wait after warmup reload for the page to settle")
    scope.add_argument("--no-preflight", action="store_true", help="Skip the single-call preflight that aborts early when DataDome is blocking the session")
    scope.add_argument("--timeout", type=float, default=30.0)
    scope.add_argument("--verbose", action="store_true")
    scope.add_argument("--write", action="store_true")

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
        if args.command == "scope":
            return command_scope(args)
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
