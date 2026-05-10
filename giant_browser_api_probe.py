#!/usr/bin/env python3
"""Probe Giant's browser API through a real Chrome session.

Giant's live web APIs are protected by DataDome from plain shell requests.
Normal Chrome browsing can access them after the browser has a valid session.
This script uses Chrome DevTools Protocol only as a transport into that normal
browser context, then runs same-origin `fetch()` calls from a Giant tab.

It does not read, print, or persist cookies.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websocket
except ImportError:  # pragma: no cover - environment guidance
    websocket = None


DEFAULT_PORT = 9227
DEFAULT_PROFILE_DIR = "/tmp/giant-cdp-profile"
DEFAULT_STORE_NUMBER = "378"
DEFAULT_SERVICE_TYPE = "B"
DEFAULT_SERVICE_LOCATION_ID = "50000732"
DEFAULT_USER_ID = "2"
DEFAULT_SEED_URL = (
    "https://giantfood.com/product/"
    "giant-choice-boneless-beef-ny-strip-steak-3-4-inch-vacuum-sealed-apx-2-3-lb/151854"
)
BASE_URL = "https://giantfood.com"

PARK_ROAD_CONTEXT = {
    "store_number": "0378",
    "store_number_api": "378",
    "store_address": "1345 Park Road N.W., Washington, DC 20010",
    "default_service_type": DEFAULT_SERVICE_TYPE,
    "default_service_location_id": DEFAULT_SERVICE_LOCATION_ID,
}


class GiantBrowserError(RuntimeError):
    pass


def devtools_url(port, path):
    return f"http://127.0.0.1:{port}{path}"


def request_json(url, timeout=5, method="GET"):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_devtools(port, timeout=12):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return request_json(devtools_url(port, "/json/version"), timeout=2)
        except Exception as error:  # noqa: BLE001 - diagnostic preserved
            last_error = error
            time.sleep(0.5)
    raise GiantBrowserError(f"Chrome DevTools is not reachable on port {port}: {last_error}")


def launch_chrome(args):
    command = [
        "open",
        "-na",
        "Google Chrome",
        "--args",
        f"--user-data-dir={args.profile_dir}",
        f"--remote-debugging-port={args.port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        args.url,
    ]
    subprocess.run(command, check=True)
    version = wait_for_devtools(args.port)
    return {
        "ok": True,
        "port": args.port,
        "profile_dir": args.profile_dir,
        "browser": version.get("Browser"),
        "seed_url": args.url,
        "next_steps": [
            "If Giant shows a challenge or store prompt, complete it in the launched Chrome window.",
            "Then run the store/search/product commands against the same --port.",
        ],
    }


def list_targets(port):
    return request_json(devtools_url(port, "/json/list"), timeout=5)


def open_giant_tab(port, url=DEFAULT_SEED_URL):
    quoted = urllib.parse.quote(url, safe="")
    try:
        return request_json(devtools_url(port, f"/json/new?{quoted}"), timeout=5, method="PUT")
    except urllib.error.HTTPError:
        return request_json(devtools_url(port, f"/json/new?{quoted}"), timeout=5)


def find_giant_page(port):
    targets = list_targets(port)
    for target in targets:
        if target.get("type") == "page" and target.get("url", "").startswith(BASE_URL):
            return target
    open_giant_tab(port)
    time.sleep(1)
    targets = list_targets(port)
    for target in targets:
        if target.get("type") == "page" and target.get("url", "").startswith(BASE_URL):
            return target
    raise GiantBrowserError("No Giant browser tab found. Run `launch` first and let the page load.")


class CDPSession:
    def __init__(self, ws_url, origin):
        if websocket is None:
            raise GiantBrowserError("Missing dependency `websocket-client`; install it with `python3 -m pip install websocket-client`.")
        self.ws = websocket.create_connection(ws_url, timeout=15, origin=origin)
        self.next_id = 0

    def close(self):
        self.ws.close()

    def call(self, method, params=None):
        self.next_id += 1
        message_id = self.next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise GiantBrowserError(json.dumps(message["error"], sort_keys=True))
                return message.get("result", {})


def evaluate_in_giant_page(port, expression):
    page = find_giant_page(port)
    session = CDPSession(page["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{port}")
    try:
        result = session.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
    finally:
        session.close()
    value = result.get("result", {}).get("value")
    if value is None:
        raise GiantBrowserError(f"Browser evaluation returned no value: {result}")
    return value


def browser_fetch(port, urls):
    expression = f"""
    (async () => {{
      const urls = {json.dumps(urls)};
      const results = [];
      for (const url of urls) {{
        try {{
          const response = await fetch(url, {{ credentials: "include" }});
          const text = await response.text();
          let payload = null;
          try {{ payload = JSON.parse(text); }} catch (error) {{}}
          results.push({{
            url,
            status: response.status,
            ok: response.ok,
            contentType: response.headers.get("content-type"),
            payload,
            text: payload ? null : text.slice(0, 1200),
          }});
        }} catch (error) {{
          results.push({{ url, ok: false, error: String(error) }});
        }}
      }}
      return {{
        page: {{ title: document.title, href: location.href }},
        results,
      }};
    }})()
    """
    return evaluate_in_giant_page(port, expression)


def product_rows(payload):
    products = ((payload or {}).get("response") or {}).get("products") or []
    if isinstance(products, dict):
        products = list(products.values())
    return products


def service_location_rows(payload):
    if isinstance(payload, list):
        return payload
    return ((payload or {}).get("response") or {}).get("locations") or []


def summarize_product(product):
    coupon = product.get("coupon") or {}
    return {
        "prodId": product.get("prodId"),
        "name": product.get("name"),
        "size": product.get("size"),
        "brand": product.get("brand"),
        "price": product.get("price"),
        "regularPrice": product.get("regularPrice"),
        "unitPrice": product.get("unitPrice"),
        "unitMeasure": product.get("unitMeasure"),
        "upc": product.get("upc"),
        "sale": (product.get("flags") or {}).get("sale"),
        "outOfStock": (product.get("flags") or {}).get("outOfStock"),
        "hasCoupon": product.get("hasCoupon"),
        "coupon": {
            "id": coupon.get("id"),
            "description": coupon.get("description"),
            "maxDiscount": coupon.get("maxDiscount"),
            "clippingRequired": coupon.get("clippingRequired"),
            "loaded": coupon.get("loaded"),
        } if coupon else None,
    }


def summarize_location(location_row):
    location = location_row.get("location") or location_row
    return {
        "id": location.get("id"),
        "name": location.get("name"),
        "address": location.get("address"),
        "city": location.get("city"),
        "state": location.get("state"),
        "zip": location.get("zip"),
        "locationNumber": location.get("locationNumber"),
        "serviceType": location.get("serviceType"),
        "ecommStoreId": location.get("ecommStoreId"),
        "priceZone": location.get("priceZone"),
        "pickupLocationId": location.get("pickupLocationId"),
        "pupId": location.get("pupId"),
        "active": location.get("active"),
        "distance": location_row.get("distance"),
    }


def normalize_result(args, response, kind):
    if args.raw:
        return response
    result = response["results"][0]
    payload = result.get("payload")
    output = {
        "ok": result.get("ok"),
        "status": result.get("status"),
        "source_type": "giant_browser_v5_api",
        "browser_page": response.get("page"),
        "store_context": PARK_ROAD_CONTEXT,
        "url": result.get("url"),
    }
    if not result.get("ok"):
        output["error"] = result.get("error") or result.get("text")
        return output
    if kind == "store":
        output["locations"] = [summarize_location(row) for row in service_location_rows(payload)]
    else:
        output["products"] = [summarize_product(product) for product in product_rows(payload)]
        pagination = ((payload or {}).get("response") or {}).get("pagination")
        if pagination:
            output["pagination"] = pagination
    return output


def command_store(args):
    url = f"/api/v5.0/serviceLocation/stores/{args.store_number}?serviceType={args.service_type}"
    return normalize_result(args, browser_fetch(args.port, [url]), "store")


def command_product(args):
    params = {
        "extendedInfo": "true",
        "flags": "true",
        "nutrition": "true",
        "substitute": "true",
        "categoryInfo": "true",
    }
    url = (
        f"/api/v5.0/products/info/{args.user_id}/{args.service_location_id}/{args.product_id}"
        f"?{urllib.parse.urlencode(params)}"
    )
    return normalize_result(args, browser_fetch(args.port, [url]), "product")


def command_search(args):
    params = {
        "keywords": args.query,
        "sort": args.sort,
        "rows": str(args.rows),
        "start": str(args.start),
        "flags": "true",
        "facet": "nutrition",
        "hkInclude": "true",
        "facetExcludeFilter": "true",
    }
    if args.filter:
        params["filter"] = args.filter
    url = (
        f"/api/v5.0/products/{args.user_id}/{args.service_location_id}"
        f"?{urllib.parse.urlencode(params)}"
    )
    return normalize_result(args, browser_fetch(args.port, [url]), "search")


def add_common_browser_args(parser):
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch = subparsers.add_parser("launch", help="launch a dedicated Chrome session with CDP enabled")
    add_common_browser_args(launch)
    launch.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)
    launch.add_argument("--url", default=DEFAULT_SEED_URL)

    store = subparsers.add_parser("store", help="resolve a Giant store number to service-location metadata")
    add_common_browser_args(store)
    store.add_argument("--store-number", default=DEFAULT_STORE_NUMBER)
    store.add_argument("--service-type", default=DEFAULT_SERVICE_TYPE, choices=["B", "P", "D"])
    store.add_argument("--raw", action="store_true")

    product = subparsers.add_parser("product", help="fetch one live-priced product by Giant product ID")
    add_common_browser_args(product)
    product.add_argument("product_id")
    product.add_argument("--service-location-id", default=DEFAULT_SERVICE_LOCATION_ID)
    product.add_argument("--user-id", default=DEFAULT_USER_ID)
    product.add_argument("--raw", action="store_true")

    search = subparsers.add_parser("search", help="search live-priced Giant products")
    add_common_browser_args(search)
    search.add_argument("query")
    search.add_argument("--service-location-id", default=DEFAULT_SERVICE_LOCATION_ID)
    search.add_argument("--user-id", default=DEFAULT_USER_ID)
    search.add_argument("--rows", type=int, default=12)
    search.add_argument("--start", type=int, default=0)
    search.add_argument("--sort", default="bestMatch asc, name asc")
    search.add_argument("--filter", default="")
    search.add_argument("--raw", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.command == "launch":
            output = launch_chrome(args)
        else:
            wait_for_devtools(args.port)
            if args.command == "store":
                output = command_store(args)
            elif args.command == "product":
                output = command_product(args)
            elif args.command == "search":
                output = command_search(args)
            else:
                raise AssertionError(args.command)
    except GiantBrowserError as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
