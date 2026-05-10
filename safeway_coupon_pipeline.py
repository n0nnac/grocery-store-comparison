#!/usr/bin/env python3
"""Reusable Safeway coupon refresh pipeline.

The pipeline refreshes public coupon offers, preserves prior enrichment and
account state, enriches targeted offer slices, and can optionally refresh
logged-in clipped state through a temporary Chrome profile copy.
"""

import argparse
import contextlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from safeway_coupon_account_state import (
    CdpClient,
    apply_account_state,
    cdp_page,
    fetch_account_payloads,
    offers_from_payload,
    parse_response,
)
from safeway_coupon_enrich import enrich_offer
from safeway_coupon_search import (
    COUPON_DETAIL_ENDPOINT,
    COUPON_GALLERY_ENDPOINT,
    COUPONS_FILE,
    DEFAULT_BANNER,
    DEFAULT_STORE_ID,
    default_account_state,
    fetch_gallery,
    load_json,
    normalize_offer,
    write_json,
)


DEFAULT_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DEFAULT_CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
DEFAULT_CHROME_PROFILE = DEFAULT_CHROME_ROOT / "Default"

PRESERVED_OFFER_FIELDS = (
    "account_state",
    "detail_endpoint",
    "upc_list",
    "offer_end_date",
    "offer_program_type",
    "offer_proto_type",
    "detail_name",
    "detail_description",
    "detail_offer_price",
    "detail_for_u_description",
    "detail_primary_category",
    "resolved_products",
    "upc_resolution",
    "matched_items",
)

DEFAULT_ENRICH_CATEGORIES = ("Meat & Seafood",)
DEFAULT_ENRICH_APPLICATION_KINDS = ("department_threshold", "basket_threshold", "points_bonus")


class PipelineArgs:
    """Small adapter for safeway_coupon_enrich.enrich_offer."""

    def __init__(self, args):
        self.store_id = args.store_id
        self.banner = args.banner
        self.timeout = args.timeout
        self.resolve_upcs = args.resolve_upcs
        self.max_upcs = args.max_upcs
        self.force = args.force_enrichment


def observed_on():
    return datetime.now(timezone.utc).date().isoformat()


def load_existing(path):
    if path.exists():
        return load_json(path)
    return {"metadata": {}, "offers": []}


def ensure_account_state(offer):
    state = offer.setdefault("account_state", {})
    for key, value in default_account_state("unauthenticated_gallery").items():
        state.setdefault(key, value)
    return offer


def merge_offer(new_offer, existing_offer):
    if not existing_offer:
        ensure_account_state(new_offer)
        return new_offer
    for field in PRESERVED_OFFER_FIELDS:
        if existing_offer.get(field) is not None:
            new_offer[field] = existing_offer[field]
    ensure_account_state(new_offer)
    return new_offer


def sort_offers(offers):
    return sorted(
        offers,
        key=lambda offer: (
            offer.get("purchase_rank") is None,
            offer.get("purchase_rank") or 999999,
            offer.get("category") or "",
            offer.get("offer_id") or "",
        ),
    )


def refresh_public_gallery(data, args):
    payload = fetch_gallery(args.store_id, args.banner, args.timeout)
    raw_offers = payload.get("companionGalleryOffer", {})
    existing_by_id = {
        str(offer.get("offer_id")): offer
        for offer in data.get("offers", [])
        if offer.get("offer_id")
    }

    refreshed = []
    public_ids = set()
    for raw_offer in raw_offers.values():
        normalized = normalize_offer(raw_offer)
        offer_id = normalized["offer_id"]
        public_ids.add(offer_id)
        refreshed.append(merge_offer(normalized, existing_by_id.get(offer_id)))

    retained_missing = 0
    if args.keep_missing:
        for offer_id, existing_offer in existing_by_id.items():
            if offer_id not in public_ids:
                ensure_account_state(existing_offer)
                refreshed.append(existing_offer)
                retained_missing += 1

    metadata = data.setdefault("metadata", {})
    metadata.update(
        {
            "source_url": f"https://www.{args.banner}.com/loyalty/coupons-deals",
            "store_id": str(args.store_id),
            "observed_on": observed_on(),
            "source_type": "coupon_pipeline",
            "gallery_endpoint": COUPON_GALLERY_ENDPOINT,
            "detail_endpoint": COUPON_DETAIL_ENDPOINT,
            "public_gallery_offers": len(raw_offers),
            "retained_missing_offers": retained_missing,
            "selected_offers": len(refreshed),
        }
    )
    data["offers"] = sort_offers(refreshed)
    return {
        "public_gallery_offers": len(raw_offers),
        "retained_missing_offers": retained_missing,
        "total_after_public_merge": len(data["offers"]),
    }


def offer_matches_enrichment(offer, query, category, application_kind):
    if category and category.lower() not in str(offer.get("category") or "").lower():
        return False
    if application_kind and (offer.get("application") or {}).get("kind") != application_kind:
        return False
    if query:
        query_l = query.lower()
        fields = ("brand", "name", "description", "value_text", "category")
        if not any(query_l in str(offer.get(field) or "").lower() for field in fields):
            return False
    return True


def enrichment_targets(data, args):
    categories = list(args.enrich_category or [])
    application_kinds = list(args.enrich_application_kind or [])
    queries = list(args.enrich_query or [])
    if not args.no_default_enrichment:
        categories.extend(DEFAULT_ENRICH_CATEGORIES)
        application_kinds.extend(DEFAULT_ENRICH_APPLICATION_KINDS)

    offers_by_id = {}
    for offer in data.get("offers", []):
        if (
            args.enrich_clipped_line_items
            and (offer.get("account_state") or {}).get("clipped") is True
            and (offer.get("application") or {}).get("allocation") == "line_item"
        ):
            offers_by_id[offer["offer_id"]] = offer
        for category in categories:
            if offer_matches_enrichment(offer, None, category, None):
                offers_by_id[offer["offer_id"]] = offer
        for kind in application_kinds:
            if offer_matches_enrichment(offer, None, None, kind):
                offers_by_id[offer["offer_id"]] = offer
        for query in queries:
            if offer_matches_enrichment(offer, query, None, None):
                offers_by_id[offer["offer_id"]] = offer

    targets = list(offers_by_id.values())
    targets.sort(key=lambda offer: (offer.get("purchase_rank") is None, offer.get("purchase_rank") or 999999))
    if args.enrichment_limit is not None:
        targets = targets[: args.enrichment_limit]
    return targets


def run_enrichment(data, args):
    targets = enrichment_targets(data, args)
    adapter = PipelineArgs(args)
    details_added = 0
    products_added = 0
    for index, offer in enumerate(targets):
        had_detail = bool(offer.get("detail_endpoint"))
        had_products = bool(offer.get("resolved_products"))
        enrich_offer(offer, adapter)
        details_added += int(not had_detail and bool(offer.get("detail_endpoint")))
        products_added += int(not had_products and bool(offer.get("resolved_products")))
        if args.sleep and index < len(targets) - 1:
            time.sleep(args.sleep)

    metadata = data.setdefault("metadata", {})
    metadata["pipeline_enrichment"] = {
        "last_checked_on": observed_on(),
        "selected_offers": len(targets),
        "new_details_added": details_added,
        "new_product_resolutions_added": products_added,
        "resolve_upcs": args.resolve_upcs,
        "max_upcs": args.max_upcs,
    }
    metadata["details_fetched"] = any(offer.get("detail_endpoint") for offer in data.get("offers", []))
    metadata["upcs_resolved"] = any(offer.get("resolved_products") for offer in data.get("offers", []))
    return metadata["pipeline_enrichment"]


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def copy_if_exists(src, dest):
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True
    return False


@contextlib.contextmanager
def launched_profile_copy(args):
    port = args.cdp_port or find_free_port()
    cdp_url = f"http://127.0.0.1:{port}"
    chrome_bin = Path(args.chrome_bin).expanduser()
    chrome_root = Path(args.chrome_root).expanduser()
    chrome_profile = Path(args.chrome_profile).expanduser()
    if not chrome_bin.exists():
        raise SystemExit(f"Chrome binary not found: {chrome_bin}")
    if not chrome_profile.exists():
        raise SystemExit(f"Chrome profile not found: {chrome_profile}")

    temp_dir = Path(tempfile.mkdtemp(prefix="safeway-chrome-profile."))
    profile_name = chrome_profile.name
    temp_profile = temp_dir / profile_name
    temp_profile.mkdir(parents=True, exist_ok=True)
    copy_if_exists(chrome_root / "Local State", temp_dir / "Local State")
    copy_if_exists(chrome_profile / "Preferences", temp_profile / "Preferences")
    copy_if_exists(chrome_profile / "Cookies", temp_profile / "Cookies")
    copy_if_exists(chrome_profile / "Cookies-journal", temp_profile / "Cookies-journal")
    copy_if_exists(chrome_profile / "Network/Cookies", temp_profile / "Network/Cookies")
    copy_if_exists(chrome_profile / "Network/Cookies-journal", temp_profile / "Network/Cookies-journal")

    command = [
        str(chrome_bin),
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--remote-allow-origins={cdp_url}",
        f"--user-data-dir={temp_dir}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--disable-gpu",
        "https://www.safeway.com/loyalty/mylist",
    ]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_cdp(cdp_url, args.cdp_startup_timeout)
        wait_for_safeway_page(cdp_url, args.cdp_startup_timeout)
        wait_for_safeway_document(cdp_url, args.cdp_startup_timeout)
        yield cdp_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(temp_dir, ignore_errors=True)


def wait_for_cdp(cdp_url, timeout):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/list", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - startup polling should keep last error.
            last_error = exc
        time.sleep(0.25)
    raise SystemExit(f"Chrome CDP did not start at {cdp_url}: {last_error}")


def wait_for_safeway_page(cdp_url, timeout):
    deadline = time.time() + timeout
    last_pages = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/list", timeout=2) as response:
                pages = json.loads(response.read().decode("utf-8"))
            last_pages = [page.get("url") for page in pages if page.get("type") == "page"]
            if any("safeway.com" in (url or "") for url in last_pages):
                return
        except Exception:  # noqa: BLE001 - startup polling is intentionally forgiving.
            pass
        time.sleep(0.25)
    raise SystemExit(f"Chrome CDP started but no safeway.com page appeared: {last_pages}")


def wait_for_safeway_document(cdp_url, timeout):
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        try:
            page = cdp_page(cdp_url)
            client = CdpClient(page["webSocketDebuggerUrl"], cdp_url.rstrip("/"))
            try:
                client.call("Runtime.enable")
                last_state = client.evaluate(
                    """
({href: location.href, origin: location.origin, readyState: document.readyState, title: document.title})
""",
                    timeout=5000,
                )
            finally:
                client.close()
            if (
                last_state
                and last_state.get("origin") == "https://www.safeway.com"
                and last_state.get("readyState") in {"interactive", "complete"}
            ):
                return
        except Exception as exc:  # noqa: BLE001 - startup polling should keep trying.
            last_state = str(exc)
        time.sleep(0.5)
    raise SystemExit(f"Safeway page did not become ready for account-state reads: {last_state}")


def run_account_state(data, args, cdp_url):
    payloads = fetch_account_payloads(cdp_url, args.store_id)
    clipped_payload = parse_response(payloads.get("clipped"), "clipped")
    gallery_payload = parse_response(payloads.get("gallery"), "gallery")
    clipped_raw = offers_from_payload(clipped_payload)
    gallery_raw = offers_from_payload(gallery_payload)
    counts = apply_account_state(data, clipped_raw, gallery_raw, observed_on(), args.add_account_only)
    return counts


def validate_data(data):
    offers = data.get("offers", [])
    seen = set()
    duplicates = []
    for offer in offers:
        offer_id = offer.get("offer_id")
        if not offer_id:
            raise SystemExit("Validation failed: offer without offer_id")
        if offer_id in seen:
            duplicates.append(offer_id)
        seen.add(offer_id)
        ensure_account_state(offer)
    if duplicates:
        raise SystemExit(f"Validation failed: duplicate offer IDs: {', '.join(sorted(set(duplicates)))}")
    return {
        "offers": len(offers),
        "details": sum(1 for offer in offers if offer.get("detail_endpoint")),
        "resolved_products": sum(1 for offer in offers if offer.get("resolved_products")),
        "clipped_true": sum(1 for offer in offers if (offer.get("account_state") or {}).get("clipped") is True),
        "clipped_false": sum(1 for offer in offers if (offer.get("account_state") or {}).get("clipped") is False),
        "clipped_unknown": sum(1 for offer in offers if (offer.get("account_state") or {}).get("clipped") is None),
    }


def print_summary(public_counts, enrichment_counts, account_counts, validation, write):
    mode = "Wrote" if write else "Dry run"
    print(f"{mode}: Safeway coupon pipeline")
    print(
        "- public gallery: "
        f"{public_counts['public_gallery_offers']} offers, "
        f"{public_counts['retained_missing_offers']} retained missing/account-only"
    )
    print(
        "- enrichment: "
        f"{enrichment_counts['selected_offers']} selected, "
        f"{enrichment_counts['new_details_added']} new details, "
        f"{enrichment_counts['new_product_resolutions_added']} new product resolutions"
    )
    if account_counts:
        print(
            "- account state: "
            f"{account_counts['clipped_endpoint_offers']} clipped endpoint offers, "
            f"{account_counts['logged_in_gallery_offers']} logged-in gallery offers, "
            f"{account_counts['new_account_gallery_offers_added']} new account-only"
        )
    else:
        print("- account state: skipped")
    print(
        "- validation: "
        f"{validation['offers']} offers, "
        f"{validation['details']} with details, "
        f"{validation['resolved_products']} with resolved products, "
        f"{validation['clipped_true']} clipped, "
        f"{validation['clipped_false']} unclipped, "
        f"{validation['clipped_unknown']} unknown"
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Run the reusable Safeway coupon refresh pipeline.")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--banner", default=DEFAULT_BANNER, help="Banner, usually safeway")
    parser.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds")
    parser.add_argument("--write", action="store_true", help=f"Write results to {COUPONS_FILE.name}")

    parser.add_argument("--keep-missing", action=argparse.BooleanOptionalAction, default=True, help="Retain saved offers missing from the public gallery")
    parser.add_argument("--no-default-enrichment", action="store_true", help="Skip default enrichment targets")
    parser.add_argument("--enrich-category", action="append", help="Category substring to enrich; can repeat")
    parser.add_argument("--enrich-application-kind", action="append", help="Application kind to enrich; can repeat")
    parser.add_argument("--enrich-query", action="append", help="Text query to enrich; can repeat")
    parser.add_argument("--enrich-clipped-line-items", action=argparse.BooleanOptionalAction, default=True, help="Enrich clipped line-item coupons")
    parser.add_argument("--enrichment-limit", type=int, help="Maximum enrichment targets")
    parser.add_argument("--resolve-upcs", action=argparse.BooleanOptionalAction, default=True, help="Resolve UPCs for enriched offers")
    parser.add_argument("--max-upcs", type=int, default=25, help="Maximum UPCs to resolve per offer; use 0 for all")
    parser.add_argument("--force-enrichment", action="store_true", help="Refetch details and product resolutions")
    parser.add_argument("--sleep", type=float, default=0.05, help="Delay between enrichment requests")

    parser.add_argument("--account-state", action="store_true", help="Refresh logged-in clipped/unclipped account state")
    parser.add_argument("--cdp-url", help="Existing Chrome DevTools Protocol URL")
    parser.add_argument("--launch-profile-copy", action="store_true", help="Launch a temporary copied Chrome profile for account-state reads")
    parser.add_argument("--add-account-only", action=argparse.BooleanOptionalAction, default=True, help="Add account-only offers from logged-in gallery")
    parser.add_argument("--chrome-bin", default=str(DEFAULT_CHROME), help="Chrome binary path")
    parser.add_argument("--chrome-root", default=str(DEFAULT_CHROME_ROOT), help="Chrome user-data root containing Local State")
    parser.add_argument("--chrome-profile", default=str(DEFAULT_CHROME_PROFILE), help="Chrome profile directory to copy")
    parser.add_argument("--cdp-port", type=int, default=0, help="CDP port for launched profile copy; 0 chooses a free port")
    parser.add_argument("--cdp-startup-timeout", type=float, default=20.0, help="Seconds to wait for launched Chrome CDP")
    return parser.parse_args()


def main():
    args = parse_args()
    data = load_existing(COUPONS_FILE)
    public_counts = refresh_public_gallery(data, args)

    account_counts = None
    if args.account_state:
        if args.cdp_url:
            account_counts = run_account_state(data, args, args.cdp_url)
        else:
            if not args.launch_profile_copy:
                args.launch_profile_copy = True
            with launched_profile_copy(args) as cdp_url:
                account_counts = run_account_state(data, args, cdp_url)

    enrichment_counts = run_enrichment(data, args)

    validation = validate_data(data)
    data.setdefault("metadata", {})["pipeline_last_run_on"] = observed_on()
    data["metadata"]["pipeline_validation"] = validation

    if args.write:
        write_json(COUPONS_FILE, data)
    print_summary(public_counts, enrichment_counts, account_counts, validation, args.write)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Request failed: {exc}") from exc
    except KeyboardInterrupt:
        sys.exit(130)
