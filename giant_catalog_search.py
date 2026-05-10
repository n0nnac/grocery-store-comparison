#!/usr/bin/env python3
"""Probe Giant's static grocery catalog pages for product prices.

Giant's live shopping app and historical Peapod API routes are protected by
DataDome from a plain shell session. The SEO/static grocery pages are reachable
and expose schema.org Product/Offer JSON-LD. This script searches those pages as
an interim base-price source while keeping freshness metadata visible.
"""

import argparse
import heapq
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser


BASE_URL = "https://giantfood.com"
DEFAULT_STORE_NUMBER = "0378"
DEFAULT_STORE_ADDRESS = "1345 Park Road, NW, Washington, DC 20010"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

QUERY_CATEGORY_HINTS = {
    "beef": ["/groceries/meat.html"],
    "steak": ["/groceries/meat.html"],
    "ground": ["/groceries/meat.html"],
    "chicken": ["/groceries/meat.html"],
    "pork": ["/groceries/meat.html"],
    "turkey": ["/groceries/meat.html"],
    "salmon": ["/groceries/seafood.html"],
    "shrimp": ["/groceries/seafood.html"],
    "fish": ["/groceries/seafood.html"],
    "seafood": ["/groceries/seafood.html"],
    "apple": ["/groceries/produce.html"],
    "banana": ["/groceries/produce.html"],
    "onion": ["/groceries/produce.html"],
    "tomato": ["/groceries/produce.html"],
    "potato": ["/groceries/produce.html"],
    "pepper": ["/groceries/produce.html"],
    "lettuce": ["/groceries/produce.html"],
    "produce": ["/groceries/produce.html"],
    "milk": ["/groceries/dairy-eggs.html", "/groceries/dairy-eggs/milk.html"],
    "egg": ["/groceries/dairy-eggs.html"],
    "cheese": ["/groceries/dairy-eggs.html"],
    "butter": ["/groceries/dairy-eggs.html"],
    "yogurt": ["/groceries/dairy-eggs.html"],
    "rice": ["/groceries/rice-pasta-beans.html"],
    "pasta": ["/groceries/rice-pasta-beans.html"],
    "bean": ["/groceries/rice-pasta-beans.html"],
    "bread": ["/groceries/bread-bakery.html"],
    "bakery": ["/groceries/bread-bakery.html"],
    "frozen": ["/groceries/frozen.html"],
}


def today_iso():
    return date.today().isoformat()


class CatalogHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.ld_json_blocks = []
        self.script_blocks = []
        self._current_link = None
        self._current_script = None
        self._current_script_type = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self._current_link = {"href": attrs["href"], "text": []}
        elif tag == "script":
            self._current_script = []
            self._current_script_type = attrs.get("type") or ""

    def handle_data(self, data):
        if self._current_link is not None:
            self._current_link["text"].append(data)
        if self._current_script is not None:
            self._current_script.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._current_link is not None:
            text = " ".join("".join(self._current_link["text"]).split())
            self.links.append({"href": self._current_link["href"], "text": html.unescape(text)})
            self._current_link = None
        elif tag == "script" and self._current_script is not None:
            script_text = "".join(self._current_script)
            if self._current_script_type == "application/ld+json":
                self.ld_json_blocks.append(script_text)
            else:
                self.script_blocks.append(script_text)
            self._current_script = None
            self._current_script_type = None


def abs_url(url):
    return urllib.parse.urljoin(BASE_URL, url)


def fetch_page(url, timeout):
    req = urllib.request.Request(
        abs_url(url),
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        return body, dict(response.headers), response.geturl()


def parse_page(html_text):
    parser = CatalogHTMLParser()
    parser.feed(html_text)
    return parser


def parse_app_config(script_blocks):
    joined = "\n".join(script_blocks)
    product_id = match_int(r"productId\s*:\s*(\d+)", joined)
    api_key = match_int(r"apiKey\s*:\s*(\d+)", joined)
    categories = []
    categories_match = re.search(r"categories\s*:\s*\[([^\]]*)\]", joined)
    if categories_match:
        categories = [
            int(value)
            for value in re.findall(r"\d+", categories_match.group(1))
        ]
    return {
        "api_key": api_key,
        "product_id": product_id,
        "categories": categories,
    }


def match_int(pattern, text):
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def normalize_price(value):
    if value is None:
        return None
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def normalize_product(item, source_url, headers, app_config=None):
    offers = item.get("offers") or {}
    brand = item.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    product_url = item.get("url") or offers.get("url") or source_url
    return {
        "name": html.unescape(item.get("name") or ""),
        "brand": html.unescape(brand or ""),
        "product_id": (app_config or {}).get("product_id"),
        "url": product_url,
        "image": item.get("image"),
        "price": normalize_price(offers.get("price")),
        "price_currency": offers.get("priceCurrency"),
        "price_valid_until": offers.get("priceValidUntil"),
        "availability": offers.get("availability"),
        "source_url": source_url,
        "source_last_modified": headers.get("Last-Modified"),
        "observed_on": today_iso(),
        "source_type": "giant_static_seo_catalog",
        "store_number": DEFAULT_STORE_NUMBER,
        "store_address": DEFAULT_STORE_ADDRESS,
    }


def extract_products(parser, source_url, headers):
    app_config = parse_app_config(parser.script_blocks)
    products = []
    for block in parser.ld_json_blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            payloads = payload
        else:
            payloads = [payload]
        for data in payloads:
            data_type = data.get("@type")
            if data_type == "ItemList":
                for element in data.get("itemListElement", []):
                    item = element.get("item") or {}
                    if item.get("@type") == "Product":
                        product = normalize_product(item, source_url, headers)
                        product["position"] = element.get("position")
                        products.append(product)
            elif data_type == "Product":
                products.append(normalize_product(data, source_url, headers, app_config))
    return products


def tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def score_text(query_terms, text):
    if not text:
        return 0
    lowered = html.unescape(text).lower()
    score = 0
    query_phrase = " ".join(query_terms)
    if query_phrase and query_phrase in lowered:
        score += 20
    for term in query_terms:
        if term in lowered:
            score += 4
    return score


def score_product(query_terms, product):
    haystack = " ".join(
        str(product.get(key) or "")
        for key in ("name", "brand", "url")
    )
    score = score_text(query_terms, haystack)
    if product.get("name") and all(term in product["name"].lower() for term in query_terms):
        score += 10
    if product.get("name", "").lower().startswith("giant "):
        score += 8
    if product.get("brand", "").lower() in {"giant", "ahold", "nature's promise"}:
        score += 4
    if product.get("price") is not None:
        score += 1
    return score


def is_grocery_html_link(url):
    parsed = urllib.parse.urlparse(abs_url(url))
    return parsed.netloc == "giantfood.com" and parsed.path.startswith("/groceries/") and parsed.path.endswith(".html")


def link_priority(query_terms, link):
    text = f"{link.get('text', '')} {link.get('href', '')}"
    return score_text(query_terms, text)


def seed_urls_for_query(query_terms):
    urls = ["/groceries/index.html"]
    for term in query_terms:
        urls.extend(QUERY_CATEGORY_HINTS.get(term, []))
        singular = term[:-1] if term.endswith("s") else term
        urls.extend(QUERY_CATEGORY_HINTS.get(singular, []))
    deduped = []
    seen = set()
    for url in urls:
        full = abs_url(url)
        if full not in seen:
            deduped.append(full)
            seen.add(full)
    return deduped


def dedupe_products(products):
    deduped = {}
    for product in products:
        key = (
            f"url:{product.get('url')}"
            if product.get("url")
            else f"id:{product.get('product_id')}"
            if product.get("product_id") is not None
            else (
                f"image:{product.get('image')}|{product.get('name')}|{product.get('price')}"
                if product.get("image")
                else f"{product.get('name')}|{product.get('price')}"
            )
        )
        current = deduped.get(key)
        if current is None or (current.get("product_id") is None and product.get("product_id") is not None):
            deduped[key] = product
    return list(deduped.values())


def crawl_search(query, max_pages, rows, detail_rows, timeout):
    query_terms = tokenize(query)
    if not query_terms:
        return {"query": query, "visited_pages": [], "docs": []}

    queue = []
    queued = set()
    visited = []
    products = []
    sequence = 0

    def push(url, priority):
        nonlocal sequence
        full = abs_url(url)
        if full in queued:
            return
        queued.add(full)
        heapq.heappush(queue, (-priority, sequence, full))
        sequence += 1

    for seed in seed_urls_for_query(query_terms):
        push(seed, 100)

    while queue and len(visited) < max_pages:
        _priority, _sequence, url = heapq.heappop(queue)
        if url in visited:
            continue
        try:
            body, headers, final_url = fetch_page(url, timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            visited.append({"url": url, "error": str(exc)})
            continue
        parser = parse_page(body)
        page_products = extract_products(parser, final_url, headers)
        products.extend(page_products)
        visited.append(
            {
                "url": final_url,
                "last_modified": headers.get("Last-Modified"),
                "product_count": len(page_products),
                "link_count": len(parser.links),
            }
        )
        for link in parser.links:
            href = link.get("href", "")
            if not is_grocery_html_link(href):
                continue
            priority = link_priority(query_terms, link)
            if priority > 0:
                push(href, priority)

    scored = []
    for product in dedupe_products(products):
        score = score_product(query_terms, product)
        if score > 0:
            product["match_score"] = score
            scored.append(product)
    scored.sort(key=lambda product: (-product["match_score"], product.get("price") is None, product.get("name") or ""))

    if detail_rows:
        scored = enrich_with_product_pages(scored, detail_rows, timeout)
        scored = dedupe_products(scored)
        scored.sort(key=lambda product: (-product["match_score"], product.get("price") is None, product.get("name") or ""))

    return {
        "query": query,
        "source": "Giant static grocery catalog JSON-LD",
        "store_number": DEFAULT_STORE_NUMBER,
        "store_address": DEFAULT_STORE_ADDRESS,
        "visited_pages": visited,
        "docs": scored[:rows],
    }


def enrich_with_product_pages(products, detail_rows, timeout):
    enriched = []
    for index, product in enumerate(products):
        if index >= detail_rows or not product.get("url") or product.get("product_id") is not None:
            enriched.append(product)
            continue
        try:
            body, headers, final_url = fetch_page(product["url"], timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            enriched.append(product)
            continue
        parser = parse_page(body)
        detail_products = extract_products(parser, final_url, headers)
        if detail_products:
            detail = detail_products[0]
            detail["match_score"] = product.get("match_score")
            enriched.append(detail)
        else:
            enriched.append(product)
    enriched.extend(products[len(enriched):])
    return enriched


def freshness_label(product):
    valid_until = product.get("price_valid_until")
    if not valid_until:
        return "unknown"
    if valid_until < today_iso():
        return f"stale after {valid_until}"
    return f"valid until {valid_until}"


def print_table(result):
    print(f"Giant store #{result['store_number']} ({DEFAULT_STORE_ADDRESS})")
    print(f"{result['query']!r}: {len(result['docs'])} matched products from {len(result['visited_pages'])} pages")
    print(f"{'Product ID':<10} {'Price':>8} {'Freshness':<24} Name")
    print("-" * 100)
    for product in result["docs"]:
        product_id = str(product.get("product_id") or "")
        price = "" if product.get("price") is None else f"${product['price']:.2f}"
        print(f"{product_id:<10} {price:>8} {freshness_label(product):<24} {product.get('name')}")
        if product.get("url"):
            print(f"{'':<10} {'':>8} {'':<24} {product['url']}")


def main():
    parser = argparse.ArgumentParser(description="Search Giant's reachable static grocery catalog pages.")
    parser.add_argument("query", help="Search text, e.g. 'milk', 'ground beef', or 'rice'")
    parser.add_argument("--rows", type=int, default=10, help="Number of products to print")
    parser.add_argument("--max-pages", type=int, default=14, help="Maximum category/static pages to crawl")
    parser.add_argument("--detail-rows", type=int, default=5, help="Fetch product detail pages for the first N matches")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print normalized JSON")
    args = parser.parse_args()

    result = crawl_search(
        args.query,
        max_pages=args.max_pages,
        rows=args.rows,
        detail_rows=args.detail_rows,
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_table(result)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
