#!/usr/bin/env python3
"""Import Safeway Rewards dashboard tiles into safeway_rewards.json.

The Rewards dashboard is account-specific, so this importer starts from a
captured text snapshot of each points tab. It can also make a conservative
best-effort Safeway product search to estimate product reward values.
"""

import argparse
import json
import re
import urllib.error
import sys
from datetime import datetime
from pathlib import Path

from safeway_api_search import DEFAULT_BANNER, DEFAULT_CHANNEL, DEFAULT_STORE_ID, fetch_search, normalize_doc
from safeway_coupon_search import write_json


ROOT = Path(__file__).parent
REWARDS_FILE = ROOT / "safeway_rewards.json"


POINT_TAB_RE = re.compile(r"^([0-9]+)\s+pts$")
USE_POINTS_RE = re.compile(r"^Use\s+([0-9]+)\s+pts$")
DOLLAR_OFF_RE = re.compile(r"^\$([0-9]+(?:\.[0-9]{1,2})?)\s+OFF$")
SKIP_LINES = {
    "Reward Details",
    "More rewards loaded",
    "",
}
SNAPSHOT_VALUE_RE = re.compile(r"- generic: (.+)$")
SNAPSHOT_USE_RE = re.compile(r"- button \"Use ([0-9]+) points for (.+?)\": Use [0-9]+ pts")


def load_json(path):
    with path.open() as f:
        return json.load(f)


def observed_on():
    return datetime.now().date().isoformat()


def normalize_text(value):
    return " ".join(str(value or "").split())


def slug(value):
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return re.sub(r"_+", "_", cleaned)


def parse_point_cost(label):
    match = re.search(r"([0-9]+)", str(label))
    if not match:
        raise ValueError(f"Could not parse point cost from label: {label}")
    return int(match.group(1))


def usable_lines(section_text):
    lines = []
    for raw_line in section_text.splitlines():
        line = normalize_text(raw_line)
        if POINT_TAB_RE.match(line):
            continue
        if line in SKIP_LINES:
            continue
        lines.append(line)
    return lines


def parse_reward_sections(raw_sections):
    rewards = []
    for section in raw_sections:
        point_cost = parse_point_cost(section["label"])
        lines = usable_lines(section.get("text") or "")
        index = 0
        while index < len(lines):
            headline = lines[index]
            index += 1
            body = []
            while index < len(lines) and not USE_POINTS_RE.match(lines[index]):
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                break
            use_match = USE_POINTS_RE.match(lines[index])
            index += 1
            used_points = int(use_match.group(1))
            if used_points != point_cost:
                continue

            reward = normalize_reward(point_cost, headline, body)
            rewards.append(reward)
    return rewards


def parse_adjacent_sections(raw_sections):
    rewards = []
    for section in raw_sections or []:
        if section.get("label") != "More ways to use":
            continue
        lines = []
        for raw_line in (section.get("snapshot") or "").splitlines():
            value_match = SNAPSHOT_VALUE_RE.search(raw_line)
            if value_match:
                lines.append(normalize_text(value_match.group(1)))
                continue
            use_match = SNAPSHOT_USE_RE.search(raw_line)
            if use_match:
                lines.append(f"Use {use_match.group(1)} pts")

        index = 0
        while index < len(lines):
            headline = lines[index]
            index += 1
            if not DOLLAR_OFF_RE.match(headline):
                continue
            body = []
            while index < len(lines) and not USE_POINTS_RE.match(lines[index]):
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                break
            point_cost = int(USE_POINTS_RE.match(lines[index]).group(1))
            index += 1
            reward = normalize_reward(point_cost, headline, body)
            reward["type"] = "service_fee_cash_off"
            reward["source_tab"] = "More ways to use"
            rewards.append(reward)
    return rewards


def normalize_reward(point_cost, headline, body):
    amount_match = DOLLAR_OFF_RE.match(headline)
    name_parts = []
    condition_parts = []
    limit = None

    for line in body:
        if line.startswith("Limit "):
            limit = line
        elif line.startswith("of ") or line.startswith("Excludes "):
            condition_parts.append(line)
        else:
            name_parts.append(line)

    name = normalize_text(" ".join(name_parts))
    condition = normalize_text(" ".join(condition_parts)) or None

    if amount_match:
        amount = float(amount_match.group(1))
        if name == "Your Next Purchase":
            reward_type = "grocery_cash_off"
        elif "Department Purchase" in name:
            reward_type = "department_cash_off"
        else:
            reward_type = "category_cash_off"
        estimated_value = amount
    elif headline == "FREE":
        reward_type = "product_reward"
        amount = None
        estimated_value = None
    else:
        reward_type = "unknown"
        amount = None
        estimated_value = None

    reward_id = f"dashboard_{point_cost}_{slug(headline + '_' + name)[:80]}"
    return {
        "id": reward_id,
        "name": name,
        "type": reward_type,
        "point_cost": point_cost,
        "display_value": headline,
        "estimated_value": estimated_value,
        "minimum_purchase": amount if reward_type in {"grocery_cash_off", "department_cash_off", "category_cash_off"} else None,
        "condition": condition,
        "limit": limit,
        "requires_clip": True,
        "availability": "account_dashboard",
        "source_type": "rewards_dashboard",
        "observed_on": observed_on(),
        "price_resolution": None,
    }


def product_query(reward):
    name = reward.get("name") or ""
    query = re.sub(r"\bExcludes?\b.*$", "", name, flags=re.IGNORECASE)
    query = re.sub(r"\bLimit\s+[0-9]+\.?$", "", query, flags=re.IGNORECASE)
    return normalize_text(query)


def token_set(value):
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if token not in {"or", "and", "the", "to", "oz", "ct", "pk", "lb", "bag", "bottle"}
    }


def token_overlap_score(query, product_name):
    query_tokens = token_set(query)
    product_tokens = token_set(product_name)
    if not query_tokens:
        return 0.0
    return len(query_tokens & product_tokens) / len(query_tokens)


def number_value(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def size_ranges(value):
    text = value.lower().replace("fl. oz", "fl oz").replace("fl oz.", "fl oz")
    pattern = re.compile(
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:to|-)\s*([0-9]+(?:\.[0-9]+)?)\s*"
        r"(fl oz|oz|lb|ct|pk|pack|rolls?|quart|gallon)"
        r"|([0-9]+(?:\.[0-9]+)?)\s*-?\s*"
        r"(fl oz|oz|lb|ct|pk|pack|rolls?|quart|gallon)"
    )
    ranges = []
    for match in pattern.finditer(text):
        if match.group(1):
            low = number_value(match.group(1))
            high = number_value(match.group(2))
            unit = normalize_unit(match.group(3))
        else:
            low = high = number_value(match.group(4))
            unit = normalize_unit(match.group(5))
        if low is not None and high is not None and unit:
            ranges.append((min(low, high), max(low, high), unit))
    return ranges


def normalize_unit(unit):
    unit = (unit or "").lower().strip()
    return {
        "pack": "pk",
        "roll": "rolls",
    }.get(unit, unit)


def sizes_compatible(reward_name, product_name):
    reward_sizes = size_ranges(reward_name)
    product_sizes = size_ranges(product_name)
    if not reward_sizes or not product_sizes:
        return None
    for reward_low, reward_high, reward_unit in reward_sizes:
        for product_low, product_high, product_unit in product_sizes:
            if reward_unit == product_unit and product_low >= reward_low and product_high <= reward_high:
                return True
    return False


def excluded_terms(reward_name):
    match = re.search(r"excludes?\s+(.+)$", reward_name, flags=re.IGNORECASE)
    if not match:
        return []
    text = match.group(1)
    text = re.sub(r"\blimit\s+[0-9]+\.?$", "", text, flags=re.IGNORECASE)
    parts = re.split(r",|\band\b|/|;", text)
    return [normalize_text(part).lower().rstrip(".") for part in parts if normalize_text(part)]


def product_is_excluded(reward_name, product_name):
    product_l = product_name.lower()
    for term in excluded_terms(reward_name):
        tokens = token_set(term)
        if tokens and tokens <= token_set(product_l):
            return True
        if term and term in product_l:
            return True
    return False


def product_conflict(reward_name, product_name):
    reward_tokens = token_set(reward_name)
    product_tokens = token_set(product_name)
    conflict_groups = [
        ("bacon", {"dressing", "dip", "bits"}),
        ("breast", {"thighs", "wings", "drumsticks"}),
        ("sausage", {"dressing", "sauce"}),
        ("pizza", {"sauce", "snack"}),
    ]
    for required, conflicts in conflict_groups:
        if required in reward_tokens and product_tokens & conflicts:
            return True
    if product_is_excluded(reward_name, product_name):
        return True
    return False


def candidate_rank(reward_name, query, candidate, search_rank):
    product_name = candidate.get("name") or ""
    size_ok = sizes_compatible(reward_name, product_name)
    conflict = product_conflict(reward_name, product_name)
    score = token_overlap_score(query, product_name)
    if size_ok is True:
        score += 0.3
    elif size_ok is False:
        score -= 0.5
    if conflict:
        score -= 1.0
    if "signature select" in reward_name.lower() and "signature select" in product_name.lower():
        score += 0.1
    return {
        "score": round(score, 4),
        "size_compatible": size_ok,
        "conflict": conflict,
        "search_rank": search_rank,
    }


def resolve_product_value(reward, args):
    if reward.get("type") != "product_reward":
        return reward
    query = product_query(reward)
    if not query:
        return reward

    try:
        payload = fetch_search(
            query,
            args.store_id,
            args.rows,
            0,
            args.banner,
            args.channel,
            args.timeout,
        )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        reward["price_resolution"] = {
            "query": query,
            "status": "error",
            "error": str(exc),
        }
        return reward

    docs = [normalize_doc(doc) for doc in payload.get("response", {}).get("docs", [])]
    candidates = []
    for index, doc in enumerate(docs):
        value = doc.get("price")
        if value is None:
            value = doc.get("base_price")
        if value is None:
            continue
        rank = candidate_rank(reward.get("name") or "", query, doc, index)
        candidates.append(
            {
                "pid": doc.get("pid"),
                "upc": doc.get("upc"),
                "name": doc.get("name"),
                "price": value,
                "current_price": doc.get("price"),
                "base_price": doc.get("base_price"),
                **rank,
            }
        )

    candidates.sort(key=lambda row: (-row["score"], row["search_rank"]))
    best = candidates[0] if candidates else None
    confidence = "none"
    if best:
        confidence = "search_top_result_review"
        if (
            best["score"] >= args.min_score
            and best.get("conflict") is not True
            and best.get("size_compatible") is not False
        ):
            reward["estimated_value"] = best["price"]
            reward["product_id"] = best["pid"]
            reward["upc"] = best["upc"]
            confidence = "search_top_result"

    reward["price_resolution"] = {
        "query": query,
        "status": "resolved" if best else "no_price_candidate",
        "confidence": confidence,
        "selected": best,
        "candidates": candidates[: args.keep_candidates],
    }
    return reward


def merge_dashboard_rewards(config, rewards, raw_sections, source_path):
    config.setdefault("dashboard_rewards", [])
    by_id = {reward["id"]: reward for reward in config.get("dashboard_rewards", [])}
    for reward in rewards:
        by_id[reward["id"]] = reward
    config["dashboard_rewards"] = sorted(
        by_id.values(),
        key=lambda row: (row.get("point_cost") or 0, row.get("type") or "", row.get("name") or ""),
    )
    config["product_rewards"] = [
        reward for reward in config["dashboard_rewards"]
        if reward.get("type") == "product_reward"
    ]
    metadata = config.setdefault("metadata", {})
    metadata["dashboard_last_checked_on"] = observed_on()
    metadata["dashboard_source_type"] = "rewards_dashboard"
    metadata["dashboard_capture"] = {
        "source_path": str(source_path),
        "sections": len(raw_sections),
        "rewards": len(rewards),
    }
    return config


def print_summary(rewards, write):
    mode = "Wrote" if write else "Dry run"
    print(f"{mode}: Safeway Rewards dashboard import")
    print(f"- rewards parsed: {len(rewards)}")
    by_points = {}
    valued = 0
    for reward in rewards:
        by_points[reward["point_cost"]] = by_points.get(reward["point_cost"], 0) + 1
        valued += int(reward.get("estimated_value") is not None)
    for points in sorted(by_points):
        print(f"- {points} pts: {by_points[points]} rewards")
    print(f"- rewards with estimated value: {valued}")


def main():
    parser = argparse.ArgumentParser(description="Import Safeway Rewards dashboard text capture.")
    parser.add_argument("capture", type=Path, help="JSON file with [{label, text}] dashboard tab captures")
    parser.add_argument("--adjacent-capture", type=Path, help="Optional JSON capture for adjacent dashboard tabs")
    parser.add_argument("--write", action="store_true", help=f"Write merged rewards to {REWARDS_FILE.name}")
    parser.add_argument("--resolve-prices", action="store_true", help="Estimate product reward values through Safeway product search")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Safeway store ID")
    parser.add_argument("--banner", default=DEFAULT_BANNER, help="Banner, usually safeway")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="pickup, delivery, or instore")
    parser.add_argument("--rows", type=int, default=5, help="Product search rows per reward")
    parser.add_argument("--keep-candidates", type=int, default=3, help="Saved product candidates per reward")
    parser.add_argument("--min-score", type=float, default=0.55, help="Minimum token overlap to accept a product value")
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout in seconds")
    args = parser.parse_args()

    raw_sections = load_json(args.capture)
    rewards = parse_reward_sections(raw_sections)
    adjacent_sections = []
    if args.adjacent_capture:
        adjacent_sections = load_json(args.adjacent_capture)
        rewards.extend(parse_adjacent_sections(adjacent_sections))
    if args.resolve_prices:
        rewards = [resolve_product_value(reward, args) for reward in rewards]

    config = load_json(REWARDS_FILE)
    merge_dashboard_rewards(config, rewards, raw_sections + adjacent_sections, args.capture)
    print_summary(rewards, args.write)
    if args.write:
        write_json(REWARDS_FILE, config)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
