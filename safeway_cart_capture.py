#!/usr/bin/env python3
"""Capture and parse Safeway cart/checkout page text.

This is a read-only bridge from the logged-in Safeway cart page to the
observed-cart JSON schema used by safeway_cart_reconcile.py.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from safeway_coupon_account_state import CdpClient, cdp_page
from safeway_coupon_search import write_json


ROOT = Path(__file__).parent
DEFAULT_CAPTURE = ROOT / f"safeway_cart_capture_{datetime.now().date().isoformat()}.json"
DEFAULT_TEMPLATE = ROOT / "safeway_cart_observed_template.json"
DEFAULT_OBSERVED = ROOT / f"safeway_cart_observed_{datetime.now().date().isoformat()}.json"

MONEY_RE = re.compile(r"[-+]?\$?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
POINTS_RE = re.compile(r"([0-9]+)\s+(?:for\s+U\s+)?points?", re.IGNORECASE)

SUMMARY_LABELS = {
    "item_subtotal": (
        "item subtotal",
        "items subtotal",
        "estimated subtotal",
        "subtotal",
    ),
    "club_card_savings": (
        "club card savings",
        "member savings",
    ),
    "coupon_savings": (
        "coupon savings",
        "coupons",
        "digital coupons",
        "for u savings",
    ),
    "rewards_savings": (
        "rewards savings",
        "reward savings",
        "rewards redeemed",
    ),
    "tax": (
        "estimated tax",
        "tax",
        "taxes",
    ),
    "fees": (
        "fees",
        "service fee",
        "delivery fee",
        "pickup fee",
        "bag fee",
    ),
    "estimated_total": (
        "estimated total",
        "order total",
        "cart total",
        "total due",
    ),
}


def load_json(path):
    with Path(path).open() as f:
        return json.load(f)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def money_value(text):
    if not text:
        return None
    matches = MONEY_RE.findall(str(text))
    if not matches:
        return None
    try:
        return round(float(matches[-1].replace(",", "")), 2)
    except ValueError:
        return None


def all_money_values(text):
    values = []
    for match in MONEY_RE.findall(str(text or "")):
        try:
            values.append(round(float(match.replace(",", "")), 2))
        except ValueError:
            pass
    return values


def normalize_line(line):
    return " ".join(str(line or "").split())


def text_lines(text):
    return [line for line in (normalize_line(line) for line in str(text or "").splitlines()) if line]


def read_page_capture(cdp_url):
    page = cdp_page(cdp_url)
    client = CdpClient(page["webSocketDebuggerUrl"], cdp_url.rstrip("/"))
    try:
        client.call("Runtime.enable")
        expression = """
(function(){
  const visibleText = document.body ? document.body.innerText : "";
  const meta = Array.from(document.querySelectorAll('meta')).map((node) => ({
    name: node.getAttribute('name'),
    property: node.getAttribute('property'),
    content: node.getAttribute('content')
  }));
  return {
    captured_at: new Date().toISOString(),
    url: location.href,
    title: document.title,
    body_text: visibleText,
    meta
  };
})()
"""
        payload = client.evaluate(expression)
    finally:
        client.close()

    return {
        "metadata": {
            "source_type": "safeway_cart_browser_capture",
            "captured_on": datetime.now().date().isoformat(),
            "cdp_url": cdp_url,
        },
        "page": payload,
    }


def detect_page_problem(lines):
    joined = " ".join(lines).lower()
    if "service problem" in joined or "technical difficulties" in joined:
        return "Safeway service problem / technical difficulties dialog was visible."
    if "cart is empty" in joined or "your cart is empty" in joined:
        return "Safeway cart appears empty."
    return None


def nearby_money(lines, index, lookahead=3, include_current=True):
    start = index if include_current else index + 1
    for probe in range(start, min(len(lines), index + lookahead + 1)):
        values = all_money_values(lines[probe])
        if values:
            return values[-1], probe
    return None, None


def extract_summary(lines):
    summary = {
        "item_subtotal": None,
        "club_card_savings": None,
        "coupon_savings": None,
        "rewards_savings": None,
        "tax": None,
        "fees": None,
        "estimated_total": None,
        "points_earned": None,
    }
    evidence = {}

    for index, line in enumerate(lines):
        lowered = line.lower()
        for field, labels in SUMMARY_LABELS.items():
            if summary[field] is not None:
                continue
            if any(label in lowered for label in labels):
                value, value_index = nearby_money(lines, index)
                if value is not None:
                    summary[field] = value
                    evidence[field] = {
                        "label_line": line,
                        "value_line": lines[value_index],
                    }

        if summary["points_earned"] is None and ("earn" in lowered or "points" in lowered):
            match = POINTS_RE.search(line)
            if match:
                summary["points_earned"] = int(match.group(1))
                evidence["points_earned"] = {"line": line}

    return summary, evidence


def candidate_item_names(template_item):
    names = [
        template_item.get("safeway_name"),
        template_item.get("matched_item"),
        template_item.get("product_id"),
    ]
    return [normalize_line(name).lower() for name in names if normalize_line(name)]


def extract_item_observations(lines, template_items):
    rows = []
    lower_lines = [line.lower() for line in lines]
    for template_item in template_items:
        row = dict(template_item)
        row.setdefault("observed_unit_price", None)
        row.setdefault("observed_line_total", None)
        row.setdefault("observed_savings", None)
        row.setdefault("notes", None)
        row["capture_evidence"] = None

        best_index = None
        best_name = None
        for name in candidate_item_names(template_item):
            for index, lowered in enumerate(lower_lines):
                if name and name in lowered:
                    best_index = index
                    best_name = name
                    break
            if best_index is not None:
                break

        if best_index is not None:
            window = lines[best_index : min(len(lines), best_index + 8)]
            values = all_money_values(" ".join(window))
            if values:
                row["observed_line_total"] = values[-1]
                if template_item.get("qty") and template_item.get("qty") != 0:
                    row["observed_unit_price"] = round(values[-1] / float(template_item["qty"]), 2)
            row["capture_evidence"] = {
                "matched_name": best_name,
                "matched_line": lines[best_index],
                "window": window,
            }
        rows.append(row)
    return rows


def parse_capture(capture, template):
    page = capture.get("page") or {}
    lines = text_lines(page.get("body_text") or "")
    summary, summary_evidence = extract_summary(lines)
    problem = detect_page_problem(lines)

    observed = {
        "metadata": {
            **(template.get("metadata") or {}),
            "source_type": "safeway_cart_browser_capture",
            "observed_on": datetime.now().date().isoformat(),
            "captured_at": page.get("captured_at") or capture.get("metadata", {}).get("captured_at"),
            "url": page.get("url"),
            "title": page.get("title"),
            "page_problem": problem,
            "summary_evidence": summary_evidence,
        },
        "summary": {
            **((template.get("summary") or {})),
            **{key: value for key, value in summary.items() if value is not None},
        },
        "items": extract_item_observations(lines, template.get("items", [])),
        "discounts": template.get("discounts", []),
        "rewards": {
            **(template.get("rewards") or {}),
        },
    }
    if not observed["metadata"]["page_problem"]:
        any_summary = any(value is not None for value in observed["summary"].values())
        any_items = any(item.get("capture_evidence") for item in observed["items"])
        if not any_summary and not any_items:
            observed["metadata"]["page_problem"] = "No cart totals or item lines were visible in the captured page."
    if observed["summary"].get("points_earned") is not None:
        observed["rewards"]["points_earned"] = observed["summary"]["points_earned"]
    return observed


def print_capture_summary(observed):
    metadata = observed.get("metadata") or {}
    summary = observed.get("summary") or {}
    print("\nSafeway cart browser capture")
    print(f"URL: {metadata.get('url')}")
    print(f"Title: {metadata.get('title')}")
    if metadata.get("page_problem"):
        print(f"Page problem: {metadata['page_problem']}")
    print(f"Item subtotal: {summary.get('item_subtotal')}")
    print(f"Coupon savings: {summary.get('coupon_savings')}")
    print(f"Rewards savings: {summary.get('rewards_savings')}")
    print(f"Tax: {summary.get('tax')}")
    print(f"Fees: {summary.get('fees')}")
    print(f"Estimated total: {summary.get('estimated_total')}")
    print(f"Points earned: {summary.get('points_earned')}")
    matched = sum(1 for item in observed.get("items", []) if item.get("capture_evidence"))
    print(f"Matched item lines: {matched}/{len(observed.get('items', []))}")


def cmd_capture(args):
    capture = read_page_capture(args.cdp_url)
    output = Path(args.output or DEFAULT_CAPTURE)
    write_json(output, capture)
    print(f"Wrote raw Safeway cart capture: {output}")
    print(f"URL: {(capture.get('page') or {}).get('url')}")


def cmd_parse(args):
    capture = load_json(args.capture)
    template = load_json(args.template)
    observed = parse_capture(capture, template)
    output = Path(args.output or DEFAULT_OBSERVED)
    write_json(output, observed)
    print(f"Wrote observed Safeway cart JSON: {output}")
    print_capture_summary(observed)


def main():
    parser = argparse.ArgumentParser(description="Read-only Safeway cart page capture and observed-cart parser.")
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser("capture", help="Capture current Safeway cart/checkout page text through CDP")
    capture_parser.add_argument("--cdp-url", default="http://127.0.0.1:9223", help="Chrome DevTools Protocol HTTP URL")
    capture_parser.add_argument("--output", help=f"Output path, default {DEFAULT_CAPTURE.name}")
    capture_parser.set_defaults(func=cmd_capture)

    parse_parser = sub.add_parser("parse", help="Parse a raw cart capture into observed-cart JSON")
    parse_parser.add_argument("capture", help="Raw capture JSON")
    parse_parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="Observed-cart template from safeway_cart_reconcile.py")
    parse_parser.add_argument("--output", help=f"Observed cart output path, default {DEFAULT_OBSERVED.name}")
    parse_parser.set_defaults(func=cmd_parse)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
