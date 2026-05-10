#!/usr/bin/env python3
"""Capture XHR/Fetch traffic on the Giant savings page while a coupon's
details modal is opened. Used to identify any endpoint that returns the
qualifying-products SKU list for a coupon.

Workflow:
  1. Make sure the CDP-enabled Chrome is running with the Giant tab open
     (see giant_browser_api_probe.py launch).
  2. Run this script with --duration 60.
  3. While it is capturing, click into one coupon's "View Coupon Details"
     modal in the browser. Optionally clip + unclip to capture clip flows too.
  4. Inspect the resulting capture JSON.

Default capture writes to giant_coupon_detail_capture.json and prints a
short text summary listing each captured request URL, method, status,
and a body snippet.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.parse
import urllib.request

try:
    import websocket  # type: ignore
except ImportError:
    websocket = None  # type: ignore


DEFAULT_PORT = 9227
DEFAULT_OUTPUT = "giant_coupon_detail_capture.json"
GIANT_HOST = "giantfood.com"


def list_targets(port):
    url = f"http://127.0.0.1:{port}/json/list"
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def find_giant_page(port):
    targets = list_targets(port)
    for target in targets:
        if target.get("type") != "page":
            continue
        if GIANT_HOST in (target.get("url") or ""):
            return target
    raise SystemExit(
        f"No Giant tab on port {port}. Run `python3 giant_browser_api_probe.py launch` "
        "and navigate to https://giantfood.com/savings/coupons/browse first."
    )


def make_call(ws, next_id_ref, method, params=None):
    next_id_ref[0] += 1
    mid = next_id_ref[0]
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    return mid


def drain_until(ws, target_id, timeout):
    """Drain WebSocket messages until we see the response with `target_id`,
    returning that response. All other messages (events) are discarded.
    """
    deadline = time.time() + timeout
    ws.settimeout(timeout)
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        msg = json.loads(raw)
        if msg.get("id") == target_id:
            return msg
    return None


def capture(args):
    if websocket is None:
        raise SystemExit("Missing dependency `websocket-client`; install it with `python3 -m pip install websocket-client`.")

    page = find_giant_page(args.port)
    print(f"Listening on tab: {page.get('url')}", file=sys.stderr)

    ws = websocket.create_connection(
        page["webSocketDebuggerUrl"],
        timeout=30,
        origin=f"http://127.0.0.1:{args.port}",
    )
    next_id_ref = [0]

    make_call(ws, next_id_ref, "Network.enable")
    make_call(ws, next_id_ref, "Page.enable")

    requests_by_id = {}
    responses_by_id = {}
    finished_ids = set()
    failed_ids = set()

    if args.reload:
        print("Triggering page reload to capture initial coupon traffic...", file=sys.stderr)
        make_call(ws, next_id_ref, "Page.reload", {"ignoreCache": True})

    print(
        f"\nCapturing for {args.duration}s. After the reload settles, click 'View Coupon Details' on one coupon.\n"
        "Tip: try one Item-target coupon (e.g. Cheez-It) and one Order/bundle coupon for contrast.\n",
        file=sys.stderr,
    )

    start = time.time()
    deadline = start + args.duration
    ws.settimeout(1.0)
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        msg = json.loads(raw)
        method = msg.get("method")
        if method == "Network.requestWillBeSent":
            params = msg.get("params") or {}
            request = params.get("request") or {}
            url = request.get("url") or ""
            request_type = params.get("type") or ""
            if GIANT_HOST not in url:
                continue
            if args.types and request_type not in args.types:
                continue
            if not args.types and request_type not in ("XHR", "Fetch", "Other", "Document"):
                continue
            if "/api/" not in url and request_type != "Document":
                continue
            requests_by_id[params.get("requestId")] = {
                "url": url,
                "method": request.get("method"),
                "postData": request.get("postData"),
                "type": request_type,
                "headers": request.get("headers") or {},
                "timestamp": params.get("timestamp"),
                "documentURL": params.get("documentURL"),
                "initiator": params.get("initiator"),
            }
        elif method == "Network.responseReceived":
            params = msg.get("params") or {}
            req_id = params.get("requestId")
            if req_id not in requests_by_id:
                continue
            response = params.get("response") or {}
            responses_by_id[req_id] = {
                "status": response.get("status"),
                "mimeType": response.get("mimeType"),
                "responseUrl": response.get("url"),
                "responseHeaders": response.get("headers") or {},
            }
        elif method == "Network.loadingFinished":
            req_id = (msg.get("params") or {}).get("requestId")
            if req_id in requests_by_id:
                finished_ids.add(req_id)
        elif method == "Network.loadingFailed":
            req_id = (msg.get("params") or {}).get("requestId")
            if req_id in requests_by_id:
                failed_ids.add(req_id)

    elapsed = time.time() - start
    print(
        f"\nCapture window done after {elapsed:.1f}s. "
        f"Captured {len(requests_by_id)} XHR/Fetch requests; finished={len(finished_ids)} failed={len(failed_ids)}.",
        file=sys.stderr,
    )

    bodies = {}
    for req_id in sorted(finished_ids):
        target_id = make_call(ws, next_id_ref, "Network.getResponseBody", {"requestId": req_id})
        result = drain_until(ws, target_id, timeout=5)
        if result is None:
            bodies[req_id] = {"error": "timeout"}
            continue
        if "error" in result:
            bodies[req_id] = {"error": result["error"]}
            continue
        payload = result.get("result") or {}
        body_text = payload.get("body") or ""
        if payload.get("base64Encoded"):
            try:
                body_text = base64.b64decode(body_text).decode("utf-8", errors="replace")
            except Exception as exc:
                bodies[req_id] = {"error": f"decode failed: {exc}"}
                continue
        body_record = {"raw_text": body_text}
        try:
            body_record["json"] = json.loads(body_text)
        except Exception:
            pass
        bodies[req_id] = body_record

    ws.close()

    captured = []
    for req_id, request in requests_by_id.items():
        captured.append({
            "requestId": req_id,
            "request": request,
            "response": responses_by_id.get(req_id),
            "body": bodies.get(req_id),
            "finished": req_id in finished_ids,
            "failed": req_id in failed_ids,
        })
    captured.sort(key=lambda r: r["request"].get("timestamp") or 0)

    output = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_seconds": round(elapsed, 2),
        "tab_url": page.get("url"),
        "requests": captured,
    }

    out_path = args.output
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {len(captured)} requests to {out_path}", file=sys.stderr)

    print("\nSummary (most-likely scope/clip endpoints first):", file=sys.stderr)
    def rank(item):
        u = item["request"].get("url") or ""
        score = 0
        for needle in ("qualifying", "products?", "/products/", "couponId", "scope", "clip", "loaded", "details"):
            if needle.lower() in u.lower():
                score += 1
        return -score
    for item in sorted(captured, key=rank):
        url = item["request"].get("url") or ""
        method_ = item["request"].get("method")
        status = (item["response"] or {}).get("status")
        body_record = item.get("body") or {}
        body_json = body_record.get("json")
        if isinstance(body_json, dict):
            top_keys = list(body_json.keys())[:6]
            snippet = f"json keys={top_keys}"
        elif isinstance(body_json, list):
            snippet = f"json list len={len(body_json)}"
        else:
            text = (body_record.get("raw_text") or "").strip().replace("\n", " ")
            snippet = (text[:140] + "...") if len(text) > 140 else text
        post_data = item["request"].get("postData")
        post_snippet = ""
        if post_data:
            post_snippet = f" postData={post_data[:120]}"
        print(f"  [{status}] {method_} {url[:160]}{post_snippet}", file=sys.stderr)
        if snippet:
            print(f"      body: {snippet}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Chrome remote-debugging port")
    parser.add_argument("--duration", type=int, default=60, help="Seconds to capture (open a coupon details modal during this window)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument("--reload", action="store_true", help="Trigger a page reload at the start of the capture")
    parser.add_argument("--types", nargs="*", default=None, help="Limit to specific resource types (e.g. XHR Fetch); default keeps API-ish requests")
    args = parser.parse_args()
    capture(args)


if __name__ == "__main__":
    main()
