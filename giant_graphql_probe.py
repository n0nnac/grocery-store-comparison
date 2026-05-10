#!/usr/bin/env python3
"""Probe Giant Food's mobile Apollo GraphQL catalog endpoint.

This uses query documents recovered from the Giant Food Android app. The live
endpoint is protected by DataDome, so plain shell requests may return a 403
challenge. To test with a browser-validated session, set GIANT_COOKIE_HEADER to
the exact Cookie header from a normal Giant browser session, or set
GIANT_DATADOME_COOKIE to just the datadome cookie value.

This script intentionally does not attempt to solve or bypass DataDome. It is a
known-endpoint harness for validating whether an already-authorized session can
return store-specific product prices for the Park Road store workflow.
"""

import argparse
import json
import os
import sys
import uuid
import urllib.error
import urllib.request


GRAPHQL_URL = "https://core.pdl.giantfood.com/prod/apollo/graphql"
OPCO = "GNTL"
ENVIRONMENT = "prod"
APP_VERSION = "9.0.1-6096"
APP_CLIENT_NAME = "com.giantfood.mobile.droid-apollo-android"
DEFAULT_ZIP = "20010"

GET_SERVICE_LOCATIONS_ID = "88920e787158b1f9ab621d4d50152861b1a5c6f91c3cd19bae8ef1e754309b20"
GET_PRODUCTS_ID = "4c9c74591cdaaaa294d3b143260860d90f0a715d336ebde307668cce70b5410c"

GET_SERVICE_LOCATIONS_QUERY = """
query getServiceLocations($zip: String!, $customerType: ShortCustomerType!, $serviceType: ServiceType!) {
  serviceLocationsV2(zip: $zip, customerType: $customerType, serviceType: $serviceType) {
    locations {
      location {
        id
        name
        pickupLocationId
        address
        city
        state
        zip
        pickupSiblingEnabled
        pickupSiblingSite
        site
        active
        serviceType
        storeNumber
        pickupPointType
        minimumOrderForCheckout
        priceZone
        ecommStoreId
        shipMethod
        deliveryPartner
        substitutionType
      }
      distance
    }
  }
}
""".strip()

GET_PRODUCTS_QUERY = """
query getProducts($keywords: String!, $start: Int!, $limit: Int!, $filter: String!, $sort: String!, $includeSponsors: Boolean!, $serviceLocationId: ID!, $adPositions: [Int]) {
  products(keywords: $keywords, start: $start, limit: $limit, filter: $filter, sort: $sort, includeSponsors: $includeSponsors, serviceLocationId: $serviceLocationId, adPositions: $adPositions) {
    products {
      __typename
      prodId
      name
      rootCatName
      rootCatId
      size
      unitPrice
      unitMeasure
      price
      regularPrice
      image { small medium large xlarge }
      carouselImages { description imageUrl isMobile }
      brand
      flags {
        active
        bogo
        lowPriceEveryday
        outOfStock
        privateLabel
        sale
        suppressed
      }
      hasCoupon
      coupon {
        title
        description
        clippingRequired
        promotionType
        multiQty
        maxDiscount
        targeted
        id
        loaded
        startDate
        endDate
      }
      availableDisplayCoupons {
        title
        description
        clippingRequired
        promotionType
        multiQty
        maxDiscount
        targeted
        id
        loaded
        startDate
        endDate
      }
      upc
      hasPriceAdjustment
      aisle
      section
      bmsm
      bmsmPodGroupId
      bmsmTiers {
        description
        discount
        discountedPrice
        type
        units
      }
      variableWeight
      weightRange {
        __typename
        minValue
        maxValue
        interval
      }
      weightedRegularPrice
      advertiseOnSale
      saleExpiration
      purchaseDisabled
    }
    facets {
      nutrition { id name }
      sustainability { id name }
      brands { id name }
      categories { id name }
    }
    pagination {
      size
      start
      total
      isEndOfList
    }
    pageImpressionId
  }
}
""".strip()


class GiantGraphQLError(RuntimeError):
    pass


def cookie_header_from_env():
    full_cookie = os.environ.get("GIANT_COOKIE_HEADER")
    if full_cookie:
        return full_cookie
    datadome = os.environ.get("GIANT_DATADOME_COOKIE")
    if datadome:
        return f"datadome={datadome}"
    return None


def build_headers(operation_name, operation_id, service_location_id=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "GiantFood/9.0.1 Android",
        "apollographql-client-name": APP_CLIENT_NAME,
        "apollographql-client-version": APP_VERSION,
        "opco": OPCO,
        "env": ENVIRONMENT,
        "GQL-Platform-Origin": "android",
        "X-APOLLO-OPERATION-NAME": operation_name,
        "X-APOLLO-OPERATION-ID": operation_id,
        "X-Correlation-Id": f"grocery-compare-{uuid.uuid4()}",
    }
    if service_location_id:
        headers["service-location-id"] = service_location_id
    cookie_header = cookie_header_from_env()
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def graphql_request(operation_name, operation_id, query, variables, service_location_id=None, timeout=20):
    payload = json.dumps(
        {
            "operationName": operation_name,
            "query": query,
            "variables": variables,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers=build_headers(operation_name, operation_id, service_location_id),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, dict(response.headers), parse_json(body)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        parsed = parse_json(body)
        if is_datadome_block(error.headers, body):
            raise GiantGraphQLError(
                "Giant GraphQL is DataDome-protected for this session. "
                "Set GIANT_COOKIE_HEADER or GIANT_DATADOME_COOKIE from a validated browser session and retry."
            ) from error
        return error.code, dict(error.headers), parsed


def parse_json(body):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def is_datadome_block(headers, body):
    return (
        headers.get("x-datadome") == "protected"
        or "captcha-delivery.com" in body
        or "DataDome" in body
    )


def service_locations(args):
    return graphql_request(
        "getServiceLocations",
        GET_SERVICE_LOCATIONS_ID,
        GET_SERVICE_LOCATIONS_QUERY,
        {
            "zip": args.zip,
            "customerType": args.customer_type,
            "serviceType": args.service_type,
        },
        timeout=args.timeout,
    )


def search_products(args):
    variables = {
        "keywords": args.query,
        "start": args.start,
        "limit": args.limit,
        "filter": args.filter,
        "sort": args.sort,
        "includeSponsors": args.include_sponsors,
        "serviceLocationId": args.service_location_id,
    }
    if args.ad_positions:
        variables["adPositions"] = args.ad_positions
    return graphql_request(
        "getProducts",
        GET_PRODUCTS_ID,
        GET_PRODUCTS_QUERY,
        variables,
        service_location_id=args.service_location_id,
        timeout=args.timeout,
    )


def print_result(status, headers, payload, include_headers=False):
    output = {"status": status, "payload": payload}
    if include_headers:
        output["headers"] = {
            key: value
            for key, value in headers.items()
            if key.lower() not in {"set-cookie", "cookie"}
        }
    print(json.dumps(output, indent=2, sort_keys=True))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--headers", action="store_true", help="include non-cookie response headers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    locations = subparsers.add_parser("service-locations", help="fetch pickup/delivery service locations")
    locations.add_argument("--timeout", type=int, default=20)
    locations.add_argument("--headers", action="store_true", help="include non-cookie response headers")
    locations.add_argument("--zip", default=DEFAULT_ZIP)
    locations.add_argument("--customer-type", default="C", help="ShortCustomerType enum; app values include C and M")
    locations.add_argument("--service-type", default="P", help="ServiceType enum; app values include B, D, and P")

    search = subparsers.add_parser("search", help="search store-priced products")
    search.add_argument("--timeout", type=int, default=20)
    search.add_argument("--headers", action="store_true", help="include non-cookie response headers")
    search.add_argument("query")
    search.add_argument("--service-location-id", required=True)
    search.add_argument("--start", type=int, default=0)
    search.add_argument("--limit", type=int, default=12)
    search.add_argument("--filter", default="")
    search.add_argument("--sort", default="")
    search.add_argument("--include-sponsors", action="store_true")
    search.add_argument("--ad-positions", type=int, nargs="*")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.command == "service-locations":
            result = service_locations(args)
        elif args.command == "search":
            result = search_products(args)
        else:
            raise AssertionError(args.command)
    except GiantGraphQLError as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    print_result(*result, include_headers=args.headers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
