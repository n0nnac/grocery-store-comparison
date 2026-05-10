#!/usr/bin/env python3
"""Update Safeway coupon clipped state from a logged-in read-only browser pass.

This tool does not clip, unclip, or change cart/list state. It reads account
coupon state through a Chrome DevTools Protocol page that is already on
safeway.com with a logged-in session.
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

import websocket

from safeway_coupon_search import (
    COUPONS_FILE,
    DEFAULT_STORE_ID,
    default_account_state,
    load_json,
    normalize_offer,
    write_json,
)


ACCOUNT_CLIPPED_PATH = (
    "/abs/pub/dce/offergallery/J4UProgram1/services/gallery/companion/clipped"
)
ACCOUNT_GALLERY_PATH = (
    "/abs/pub/dce/offergallery/J4UProgram1/services/gallery/companion/v1/offers"
)
ACCOUNT_STATE_FILE = COUPONS_FILE.with_name("safeway_coupon_account_state.local.json")


class CdpClient:
    def __init__(self, websocket_url, origin):
        self.websocket = websocket.create_connection(websocket_url, timeout=20, origin=origin)
        self.next_id = 1

    def close(self):
        self.websocket.close()

    def call(self, method, params=None):
        message_id = self.next_id
        self.next_id += 1
        self.websocket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.websocket.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message

    def evaluate(self, expression, timeout=60000):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout,
            },
        )
        if result.get("result", {}).get("exceptionDetails"):
            details = result["result"]["exceptionDetails"]
            text = details.get("text") or "browser-side JavaScript error"
            description = details.get("exception", {}).get("description")
            raise RuntimeError(description or text)
        value = result.get("result", {}).get("result", {}).get("value")
        return value


def cdp_page(cdp_url):
    with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/list", timeout=10) as response:
        pages = json.loads(response.read().decode("utf-8"))
    for page in pages:
        if page.get("type") == "page" and "safeway.com" in page.get("url", ""):
            return page
    raise SystemExit(f"No safeway.com page found at {cdp_url}")


def fetch_account_payloads(cdp_url, store_id):
    page = cdp_page(cdp_url)
    client = CdpClient(page["webSocketDebuggerUrl"], cdp_url.rstrip("/"))
    try:
        client.call("Runtime.enable")
        expression = f"""
(async function(){{
  const paths = {{
    clipped: "https://www.safeway.com{ACCOUNT_CLIPPED_PATH}?storeId={store_id}",
    gallery: "https://www.safeway.com{ACCOUNT_GALLERY_PATH}?storeId={store_id}"
  }};
  const output = {{}};
  for (const [name, path] of Object.entries(paths)) {{
    const response = await fetch(path, {{
      credentials: "include",
      headers: {{
        "Accept": "application/json, text/plain, */*",
        "x-swy-banner": "safeway",
        "x-swy-client-id": "web-portal",
        "X-SWY_API_KEY": "emju",
        "X-SWY_VERSION": "3.0"
      }}
    }});
    const text = await response.text();
    output[name] = {{
      path,
      status: response.status,
      contentType: response.headers.get("content-type"),
      text
    }};
  }}
  return output;
}})()
"""
        return client.evaluate(expression)
    finally:
        client.close()


def parse_response(response, label):
    if not response:
        raise SystemExit(f"Missing {label} response")
    status = response.get("status")
    if status != 200:
        sample = (response.get("text") or "")[:500]
        raise SystemExit(f"{label} endpoint returned HTTP {status}: {sample}")
    text = response.get("text") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} endpoint returned non-JSON: {exc}") from exc


def offers_from_payload(payload):
    if not payload:
        return []
    if isinstance(payload.get("companionGalleryOffer"), dict):
        return list(payload["companionGalleryOffer"].values())
    if isinstance(payload.get("companionGalleryOfferList"), list):
        return payload["companionGalleryOfferList"]
    return []


def id_set(offers):
    return {str(offer.get("offerId") or offer.get("offer_id")) for offer in offers if offer.get("offerId") or offer.get("offer_id")}


def ensure_account_state(offer, source_type):
    state = offer.setdefault("account_state", {})
    defaults = default_account_state(source_type)
    for key, value in defaults.items():
        state.setdefault(key, value)
    return state


def normalize_account_offer(raw_offer, clipped_ids, observed_on):
    offer = normalize_offer(raw_offer)
    offer["source_type"] = "coupon_account_gallery_api"
    offer["endpoint"] = ACCOUNT_GALLERY_PATH
    state = ensure_account_state(offer, "logged_in_gallery")
    offer_id = offer["offer_id"]
    state["source_type"] = "logged_in_gallery"
    state["clipped"] = offer_id in clipped_ids
    state["clip_status_confirmed_on"] = observed_on
    state["household_specific"] = None
    if raw_offer.get("clipId"):
        state["clip_id_present"] = True
    if raw_offer.get("clipTs"):
        state["clip_timestamp"] = raw_offer.get("clipTs")
    return offer


def apply_account_state(data, clipped_raw, gallery_raw, observed_on, add_new):
    offers = data.setdefault("offers", [])
    by_id = {str(offer.get("offer_id")): offer for offer in offers}

    clipped_ids = id_set(clipped_raw)
    gallery_ids = id_set(gallery_raw)

    updated_true = 0
    updated_false = 0
    added = 0

    for offer_id, offer in by_id.items():
        state = ensure_account_state(offer, "logged_in_gallery")
        if offer_id in clipped_ids:
            state["source_type"] = "logged_in_gallery"
            state["clipped"] = True
            state["clip_status_confirmed_on"] = observed_on
            updated_true += 1
        elif offer_id in gallery_ids:
            state["source_type"] = "logged_in_gallery"
            state["clipped"] = False
            state["clip_status_confirmed_on"] = observed_on
            updated_false += 1

    if add_new:
        for raw_offer in gallery_raw:
            offer_id = str(raw_offer.get("offerId"))
            if offer_id and offer_id not in by_id:
                offers.append(normalize_account_offer(raw_offer, clipped_ids, observed_on))
                by_id[offer_id] = offers[-1]
                added += 1

        for raw_offer in clipped_raw:
            offer_id = str(raw_offer.get("offerId"))
            if offer_id and offer_id not in by_id:
                offers.append(normalize_account_offer(raw_offer, clipped_ids, observed_on))
                by_id[offer_id] = offers[-1]
                added += 1

    metadata = data.setdefault("metadata", {})
    metadata["account_state_last_checked_on"] = observed_on
    metadata["account_state_source"] = "logged_in_cdp"
    metadata["account_state_endpoints"] = {
        "clipped": ACCOUNT_CLIPPED_PATH,
        "gallery": ACCOUNT_GALLERY_PATH,
    }
    metadata["account_state_counts"] = {
        "clipped_endpoint_offers": len(clipped_ids),
        "logged_in_gallery_offers": len(gallery_ids),
        "existing_marked_clipped": updated_true,
        "existing_marked_unclipped": updated_false,
        "new_account_gallery_offers_added": added,
    }
    return metadata["account_state_counts"]


def print_summary(counts, write):
    mode = "Wrote" if write else "Dry run"
    print(f"{mode}: account-state update")
    print(f"- clipped endpoint offers: {counts['clipped_endpoint_offers']}")
    print(f"- logged-in gallery offers: {counts['logged_in_gallery_offers']}")
    print(f"- existing offers marked clipped: {counts['existing_marked_clipped']}")
    print(f"- existing offers marked unclipped: {counts['existing_marked_unclipped']}")
    print(f"- new account-gallery offers added: {counts['new_account_gallery_offers_added']}")


def local_account_state_payload(data, counts, observed_on):
    state_by_id = {}
    account_only_offers = []
    for offer in data.get("offers", []):
        offer_id = str(offer.get("offer_id") or "")
        state = offer.get("account_state")
        if offer_id and state and state.get("source_type") == "logged_in_gallery":
            state_by_id[offer_id] = state
            if offer.get("source_type") == "coupon_account_gallery_api":
                account_only_offers.append(offer)
    return {
        "metadata": {
            "source_type": "local_account_state_overlay",
            "observed_on": observed_on,
            "counts": counts,
            "notes": "Ignored local overlay. Do not commit account-specific clipped state.",
        },
        "account_state_by_offer_id": state_by_id,
        "account_only_offers": account_only_offers,
    }


def main():
    parser = argparse.ArgumentParser(description="Refresh Safeway coupon account clipped state.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223", help="Chrome DevTools Protocol HTTP URL")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--add-new", action="store_true", help="Add offers that appear only in the logged-in gallery")
    parser.add_argument("--write", action="store_true", help=f"Write account state to ignored {ACCOUNT_STATE_FILE.name}")
    parser.add_argument("--write-public", action="store_true", help=f"Also write account state into tracked {COUPONS_FILE.name}")
    args = parser.parse_args()

    observed_on = datetime.now(timezone.utc).date().isoformat()
    account_payloads = fetch_account_payloads(args.cdp_url, args.store_id)
    clipped_payload = parse_response(account_payloads.get("clipped"), "clipped")
    gallery_payload = parse_response(account_payloads.get("gallery"), "gallery")
    clipped_raw = offers_from_payload(clipped_payload)
    gallery_raw = offers_from_payload(gallery_payload)

    data = load_json(COUPONS_FILE)
    counts = apply_account_state(data, clipped_raw, gallery_raw, observed_on, args.add_new)
    print_summary(counts, args.write or args.write_public)

    if args.write:
        write_json(ACCOUNT_STATE_FILE, local_account_state_payload(data, counts, observed_on))
    if args.write_public:
        write_json(COUPONS_FILE, data)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
